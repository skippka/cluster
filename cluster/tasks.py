import base64
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .report import export_reports


LEASE_SECONDS = 20
MAX_ATTEMPTS = 5
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 24 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea", ".vscode"}


@dataclass
class Node:
    node_id: str
    name: str
    address: str
    system: str
    cpu_total: int
    cpu_usage: float
    ram_total: float
    ram_free: float
    disk_total: float
    disk_free: float
    docker: bool = False
    runtimes: list[str] = field(default_factory=list)
    battery_percent: int | None = None
    ac_plugged: bool = True
    state: str = "free"
    last_seen: float = 0.0


@dataclass
class Job:
    job_id: str
    kind: str
    node_id: str
    node_name: str
    cpu: int
    ram: float
    disk: float
    command: str = ""
    bundle_b64: str = ""
    entry_name: str = ""
    port: int = 0
    state: str = "queued"
    output: str = ""
    url: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def pack_target(target_path: str | Path) -> tuple[str, str]:
    path = Path(target_path).expanduser().resolve(strict=True)
    buf = io.BytesIO()
    total = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if path.is_file():
            data = path.read_bytes()
            if len(data) > MAX_BUNDLE_BYTES:
                raise ValueError("File exceeds 24 MB limit")
            zf.writestr(path.name, data)
            entry = path.name
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if any(part in SKIP_DIRS for part in item.relative_to(path).parts):
                    continue
                if item.is_symlink() or not item.is_file():
                    continue
                raw = item.read_bytes()
                total += len(raw)
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("Project folder exceeds 24 MB limit")
                zf.writestr(item.relative_to(path).as_posix(), raw)
            entry = ""
        else:
            raise ValueError("Invalid target path")

    return base64.b64encode(buf.getvalue()).decode("ascii"), entry


def analyze_chunk(payload: dict) -> dict:
    raw = base64.b64decode(payload["content"])
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("File exceeds 2 MB limit")
    text = raw.decode("utf-8")
    words_list = re.findall(r"[^\W\d_]+", text.lower())
    counts = Counter(words_list)
    return {
        "path": payload["path"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": len(text.splitlines()),
        "words": len(words_list),
        "top_words": counts.most_common(20),
    }


def summarize_results(rows: list[dict]) -> dict:
    groups: dict[str, list[str]] = {}
    for row in rows:
        if "sha256" in row:
            groups.setdefault(row["sha256"], []).append(row["path"])
    duplicates = [paths for paths in groups.values() if len(paths) > 1]
    return {
        "files": len(rows),
        "bytes": sum(r.get("bytes", 0) for r in rows),
        "lines": sum(r.get("lines", 0) for r in rows),
        "words": sum(r.get("words", 0) for r in rows),
        "duplicates": duplicates,
        "results": rows,
    }


class SwarmQueue:
    def __init__(self, data_dir: Path, clock=time.time):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "swarm.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, name TEXT, created REAL, cancelled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, batch TEXT, payload TEXT, digest TEXT,
                state TEXT DEFAULT 'pending', node TEXT DEFAULT '', token TEXT DEFAULT '',
                deadline REAL DEFAULT 0, attempts INTEGER DEFAULT 0, result TEXT DEFAULT '',
                error TEXT DEFAULT '', worker_name TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reliability (
                node TEXT PRIMARY KEY, completed INTEGER DEFAULT 0, expired INTEGER DEFAULT 0
            );
            """
        )
        with self.db:
            self.db.execute(
                "UPDATE chunks SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, "
                "token='', node='', error='Controller restarted' WHERE state='running'",
                (MAX_ATTEMPTS,),
            )

    def close(self):
        self.db.close()

    def submit_folder(self, folder: str | Path) -> tuple[str, int, list[dict]]:
        root = Path(folder).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Choose an existing folder")
        files, skipped, total = [], [], 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.suffix.lower() not in (".txt", ".md", ".log"):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError("over 2 MB")
                raw.decode("utf-8")
                if total + len(raw) > MAX_BATCH_BYTES:
                    raise ValueError("exceeds 32 MB batch limit")
                total += len(raw)
                files.append((rel, raw))
            except (OSError, UnicodeDecodeError, ValueError) as err:
                skipped.append({"path": rel, "reason": str(err)})

        if not files:
            raise ValueError("No valid UTF-8 (.txt, .md, .log) files found")

        batch_id = uuid.uuid4().hex[:8]
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO batches VALUES (?, ?, ?, 0)",
                (batch_id, root.name, self.clock()),
            )
            for rel, raw in files:
                payload = json.dumps(
                    {"kind": "corpus", "path": rel, "content": base64.b64encode(raw).decode("ascii")}
                )
                digest = hashlib.sha256(raw).hexdigest()
                self.db.execute(
                    "INSERT INTO chunks (id, batch, payload, digest) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex[:10], batch_id, payload, digest),
                )
        return batch_id, len(files), skipped

    def submit_map(self, folder: str | Path, command: str) -> tuple[str, int]:
        root = Path(folder).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Choose an existing folder")
        cmd = command.strip()
        if not cmd:
            raise ValueError("Provide a script path or command to run per file")

        script_name = ""
        script_b64 = ""
        candidate = Path(cmd.strip('"').strip("'")).expanduser()
        if candidate.is_file():
            script_name = candidate.name
            script_b64 = base64.b64encode(candidate.read_bytes()).decode("ascii")
            ext = candidate.suffix.lower()
            cmd = f'python "{script_name}"' if ext == ".py" else (f'node "{script_name}"' if ext == ".js" else f'"{script_name}"')

        files, total = [], 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                continue
            total += len(raw)
            if total > MAX_BATCH_BYTES:
                break
            files.append((path.relative_to(root).as_posix(), raw))

        if not files:
            raise ValueError("No files found to distribute")

        batch_id = uuid.uuid4().hex[:8]
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO batches VALUES (?, ?, ?, 0)",
                (batch_id, f"map:{root.name}", self.clock()),
            )
            for rel, raw in files:
                payload = json.dumps(
                    {
                        "kind": "map",
                        "path": rel,
                        "command": cmd,
                        "script_name": script_name,
                        "script_b64": script_b64,
                        "content": base64.b64encode(raw).decode("ascii"),
                    }
                )
                digest = hashlib.sha256(raw).hexdigest()
                self.db.execute(
                    "INSERT INTO chunks (id, batch, payload, digest) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex[:10], batch_id, payload, digest),
                )
        return batch_id, len(files)

    def expire_leases(self):
        now = self.clock()
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT id, node, attempts FROM chunks WHERE state='running' AND deadline < ?",
                (now,),
            ).fetchall()
            for r in rows:
                new_state = "failed" if r["attempts"] >= MAX_ATTEMPTS else "pending"
                self.db.execute(
                    "UPDATE chunks SET state=?, token='', node='', error='Lease timeout (20s)' WHERE id=?",
                    (new_state, r["id"]),
                )
                if r["node"]:
                    self.db.execute(
                        "INSERT INTO reliability (node, completed, expired) VALUES (?, 0, 1) "
                        "ON CONFLICT(node) DO UPDATE SET expired=expired+1",
                        (r["node"],),
                    )

    def active_for_node(self, node_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM chunks WHERE node=? AND state='running' LIMIT 1",
            (node_id,),
        ).fetchone()
        return row is not None

    def claim(self, node_id: str, allowed_jobs: list[str] | None = None) -> dict | None:
        with self.lock:
            self.expire_leases()
            if self.active_for_node(node_id):
                return None
            rows = self.db.execute(
                "SELECT c.id, c.payload, c.digest FROM chunks c "
                "JOIN batches b ON b.id = c.batch "
                "WHERE c.state='pending' AND b.cancelled=0 ORDER BY b.created, c.id LIMIT 20"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                ckind = payload.get("kind", "corpus")
                req_perm = "exec" if ckind == "map" else "corpus"
                if allowed_jobs is not None and req_perm not in allowed_jobs:
                    continue
                token = uuid.uuid4().hex
                with self.db:
                    self.db.execute(
                        "UPDATE chunks SET state='running', node=?, token=?, deadline=?, attempts=attempts+1 WHERE id=?",
                        (node_id, token, self.clock() + LEASE_SECONDS, row["id"]),
                    )
                return {
                    "job_id": row["id"],
                    "kind": ckind,
                    "token": token,
                    "payload": payload,
                    "digest": row["digest"],
                }
            return None

    def renew(self, node_id: str, chunk_id: str, token: str):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE chunks SET deadline=? WHERE id=? AND node=? AND token=? AND state='running'",
                (self.clock() + LEASE_SECONDS, chunk_id, node_id, token),
            )

    def finish(self, node_id: str, node_name: str, chunk_id: str, token: str, ok: bool, result=None, error: str = "") -> bool:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT payload, digest, attempts FROM chunks WHERE id=? AND node=? AND token=? AND state='running'",
                (chunk_id, node_id, token),
            ).fetchone()
            if not row:
                return False
            ckind = json.loads(row["payload"]).get("kind", "corpus")
            digest_ok = (ckind != "corpus") or (isinstance(result, dict) and result.get("sha256") == row["digest"])
            if ok and isinstance(result, dict) and digest_ok:
                self.db.execute(
                    "UPDATE chunks SET state='done', token='', result=?, worker_name=?, error='' WHERE id=?",
                    (json.dumps(result), node_name, chunk_id),
                )
                self.db.execute(
                    "INSERT INTO reliability (node, completed, expired) VALUES (?, 1, 0) "
                    "ON CONFLICT(node) DO UPDATE SET completed=completed+1",
                    (node_id,),
                )
                return True

            err_msg = error or "Digest mismatch"
            new_state = "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
            self.db.execute(
                "UPDATE chunks SET state=?, token='', node='', error=? WHERE id=?",
                (new_state, err_msg, chunk_id),
            )
            return True

    def release_node(self, node_id: str):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE chunks SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, "
                "token='', node='', error='Access revoked' WHERE node=? AND state='running'",
                (MAX_ATTEMPTS, node_id),
            )

    def batches(self) -> list[dict]:
        with self.lock:
            self.expire_leases()
            rows = self.db.execute("SELECT id, name, created, cancelled FROM batches ORDER BY created DESC").fetchall()
            out = []
            for b in rows:
                counts = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
                total = 0
                retries = 0
                for c in self.db.execute("SELECT state, attempts FROM chunks WHERE batch=?", (b["id"],)):
                    counts[c["state"]] = counts.get(c["state"], 0) + 1
                    total += 1
                    retries += max(0, c["attempts"] - 1)
                out.append({"id": b["id"], "name": b["name"], "total": total, "counts": counts, "retries": retries})
            return out

    def export(self, batch_id: str) -> tuple[Path, dict]:
        with self.lock:
            batch = self.db.execute("SELECT id, name FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise ValueError(f"Batch '{batch_id}' not found")
            chunks = self.db.execute(
                "SELECT state, result, worker_name FROM chunks WHERE batch=?", (batch_id,)
            ).fetchall()
            done_rows = []
            workers = set()
            for c in chunks:
                if c["state"] == "done" and c["result"]:
                    item = json.loads(c["result"])
                    item["worker"] = c["worker_name"]
                    done_rows.append(item)
                    if c["worker_name"]:
                        workers.add(c["worker_name"])
            summary = summarize_results(done_rows)
            report = {
                "batch": batch_id,
                "name": batch["name"],
                "total_files": len(chunks),
                "complete": len(done_rows) == len(chunks),
                "workers": sorted(workers),
                **summary,
            }
            json_path = export_reports(report, self.root / "reports", batch_id)
            return json_path, report
