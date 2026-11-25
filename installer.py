import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from cluster.gui import ACCENT, ACCENT_TEXT, BG, BORDER, CARD, FONT_BOLD, FONT_MONO, FONT_UI, GREEN, MUTED, RED, SURFACE, TEXT, RoundedCard, make_pill


INSTALL_TEXTS = {
    "en": {
        "title": "cluster setup",
        "sub": "Standalone installer: bundles Python runtime, TLS cryptography, psutil & examples",
        "dir_lbl": "Install location",
        "browse": "Browse",
        "chk_desktop": "Create Desktop shortcut",
        "chk_start": "Create Start Menu shortcut",
        "chk_launch": "Launch cluster after install",
        "btn_install": "Install",
        "btn_close": "Close",
        "btn_launch": "Launch cluster",
        "step_copy": "Installing bundled runtime and libraries...",
        "step_examples": "Copying real-world examples...",
        "step_shortcuts": "Creating Windows shortcuts...",
        "step_verify": "Verifying installation...",
        "done": "Installation complete. Ready to run on any Windows PC.",
        "err": "Install failed: {err}",
    },
    "uk": {
        "title": "Встановлення cluster",
        "sub": "Автономний інсталятор: Python, бібліотеки TLS/psutil та приклади всередині",
        "dir_lbl": "Папка встановлення",
        "browse": "Обрати",
        "chk_desktop": "Створити ярлик на робочому столі",
        "chk_start": "Створити ярлик у меню Пуск",
        "chk_launch": "Запустити cluster після встановлення",
        "btn_install": "Встановити",
        "btn_close": "Закрити",
        "btn_launch": "Запустити cluster",
        "step_copy": "Встановлення вбудованого Python та бібліотек...",
        "step_examples": "Копіювання прикладів задач...",
        "step_shortcuts": "Створення ярликів Windows...",
        "step_verify": "Перевірка файлів...",
        "done": "Встановлення завершено. Програма готова до роботи.",
        "err": "Помилка: {err}",
    },
}


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _create_shortcut(lnk_path: Path, target_exe: Path, work_dir: Path, icon_path: Path):
    import win32com.client
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(lnk_path))
    shortcut.TargetPath = str(target_exe)
    shortcut.WorkingDirectory = str(work_dir)
    if icon_path.exists():
        shortcut.IconLocation = str(icon_path)
    shortcut.Save()


class ClusterInstallerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("cluster setup")
        self.root.geometry("560x430")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        self.lang = "en"
        self.bundle = _bundle_root()
        self.payload = self.bundle / "payload"
        if not self.payload.exists():
            self.payload = self.bundle

        self.logo_img = None
        self._load_icons()
        self._setup_styles()
        self._build_ui()

    def _load_icons(self):
        for base in (self.payload / "assets", self.bundle / "assets"):
            ico = base / "logo.ico"
            if ico.exists():
                try:
                    self.root.iconbitmap(str(ico))
                except tk.TclError:
                    pass
                small = base / "logo_small.png"
                if small.exists():
                    try:
                        self.logo_img = tk.PhotoImage(file=str(small))
                    except tk.TclError:
                        pass
                break

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor=SURFACE, background=ACCENT, thickness=6, borderwidth=0)

    def t(self, key: str, **kwargs):
        val = INSTALL_TEXTS[self.lang][key]
        return val.format(**kwargs) if kwargs else val

    def _set_lang(self, lang: str):
        self.lang = lang
        self._build_ui()

    def _build_ui(self):
        for w in self.root.winfo_children():
            w.destroy()

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=18)

        card = RoundedCard(outer, radius=24, pad=24)
        card.pack(fill="both", expand=True)
        box = card.inner

        hdr = tk.Frame(box, bg=CARD)
        hdr.pack(fill="x")
        if self.logo_img is not None:
            tk.Label(hdr, image=self.logo_img, bg=CARD).pack(side="left", padx=(0, 8))
        else:
            tk.Label(hdr, text="●", bg=CARD, fg=ACCENT, font=("Consolas", 14, "bold")).pack(side="left", padx=(0, 8))
        tk.Label(hdr, text=self.t("title"), bg=CARD, fg=TEXT, font=("Consolas", 15, "bold")).pack(side="left")

        make_pill(
            hdr, "UA", lambda: self._set_lang("uk"),
            bg=ACCENT if self.lang == "uk" else SURFACE, fg=ACCENT_TEXT if self.lang == "uk" else MUTED, padx=9, pady=2,
        ).pack(side="right", padx=(4, 0))
        make_pill(
            hdr, "EN", lambda: self._set_lang("en"),
            bg=ACCENT if self.lang == "en" else SURFACE, fg=ACCENT_TEXT if self.lang == "en" else MUTED, padx=9, pady=2,
        ).pack(side="right")

        tk.Label(box, text=self.t("sub"), bg=CARD, fg=MUTED, font=FONT_UI, wraplength=470, justify="left").pack(anchor="w", pady=(8, 16))

        default_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "cluster"
        self.dir_var = tk.StringVar(value=str(default_dir))

        tk.Label(box, text=self.t("dir_lbl"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
        d_row = tk.Frame(box, bg=CARD)
        d_row.pack(fill="x", pady=(4, 12))
        tk.Entry(d_row, textvariable=self.dir_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        make_pill(
            d_row, self.t("browse"),
            lambda: self.dir_var.set(filedialog.askdirectory(initialdir=self.dir_var.get()) or self.dir_var.get()),
            bg=SURFACE, fg=TEXT, padx=12, pady=4,
        ).pack(side="right")

        self.desktop_var = tk.BooleanVar(value=True)
        self.start_var = tk.BooleanVar(value=True)
        self.launch_var = tk.BooleanVar(value=True)
        for txt, var in (
            (self.t("chk_desktop"), self.desktop_var),
            (self.t("chk_start"), self.start_var),
            (self.t("chk_launch"), self.launch_var),
        ):
            tk.Checkbutton(box, text=txt, variable=var, bg=CARD, fg=TEXT, selectcolor=SURFACE, activebackground=CARD).pack(anchor="w", pady=2)

        self.prog = ttk.Progressbar(box, maximum=100)
        self.prog.pack(fill="x", pady=(16, 6))
        self.status_lbl = tk.Label(box, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.status_lbl.pack(anchor="w")

        self.foot = tk.Frame(box, bg=CARD)
        self.foot.pack(fill="x", side="bottom")
        make_pill(self.foot, self.t("btn_install"), self._start_install, bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=20, pady=6).pack(side="right")
        make_pill(self.foot, self.t("btn_close"), self.root.destroy, bg=SURFACE, fg=MUTED, padx=14, pady=6).pack(side="right", padx=(0, 8))

    def _start_install(self):
        dest = Path(self.dir_var.get().strip())
        threading.Thread(target=self._run_install, args=(dest,), daemon=True).start()

    def _ui_step(self, pct: int, msg: str, color: str = MUTED):
        self.root.after(0, lambda: (self.prog.configure(value=pct), self.status_lbl.config(text=msg, fg=color)))

    def _run_install(self, dest: Path):
        try:
            self._ui_step(20, self.t("step_copy"))
            dest.mkdir(parents=True, exist_ok=True)
            exe_src = self.payload / "cluster.exe"
            if not exe_src.exists():
                exe_src = self.bundle / "dist" / "cluster.exe"
            exe_dst = dest / "cluster.exe"
            shutil.copy2(exe_src, exe_dst)

            self._ui_step(50, self.t("step_examples"))
            for sub in ("examples", "assets"):
                src_sub = self.payload / sub
                if src_sub.exists():
                    dst_sub = dest / sub
                    if dst_sub.exists():
                        shutil.rmtree(dst_sub, ignore_errors=True)
                    shutil.copytree(src_sub, dst_sub)

            readme = self.payload / "README.md"
            if readme.exists():
                shutil.copy2(readme, dest / "README.md")

            self._ui_step(75, self.t("step_shortcuts"))
            ico = dest / "assets" / "logo.ico"
            if self.desktop_var.get():
                try:
                    import win32com.client
                    shell = win32com.client.Dispatch("WScript.Shell")
                    desktop = Path(shell.SpecialFolders("Desktop"))
                    _create_shortcut(desktop / "cluster.lnk", exe_dst, dest, ico)
                except Exception:
                    pass
            if self.start_var.get():
                try:
                    appdata = Path(os.environ.get("APPDATA", ""))
                    if appdata.exists():
                        _create_shortcut(appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "cluster.lnk", exe_dst, dest, ico)
                except Exception:
                    pass

            self._ui_step(95, self.t("step_verify"))
            if not exe_dst.exists() or exe_dst.stat().st_size < 100000:
                raise RuntimeError("Missing cluster.exe after copy")

            self._ui_step(100, self.t("done"), GREEN)
            self.root.after(0, lambda: self._on_finished(exe_dst, dest))
        except Exception as err:
            self._ui_step(0, self.t("err", err=err), RED)

    def _on_finished(self, exe_dst: Path, dest: Path):
        if self.launch_var.get():
            subprocess.Popen([str(exe_dst)], cwd=str(dest))
            self.root.after(400, self.root.destroy)
            return
        for w in self.foot.winfo_children():
            w.destroy()
        make_pill(
            self.foot, self.t("btn_launch"),
            lambda: (subprocess.Popen([str(exe_dst)], cwd=str(dest)), self.root.destroy()),
            bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=18, pady=6,
        ).pack(side="right")
        make_pill(self.foot, self.t("btn_close"), self.root.destroy, bg=SURFACE, fg=MUTED, padx=14, pady=6).pack(side="right", padx=(0, 8))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ClusterInstallerApp().run()
