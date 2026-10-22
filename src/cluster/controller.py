import json
import hmac
import platform
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ui
from . import __version__
from .models import Job, Node
from .scheduler import is_online, select_node
from .storage import Storage
from .system_info import local_ip
from .swarm import Swarm
from .trust import TrustStore, controller_identity, invitation


class ClusterController:
    def __init__(self, port: int, cluster_key: str, data_dir: Path, secure=False):
        self.port = port
        self.cluster_key = cluster_key
        self.started_at = time.time()
        self.nodes: dict[str, Node] = {}
        self.lock = threading.RLock()
        self.storage = Storage(data_dir / "cluster.db")
        self.jobs = self.storage.load_jobs()
        existing_ids = [int(value) for value in self.jobs if value.isdigit()]
        self.next_job_id = max(existing_ids, default=0) + 1
        self.server: ThreadingHTTPServer | None = None
        self.swarm = Swarm(data_dir)
        self.offers = {}
        self.permissions = {}
        self.secure = secure
        self.trust = TrustStore(data_dir) if secure else None
        self.tls_context, self.fingerprint = controller_identity(data_dir) if secure else (None, '')
        self.pair_attempts = {}

    def issue_invitation(self, address=None):
        if not self.trust:
            raise ValueError('Pairing is only available in TLS mode')
        code = self.trust.invite()
        self.event('Created a one-use invitation (expires in five minutes)')
        return invitation(address or local_ip(), self.port, self.fingerprint, code)

    def revoke_device(self, node_id):
        with self.lock:
            if not self.trust or not self.trust.revoke(node_id):
                return False
            self.nodes.pop(node_id, None)
            self.offers.pop(node_id, None)
            self.permissions.pop(node_id, None)
            self.swarm.release_node(node_id)
            self.event(f'Revoked device {node_id}', 'warning')
            return True

    def event(self, message: str, level: str = "info"):
        self.storage.add_event(time.strftime("%Y-%m-%d %H:%M:%S"), level, message)

    def register(self, data: dict, address: str):
        with self.lock:
            if not isinstance(data.get('name'), str) or not 1 <= len(data['name']) <= 48 or not data['name'].isprintable():
                raise ValueError('Invalid node name')
            is_new = data["node_id"] not in self.nodes
            self.nodes[data["node_id"]] = Node(
                node_id=data["node_id"],
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
                state="free",
                last_seen=time.time(),
            )
            if is_new:
                self.event(f"Node {data['name']} connected from {address}", "success")
            if self.swarm.active(data["node_id"]) or any(j.node_id == data["node_id"] and j.state in ("queued", "running") for j in self.jobs.values()):
                self.nodes[data["node_id"]].state = "busy"
            self.offers[data["node_id"]] = data.get("sharing", {})
            self.permissions[data['node_id']] = data.get('allowed_jobs', ['corpus', 'demo'] if self.secure else ['corpus', 'demo', 'command', 'docker'])

    def heartbeat(self, data: dict):
        with self.lock:
            node = self.nodes.get(data["node_id"])
            if node is None:
                return False
            node.last_seen = time.time()
            node.cpu_usage = float(data.get("cpu_usage", node.cpu_usage))
            node.ram_free = float(data.get("ram_free", node.ram_free))
            node.disk_free = float(data.get("disk_free", node.disk_free))
            incoming_state = data.get("state", node.state)
            self.offers[node.node_id] = data.get("sharing", {})
            if 'allowed_jobs' in data:
                self.permissions[node.node_id] = data['allowed_jobs']
            legacy_active = any(j.node_id == node.node_id and j.state in ("queued", "running") for j in self.jobs.values())
            if not (legacy_active and incoming_state == "free"):
                node.state = incoming_state
            lease = data.get("lease")
            if lease:
                self.swarm.renew(node.node_id, lease["job_id"], lease["token"])
            return True

    def next_task(self, node_id: str) -> dict | None:
        with self.lock:
            self.swarm.expire()
            if node_id not in self.nodes or self.swarm.active(node_id):
                return None
            for job in self.jobs.values():
                if job.node_id == node_id and job.state == "queued":
                    if job.kind not in self.permissions.get(node_id, []):
                        job.state = 'failed'
                        job.output = 'Worker owner disabled this job type before execution'
                        self.storage.save_job(job)
                        self.nodes[node_id].state = 'free'
                        continue
                    job.state = "running"
                    job.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    if node_id in self.nodes:
                        self.nodes[node_id].state = "busy"
                    self.storage.save_job(job)
                    self.event(f"Job #{job.job_id} started on {job.node_name}")
                    return job.to_dict()
            candidates = []
            for node in self.nodes.values():
                offer = self.offers.get(node.node_id, {})
                if not is_online(node) or node.state != "free" or not offer.get("ready") or self.swarm.active(node.node_id) or 'corpus' not in self.permissions.get(node.node_id, []):
                    continue
                score, reason = self.swarm.rank(node.node_id, offer)
                candidates.append((score, node.node_id, reason))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            if candidates and candidates[0][1] == node_id:
                job = self.swarm.claim(node_id, self.nodes[node_id].name)
                if job:
                    self.nodes[node_id].state = "busy"
                    self.event(f"Chunk {job['job_id']} -> {self.nodes[node_id].name}: {candidates[0][2]}")
                return job
        return None

    def finish_chunk(self, data):
        with self.lock:
            accepted = self.swarm.complete(data)
            node = self.nodes.get(data["node_id"])
            legacy_active = any(j.node_id == data["node_id"] and j.state in ("queued", "running") for j in self.jobs.values())
            if node and not self.swarm.active(node.node_id) and not legacy_active:
                node.state = "free"
            return accepted

    def finish_task(self, data: dict):
        with self.lock:
            job = self.jobs.get(data["job_id"])
            if job is None:
                return False
            if data.get("node_id") != job.node_id or job.state != "running" or data.get("state") not in ("done", "failed"):
                return False
            job.state = data["state"]
            job.output = data.get("output", "")[-8000:]
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            node = self.nodes.get(job.node_id)
            if node:
                node.state = "free"
            self.storage.save_job(job)
            level = "success" if job.state == "done" else "error"
            self.event(f"Job #{job.job_id} finished with state {job.state}", level)
            return True

    def create_job(
        self,
        kind: str,
        cpu: int,
        ram: float,
        disk: float,
        command: str = "",
        image: str = "",
        ports: str = "",
    ) -> Job | None:
        with self.lock:
            eligible = [node for node in self.nodes.values() if kind in self.permissions.get(node.node_id, [])]
            node = select_node(eligible, cpu, ram, disk, kind == "docker")
            if node is None:
                self.event("A job could not be scheduled: no suitable free node", "warning")
                return None
            job = Job(
                job_id=str(self.next_job_id),
                kind=kind,
                cpu=cpu,
                ram=ram,
                disk=disk,
                command=command,
                image=image,
                ports=ports,
                node_id=node.node_id,
                node_name=node.name,
                state="queued",
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self.next_job_id += 1
            node.state = "reserved"
            self.jobs[job.job_id] = job
            self.storage.save_job(job)
            self.event(f"Job #{job.job_id} assigned to {node.name}", "success")
            return job

    def start(self):
        controller = self

        class RequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def send_data(self, data: dict, code: int = 200):
                body = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def read_data(self):
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError("Request body must be between 1 byte and 4 MiB")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                return data

            def authorized(self):
                self.identity = None
                if controller.secure:
                    header = self.headers.get('Authorization', '')
                    self.identity = controller.trust.authenticate(header[7:] if header.startswith('Bearer ') else '')
                    if self.identity:
                        return True
                    self.send_data({'error': 'Device is not paired or its access was revoked'}, 401)
                    return False
                if hmac.compare_digest(self.headers.get("X-Cluster-Key", "").encode(), controller.cluster_key.encode()):
                    return True
                self.send_data({"error": "Invalid cluster key"}, 401)
                return False

            def do_POST(self):
                if self.path == '/api/v1/pair' and controller.secure:
                    try:
                        now = time.time()
                        with controller.lock:
                            recent = [stamp for stamp in controller.pair_attempts.get(self.client_address[0], []) if now - stamp < 60]
                            if len(recent) >= 5:
                                self.send_data({'error': 'Too many pairing attempts; wait a minute'}, 429)
                                return
                            if len(controller.pair_attempts) > 1024:
                                controller.pair_attempts.clear()
                            controller.pair_attempts[self.client_address[0]] = recent + [now]
                        data = self.read_data()
                        issued = controller.trust.enroll(data['invite'], data['name'])
                        controller.event(f"Paired device {issued['name']} ({issued['node_id']})", 'success')
                        self.send_data(issued)
                    except (KeyError, TypeError, ValueError):
                        self.send_data({'error': 'Invitation expired, invalid or already used'}, 400)
                    return
                if not self.authorized():
                    return
                try:
                    data = self.read_data()
                    if controller.secure and data.get('node_id') != self.identity:
                        self.send_data({'error': 'A device can only act as itself'}, 403)
                        return
                    if self.path == "/api/v1/nodes/register":
                        controller.register(data, self.client_address[0])
                        self.send_data({"ok": True})
                    elif self.path == "/api/v1/nodes/heartbeat":
                        found = controller.heartbeat(data)
                        self.send_data({"ok": found, "register": not found})
                    elif self.path == "/api/v1/jobs/result":
                        found = controller.finish_task(data)
                        self.send_data({"ok": found}, 200 if found else 404)
                    elif self.path == "/api/v1/swarm/result":
                        self.send_data({"ok": controller.finish_chunk(data)})
                    else:
                        self.send_data({"error": "Unknown endpoint"}, 404)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    self.send_data({"error": str(error)}, 400)

            def do_GET(self):
                if not self.authorized():
                    return
                prefix = "/api/v1/jobs/next/"
                if self.path.startswith(prefix):
                    if controller.secure and self.path[len(prefix):] != self.identity:
                        self.send_data({'error': 'A device can only fetch its own work'}, 403)
                        return
                    task = controller.next_task(self.path[len(prefix):])
                    self.send_data({"job": task})
                elif self.path == "/api/v1/health":
                    self.send_data({"ok": True, "version": __version__})
                else:
                    self.send_data({"error": "Unknown endpoint"}, 404)

        class ApiServer(ThreadingHTTPServer):
            def process_request_thread(self, request, client_address):
                request.settimeout(10)
                if controller.tls_context:
                    try:
                        request = controller.tls_context.wrap_socket(request, server_side=True)
                    except OSError:
                        request.close()
                        return
                super().process_request_thread(request, client_address)

        self.server = ApiServer(("0.0.0.0" if self.secure else '127.0.0.1', self.port), RequestHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.event(f"Controller started on port {self.port}", "success")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.swarm.close()
        self.storage.connection.close()
        if self.trust:
            self.trust.close()

    def uptime(self):
        seconds = int(time.time() - self.started_at)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"


def show_dashboard(app: ClusterController, clear_screen=True):
    if clear_screen:
        ui.clear()
    ui.logo()
    ui.rule()
    print(f" {ui.BOLD}Controller{ui.RESET}  {socket.gethostname()}@{local_ip()}:{app.port}")
    print(f" {ui.BOLD}System{ui.RESET}      {platform.system()} {platform.release()}")
    print(f" {ui.BOLD}Access{ui.RESET}      " + ('TLS / paired device credentials' if app.secure else 'Localhost regression-test transport'))
    print(f" {ui.BOLD}Uptime{ui.RESET}      {app.uptime()}   {ui.BOLD}Nodes{ui.RESET} {len(app.nodes)}   {ui.BOLD}Jobs{ui.RESET} {len(app.jobs)}")
    ui.rule()

    with app.lock:
        if not app.nodes:
            print(ui.YELLOW + "\n  No worker nodes connected yet.\n" + ui.RESET)
            print(f"  {ui.MUTED}Run invite to connect your first worker.{ui.RESET}\n")
        for node in app.nodes.values():
            state = node.state if is_online(node) else "offline"
            ram_used = 100 * (1 - node.ram_free / max(node.ram_total, 0.1))
            disk_used = 100 * (1 - node.disk_free / max(node.disk_total, 0.1))
            dot = ui.GREEN + "●" + ui.RESET if state != "offline" else ui.RED + "●" + ui.RESET
            print(f"\n {dot} {ui.BOLD}{node.name}{ui.RESET}  {ui.DIM}{node.address} · {node.system}{ui.RESET}")
            print(f"   CPU   {ui.bar(node.cpu_usage)} {node.cpu_usage:5.1f}%   {node.cpu_total} logical cores")
            print(f"   RAM   {ui.bar(ram_used)} {ram_used:5.1f}%   {node.ram_free:.1f}/{node.ram_total:.1f} GB free")
            print(f"   DISK  {ui.bar(disk_used)} {disk_used:5.1f}%   {node.disk_free:.1f}/{node.disk_total:.1f} GB free")
            docker = ui.GREEN + "available" + ui.RESET if node.docker else ui.DIM + "not installed" + ui.RESET
            print(f"   State {ui.status(state):27} Docker {docker}")
            offer = app.offers.get(node.node_id, {})
            score, reason = app.swarm.rank(node.node_id, offer)
            print(f"   Share {offer.get('reason', 'Legacy worker: no sharing offer')}")
            print(f"   Offer {offer.get('cores', 0)} cores / {offer.get('ram', 0)} GiB | score {score} | battery {offer.get('battery', 'unknown')}")
        print()
        ui.rule()
        counts = {state: sum(j.state == state for j in app.jobs.values()) for state in ("queued", "running", "done", "failed")}
        print(f" Jobs: {counts['queued']} queued / {counts['running']} running / {counts['done']} done / {counts['failed']} failed")
        show_swarm(app)


def show_swarm(app):
    print("\n BORROWED COMPUTE / FILE BATCHES")
    ui.rule()
    for batch in app.swarm.batches():
        counts = batch["counts"]
        done = counts.get("done", 0)
        progress = done / batch["total"] * 100
        print(f" {batch['id']}  {batch['name']}  {ui.bar(progress)} {done}/{batch['total']} files")
        print(f"   pending={counts.get('pending', 0)} running={counts.get('running', 0)} failed={counts.get('failed', 0)} cancelled={counts.get('cancelled', 0)} retries={batch['retries']}")
    print(" Commands: batch / batches / explain / report / cancel-batch / sample")


def swarm_dialog(app, command):
    if command == "batches":
        show_swarm(app)
    elif command == "explain":
        with app.lock:
            for node in app.nodes.values():
                offer = app.offers.get(node.node_id, {})
                score, reason = app.swarm.rank(node.node_id, offer)
                print(f" {node.name}: {offer.get('reason', 'No offer')}; score {score}")
                print(f"   {reason}; state={node.state}; online={is_online(node)}")
    elif command == "batch":
        print(" Analyze UTF-8 .txt/.md/.log files: word/line counts, SHA-256, duplicates.")
        print(" Selected file contents will be sent to your trusted workers over LAN.")
        demo_dir = Path(__file__).resolve().parents[2] / "demo_corpus"
        hint = f" [Enter for {demo_dir.name}]" if demo_dir.exists() else ""
        folder = input(f"Folder to analyze{hint}: ").strip().strip('"')
        if not folder and demo_dir.exists():
            folder = str(demo_dir)
        batch, count, skipped = app.swarm.submit(folder)
        print(f" Batch {batch}: {count} files queued; {len(skipped)} skipped. Use batches/report.")
        for item in skipped[:10]:
            print(f"   Skipped {item['path']}: {item['reason']}")
    elif command == "report":
        batch_id = input("Batch ID: ").strip()
        target, report = app.swarm.export(batch_id)
        print(f" {report['files']}/{report['total_files']} files, {report['words']} words, {len(report['duplicates'])} duplicate groups")
        print(f" Complete: {report['complete']}.")
        print(f" Reports: {target}, {target.with_suffix('.csv')}, and {target.with_suffix('.html')}")
        open_choice = input(" Open visual HTML dashboard in browser? [Y/n]: ").strip().lower()
        if open_choice != 'n':
            import webbrowser
            webbrowser.open(str(target.with_suffix('.html').resolve()))
    elif command == "cancel-batch":
        print("Cancelled." if app.swarm.cancel(input("Batch ID: ").strip()) else "Unknown batch.")
    elif command == "sample":
        import tempfile
        folder = Path(tempfile.mkdtemp(prefix="sample-", dir=app.swarm.root))
        for index in range(24):
            content = f"Document group {index % 12}\n" + "Borrowed compute makes spare laptops useful.\n" * 1000
            (folder / f"document-{index:02}.txt").write_text(content, encoding="utf-8")
        batch, count, _ = app.swarm.submit(folder)
        print(f" Sample {batch}: {count} files; expected 12 duplicate groups. Use batches/report.")


def show_jobs(app: ClusterController):
    print("\n" + ui.BOLD + " JOB HISTORY" + ui.RESET)
    ui.rule()
    print(f" {'ID':>3}  {'TYPE':10} {'NODE':18} {'CPU':>4} {'RAM':>8}  {'STATE':>10}")
    ui.rule()
    with app.lock:
        if not app.jobs:
            print(ui.DIM + " No jobs have been created." + ui.RESET)
        for job in app.jobs.values():
            print(f" {job.job_id:>3}  {job.kind:10} {job.node_name[:18]:18} {job.cpu:>4} {job.ram:>6.1f}G  {job.state:>10}")
            if job.output:
                print(ui.DIM + "      output: " + job.output.replace("\n", " ")[:120] + ui.RESET)
    print()


def show_logs(app: ClusterController):
    level_colors = {"success": ui.GREEN, "warning": ui.YELLOW, "error": ui.RED, "info": ui.BLUE}
    print("\n" + ui.BOLD + " EVENT LOG" + ui.RESET)
    ui.rule()
    for event in app.storage.recent_events():
        color = level_colors.get(event["level"], ui.BLUE)
        print(f" {ui.DIM}{event['created_at']}{ui.RESET}  {color}●{ui.RESET}  {event['message']}")
    print()


def ask_number(prompt: str, kind=float):
    while True:
        try:
            value = kind(input(prompt).strip().replace(",", "."))
            if value > 0:
                return value
        except ValueError:
            pass
        print(ui.RED + "Please enter a positive number." + ui.RESET)


def create_job_dialog(app: ClusterController):
    print("\n" + ui.BOLD + " CREATE A NEW JOB" + ui.RESET)
    print(" [1] System command")
    print(" [2] Docker container")
    choice = input(ui.CYAN + "Job type: " + ui.RESET).strip()
    if choice not in ("1", "2"):
        print(ui.RED + "Unknown job type." + ui.RESET)
        return
    kind = "command" if choice == "1" else "docker"
    cpu = ask_number("Required CPU cores: ", int)
    ram = ask_number("Required RAM (GB): ")
    disk = ask_number("Required free disk (GB): ")
    command = image = ports = ""
    if kind == "command":
        command = input("Command to run: ").strip()
        if not command:
            print(ui.RED + "Command cannot be empty." + ui.RESET)
            return
    else:
        image = input("Docker image (example: nginx:alpine): ").strip()
        if not image:
            print(ui.RED + "Docker image cannot be empty." + ui.RESET)
            return
        ports = input("Port mapping (example: 8080:80, optional): ").strip()
        command = input("Container command (optional): ").strip()
    job = app.create_job(kind, cpu, ram, disk, command, image, ports)
    if job:
        print(ui.GREEN + f"Job #{job.job_id} assigned to {job.node_name}." + ui.RESET)
    else:
        print(ui.RED + "No eligible worker: check free resources and the owner's allowed job types." + ui.RESET)


def run_controller(port: int, cluster_key: str, data_dir: Path, secure=True):
    app = ClusterController(port, cluster_key, data_dir, secure=secure)
    try:
        app.start()
    except OSError as error:
        print(ui.RED + f"Cannot start controller: {error}" + ui.RESET)
        return
    show_dashboard(app)
    print(ui.MUTED + " Type invite to pair a worker, or help for commands." + ui.RESET)
    try:
        while True:
            command = input(ui.CYAN + ui.BOLD + "cluster@controller > " + ui.RESET).strip().lower()
            if command in ("dashboard", "nodes", "status"):
                show_dashboard(app)
            elif command == "watch":
                try:
                    while True:
                        show_dashboard(app)
                        print(ui.DIM + " Live mode · Ctrl+C returns to the prompt" + ui.RESET)
                        time.sleep(2)
                except KeyboardInterrupt:
                    print()
            elif command == "jobs":
                show_jobs(app)
            elif command == "logs":
                show_logs(app)
            elif command == 'invite':
                print('\n One use / expires in 5 minutes / share privately with the device owner.\n')
                print(app.issue_invitation())
                print()
            elif command == 'devices':
                for device in app.trust.devices() if app.trust else []:
                    print(f" {device['id']}  {device['name']}  {'REVOKED' if device['revoked'] else 'PAIRED'}")
            elif command == 'revoke':
                node_id = input('Device ID (see devices): ').strip()
                print('Access revoked. Pending file chunks released.' if app.revoke_device(node_id) else 'Unknown or already revoked device.')
            elif command in ("batch", "batches", "explain", "report", "cancel-batch", "sample"):
                try:
                    swarm_dialog(app, command)
                except (ValueError, OSError) as error:
                    print(ui.RED + str(error) + ui.RESET)
            elif command == "run":
                create_job_dialog(app)
            elif command == "demo":
                job = app.create_job("demo", 1, 0.2, 0.1)
                message = f"Demo job #{job.job_id} assigned to {job.node_name}." if job else "No free node is available."
                print((ui.GREEN if job else ui.RED) + message + ui.RESET)
            elif command == "clear":
                ui.clear()
                ui.logo()
            elif command == "help":
                print("\n  dashboard/nodes  перегляд статусу та ресурсів підключених вузлів")
                print("  invite           створити одноразове запрошення для воркера (5 хв)")
                print("  devices          список підключених та відкликаних пристроїв")
                print("  revoke           відкликати доступ у пристрою та повернути його завдання")
                print("  watch            живий моніторинг кластера (оновлення кожні 2 сек)")
                print("  sample / batch   розподілити тестові або власні файли по воркерах")
                print("  batches          прогрес обробки файлів та кількість спроб")
                print("  report           експорт підсумкового звіту в JSON, CSV та HTML-дашборд")
                print("  explain          перевірити готовність та рейтинг кожного воркера")
                print("  demo             запустити тестове обчислення на кластері")
                print("  run              запустити системну команду або Docker-контейнер")
                print("  jobs             історія виконаних завдань")
                print("  logs             журнал подій контролера")
                print("  clear            очистити екран термінала")
                print("  exit             зупинити роботу контролера\n")
            elif command in ("exit", "quit"):
                break
            elif command:
                print(ui.RED + "Unknown command. Type 'help'." + ui.RESET)
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        print(ui.YELLOW + "Controller stopped." + ui.RESET)
        app.stop()
