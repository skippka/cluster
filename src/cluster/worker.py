import json
import os
import socket
import subprocess
import shlex
import ssl
import threading
import time
import urllib.error
import uuid
from pathlib import Path

from . import ui
from .protocol import api_request
from .system_info import cpu_percent, disk_stats, docker_available, memory_stats, system_name
from .sharing import configure_sharing, load_policy, evaluate, power_status, allows_job
from .corpus import analyze


def node_identity(data_dir: Path) -> str:
    path = data_dir / "node_id"
    data_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = str(uuid.uuid4())
    path.write_text(value, encoding="utf-8")
    return value


def snapshot(node_id: str, name: str, docker: bool, data_dir=None) -> dict:
    ram_total, ram_free = memory_stats()
    disk_total, disk_free = disk_stats()
    info = {
        "node_id": node_id,
        "name": name,
        "system": system_name(),
        "cpu_total": os.cpu_count() or 1,
        "cpu_usage": cpu_percent(),
        "ram_total": ram_total,
        "ram_free": ram_free,
        "disk_total": disk_total,
        "disk_free": disk_free,
        "docker": docker,
    }
    if data_dir:
        policy = load_policy(data_dir)
        info["sharing"] = evaluate(policy, info, power_status())
        info['allowed_jobs'] = policy['allowed_jobs'] if policy['enabled'] else []
    return info


def heartbeat(server: str, key: str, node_id: str, state: str, data_dir=None, lease=None) -> dict:
    _, ram_free = memory_stats()
    _, disk_free = disk_stats()
    info = {"node_id": node_id, "cpu_usage": cpu_percent(), "cpu_total": os.cpu_count() or 1,
            "ram_free": ram_free, "disk_free": disk_free, "state": state}
    if data_dir:
        policy = load_policy(data_dir)
        info["sharing"] = evaluate(policy, info, power_status())
        info['allowed_jobs'] = policy['allowed_jobs'] if policy['enabled'] else []
    if lease:
        info["lease"] = {"job_id": lease["job_id"], "token": lease["token"]}
    return api_request(
        server,
        key,
        "POST",
        "/nodes/heartbeat",
        info,
    )


def wait_for_process(process, server: str, key: str, node_id: str):
    while process.poll() is None:
        try:
            heartbeat(server, key, node_id, "busy")
        except Exception:
            pass
        time.sleep(1)
    stdout, stderr = process.communicate()
    return stdout or "", stderr or ""


def execute(job: dict, server: str, key: str, node_id: str) -> tuple[str, str]:
    print(ui.CYAN + f"[{time.strftime('%H:%M:%S')}] Received job #{job['job_id']} ({job['kind']})" + ui.RESET)
    try:
        if job["kind"] == "demo":
            print(ui.DIM + "Running distributed calculation sample..." + ui.RESET)
            result = sum(number * number for number in range(5_000_000))
            time.sleep(2)
            return "done", f"Calculation result: {result}"

        if job["kind"] == "docker":
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                f"cluster_job_{job['job_id']}",
                "--cpus",
                str(job["cpu"]),
                "--memory",
                f"{job['ram']}g",
            ]
            if job.get("ports"):
                command.extend(["-p", job["ports"]])
            command.append(job["image"])
            if job.get("command"):
                command.extend(shlex.split(job["command"]))
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace"
            )
        else:
            process = subprocess.Popen(
                job["command"],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )

        stdout, stderr = wait_for_process(process, server, key, node_id)
        output = (stdout + "\n" + stderr).strip()
        return ("done" if process.returncode == 0 else "failed"), output
    except Exception as error:
        return "failed", str(error)


def show_worker(name: str, server: str, docker: bool):
    ram_total, ram_free = memory_stats()
    disk_total, disk_free = disk_stats()
    ram_used = 100 * (1 - ram_free / max(ram_total, 0.1))
    disk_used = 100 * (1 - disk_free / max(disk_total, 0.1))
    ui.clear()
    ui.logo("ВОРКЕР КЛАСТЕРА (WORKER AGENT)")
    ui.rule(66)
    print(f" {ui.BOLD}Вузол (Node){ui.RESET}        {name}")
    print(f" {ui.BOLD}Хост (Host){ui.RESET}        {socket.gethostname()}")
    print(f" {ui.BOLD}ОС (System){ui.RESET}        {system_name()}")
    print(f" {ui.BOLD}Контролер{ui.RESET}          {server}")
    print(f" {ui.BOLD}CPU{ui.RESET}         {os.cpu_count() or 1} logical cores")
    print(f" {ui.BOLD}RAM{ui.RESET}         {ui.bar(ram_used, 24)} {ram_free:.1f}/{ram_total:.1f} GB free")
    print(f" {ui.BOLD}Disk{ui.RESET}        {ui.bar(disk_used, 24)} {disk_free:.1f}/{disk_total:.1f} GB free")
    docker_text = ui.GREEN + "available" if docker else ui.YELLOW + "not installed"
    print(f" {ui.BOLD}Docker{ui.RESET}      {docker_text}{ui.RESET}")
    ui.rule(66)


def process_chunk(job, server, key, node_id, data_dir):
    stop = threading.Event()
    def keep_alive():
        while not stop.is_set():
            try:
                heartbeat(server, key, node_id, "busy", data_dir, job)
            except (OSError, ValueError):
                pass
            stop.wait(3)
    pulse = threading.Thread(target=keep_alive, daemon=True)
    pulse.start()
    try:
        offer = snapshot(node_id, "", False, data_dir)["sharing"]
        message = {"job_id": job["job_id"], "node_id": node_id, "token": job["token"]}
        if not offer["ready"] or not allows_job(data_dir, 'corpus'):
            message.update(state="failed", error=offer["reason"])
        else:
            try:
                message.update(state="done", result=analyze(job["payload"]))
            except (ValueError, KeyError) as error:
                message.update(state="failed", error=str(error))
        reply = api_request(server, key, "POST", "/swarm/result", message)
        print(f" File {job['payload']['path']}: {message['state']}; accepted={reply['ok']}")
        return reply["ok"]
    finally:
        stop.set()
        pulse.join(timeout=12)


def run_worker(server: str, key: str, name: str, data_dir: Path, stop_event=None, poll_seconds=1, display=True):
    server = server.rstrip("/")
    node_id = getattr(key, 'node_id', None) or node_identity(data_dir)
    configure_sharing(data_dir)
    docker = docker_available()
    cpu_percent()
    if display:
        show_worker(name, server, docker)
    print(f" Sharing policy: {data_dir / 'sharing.json'} (reloaded on every heartbeat)")
    print(" Set enabled=false to pause new file chunks. Current file finishes first.")
    registered = False
    stop_event = stop_event or threading.Event()

    while not stop_event.is_set():
        try:
            if not registered:
                info = snapshot(node_id, name, docker, data_dir)
                api_request(server, key, "POST", "/nodes/register", info)
                registered = True
                print(ui.GREEN + "● Connected. Waiting for jobs..." + ui.RESET)

            reply = heartbeat(server, key, node_id, "free", data_dir)
            if reply.get("register"):
                registered = False
                continue

            response = api_request(server, key, "GET", f"/jobs/next/{node_id}")
            job = response.get("job")
            if job and job["kind"] == "corpus":
                process_chunk(job, server, key, node_id, data_dir)
            elif job:
                if allows_job(data_dir, job['kind']):
                    state, output = execute(job, server, key, node_id)
                else:
                    state, output = 'failed', 'Worker owner did not permit this job type'
                api_request(
                    server,
                    key,
                    "POST",
                    "/jobs/result",
                    {
                        "job_id": job["job_id"],
                        "node_id": node_id,
                        "state": state,
                        "output": output,
                    },
                )
                color = ui.GREEN if state == "done" else ui.RED
                print(color + f"Job #{job['job_id']} finished: {state}." + ui.RESET)
            stop_event.wait(poll_seconds)
        except KeyboardInterrupt:
            print(ui.YELLOW + "\nWorker stopped." + ui.RESET)
            return
        except ssl.SSLError as error:
            print(ui.RED + f'Controller verification failed: {error}' + ui.RESET)
            return
        except urllib.error.HTTPError as error:
            if error.code == 401:
                print(ui.RED + "Access denied. This device may have been revoked; pair again with a new invitation." + ui.RESET)
                return
            print(ui.YELLOW + f"HTTP error {error.code}. Retrying in 5 seconds..." + ui.RESET)
            registered = False
            stop_event.wait(5)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            message = "Connection lost" if registered else "Cannot reach controller"
            print(ui.YELLOW + f"[{time.strftime('%H:%M:%S')}] {message}. Retrying..." + ui.RESET)
            registered = False
            stop_event.wait(5)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            print(ui.RED + f"Worker error: {error}" + ui.RESET)
            registered = False
            stop_event.wait(5)
