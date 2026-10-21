import getpass
import socket
from pathlib import Path

from . import ui
from .controller import run_controller
from .worker import run_worker
from .trust import NodeSession


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


def ask_port() -> int | None:
    value = input("Порт [8000]: ").strip()
    try:
        port = int(value) if value else 8000
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except ValueError:
        print(ui.RED + "Порт має бути числом від 1 до 65535." + ui.RESET)
        return None


def main():    
    ui.enable_colors()
    ui.clear()
    ui.logo()
    ui.start_menu()
    try:
        _session_menu()
    except (KeyboardInterrupt, EOFError):
        print('\nСесію завершено.')
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(ui.RED + str(error) + ui.RESET)
    except ImportError:
        print('Спочатку встановіть залежності: python -m pip install -r requirements.txt')
    finally:
        ui.restore()


def _session_menu():
    choice = input(f"  {ui.ACCENT}>{ui.RESET} Оберіть опцію [1 / 2 / 3]: ").strip()

    if choice == "1":
        port = ask_port()
        if port is not None:
            run_controller(port, '', DATA_DIR, secure=True)
    elif choice == "2":
        session = None
        if (DATA_DIR / 'connection.json').exists():
            session = NodeSession.load(DATA_DIR)
            print(f'\n  Збережений контролер: {session.server}')
            if input('  Перепідключитися? [Y/n]: ').strip().lower() == 'n':
                session = None
        if session is None:
            print('\n  Введіть команду "invite" на контролері та вставте запрошення сюди:')
            invite = input('  Запрошення (cluster://...): ').strip()
            name = input(f"  Ім'я цього ПК [{socket.gethostname()}]: ").strip() or socket.gethostname()
            session = NodeSession.pair(invite, name, DATA_DIR)
            print(ui.GREEN + '  Успішно підключено до контролера!' + ui.RESET)
        run_worker(session.server, session, session.name, DATA_DIR)
    elif choice == "3":
        print(ui.YELLOW + "До побачення!" + ui.RESET)
    else:
        print(ui.RED + "Невідома опція." + ui.RESET)
