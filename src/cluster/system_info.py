import platform
import socket
import subprocess
import psutil


def memory_stats() -> tuple[float, float]:
    vm = psutil.virtual_memory()
    total_gb = round(vm.total / (1024 ** 3), 1)
    free_gb = round(vm.available / (1024 ** 3), 1)
    return total_gb, free_gb


def disk_stats() -> tuple[float, float]:
    du = psutil.disk_usage("/")
    total_gb = round(du.total / (1024 ** 3), 1)
    free_gb = round(du.free / (1024 ** 3), 1)
    return total_gb, free_gb


def cpu_percent() -> float:
    return round(psutil.cpu_percent(interval=0.1), 1)


def docker_available() -> bool:
    try:
        res = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def system_name() -> str:
    return f"{platform.system()} {platform.release()}"


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
