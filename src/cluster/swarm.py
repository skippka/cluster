import base64
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .corpus import MAX_FILE_BYTES, summarize

LEASE_SECONDS = 20
MAX_ATTEMPTS = 5
MAX_BATCH_BYTES = 32 * 1024 * 1024


class Swarm:
    def __init__(self, data_dir, clock=time.time):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "swarm.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, name TEXT, created REAL, cancelled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, batch TEXT, payload TEXT, digest TEXT,
                state TEXT DEFAULT 'pending', node TEXT DEFAULT '', token TEXT DEFAULT '',
                deadline REAL DEFAULT 0, attempts INTEGER DEFAULT 0, result TEXT DEFAULT '',
                error TEXT DEFAULT '', worker_name TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS reliability (
                node TEXT PRIMARY KEY, completed INTEGER DEFAULT 0, expired INTEGER DEFAULT 0);
        ''')
        with self.db:
            self.db.execute("UPDATE chunks SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, token='', node='', error='Controller restarted' WHERE state='running'", (MAX_ATTEMPTS,))

    def close(self):
        self.db.close()

    def submit(self, folder):
        root = Path(folder).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Choose a directory")
        files, skipped, total = [], [], 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.suffix.lower() not in (".txt", ".md", ".log"):
                continue
            relative = path.relative_to(root).as_posix()
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            try:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError("over 1 MiB")
                raw.decode("utf-8", errors="strict")
            except (OSError, ValueError) as error:
                skipped.append({"path": relative, "reason": str(error)})
                continue
            total += len(raw)
            if total > MAX_BATCH_BYTES or len(files) >= 500:
                raise ValueError("Batch exceeds 32 MiB or 500 files; select a smaller folder")
            files.append((relative, raw))
        if not files:
            raise ValueError("No valid UTF-8 .txt/.md/.log files (max 1 MiB each)")
        batch = uuid.uuid4().hex[:12]
        with self.lock, self.db:
            self.db.execute("INSERT INTO batches(id,name,created) VALUES(?,?,?)", (batch, root.name, self.clock()))
            for index, (relative, raw) in enumerate(files):
                payload = {"path": relative, "content": base64.b64encode(raw).decode("ascii")}
                self.db.execute("INSERT INTO chunks(id,batch,payload,digest) VALUES(?,?,?,?)",
                                (f"{batch}-{index}", batch, json.dumps(payload), hashlib.sha256(raw).hexdigest()))
        return batch, len(files), skipped

    def expire(self):
        with self.lock, self.db:
            rows = self.db.execute("SELECT * FROM chunks WHERE state='running' AND deadline<=?", (self.clock(),)).fetchall()
            for row in rows:
                self.db.execute("INSERT INTO reliability(node,expired) VALUES(?,1) ON CONFLICT(node) DO UPDATE SET expired=expired+1", (row["node"],))
                state = "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
                self.db.execute("UPDATE chunks SET state=?, node='', token='', error='Lease expired; worker disconnected' WHERE id=?", (state, row["id"]))

    def active(self, node):
        with self.lock:
            return self.db.execute("SELECT 1 FROM chunks WHERE node=? AND state='running'", (node,)).fetchone() is not None

    def rank(self, node, offer):
        with self.lock:
            row = self.db.execute("SELECT completed,expired FROM reliability WHERE node=?", (node,)).fetchone()
        good, missed = (row["completed"], row["expired"]) if row else (0, 0)
        reliability = (good + 2) / (good + missed + 2)
        headroom = max(0, 100 - float(offer.get("cpu_usage", 100))) / 100
        score = 60 * headroom + 30 * reliability + (10 if offer.get("ac") is True else 0)
        return round(score, 1), f"CPU headroom {headroom:.0%}; lease reliability {reliability:.0%}; AC {offer.get('ac')}"

    def claim(self, node, name):
        self.expire()
        with self.lock, self.db:
            if self.active(node):
                return None
            row = self.db.execute("SELECT c.* FROM chunks c JOIN batches b ON b.id=c.batch WHERE c.state='pending' AND b.cancelled=0 ORDER BY b.created,c.id LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE chunks SET state='running',node=?,worker_name=?,token=?,deadline=?,attempts=attempts+1 WHERE id=?",
                            (node, name, token, self.clock() + LEASE_SECONDS, row["id"]))
            return {"job_id": row["id"], "kind": "corpus", "token": token, "batch": row["batch"],
                    "payload": json.loads(row["payload"]), "lease_seconds": LEASE_SECONDS}

    def renew(self, node, chunk, token):
        self.expire()
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE chunks SET deadline=? WHERE id=? AND node=? AND token=? AND state='running'",
                                     (self.clock() + LEASE_SECONDS, chunk, node, token))
            return cursor.rowcount == 1

    def complete(self, data):
        self.expire()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM chunks WHERE id=?", (data["job_id"],)).fetchone()
            if row is None or row["token"] != data["token"] or row["node"] != data["node_id"]:
                return False
            if row["state"] == "done":
                return True
            if row["state"] != "running":
                return False
            if data.get("state") != "done":
                state = "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
                self.db.execute("UPDATE chunks SET state=?,token='',node='',error=? WHERE id=?",
                                (state, str(data.get("error", "Worker failed"))[:500], row["id"]))
                return True
            result = data["result"]
            payload = json.loads(row["payload"])
            if result.get("sha256") != row["digest"] or result.get("path") != payload["path"]:
                raise ValueError("Result does not match the assigned file")
            for field in ("bytes", "lines", "words"):
                if type(result.get(field)) is not int or not 0 <= result[field] <= MAX_FILE_BYTES:
                    raise ValueError("Invalid result count")
            if result["bytes"] != len(base64.b64decode(payload["content"])):
                raise ValueError("Result size mismatch")
            top = result.get("top_words")
            if not isinstance(top, list) or len(top) > 20:
                raise ValueError("Invalid word summary")
            for pair in top:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not isinstance(pair[0], str) or type(pair[1]) is not int or pair[1] < 0:
                    raise ValueError("Invalid word entry")
            self.db.execute("UPDATE chunks SET state='done',result=?,error='' WHERE id=?", (json.dumps(result), row["id"]))
            self.db.execute("INSERT INTO reliability(node,completed) VALUES(?,1) ON CONFLICT(node) DO UPDATE SET completed=completed+1", (data["node_id"],))
            return True

    def batches(self):
        self.expire()
        with self.lock:
            result = []
            for batch in self.db.execute("SELECT * FROM batches ORDER BY created").fetchall():
                counts = {row["state"]: row["n"] for row in self.db.execute("SELECT state,COUNT(*) n FROM chunks WHERE batch=? GROUP BY state", (batch["id"],))}
                attempts = self.db.execute("SELECT SUM(MAX(attempts-1,0)) FROM chunks WHERE batch=?", (batch["id"],)).fetchone()[0]
                result.append({**dict(batch), "counts": counts, "total": sum(counts.values()), "retries": attempts or 0})
            return result

    def cancel(self, batch):
        with self.lock, self.db:
            changed = self.db.execute("UPDATE batches SET cancelled=1 WHERE id=?", (batch,)).rowcount
            self.db.execute("UPDATE chunks SET state='cancelled',token='' WHERE batch=? AND state IN ('pending','running')", (batch,))
            return bool(changed)

    def release_node(self, node_id):
        with self.lock, self.db:
            self.db.execute("UPDATE chunks SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END, token='', node='', error='Device access revoked' WHERE state='running' AND node=?", (MAX_ATTEMPTS, node_id))

    def export(self, batch):
        with self.lock:
            info = self.db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
            if info is None:
                raise ValueError("Unknown batch")
            rows = self.db.execute("SELECT * FROM chunks WHERE batch=? ORDER BY id", (batch,)).fetchall()
            report = summarize([json.loads(row["result"]) for row in rows if row["state"] == "done"])
            report.update({"batch": batch, "complete": all(row["state"] == "done" for row in rows),
                           "total_files": len(rows), "cancelled": bool(info["cancelled"]),
                           "workers": sorted({row["worker_name"] for row in rows if row["state"] == "done"}),
                           "unfinished": [{"path": json.loads(row["payload"])["path"], "state": row["state"], "error": row["error"]} for row in rows if row["state"] != "done"]})
        directory = self.root / "reports"
        directory.mkdir(exist_ok=True)
        target = directory / f"{batch}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with target.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["path", "bytes", "lines", "words", "sha256"])
            for row in report["results"]:
                writer.writerow([row["path"], row["bytes"], row["lines"], row["words"], row["sha256"]])
        return target, report
