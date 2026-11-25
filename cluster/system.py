import functools
import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

import psutil


DEFAULT_POLICY = {
    "enabled": True,
    "max_cores": 2,
    "max_ram_gb": 2.0,
    "reserve_ram_gb": 2.0,
    "max_host_cpu": 80.0,
    "minimum_battery": 30,
    "ac_only": False,
    "allowed_jobs": ["corpus", "demo", "exec", "host"],
}


def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


@functools.lru_cache(maxsize=1)
def detect_runtimes() -> list[str]:
    tools = [
        ("python", ["python", "python3", "py"]),
        ("node", ["node"]),
        ("gcc", ["gcc"]),
        ("g++", ["g++"]),
        ("go", ["go"]),
        ("rust", ["cargo", "rustc"]),
        ("java", ["java"]),
        ("dotnet", ["dotnet"]),
        ("php", ["php"]),
        ("docker", ["docker"]),
        ("ollama", ["ollama"]),
        ("git", ["git"]),
    ]
    found = []
    for name, bins in tools:
        if any(shutil.which(b) for b in bins):
            found.append(name)
    if "python" not in found:
        found.insert(0, "python-builtin")
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip():
                found.append("gpu")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return found


def get_system_stats() -> dict:
    mem = psutil.virtual_memory()
    disk_root = os.path.abspath(os.sep)
    disk = psutil.disk_usage(disk_root)
    battery = psutil.sensors_battery()
    runtimes = detect_runtimes()

    return {
        "system": f"{platform.system()} {platform.release()}",
        "cpu_total": psutil.cpu_count(logical=True) or 1,
        "cpu_usage": round(psutil.cpu_percent(interval=None), 1),
        "ram_total": round(mem.total / (1024 ** 3), 2),
        "ram_free": round(mem.available / (1024 ** 3), 2),
        "disk_total": round(disk.total / (1024 ** 3), 1),
        "disk_free": round(disk.free / (1024 ** 3), 1),
        "battery_percent": round(battery.percent) if battery else None,
        "ac_plugged": bool(battery.power_plugged) if battery else True,
        "docker": "docker" in runtimes,
        "runtimes": runtimes,
    }


def load_policy(data_dir: Path) -> dict:
    path = Path(data_dir) / "sharing.json"
    if not path.exists():
        return dict(DEFAULT_POLICY)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {**DEFAULT_POLICY, **raw}
    except (OSError, ValueError, TypeError):
        return {**DEFAULT_POLICY, "enabled": False, "error": "Invalid sharing.json"}


def save_policy(data_dir: Path, policy: dict):
    path = Path(data_dir) / "sharing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2, ensure_ascii=False), encoding="utf-8")


def check_sharing_ready(policy: dict, stats: dict) -> tuple[bool, str, dict]:
    if not policy.get("enabled", True):
        return False, policy.get("error", "Paused by owner"), {"cpu": 0, "ram_gb": 0.0}

    cpu_usage = float(stats["cpu_usage"])
    ram_free = float(stats["ram_free"])
    reserve = float(policy.get("reserve_ram_gb", 2.0))
    available_ram = max(0.0, round(ram_free - reserve, 2))
    shared_ram = min(float(policy.get("max_ram_gb", 2.0)), available_ram)
    shared_cores = min(int(stats["cpu_total"]), int(policy.get("max_cores", 2)))

    battery_pct = stats.get("battery_percent")
    ac_plugged = stats.get("ac_plugged", True)

    if policy.get("ac_only") and not ac_plugged:
        return False, "Paused: on battery", {"cpu": 0, "ram_gb": 0.0}

    if battery_pct is not None and not ac_plugged and battery_pct < float(policy.get("minimum_battery", 30)):
        return False, f"Paused: low battery ({battery_pct}%)", {"cpu": 0, "ram_gb": 0.0}

    if cpu_usage > float(policy.get("max_host_cpu", 80.0)):
        return False, f"Paused: host busy (CPU {cpu_usage}%)", {"cpu": 0, "ram_gb": 0.0}

    if shared_ram < 0.1 and float(policy.get("max_ram_gb", 2.0)) > 0:
        return False, f"Paused: low RAM ({ram_free} GB)", {"cpu": 0, "ram_gb": 0.0}

    return True, "Ready", {"cpu": shared_cores, "ram_gb": shared_ram}
