import json
import socket
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .security import DISCOVERY_PORT, TrustStore, ensure_controller_tls, make_invite_link
from .system import local_ip
from .tasks import Job, Node, SwarmQueue, pack_target


def is_online(node: Node, timeout: int = 15) -> bool:
    return (time.time() - node.last_seen) < timeout


def required_runtime(command: str, entry_name: str = "") -> str | None:
    cmd = (command or "").strip().lower()
    first = cmd.split()[0] if cmd else ""
    mapping = {
        "python": "python", "python3": "python", "py": "python",
        "node": "node", "npm": "node", "npx": "node",
        "go": "go", "gcc": "gcc", "g++": "g++",
        "cargo": "rust", "rustc": "rust",
        "java": "java", "javac": "java",
        "dotnet": "dotnet", "php": "php", "docker": "docker",
    }
    if first in mapping:
        return mapping[first]
    ext = Path(entry_name).suffix.lower() if entry_name else ""
    ext_map = {".py": "python", ".js": "node", ".go": "go", ".c": "gcc", ".cpp": "g++", ".java": "java", ".php": "php"}
    return ext_map.get(ext)


def has_runtime(node: Node, req: str | None) -> bool:
    if not req:
        return True
    if req == "python":
        return "python" in node.runtimes or "python-builtin" in node.runtimes or not node.runtimes
    return req in node.runtimes


def select_node(
    nodes: list[Node],
    cpu: int,
    ram: float,
    disk: float,
    kind: str,
    permissions: dict[str, list[str]],
    req_runtime: str | None = None,
) -> Node | None:
    candidates = []
    for node in nodes:
        if not is_online(node) or node.state != "free":
            continue
        if kind not in permissions.get(node.node_id, []):
            continue
        if not has_runtime(node, req_runtime):
            continue
        if node.cpu_total < cpu or node.ram_free < ram or node.disk_free < disk:
            continue
        score = (node.ram_free - ram, node.cpu_total - cpu, node.disk_free - disk)
        candidates.append((score, node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


class ClusterController:
    def __init__(self, port: int, data_dir: Path, on_log=None):
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.on_log = on_log or (lambda msg, level="info": None)
        self.lock = threading.RLock()

        self.nodes: dict[str, Node] = {}
        self.offers: dict[str, dict] = {}
        self.permissions: dict[str, list[str]] = {}
        self.jobs: dict[str, Job] = {}
        self.stop_requests: dict[str, list[str]] = {}
        self.next_job_id = 1
        self.events: list[tuple[str, str, str]] = []

        self.swarm = SwarmQueue(self.data_dir)
        self.trust = TrustStore(self.data_dir)
        self.tls_context, self.fingerprint = ensure_controller_tls(self.data_dir)
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._beacon_stop = threading.Event()
        self._beacon_thread: threading.Thread | None = None

        self._init_db()

    def _init_db(self):
        self.db = sqlite3.connect(self.data_dir / "cluster.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs_v2 (
                    job_id TEXT PRIMARY KEY, kind TEXT, node_id TEXT, node_name TEXT,
                    cpu INTEGER, ram REAL, disk REAL, command TEXT, bundle_b64 TEXT,
                    entry_name TEXT, port INTEGER, state TEXT, output TEXT, url TEXT, created_at TEXT
                )
                """
            )
        for row in self.db.execute("SELECT * FROM jobs_v2 ORDER BY CAST(job_id AS INTEGER)").fetchall():
            job = Job(**dict(row))
            if job.state in ("queued", "running") and job.kind == "host":
                job.state = "stopped"
            self.jobs[job.job_id] = job
        ids = [int(j) for j in self.jobs if j.isdigit()]
        self.next_job_id = max(ids, default=0) + 1

    def _save_job(self, job: Job):
        with self.db:
            self.db.execute(
                """
                INSERT OR REPLACE INTO jobs_v2 VALUES
                (:job_id, :kind, :node_id, :node_name, :cpu, :ram, :disk,
                 :command, :bundle_b64, :entry_name, :port, :state, :output, :url, :created_at)
                """,
                job.to_dict(),
            )

    def log(self, message: str, level: str = "info"):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.events.append((stamp, level, message))
            if len(self.events) > 200:
                self.events.pop(0)
        self.on_log(message, level)

    def issue_invitation(self, host: str | None = None) -> str:
        code = self.trust.create_invite()
        ip = host or local_ip()
        link = make_invite_link(ip, self.port, self.fingerprint, code)
        self.log(f"Invite code: {code} (valid 5 min)", "info")
        return link

    def issue_invite_pair(self, host: str | None = None) -> tuple[str, str]:
        code = self.trust.create_invite()
        ip = host or local_ip()
        link = make_invite_link(ip, self.port, self.fingerprint, code)
        self.log(f"Invite code: {code} (valid 5 min)", "info")
        return code, link

    def approve_request(self, req_id: str) -> bool:
        issued = self.trust.approve_request(req_id)
        if issued:
            self.log(f"Approved access for '{issued['name']}'", "success")
            return True
        return False

    def decline_request(self, req_id: str) -> bool:
        ok = self.trust.decline_request(req_id)
        if ok:
            self.log(f"Declined access request {req_id}", "warning")
        return ok

    def revoke_device(self, node_id: str) -> bool:
        with self.lock:
            if not self.trust.revoke(node_id):
                return False
            self.nodes.pop(node_id, None)
            self.offers.pop(node_id, None)
            self.permissions.pop(node_id, None)
            self.swarm.release_node(node_id)
            self.log(f"Revoked access for {node_id}", "warning")
            return True

    def register(self, data: dict, address: str):
        with self.lock:
            nid = data["node_id"]
            is_new = nid not in self.nodes
            self.nodes[nid] = Node(
                node_id=nid,
                name=data["name"],
                address=address,
                system=data.get("system", "Unknown"),
                cpu_total=int(data["cpu_total"]),
                cpu_usage=float(data.get("cpu_usage", 0)),
                ram_total=float(data["ram_total"]),
                ram_free=float(data["ram_free"]),
                disk_total=float(data["disk_total"]),
                disk_free=float(data["disk_free"]),
                docker=bool(data.get("docker", False)),
                runtimes=list(data.get("runtimes", [])),
                battery_percent=data.get("battery_percent"),
                ac_plugged=bool(data.get("ac_plugged", True)),
                state="free",
                last_seen=time.time(),
            )
            self.offers[nid] = data.get("sharing", {})
            self.permissions[nid] = data.get("allowed_jobs", ["corpus", "demo"])
            if is_new:
                self.log(f"Node '{data['name']}' ({address}) connected", "success")

    def heartbeat(self, data: dict) -> bool:
        with self.lock:
            node = self.nodes.get(data["node_id"])
            if not node:
                return False
            node.last_seen = time.time()
            node.cpu_usage = float(data.get("cpu_usage", node.cpu_usage))
            node.ram_free = float(data.get("ram_free", node.ram_free))
            node.disk_free = float(data.get("disk_free", node.disk_free))
            node.battery_percent = data.get("battery_percent", node.battery_percent)
            node.ac_plugged = bool(data.get("ac_plugged", node.ac_plugged))
            if "runtimes" in data:
                node.runtimes = list(data["runtimes"])
            self.offers[node.node_id] = data.get("sharing", {})
            if "allowed_jobs" in data:
                self.permissions[node.node_id] = data["allowed_jobs"]
            incoming_state = data.get("state", node.state)
            active_legacy = any(
                j.node_id == node.node_id and j.state in ("queued", "running") and j.kind != "host"
                for j in self.jobs.values()
            )
            if not (active_legacy and incoming_state == "free"):
                node.state = incoming_state
            lease = data.get("lease")
            if lease:
                self.swarm.renew(node.node_id, lease["job_id"], lease["token"])
            return True

    def create_job(
        self,
        kind: str,
        cpu: int = 1,
        ram: float = 0.2,
        disk: float = 0.1,
        command: str = "",
        target_path: str | Path | None = None,
        port: int = 0,
    ) -> Job | None:
        bundle_b64 = ""
        entry_name = ""
        if target_path:
            bundle_b64, entry_name = pack_target(target_path)

        req_rt = required_runtime(command, entry_name) if kind == "exec" else None

        with self.lock:
            node = select_node(list(self.nodes.values()), cpu, ram, disk, kind, self.permissions, req_rt)
            if not node:
                return None
            jid = str(self.next_job_id)
            self.next_job_id += 1
            url = f"http://{node.address}:{port}" if kind == "host" and port else ""
            job = Job(
                job_id=jid,
                kind=kind,
                node_id=node.node_id,
                node_name=node.name,
                cpu=cpu,
                ram=ram,
                disk=disk,
                command=command,
                bundle_b64=bundle_b64,
                entry_name=entry_name,
                port=port,
                state="queued",
                url=url,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self.jobs[jid] = job
            self._save_job(job)
            if kind != "host":
                node.state = "reserved"
            self.log(f"Task #{jid} ({kind}) -> '{node.name}'", "info")
            return job

    def stop_service(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.kind != "host" or job.state not in ("queued", "running"):
                return False
            self.stop_requests.setdefault(job.node_id, []).append(job_id)
            job.state = "stopped"
            self._save_job(job)
            self.log(f"Stop requested for service #{job_id}", "warning")
            return True

    def next_task(self, node_id: str) -> dict | None:
        with self.lock:
            self.swarm.expire_leases()
            stops = self.stop_requests.pop(node_id, [])
            if stops:
                return {"kind": "stop_host", "job_id": stops[0]}
            if node_id not in self.nodes or self.swarm.active_for_node(node_id):
                return None
            allowed = self.permissions.get(node_id, [])
            for job in self.jobs.values():
                if job.node_id == node_id and job.state == "queued":
                    if job.kind not in allowed:
                        job.state = "failed"
                        job.output = "Disabled by worker owner"
                        self._save_job(job)
                        continue
                    job.state = "running"
                    self._save_job(job)
                    if job.kind != "host":
                        self.nodes[node_id].state = "busy"
                    return job.to_dict()
            return self.swarm.claim(node_id, allowed)

    def finish_job(self, data: dict) -> bool:
        with self.lock:
            job = self.jobs.get(data.get("job_id"))
            if not job:
                return False
            job.state = data.get("state", "done")
            job.output = data.get("output", "")
            if data.get("url"):
                job.url = data["url"]
            self._save_job(job)
            node = self.nodes.get(job.node_id)
            if node and job.kind != "host":
                node.state = "free"
            lvl = "success" if job.state in ("done", "running") else "error"
            summary = job.output.strip().splitlines()[-1][:140] if job.output.strip() else job.state
            self.log(f"Task #{job.job_id} ({job.kind}) {job.state}: {summary}", lvl)
            return True

    def finish_chunk(self, data: dict) -> bool:
        with self.lock:
            node = self.nodes.get(data["node_id"])
            name = node.name if node else data["node_id"]
            ok = self.swarm.finish(
                data["node_id"],
                name,
                data["job_id"],
                data["token"],
                bool(data.get("ok")),
                data.get("result"),
                data.get("error", ""),
            )
            if node:
                node.state = "free"
            return ok

    def submit_sample(self) -> tuple[str, int]:
        folder = Path(tempfile.mkdtemp(prefix="sample-", dir=self.swarm.root))
        for i in range(24):
            content = (
                f"Document group {i % 12}\n"
                + "Distributed cluster computes tasks and finds duplicates.\n" * 400
            )
            (folder / f"doc-{i:02d}.txt").write_text(content, encoding="utf-8")
        batch_id, count, _ = self.swarm.submit_folder(folder)
        self.log(f"Sample batch {batch_id}: {count} files queued", "info")
        return batch_id, count

    def start(self):
        controller = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def send_json(self, obj: dict, code: int = 200):
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 32 * 1024 * 1024:
                    raise ValueError("Invalid request size")
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def auth(self) -> str | None:
                header = self.headers.get("Authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                nid = controller.trust.authenticate(token)
                if not nid:
                    self.send_json({"error": "Unauthorized or revoked"}, 401)
                    return None
                return nid

            def do_POST(self):
                if self.path == "/api/v1/pair":
                    try:
                        data = self.read_json()
                        issued = controller.trust.redeem_invite(data["invite"], data["name"])
                        controller.log(f"Paired device {issued['name']} ({issued['node_id']})", "success")
                        self.send_json(issued)
                    except (KeyError, ValueError, TypeError) as err:
                        self.send_json({"error": str(err)}, 400)
                    return

                if self.path == "/api/v1/pair/request":
                    try:
                        data = self.read_json()
                        req_id = controller.trust.create_access_request(data["name"], self.client_address[0])
                        controller.log(f"Access request from '{data['name']}' ({self.client_address[0]})", "warning")
                        self.send_json({"request_id": req_id})
                    except (KeyError, ValueError, TypeError) as err:
                        self.send_json({"error": str(err)}, 400)
                    return

                nid = self.auth()
                if not nid:
                    return
                try:
                    data = self.read_json()
                    if data.get("node_id") != nid:
                        self.send_json({"error": "Device ID mismatch"}, 403)
                        return
                    if self.path == "/api/v1/nodes/register":
                        controller.register(data, self.client_address[0])
                        self.send_json({"ok": True})
                    elif self.path == "/api/v1/nodes/heartbeat":
                        found = controller.heartbeat(data)
                        self.send_json({"ok": found, "register": not found})
                    elif self.path == "/api/v1/jobs/result":
                        self.send_json({"ok": controller.finish_job(data)})
                    elif self.path == "/api/v1/swarm/result":
                        self.send_json({"ok": controller.finish_chunk(data)})
                    else:
                        self.send_json({"error": "Unknown endpoint"}, 404)
                except (KeyError, ValueError, TypeError) as err:
                    self.send_json({"error": str(err)}, 400)

            def do_GET(self):
                if self.path.startswith("/api/v1/pair/status/"):
                    req_id = self.path.removeprefix("/api/v1/pair/status/")
                    self.send_json(controller.trust.get_request_status(req_id))
                    return
                nid = self.auth()
                if not nid:
                    return
                if self.path.startswith("/api/v1/jobs/next/"):
                    target = self.path.removeprefix("/api/v1/jobs/next/")
                    if target != nid:
                        self.send_json({"error": "Forbidden"}, 403)
                        return
                    self.send_json({"job": controller.next_task(nid)})
                elif self.path == "/api/v1/health":
                    self.send_json({"ok": True, "version": __version__})
                else:
                    self.send_json({"error": "Unknown endpoint"}, 404)

        class SecureServer(ThreadingHTTPServer):
            def process_request_thread(self, request, client_address):
                request.settimeout(15)
                try:
                    request = controller.tls_context.wrap_socket(request, server_side=True)
                except OSError:
                    self.close_request(request)
                    return
                super().process_request_thread(request, client_address)

        self.server = SecureServer(("0.0.0.0", self.port), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self._beacon_stop.clear()
        self._beacon_thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._beacon_thread.start()
        self.log(f"Controller started at https://{local_ip()}:{self.port}", "success")

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._beacon_stop.is_set():
                payload = json.dumps({
                    "app": "cluster",
                    "name": socket.gethostname(),
                    "ip": local_ip(),
                    "port": self.port,
                    "pin": self.fingerprint,
                }).encode("utf-8")
                try:
                    sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    pass
                self._beacon_stop.wait(1.5)
        finally:
            sock.close()

    def stop(self):
        self._beacon_stop.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.swarm.close()
        self.db.close()
