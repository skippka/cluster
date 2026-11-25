import base64
import contextlib
import http.server
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

from .security import WorkerSession
from .system import check_sharing_ready, get_system_stats, load_policy, local_ip
from .tasks import analyze_chunk


def _unpack_bundle(bundle_b64: str, target_dir: Path):
    raw = base64.b64decode(bundle_b64)
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        for member in zf.infolist():
            dest = (target_dir / member.filename).resolve()
            if not dest.is_relative_to(target_dir.resolve()):
                raise ValueError("Unsafe path in archive")
        zf.extractall(target_dir)


def _auto_command(workdir: Path, entry_name: str) -> str:
    if entry_name:
        ext = Path(entry_name).suffix.lower()
        if ext == ".py":
            return f'python "{entry_name}"'
        if ext == ".js":
            return f'node "{entry_name}"'
        if ext == ".go":
            return f'go run "{entry_name}"'
        if ext == ".c":
            return f'gcc "{entry_name}" -o app.exe && app.exe' if os.name == "nt" else f'gcc "{entry_name}" -o app && ./app'
        if ext == ".cpp":
            return f'g++ "{entry_name}" -o app.exe && app.exe' if os.name == "nt" else f'g++ "{entry_name}" -o app && ./app'
        if ext == ".java":
            return f'java "{entry_name}"'
        if ext == ".php":
            return f'php "{entry_name}"'
        if ext == ".ps1":
            return f'powershell -ExecutionPolicy Bypass -File "{entry_name}"'
        if ext in (".bat", ".cmd"):
            return f'"{entry_name}"'
        if ext == ".sh":
            return f'bash "{entry_name}"'
        return f'"{entry_name}"'

    for candidate in ("main.py", "app.py", "run.py", "train.py"):
        if (workdir / candidate).exists():
            return f'python "{candidate}"'
    for candidate in ("index.js", "server.js", "app.js"):
        if (workdir / candidate).exists():
            return f'node "{candidate}"'
    if (workdir / "main.go").exists():
        return "go run ."
    if (workdir / "Cargo.toml").exists():
        return "cargo run"
    if (workdir / "package.json").exists():
        return "npm start"
    raise ValueError("Specify a command to run this project")


def _run_python_builtin(script_path: Path, workdir: Path, args: list[str] | None = None) -> tuple[str, str]:
    code = script_path.read_text(encoding="utf-8")
    buf = io.StringIO()
    old_cwd = os.getcwd()
    old_path = list(sys.path)
    old_argv = list(sys.argv)
    try:
        os.chdir(workdir)
        sys.path.insert(0, str(workdir))
        sys.argv = [str(script_path)] + (args or [])
        ns = {"__name__": "__main__", "__file__": str(script_path)}
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(code, str(script_path), "exec"), ns)
        return "done", buf.getvalue().strip() or "Completed"
    except Exception:
        return "failed", (buf.getvalue() + "\n" + traceback.format_exc()).strip()
    finally:
        os.chdir(old_cwd)
        sys.path[:] = old_path
        sys.argv[:] = old_argv


class ClusterWorker:
    def __init__(self, session: WorkerSession, data_dir: Path, poll_seconds: float = 1.0, on_log=None):
        self.session = session
        self.data_dir = Path(data_dir)
        self.poll_seconds = poll_seconds
        self.on_log = on_log or (lambda msg, level="info": None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active_hosts: dict[str, dict] = {}

    def log(self, msg: str, level: str = "info"):
        self.on_log(msg, level)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        for jid in list(self.active_hosts):
            self._stop_host(jid)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _register(self):
        stats = get_system_stats()
        policy = load_policy(self.data_dir)
        ready, reason, share = check_sharing_ready(policy, stats)
        self.session.request_json(
            "POST",
            "/api/v1/nodes/register",
            {
                "node_id": self.session.node_id,
                "name": self.session.name,
                **stats,
                "allowed_jobs": policy.get("allowed_jobs", ["corpus", "demo"]),
                "sharing": {"ready": ready, "reason": reason, **share},
            },
        )
        self.log(f"Worker '{self.session.name}' connected ({reason})", "success")

    def _loop(self):
        registered = False
        while not self._stop.is_set():
            try:
                if not registered:
                    self._register()
                    registered = True

                stats = get_system_stats()
                policy = load_policy(self.data_dir)
                ready, reason, share = check_sharing_ready(policy, stats)

                hb = self.session.request_json(
                    "POST",
                    "/api/v1/nodes/heartbeat",
                    {
                        "node_id": self.session.node_id,
                        "cpu_usage": stats["cpu_usage"],
                        "ram_free": stats["ram_free"],
                        "disk_free": stats["disk_free"],
                        "battery_percent": stats["battery_percent"],
                        "ac_plugged": stats["ac_plugged"],
                        "runtimes": stats["runtimes"],
                        "state": "free" if ready else "paused",
                        "sharing": {"ready": ready, "reason": reason, **share},
                        "allowed_jobs": policy.get("allowed_jobs", ["corpus", "demo"]),
                    },
                )
                if hb.get("register"):
                    registered = False
                    continue

                resp = self.session.request_json("GET", f"/api/v1/jobs/next/{self.session.node_id}")
                task = resp.get("job")
                if not task:
                    self._stop.wait(self.poll_seconds)
                    continue

                kind = task.get("kind")
                if kind == "stop_host":
                    self._stop_host(task["job_id"])
                    continue

                if not ready:
                    self._stop.wait(self.poll_seconds)
                    continue

                if kind in ("corpus", "map"):
                    self._run_swarm_chunk(task)
                else:
                    self._run_job(task)

            except Exception as err:
                registered = False
                self.log(f"Connection warning: {err}", "warning")
                self._stop.wait(min(5.0, self.poll_seconds * 2))

    def _run_swarm_chunk(self, task: dict):
        kind = task.get("kind", "corpus")
        try:
            if kind == "corpus":
                result = analyze_chunk(task["payload"])
                msg = f"Analyzed {result['path']} ({result['words']} words)"
            else:
                payload = task["payload"]
                with tempfile.TemporaryDirectory(prefix="cluster-map-") as tmp:
                    workdir = Path(tmp)
                    fpath = workdir / Path(payload["path"]).name
                    fpath.write_bytes(base64.b64decode(payload["content"]))
                    if payload.get("script_name") and payload.get("script_b64"):
                        (workdir / payload["script_name"]).write_bytes(base64.b64decode(payload["script_b64"]))
                    cmd = payload["command"]
                    if "{file}" in cmd:
                        cmd = cmd.replace("{file}", f'"{fpath.name}"')
                    else:
                        cmd = f'{cmd} "{fpath.name}"'

                    parts = cmd.split()
                    if parts and parts[0].lower() in ("python", "python3", "py"):
                        py_bin = sys.executable if not getattr(sys, "frozen", False) else (shutil.which("python") or shutil.which("py"))
                        if not py_bin and payload.get("script_name"):
                            st, out = _run_python_builtin(workdir / payload["script_name"], workdir, [fpath.name])
                            result = {"path": payload["path"], "exit_code": 0 if st == "done" else 1, "output": out[:2000]}
                            if st != "done":
                                raise RuntimeError(out or "Script failed")
                        else:
                            if py_bin:
                                prefix = f'"{py_bin}" -3' if Path(py_bin).stem.lower() == "py" else f'"{py_bin}"'
                                cmd = prefix + cmd[len(parts[0]):]
                            proc = subprocess.run(cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=180)
                            result = {"path": payload["path"], "exit_code": proc.returncode, "output": (proc.stdout + proc.stderr).strip()[:2000]}
                            if proc.returncode != 0:
                                raise RuntimeError(result["output"] or f"Exit code {proc.returncode}")
                    else:
                        proc = subprocess.run(cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=180)
                        result = {"path": payload["path"], "exit_code": proc.returncode, "output": (proc.stdout + proc.stderr).strip()[:2000]}
                        if proc.returncode != 0:
                            raise RuntimeError(result["output"] or f"Exit code {proc.returncode}")
                msg = f"Processed {payload['path']}"

            self.session.request_json(
                "POST",
                "/api/v1/swarm/result",
                {"node_id": self.session.node_id, "job_id": task["job_id"], "token": task["token"], "ok": True, "result": result},
            )
            self.log(msg, "info")
        except Exception as err:
            self.session.request_json(
                "POST",
                "/api/v1/swarm/result",
                {"node_id": self.session.node_id, "job_id": task["job_id"], "token": task["token"], "ok": False, "error": str(err)},
            )
            self.log(f"Chunk error: {err}", "error")

    def _run_job(self, task: dict):
        jid = task["job_id"]
        kind = task["kind"]
        self.log(f"Running task #{jid} ({kind})...", "info")
        try:
            if kind == "demo":
                t0 = time.perf_counter()
                primes = sum(1 for n in range(2, 18000) if all(n % d for d in range(2, int(math.sqrt(n)) + 1)))
                dt = time.perf_counter() - t0
                out = f"Found {primes} primes up to 18000 in {dt:.2f}s"
                state, url = "done", ""
            elif kind == "exec":
                state, out = self._run_exec(task)
                url = ""
            elif kind == "host":
                state, out, url = self._start_host(jid, task)
            else:
                state, out, url = "failed", f"Unknown task kind: {kind}", ""
        except Exception as err:
            state, out, url = "failed", str(err), ""

        self.session.request_json(
            "POST",
            "/api/v1/jobs/result",
            {"job_id": jid, "node_id": self.session.node_id, "state": state, "output": out[:4000], "url": url},
        )
        self.log(f"Task #{jid} ({state}): {out[:160]}", "success" if state in ("done", "running") else "error")

    def _run_exec(self, task: dict) -> tuple[str, str]:
        bundle_b64 = task.get("bundle_b64", "")
        entry_name = task.get("entry_name", "")
        cmd = (task.get("command") or "").strip()

        with tempfile.TemporaryDirectory(prefix="cluster-exec-") as tmp:
            workdir = Path(tmp)
            if bundle_b64:
                _unpack_bundle(bundle_b64, workdir)
            if not cmd:
                cmd = _auto_command(workdir, entry_name)

            parts = cmd.split()
            if parts and parts[0].lower() in ("python", "python3", "py"):
                py_bin = sys.executable if not getattr(sys, "frozen", False) else (shutil.which("python") or shutil.which("py"))
                if not py_bin and len(parts) >= 2:
                    script_name = parts[1].strip('"').strip("'")
                    script_file = workdir / script_name
                    if script_file.exists():
                        return _run_python_builtin(script_file, workdir)
                if py_bin:
                    prefix = f'"{py_bin}" -3' if Path(py_bin).stem.lower() == "py" else f'"{py_bin}"'
                    cmd = prefix + cmd[len(parts[0]):]

            proc = subprocess.run(cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=300)
            out = (proc.stdout + proc.stderr).strip() or f"Exit code: {proc.returncode}"
            return ("done" if proc.returncode == 0 else "failed"), out

    def _start_host(self, job_id: str, task: dict) -> tuple[str, str, str]:
        port = int(task.get("port") or 8080)
        cmd = (task.get("command") or "").strip()
        bundle_b64 = task.get("bundle_b64", "")
        url = f"http://{local_ip()}:{port}"

        host_dir = Path(tempfile.mkdtemp(prefix=f"cluster-host-{job_id}-"))
        if bundle_b64:
            _unpack_bundle(bundle_b64, host_dir)

        if cmd:
            final_cmd = cmd.replace("{PORT}", str(port))
            parts = final_cmd.split()
            if parts and parts[0].lower() in ("python", "python3", "py"):
                py_bin = sys.executable if not getattr(sys, "frozen", False) else (shutil.which("python") or shutil.which("py"))
                if py_bin:
                    prefix = f'"{py_bin}" -3' if Path(py_bin).stem.lower() == "py" else f'"{py_bin}"'
                    final_cmd = prefix + final_cmd[len(parts[0]):]
            proc = subprocess.Popen(final_cmd, shell=True, cwd=str(host_dir))
            time.sleep(0.6)
            if proc.poll() is not None:
                return "failed", f"Service exited immediately (code {proc.returncode})", ""
            self.active_hosts[job_id] = {"proc": proc, "dir": host_dir}
            return "running", f"Service running at {url}", url

        serve_dir = host_dir if bundle_b64 else (Path(cmd) if cmd and Path(cmd).is_dir() else None)
        worker_name = self.session.name

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(serve_dir) if serve_dir else None, **kwargs)

            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if serve_dir:
                    return super().do_GET()
                body = (
                    f"<html><body style='background:#0e0e10;color:#ececef;font-family:sans-serif;padding:40px;'>"
                    f"<h2>cluster</h2>"
                    f"<p>Node: <b>{worker_name}</b></p>"
                    f"<p>Port: <b>{port}</b> | Service #{job_id}</p>"
                    f"</body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.active_hosts[job_id] = {"server": srv, "dir": host_dir}
        return "running", f"Hosting active at {url}", url

    def _stop_host(self, job_id: str):
        info = self.active_hosts.pop(job_id, None)
        if not info:
            return
        if "server" in info:
            info["server"].shutdown()
            info["server"].server_close()
        if "proc" in info:
            proc = info["proc"]
            if proc.poll() is None:
                proc.terminate()
        shutil.rmtree(info.get("dir", ""), ignore_errors=True)
        self.log(f"Service #{job_id} stopped", "warning")
