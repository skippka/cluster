"""Точка входа Cluster LAN."""

import sys
from pathlib import Path

from cluster.gui import ClusterTerminalApp


def get_paths() -> tuple[Path, Path]:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", exe_dir))
        return exe_dir / "data", bundle_dir
    root = Path(__file__).resolve().parent
    return root / "data", root


if __name__ == "__main__":
    data_dir, project_root = get_paths()
    app = ClusterTerminalApp(data_dir=data_dir, project_root=project_root)
    app.run()
