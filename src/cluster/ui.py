import os
import re
import shutil
import sys

from . import __version__

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ACCENT = "\033[96m"
CYAN = "\033[96m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GRAY = "\033[90m"
WHITE = "\033[97m"
MUTED = "\033[90m"


def enable_colors():
    if os.name == "nt":
        os.system("")


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def restore():
    print(RESET, end="", flush=True)


def clean(value):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "".join(char for char in text if char.isprintable())


def width():
    return min(72, max(30, shutil.get_terminal_size((80, 30)).columns - 5))


def logo(subtitle="СИСТЕМА РОЗПОДІЛЕНИХ ОБЧИСЛЕНЬ (LAN)"):
    print()
    print(f"  {CYAN}===================================================={RESET}")
    print(f"  {WHITE}{BOLD}             CLUSTER SYSTEM (v{__version__}){RESET}")
    if subtitle:
        print(f"  {MUTED}       {subtitle}{RESET}")
    print(f"  {CYAN}===================================================={RESET}")
    print()


def card(number, title, description, detail="", badge=""):
    print(f"  {CYAN}[{number}]{RESET} {WHITE}{BOLD}{title}{RESET}")
    print(f"      {WHITE}{description}{RESET}")
    if detail:
        print(f"      {MUTED}{detail}{RESET}")


def start_menu():
    print(f"  {WHITE}{BOLD}Оберіть роль комп'ютера у кластері:{RESET}\n")
    card("1", "Контролер (Controller)", "Запустити сервер керування кластером", "Приймає підключення та розподіляє завдання")
    print()
    card("2", "Воркер (Worker)", "Підключитися як обчислювальний вузол", "Виконує надіслані завдання на цьому ПК")
    print()
    print(f"  {MUTED}[3] Вихід{RESET}\n")


def rule(size=72):
    columns = min(size, shutil.get_terminal_size((72, 24)).columns - 1)
    print(GRAY + "-" * columns + RESET)


def color_for_percent(value):
    return GREEN if value < 55 else YELLOW if value < 80 else RED


def bar(value, width=18):
    value = max(0.0, min(100.0, float(value)))
    filled = round(width * value / 100)
    return color_for_percent(value) + "#" * filled + GRAY + "-" * (width - filled) + RESET


def status(value):
    colors = {
        "online": GREEN,
        "free": GREEN,
        "busy": YELLOW,
        "reserved": ACCENT,
        "offline": RED,
        "queued": ACCENT,
        "running": YELLOW,
        "done": GREEN,
        "failed": RED,
    }
    return colors.get(value, WHITE) + clean(value).upper() + RESET
