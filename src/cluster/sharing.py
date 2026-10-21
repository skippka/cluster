import json
import math
from pathlib import Path
import psutil

DEFAULT_POLICY = {
    "enabled": True,
    "max_cores": 2,
    "max_ram_gb": 2.0,
    "reserve_ram_gb": 2.0,
    "max_host_cpu": 75.0,
    "minimum_battery": 35,
    "ac_only": False,
    "allowed_jobs": ["corpus", "demo"],
}


def load_policy(data_dir):
    path = Path(data_dir) / "sharing.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        policy = {**DEFAULT_POLICY, **value}
        for key in ("max_cores", "max_ram_gb", "reserve_ram_gb", "max_host_cpu", "minimum_battery"):
            if isinstance(policy[key], bool) or not math.isfinite(float(policy[key])) or float(policy[key]) < 0:
                raise ValueError(key)
        for key in ("enabled", "ac_only"):
            if not isinstance(policy[key], bool):
                raise ValueError(key)
        if not isinstance(policy["allowed_jobs"], list) or any(
            val not in ("corpus", "demo", "command", "docker") for val in policy["allowed_jobs"]
        ):
            raise ValueError("allowed_jobs")
        return policy
    except (OSError, ValueError, TypeError):
        return {**DEFAULT_POLICY, "enabled": False, "error": "Invalid or missing sharing.json"}


def configure_sharing(data_dir):
    path = Path(data_dir) / "sharing.json"
    if path.exists():
        return
    print("\nНалаштування виділення ресурсів для кластера:")
    print(" [1] Збалансований: резервувати 2 ГБ RAM, пауза якщо CPU > 75%")
    print(" [2] Тільки від розетки (AC): збалансований + захист батареї")
    print(" [3] Вимкнено: не приймати завдання")
    choice = input("Оберіть профіль [1]: ").strip() or "1"
    policy = {**DEFAULT_POLICY, "enabled": choice in ("1", "2"), "ac_only": choice == "2"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")


def allows_job(data_dir, kind):
    policy = load_policy(data_dir)
    if not policy.get("enabled") or "error" in policy:
        return False
    return kind in policy["allowed_jobs"]


def power_status():
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return {"ac": None, "battery": None}
        return {"ac": bool(battery.power_plugged), "battery": int(battery.percent)}
    except Exception:
        return {"ac": None, "battery": None}


def evaluate(policy, metrics, power):
    cores = max(0, min(int(metrics["cpu_total"]), int(policy["max_cores"])))
    ram = max(0.0, min(float(policy["max_ram_gb"]), metrics["ram_free"] - float(policy["reserve_ram_gb"])))
    reason = "Ready: spare capacity available"
    if not policy["enabled"]:
        reason = policy.get("error", "Sharing paused by owner")
    elif policy["ac_only"] and power["ac"] is not True:
        reason = "Waiting for confirmed AC power"
    elif power["battery"] is not None and power["battery"] < policy["minimum_battery"] and power["ac"] is not True:
        reason = "Protecting low battery"
    elif metrics["cpu_usage"] > policy["max_host_cpu"]:
        reason = "Owner workload: host CPU is above admission threshold"
    elif cores < 1 or ram < 0.25:
        reason = "Keeping reserved capacity for owner"
        
    return {
        "ready": reason.startswith("Ready:"),
        "reason": reason,
        "cores": cores,
        "ram": round(ram, 2),
        "cpu_usage": metrics["cpu_usage"],
        **power,
    }
