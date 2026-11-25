import queue
import socket
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from .controller import ClusterController, is_online
from .lang import TEXTS, load_lang, save_lang
from .security import WorkerSession, scan_lan_controllers
from .system import DEFAULT_POLICY, get_system_stats, local_ip, save_policy
from .worker import ClusterWorker


BG = "#0e0e10"
CARD = "#161619"
SURFACE = "#1c1c21"
BORDER = "#27272d"
TEXT = "#ececef"
MUTED = "#8a8a93"
ACCENT = "#d97757"
ACCENT_TEXT = "#140b07"
GREEN = "#4ade80"
RED = "#f87171"
AMBER = "#fbbf24"
FONT_MONO = ("Consolas", 10)
FONT_UI = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")


class RoundedCard(tk.Canvas):
    def __init__(self, parent, bg=CARD, border=BORDER, outer_bg=BG, radius=20, pad=16):
        super().__init__(parent, bg=outer_bg, highlightthickness=0, bd=0)
        self.card_bg = bg
        self.border_color = border
        self.radius = radius
        self.pad = pad
        self.inner = tk.Frame(self, bg=bg)
        self._win_id = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self._shape_id = None
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None):
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        r = min(self.radius, w // 3, h // 3)
        x1, y1, x2, y2 = 1, 1, w - 2, h - 2
        pts = [
            x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2,
            x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r,
            x1, y1 + r, x1, y1 + r, x1, y1,
        ]
        if self._shape_id is not None:
            self.delete(self._shape_id)
        self._shape_id = self.create_polygon(
            pts, smooth=True, splinesteps=24, fill=self.card_bg, outline=self.border_color, width=1
        )
        self.tag_lower(self._shape_id)
        self.coords(self._win_id, self.pad, self.pad)
        self.itemconfigure(self._win_id, width=max(1, w - self.pad * 2), height=max(1, h - self.pad * 2))


def _blend_hex(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = max(0, min(255, int(r + (255 - r) * factor) if factor > 0 else int(r * (1 + factor))))
    g = max(0, min(255, int(g + (255 - g) * factor) if factor > 0 else int(g * (1 + factor))))
    b = max(0, min(255, int(b + (255 - b) * factor) if factor > 0 else int(b * (1 + factor))))
    return f"#{r:02x}{g:02x}{b:02x}"


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg=SURFACE, fg=TEXT, font=("Segoe UI", 9, "bold"), padx=14, pady=6, radius=18):
        try:
            outer_bg = parent.cget("bg")
        except tk.TclError:
            outer_bg = CARD
        f_obj = tkfont.Font(font=font)
        w = f_obj.measure(text) + padx * 2
        h = f_obj.metrics("linespace") + pady * 2
        super().__init__(parent, width=w, height=h, bg=outer_bg, highlightthickness=0, bd=0, cursor="hand2")
        self._cmd = command
        self._bg = bg
        self._hover = _blend_hex(bg, 0.12)
        self._pressed = _blend_hex(bg, -0.10)
        self._border = _blend_hex(bg, 0.18) if bg != ACCENT else ACCENT
        r = min(radius, h // 2, w // 2)
        x1, y1, x2, y2 = 1, 1, w - 2, h - 2
        pts = [
            x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2,
            x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r,
            x1, y1 + r, x1, y1 + r, x1, y1,
        ]
        self._shape = self.create_polygon(pts, smooth=True, splinesteps=28, fill=bg, outline=self._border, width=1)
        self.create_text(w / 2, h / 2, text=text, fill=fg, font=font)
        self.bind("<Enter>", lambda _e: self.itemconfigure(self._shape, fill=self._hover))
        self.bind("<Leave>", lambda _e: self.itemconfigure(self._shape, fill=self._bg))
        self.bind("<ButtonPress-1>", lambda _e: self.itemconfigure(self._shape, fill=self._pressed))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_release(self, event):
        self.itemconfigure(self._shape, fill=self._hover)
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self._cmd:
                self._cmd()


def make_pill(parent, text, command, bg=SURFACE, fg=TEXT, font=("Segoe UI", 9, "bold"), padx=14, pady=6):
    return RoundedButton(parent, text=text, command=command, bg=bg, fg=fg, font=font, padx=padx, pady=pady, radius=999)


class ClusterTerminalApp:
    def __init__(self, data_dir: Path, project_root: Path):
        self.data_dir = Path(data_dir)
        self.project_root = Path(project_root)
        self.root = tk.Tk()
        self.root.title("cluster")
        self.root.geometry("1240x760")
        self.root.minsize(980, 620)
        self.root.configure(bg=BG)

        self.logo_img = None
        self._load_icons()

        self.controller: ClusterController | None = None
        self.workers: list[ClusterWorker] = []
        self.lan_controllers: list[dict] = []
        self.mode = "IDLE"
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.cmd_history: list[str] = []
        self.history_idx = -1
        self._modal_backdrop = None

        saved = load_lang(self.data_dir)
        self.lang = saved or "en"

        self._setup_styles()
        if saved is None:
            self._build_language_screen()
        else:
            self._build_start_screen()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._poll_ui)

    def _assets_dir(self) -> Path:
        for base in (self.project_root, self.data_dir.parent):
            cand = base / "assets"
            if cand.exists():
                return cand
        return self.project_root / "assets"

    def _examples_dir(self) -> Path:
        for base in (self.project_root, self.data_dir.parent):
            cand = base / "examples"
            if cand.exists():
                return cand
        return self.project_root / "examples"

    def _load_icons(self):
        assets = self._assets_dir()
        ico = assets / "logo.ico"
        if ico.exists():
            try:
                self.root.iconbitmap(str(ico))
            except tk.TclError:
                pass
        small = assets / "logo_small.png"
        if small.exists():
            try:
                self.logo_img = tk.PhotoImage(file=str(small))
            except tk.TclError:
                self.logo_img = None

    def t(self, key: str, **kwargs):
        val = TEXTS[self.lang][key]
        return val.format(**kwargs) if kwargs and isinstance(val, str) else val

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor=SURFACE, background=ACCENT, thickness=6, borderwidth=0)
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE, foreground=TEXT, borderwidth=0)

    def _clear_root(self):
        self._close_modal()
        for w in self.root.winfo_children():
            w.destroy()

    def _set_language(self, lang: str):
        self.lang = lang
        save_lang(self.data_dir, lang)
        self._build_start_screen()

    def _render_brand(self, parent, bg=CARD, font=("Consolas", 18, "bold")):
        hdr = tk.Frame(parent, bg=bg)
        if self.logo_img is not None:
            tk.Label(hdr, image=self.logo_img, bg=bg).pack(side="left", padx=(0, 8))
        else:
            tk.Label(hdr, text="●", bg=bg, fg=ACCENT, font=font).pack(side="left", padx=(0, 8))
        tk.Label(hdr, text="cluster", bg=bg, fg=TEXT, font=font).pack(side="left")
        return hdr

    def _build_language_screen(self):
        self._clear_root()
        self.mode = "IDLE"

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(expand=True, fill="both")

        card = RoundedCard(outer, radius=24, pad=32)
        card.place(relx=0.5, rely=0.5, anchor="center", width=440, height=260)
        box = card.inner

        self._render_brand(box).pack(pady=(6, 12))
        tk.Label(box, text=TEXTS["en"]["choose_lang_title"], bg=CARD, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(pady=(4, 2))
        tk.Label(box, text=TEXTS["en"]["choose_lang_sub"], bg=CARD, fg=MUTED, font=FONT_UI).pack(pady=(0, 22))

        btns = tk.Frame(box, bg=CARD)
        btns.pack()
        make_pill(btns, "English", lambda: self._set_language("en"), bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=22, pady=9).pack(side="left", padx=8)
        make_pill(btns, "Українська", lambda: self._set_language("uk"), bg=SURFACE, fg=TEXT, font=FONT_BOLD, padx=22, pady=9).pack(side="left", padx=8)

    def _build_start_screen(self):
        self._clear_root()
        self.mode = "IDLE"

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(expand=True, fill="both", padx=24, pady=18)

        lang_bar = tk.Frame(outer, bg=BG)
        lang_bar.pack(fill="x")
        make_pill(
            lang_bar, "UA", lambda: self._set_language("uk"),
            bg=ACCENT if self.lang == "uk" else SURFACE, fg=ACCENT_TEXT if self.lang == "uk" else MUTED, padx=10, pady=3,
        ).pack(side="right", padx=3)
        make_pill(
            lang_bar, "EN", lambda: self._set_language("en"),
            bg=ACCENT if self.lang == "en" else SURFACE, fg=ACCENT_TEXT if self.lang == "en" else MUTED, padx=10, pady=3,
        ).pack(side="right", padx=3)

        card = RoundedCard(outer, radius=24, pad=28)
        card.place(relx=0.5, rely=0.5, anchor="center", width=680, height=540)
        box = card.inner

        self._render_brand(box).pack(anchor="w")
        tk.Label(box, text=self.t("app_subtitle"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w", pady=(4, 14))

        c_card = RoundedCard(box, bg=SURFACE, border=BORDER, outer_bg=CARD, radius=16, pad=14)
        c_card.configure(height=84)
        c_card.pack(fill="x", pady=5)
        c_frame = c_card.inner
        tk.Label(c_frame, text=self.t("btn_ctrl_title"), bg=SURFACE, fg=TEXT, font=FONT_BOLD).pack(anchor="w")
        row1 = tk.Frame(c_frame, bg=SURFACE)
        row1.pack(fill="x", pady=(6, 0))
        tk.Label(row1, text=self.t("port_lbl"), bg=SURFACE, fg=MUTED, font=FONT_UI).pack(side="left")
        self.port_var = tk.StringVar(value="8000")
        tk.Entry(row1, textvariable=self.port_var, width=8, bg=BG, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", padx=8, ipady=3)
        make_pill(row1, self.t("btn_start_ctrl"), self._start_controller_mode, bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=16, pady=5).pack(side="right")

        w_card = RoundedCard(box, bg=SURFACE, border=BORDER, outer_bg=CARD, radius=16, pad=14)
        w_card.configure(height=214)
        w_card.pack(fill="x", pady=5)
        w_frame = w_card.inner
        tk.Label(w_frame, text=self.t("btn_worker_title"), bg=SURFACE, fg=TEXT, font=FONT_BOLD).pack(anchor="w")

        row_lan = tk.Frame(w_frame, bg=SURFACE)
        row_lan.pack(fill="x", pady=(8, 4))
        tk.Label(row_lan, text=self.t("lan_found"), bg=SURFACE, fg=MUTED, font=FONT_UI, width=10, anchor="w").pack(side="left")
        self.lan_var = tk.StringVar(value=self.t("lan_none"))
        self.lan_combo = ttk.Combobox(row_lan, textvariable=self.lan_var, state="readonly")
        self.lan_combo.pack(side="left", fill="x", expand=True)
        make_pill(row_lan, self.t("btn_scan_lan"), self._scan_lan_async, bg=CARD, fg=TEXT, padx=12, pady=3).pack(side="right", padx=(6, 0))

        row2 = tk.Frame(w_frame, bg=SURFACE)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text=self.t("invite_lbl"), bg=SURFACE, fg=MUTED, font=FONT_UI, width=10, anchor="w").pack(side="left")
        self.invite_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.invite_var, bg=BG, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", fill="x", expand=True, ipady=3)

        row3 = tk.Frame(w_frame, bg=SURFACE)
        row3.pack(fill="x", pady=4)
        tk.Label(row3, text=self.t("pc_name_lbl"), bg=SURFACE, fg=MUTED, font=FONT_UI, width=10, anchor="w").pack(side="left")
        self.worker_name_var = tk.StringVar(value=socket.gethostname())
        tk.Entry(row3, textvariable=self.worker_name_var, width=22, bg=BG, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", ipady=3)

        perms = tk.Frame(w_frame, bg=SURFACE)
        perms.pack(fill="x", pady=(6, 0))
        self.allow_exec_var = tk.BooleanVar(value=True)
        self.allow_host_var = tk.BooleanVar(value=True)
        tk.Checkbutton(perms, text=self.t("chk_exec"), variable=self.allow_exec_var, bg=SURFACE, fg=TEXT, selectcolor=BG, activebackground=SURFACE).pack(side="left", padx=(0, 8))
        tk.Checkbutton(perms, text=self.t("chk_host"), variable=self.allow_host_var, bg=SURFACE, fg=TEXT, selectcolor=BG, activebackground=SURFACE).pack(side="left")
        make_pill(perms, self.t("btn_connect"), self._start_worker_mode, bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=14, pady=4).pack(side="right")
        make_pill(perms, self.t("btn_req_access"), self._request_access_async, bg=CARD, fg=TEXT, padx=12, pady=4).pack(side="right", padx=(0, 6))

        d_card = RoundedCard(box, bg=SURFACE, border=BORDER, outer_bg=CARD, radius=16, pad=14)
        d_card.configure(height=60)
        d_card.pack(fill="x", pady=5)
        d_frame = d_card.inner
        tk.Label(d_frame, text=self.t("btn_demo_title"), bg=SURFACE, fg=TEXT, font=FONT_BOLD).pack(side="left")
        make_pill(d_frame, self.t("btn_demo"), self._start_demo_mode, bg=CARD, fg=ACCENT, font=FONT_BOLD, padx=14, pady=4).pack(side="right")

        self.start_err_lbl = tk.Label(box, text="", bg=CARD, fg=RED, font=FONT_UI)
        self.start_err_lbl.pack(anchor="w", pady=(8, 0))

        self._scan_lan_async()

    def _build_workspace(self, badge_text: str):
        self._clear_root()

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=16, pady=16)
        main.grid_columnconfigure(0, weight=7, minsize=560)
        main.grid_columnconfigure(1, weight=3, minsize=310)
        main.grid_rowconfigure(0, weight=1)

        term_card = RoundedCard(main, radius=22, pad=18)
        term_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left = term_card.inner
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        hdr = tk.Frame(left, bg=CARD)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._render_brand(hdr, font=("Consolas", 12, "bold")).pack(side="left")
        tk.Label(hdr, text=badge_text, bg=SURFACE, fg=MUTED, font=("Consolas", 9), padx=8, pady=2).pack(side="left", padx=10)
        make_pill(hdr, self.t("btn_menu"), self._reset_to_menu, bg=SURFACE, fg=MUTED, padx=10, pady=3).pack(side="right")
        if self.mode in ("CONTROLLER", "DEMO"):
            make_pill(hdr, self.t("btn_invite"), lambda: self._run_cmd("/invite"), bg=SURFACE, fg=ACCENT, padx=10, pady=3).pack(side="right", padx=(0, 6))

        self.term_text = tk.Text(
            left, bg=CARD, fg=TEXT, font=FONT_MONO, relief="flat", bd=0,
            wrap="word", state="disabled", padx=4, pady=4, insertbackground=ACCENT,
        )
        self.term_text.grid(row=1, column=0, sticky="nsew")
        self.term_text.tag_config("info", foreground=TEXT)
        self.term_text.tag_config("success", foreground=GREEN)
        self.term_text.tag_config("warning", foreground=AMBER)
        self.term_text.tag_config("error", foreground=RED)
        self.term_text.tag_config("cmd", foreground=ACCENT, font=("Consolas", 10, "bold"))
        self.term_text.tag_config("muted", foreground=MUTED)

        if self.mode in ("CONTROLLER", "DEMO"):
            chips = tk.Frame(left, bg=CARD)
            chips.grid(row=2, column=0, sticky="ew", pady=(8, 6))
            for label, fn in (
                (f"/run  {self.t('chip_run')}", self._ui_run_task),
                (f"/host  {self.t('chip_host')}", self._ui_host_service),
                (f"/batch  {self.t('chip_batch')}", self._ui_analyze_folder),
                (f"/report  {self.t('chip_report')}", lambda: self._run_cmd("/report")),
                (f"/demo  {self.t('chip_demo')}", self._ui_examples_modal),
            ):
                make_pill(chips, label, fn, bg=SURFACE, fg=MUTED, font=("Consolas", 9), padx=10, pady=3).pack(side="left", padx=(0, 6))

        prompt_wrap = RoundedCard(left, bg=SURFACE, border=BORDER, outer_bg=CARD, radius=14, pad=10)
        prompt_wrap.grid(row=3, column=0, sticky="ew")
        prompt_wrap.configure(height=44)
        p_inner = prompt_wrap.inner
        tk.Label(p_inner, text=">", bg=SURFACE, fg=ACCENT, font=("Consolas", 11, "bold")).pack(side="left", padx=(2, 8))
        self.cmd_entry = tk.Entry(
            p_inner, bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
            font=FONT_MONO, relief="flat", bd=0,
        )
        self.cmd_entry.pack(side="left", fill="both", expand=True)
        self.cmd_entry.bind("<Return>", self._on_enter_cmd)
        self.cmd_entry.bind("<Up>", self._on_hist_up)
        self.cmd_entry.bind("<Down>", self._on_hist_down)
        self.cmd_entry.focus_set()

        right_col = tk.Frame(main, bg=BG)
        right_col.grid(row=0, column=1, sticky="nsew")
        right_col.grid_rowconfigure(0, weight=4)
        right_col.grid_rowconfigure(1, weight=6)
        right_col.grid_columnconfigure(0, weight=1)

        stats_card = RoundedCard(right_col, radius=22, pad=16)
        stats_card.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        s_box = stats_card.inner

        tk.Label(s_box, text=self.t("sec_stats"), bg=CARD, fg=TEXT, font=("Consolas", 11, "bold")).pack(anchor="w", pady=(0, 8))
        self.sys_cpu_lbl = tk.Label(s_box, text="CPU  --%", bg=CARD, fg=MUTED, font=FONT_MONO)
        self.sys_cpu_lbl.pack(anchor="w")
        self.sys_cpu_bar = ttk.Progressbar(s_box, maximum=100)
        self.sys_cpu_bar.pack(fill="x", pady=(3, 8))

        self.sys_ram_lbl = tk.Label(s_box, text="RAM  --", bg=CARD, fg=MUTED, font=FONT_MONO)
        self.sys_ram_lbl.pack(anchor="w")
        self.sys_ram_bar = ttk.Progressbar(s_box, maximum=100)
        self.sys_ram_bar.pack(fill="x", pady=(3, 8))

        self.sys_disk_lbl = tk.Label(s_box, text="Disk --", bg=CARD, fg=MUTED, font=FONT_MONO)
        self.sys_disk_lbl.pack(anchor="w")
        self.sys_bat_lbl = tk.Label(s_box, text="Power --", bg=CARD, fg=MUTED, font=FONT_MONO)
        self.sys_bat_lbl.pack(anchor="w", pady=(2, 8))
        self.summary_lbl = tk.Label(s_box, text="", bg=CARD, fg=TEXT, font=("Consolas", 9))
        self.summary_lbl.pack(anchor="w")

        nodes_card = RoundedCard(right_col, radius=22, pad=16)
        nodes_card.grid(row=1, column=0, sticky="nsew")
        n_box = nodes_card.inner
        n_box.grid_rowconfigure(2, weight=1)
        n_box.grid_columnconfigure(0, weight=1)

        tk.Label(n_box, text=self.t("sec_nodes"), bg=CARD, fg=TEXT, font=("Consolas", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.reqs_box = tk.Frame(n_box, bg=CARD)
        self.reqs_box.grid(row=1, column=0, sticky="ew")

        self.nodes_canvas = tk.Canvas(n_box, bg=CARD, highlightthickness=0)
        self.nodes_canvas.grid(row=2, column=0, sticky="nsew")
        self.nodes_inner = tk.Frame(self.nodes_canvas, bg=CARD)
        self._nodes_win = self.nodes_canvas.create_window((0, 0), window=self.nodes_inner, anchor="nw")
        self.nodes_inner.bind("<Configure>", lambda e: self.nodes_canvas.configure(scrollregion=self.nodes_canvas.bbox("all")))
        self.nodes_canvas.bind("<Configure>", lambda e: self.nodes_canvas.itemconfigure(self._nodes_win, width=e.width))

        self.jobs_box = tk.Frame(n_box, bg=CARD)
        self.jobs_box.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _open_modal(self, title: str, width: int, height: int, builder):
        self._close_modal()
        backdrop = tk.Frame(self.root, bg="#08080a")
        backdrop.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._modal_backdrop = backdrop

        card = RoundedCard(backdrop, bg=CARD, border=BORDER, outer_bg="#08080a", radius=22, pad=22)
        card.place(relx=0.5, rely=0.5, anchor="center", width=width, height=height)
        box = card.inner

        hdr = tk.Frame(box, bg=CARD)
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(hdr, text=title, bg=CARD, fg=TEXT, font=("Consolas", 12, "bold")).pack(side="left")
        make_pill(hdr, "✕", self._close_modal, bg=SURFACE, fg=MUTED, padx=9, pady=2).pack(side="right")

        builder(box)

    def _close_modal(self):
        if self._modal_backdrop is not None and self._modal_backdrop.winfo_exists():
            self._modal_backdrop.destroy()
        self._modal_backdrop = None

    def _ui_run_task(self):
        def build(box):
            path_var = tk.StringVar()
            cmd_var = tk.StringVar()

            tk.Label(box, text=self.t("dlg_path"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            p_row = tk.Frame(box, bg=CARD)
            p_row.pack(fill="x", pady=(4, 12))
            tk.Entry(p_row, textvariable=path_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))

            def browse_file():
                p = filedialog.askopenfilename(title=self.t("dlg_run_file"))
                if p:
                    path_var.set(p)

            def browse_dir():
                p = filedialog.askdirectory(title=self.t("dlg_run_folder"))
                if p:
                    path_var.set(p)

            make_pill(p_row, self.t("dlg_single_file"), browse_file, bg=SURFACE, fg=TEXT, padx=10, pady=4).pack(side="left", padx=(0, 4))
            make_pill(p_row, self.t("dlg_folder"), browse_dir, bg=SURFACE, fg=TEXT, padx=10, pady=4).pack(side="left")

            tk.Label(box, text=self.t("dlg_cmd"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            tk.Entry(box, textvariable=cmd_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(fill="x", pady=(4, 16), ipady=5)

            foot = tk.Frame(box, bg=CARD)
            foot.pack(fill="x", side="bottom")

            def submit():
                target = path_var.get().strip()
                cmd = cmd_var.get().strip()
                if not target and not cmd:
                    return
                self._close_modal()
                if target and cmd:
                    self._run_cmd(f"/run {target} :: {cmd}")
                elif target:
                    self._run_cmd(f"/run {target}")
                else:
                    self._run_cmd(f"/run {cmd}")

            make_pill(foot, self.t("dlg_run_btn"), submit, bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=18, pady=6).pack(side="right")
            make_pill(foot, self.t("dlg_cancel"), self._close_modal, bg=SURFACE, fg=MUTED, padx=14, pady=6).pack(side="right", padx=(0, 8))

        self._open_modal(self.t("dlg_run_title"), 560, 250, build)

    def _ui_host_service(self):
        ex = self._examples_dir()

        def build(box):
            port_var = tk.StringVar(value="8080")
            dir_var = tk.StringVar(value=str(ex / "site_demo") if (ex / "site_demo").exists() else "")
            cmd_var = tk.StringVar()

            tk.Label(box, text=self.t("dlg_host_port"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            tk.Entry(box, textvariable=port_var, width=12, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(anchor="w", pady=(4, 10), ipady=4)

            tk.Label(box, text=self.t("dlg_host_folder"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            d_row = tk.Frame(box, bg=CARD)
            d_row.pack(fill="x", pady=(4, 10))
            tk.Entry(d_row, textvariable=dir_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
            make_pill(
                d_row, self.t("dlg_browse"),
                lambda: dir_var.set(filedialog.askdirectory(title=self.t("dlg_host_folder"), initialdir=str(ex)) or dir_var.get()),
                bg=SURFACE, fg=TEXT, padx=10, pady=4,
            ).pack(side="left", padx=(0, 4))
            make_pill(
                d_row, self.t("dlg_use_demo_site"),
                lambda: dir_var.set(str(ex / "site_demo")),
                bg=SURFACE, fg=ACCENT, padx=10, pady=4,
            ).pack(side="left")

            tk.Label(box, text=self.t("dlg_host_cmd"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            tk.Entry(box, textvariable=cmd_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(fill="x", pady=(4, 16), ipady=5)

            foot = tk.Frame(box, bg=CARD)
            foot.pack(fill="x", side="bottom")

            def submit():
                try:
                    port = int(port_var.get().strip() or "8080")
                except ValueError:
                    return
                folder = dir_var.get().strip()
                cmd = cmd_var.get().strip()
                self._close_modal()
                try:
                    job = self.controller.create_job("host", 1, 0.2, 0.1, command=cmd, target_path=folder or None, port=port)
                    self._append_terminal(self.t("msg_host_started", jid=job.job_id, url=job.url) if job else self.t("msg_host_no_node"), "success" if job else "error")
                except Exception as err:
                    self._append_terminal(str(err), "error")

            make_pill(foot, self.t("dlg_host_btn"), submit, bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=18, pady=6).pack(side="right")
            make_pill(foot, self.t("dlg_cancel"), self._close_modal, bg=SURFACE, fg=MUTED, padx=14, pady=6).pack(side="right", padx=(0, 8))

        self._open_modal(self.t("dlg_host_title"), 540, 320, build)

    def _ui_analyze_folder(self):
        ex = self._examples_dir()

        def build(box):
            dir_var = tk.StringVar(value=str(ex / "parallel_texts") if (ex / "parallel_texts").exists() else "")

            tk.Label(box, text=self.t("dlg_path"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w")
            d_row = tk.Frame(box, bg=CARD)
            d_row.pack(fill="x", pady=(4, 12))
            tk.Entry(d_row, textvariable=dir_var, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, relief="flat", font=FONT_MONO).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
            make_pill(
                d_row, self.t("dlg_browse"),
                lambda: dir_var.set(filedialog.askdirectory(title=self.t("dlg_run_folder"), initialdir=str(ex)) or dir_var.get()),
                bg=SURFACE, fg=TEXT, padx=10, pady=4,
            ).pack(side="left", padx=(0, 4))
            make_pill(
                d_row, self.t("dlg_use_sample_dir"),
                lambda: dir_var.set(str(ex / "parallel_texts")),
                bg=SURFACE, fg=ACCENT, padx=10, pady=4,
            ).pack(side="left")

            foot = tk.Frame(box, bg=CARD)
            foot.pack(fill="x", side="bottom")
            make_pill(
                foot, self.t("dlg_batch_btn"),
                lambda: (self._close_modal(), self._run_cmd(f"/batch {dir_var.get().strip()}")),
                bg=ACCENT, fg=ACCENT_TEXT, font=FONT_BOLD, padx=18, pady=6,
            ).pack(side="right")
            make_pill(
                foot, self.t("dlg_sample_24"),
                lambda: (self._close_modal(), self._run_cmd("/sample")),
                bg=SURFACE, fg=TEXT, padx=14, pady=6,
            ).pack(side="right", padx=(0, 8))
            make_pill(foot, self.t("dlg_cancel"), self._close_modal, bg=SURFACE, fg=MUTED, padx=14, pady=6).pack(side="right", padx=(0, 8))

        self._open_modal(self.t("dlg_batch_title"), 540, 200, build)

    def _ui_examples_modal(self):
        ex = self._examples_dir()

        def build(box):
            tk.Label(box, text=self.t("dlg_examples_sub"), bg=CARD, fg=MUTED, font=FONT_UI).pack(anchor="w", pady=(0, 10))
            items = [
                (self.t("dlg_ex_1_title"), self.t("dlg_ex_1_desc"), lambda: self._run_cmd(f"/run {ex / 'ai_train_demo.py'}")),
                (self.t("dlg_ex_2_title"), self.t("dlg_ex_2_desc"), lambda: self._run_cmd(f"/run {ex / 'monte_carlo_pi.py'}")),
                (self.t("dlg_ex_3_title"), self.t("dlg_ex_3_desc"), lambda: self._run_cmd(f"/map {ex / 'parallel_texts'} :: {ex / 'text_stats_map.py'}")),
                (self.t("dlg_ex_4_title"), self.t("dlg_ex_4_desc"), lambda: self._run_cmd(f"/host 8080 {ex / 'site_demo'}")),
                (self.t("dlg_ex_5_title"), self.t("dlg_ex_5_desc"), lambda: self._run_cmd("/sample")),
            ]
            for title, desc, fn in items:
                row = tk.Frame(box, bg=SURFACE, padx=12, pady=8)
                row.pack(fill="x", pady=4)
                txt = tk.Frame(row, bg=SURFACE)
                txt.pack(side="left", fill="x", expand=True)
                tk.Label(txt, text=title, bg=SURFACE, fg=TEXT, font=FONT_BOLD).pack(anchor="w")
                tk.Label(txt, text=desc, bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
                make_pill(row, self.t("dlg_run_btn"), lambda f=fn: (self._close_modal(), f()), bg=ACCENT, fg=ACCENT_TEXT, font=("Segoe UI", 9, "bold"), padx=12, pady=4).pack(side="right", padx=(8, 0))

        self._open_modal(self.t("dlg_examples_title"), 620, 410, build)

    def _scan_lan_async(self):
        def work():
            found = scan_lan_controllers(timeout=1.4)

            def apply():
                if not hasattr(self, "lan_combo") or not self.lan_combo.winfo_exists():
                    return
                self.lan_controllers = found
                if found:
                    labels = [f"{c['name']} ({c['ip']}:{c['port']})" for c in found]
                    self.lan_combo["values"] = labels
                    self.lan_var.set(labels[0])
                else:
                    self.lan_combo["values"] = []
                    self.lan_var.set(self.t("lan_none"))

            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _selected_lan_controller(self) -> dict | None:
        if not self.lan_controllers or not hasattr(self, "lan_combo"):
            return None
        idx = self.lan_combo.current()
        if 0 <= idx < len(self.lan_controllers):
            return self.lan_controllers[idx]
        return self.lan_controllers[0]

    def _worker_allowed_jobs(self) -> list[str]:
        allowed = ["corpus", "demo"]
        if self.allow_exec_var.get():
            allowed.append("exec")
        if self.allow_host_var.get():
            allowed.append("host")
        return allowed

    def _launch_worker_ui(self, session: WorkerSession, allowed: list[str]):
        self.mode = "WORKER"
        worker_dir = self.data_dir / "worker"
        w = ClusterWorker(session, worker_dir, poll_seconds=1.0, on_log=self._enqueue_log)
        w.start()
        self.workers.append(w)
        self._build_workspace(f"worker · {session.name}")
        self._append_terminal(self.t("term_worker", server=session.server, name=session.name), "success")
        self._append_terminal(self.t("msg_allowed", allowed=", ".join(allowed)), "muted")

    def _start_controller_mode(self):
        try:
            port = int(self.port_var.get().strip() or "8000")
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.start_err_lbl.config(text=self.t("err_port"))
            return

        self.mode = "CONTROLLER"
        self.controller = ClusterController(port, self.data_dir, on_log=self._enqueue_log)
        try:
            self.controller.start()
        except OSError as err:
            self.start_err_lbl.config(text=self.t("err_ctrl_start", err=err))
            self.controller = None
            return

        self._build_workspace(f"controller :{self.controller.port}")
        self._append_terminal(self.t("term_welcome", ip=local_ip(), port=self.controller.port), "muted")

    def _start_worker_mode(self):
        invite = self.invite_var.get().strip()
        name = self.worker_name_var.get().strip() or socket.gethostname()
        allowed = self._worker_allowed_jobs()
        worker_dir = self.data_dir / "worker"
        save_policy(worker_dir, {**DEFAULT_POLICY, "allowed_jobs": allowed})

        try:
            if invite.startswith("cluster://"):
                session = WorkerSession.pair(invite, name, worker_dir)
            elif invite:
                ctrl = self._selected_lan_controller()
                if not ctrl:
                    self.start_err_lbl.config(text=self.t("lan_none"))
                    return
                session = WorkerSession.pair_with_code(ctrl["ip"], ctrl["port"], ctrl["pin"], invite, name, worker_dir)
            elif (worker_dir / "connection.json").exists():
                session = WorkerSession.load(worker_dir)
            else:
                self.start_err_lbl.config(text=self.t("err_paste_invite"))
                return
        except Exception as err:
            self.start_err_lbl.config(text=self.t("err_connect", err=err))
            return

        self._launch_worker_ui(session, allowed)

    def _request_access_async(self):
        ctrl = self._selected_lan_controller()
        if not ctrl:
            self.start_err_lbl.config(text=self.t("lan_none"))
            return
        name = self.worker_name_var.get().strip() or socket.gethostname()
        allowed = self._worker_allowed_jobs()
        worker_dir = self.data_dir / "worker"
        save_policy(worker_dir, {**DEFAULT_POLICY, "allowed_jobs": allowed})
        self.start_err_lbl.config(fg=AMBER, text=self.t("waiting_approve"))

        def work():
            try:
                session = WorkerSession.request_access_and_wait(
                    ctrl["ip"], ctrl["port"], ctrl["pin"], name, worker_dir, timeout=60.0
                )
                self.root.after(0, lambda: self._launch_worker_ui(session, allowed))
            except Exception as err:
                self.root.after(0, lambda e=err: self.start_err_lbl.config(fg=RED, text=self.t("err_connect", err=e)))

        threading.Thread(target=work, daemon=True).start()

    def _start_demo_mode(self):
        self.mode = "DEMO"
        demo_dir = Path(tempfile.mkdtemp(prefix="cluster-demo-"))
        self.controller = ClusterController(0, demo_dir / "controller", on_log=self._enqueue_log)
        self.controller.start()

        for i in range(2):
            w_dir = demo_dir / f"worker-{i+1}"
            save_policy(w_dir, {**DEFAULT_POLICY, "reserve_ram_gb": 0, "max_host_cpu": 99, "minimum_battery": 0})
            invite = self.controller.issue_invitation("127.0.0.1")
            session = WorkerSession.pair(invite, f"Node-{i+1}", w_dir)
            w = ClusterWorker(session, w_dir, poll_seconds=0.5, on_log=self._enqueue_log)
            w.start()
            self.workers.append(w)

        self._build_workspace(f"demo :{self.controller.port}")
        self._append_terminal(self.t("term_demo"), "muted")

    def _reset_to_menu(self):
        self._stop_all()
        self._build_start_screen()

    def _enqueue_log(self, msg: str, level: str = "info"):
        self.log_queue.put((msg, level))

    def _append_terminal(self, text: str, tag: str = "info"):
        if not hasattr(self, "term_text") or not self.term_text.winfo_exists():
            return
        stamp = time.strftime("%H:%M:%S")
        self.term_text.configure(state="normal")
        if tag == "cmd":
            self.term_text.insert("end", f"> {text}\n", "cmd")
        else:
            self.term_text.insert("end", f"[{stamp}] {text}\n", tag)
        self.term_text.see("end")
        self.term_text.configure(state="disabled")

    def _on_enter_cmd(self, _event):
        raw = self.cmd_entry.get().strip()
        if not raw:
            return
        self.cmd_entry.delete(0, "end")
        self.cmd_history.append(raw)
        self.history_idx = len(self.cmd_history)
        self._run_cmd(raw)

    def _on_hist_up(self, _event):
        if self.cmd_history and self.history_idx > 0:
            self.history_idx -= 1
            self.cmd_entry.delete(0, "end")
            self.cmd_entry.insert(0, self.cmd_history[self.history_idx])

    def _on_hist_down(self, _event):
        if self.cmd_history and self.history_idx < len(self.cmd_history) - 1:
            self.history_idx += 1
            self.cmd_entry.delete(0, "end")
            self.cmd_entry.insert(0, self.cmd_history[self.history_idx])

    def _run_cmd(self, raw: str):
        self._append_terminal(raw, "cmd")
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "clear":
            self.term_text.configure(state="normal")
            self.term_text.delete("1.0", "end")
            self.term_text.configure(state="disabled")
            return

        if cmd == "help":
            for line in self.t("help_lines"):
                self._append_terminal(line, "muted")
            return

        if not self.controller:
            self._append_terminal(self.t("msg_ctrl_only"), "warning")
            return

        if cmd == "invite":
            code, link = self.controller.issue_invite_pair()
            self.root.clipboard_clear()
            self.root.clipboard_append(link)
            self._append_terminal(self.t("msg_invite_copied", code=code, link=link), "success")
        elif cmd == "nodes":
            if not self.controller.nodes:
                self._append_terminal(self.t("msg_no_workers"), "warning")
            for n in self.controller.nodes.values():
                st = "online" if is_online(n) else "offline"
                rts = ", ".join(n.runtimes[:6]) or "-"
                self._append_terminal(f"  {n.node_id}  {n.name} ({n.address})  {st}  cpu {n.cpu_usage}%  ram {n.ram_free}GB  [{rts}]")
        elif cmd == "sample":
            bid, cnt = self.controller.submit_sample()
            self._append_terminal(self.t("msg_sample_queued", bid=bid, cnt=cnt), "success")
        elif cmd == "batch":
            if not arg:
                self._ui_analyze_folder()
                return
            try:
                bid, cnt, skipped = self.controller.swarm.submit_folder(arg)
                self._append_terminal(self.t("msg_batch_queued", bid=bid, cnt=cnt, skipped=len(skipped)), "success")
            except Exception as err:
                self._append_terminal(str(err), "error")
        elif cmd == "map":
            if "::" not in arg:
                self._append_terminal("Usage: /map <folder> :: <script_or_command>", "warning")
                return
            folder, command = [p.strip() for p in arg.split("::", 1)]
            try:
                bid, cnt = self.controller.swarm.submit_map(folder, command)
                self._append_terminal(self.t("msg_map_queued", bid=bid, cnt=cnt, cmd=command), "success")
            except Exception as err:
                self._append_terminal(str(err), "error")
        elif cmd == "batches":
            for b in self.controller.swarm.batches():
                c = b["counts"]
                self._append_terminal(f"  {b['id']} ({b['name']}): {c['done']}/{b['total']} done, running={c['running']}, retries={b['retries']}")
        elif cmd == "report":
            bid = arg
            if not bid:
                batches = self.controller.swarm.batches()
                if not batches:
                    self._append_terminal(self.t("msg_no_batches"), "warning")
                    return
                bid = batches[0]["id"]
            try:
                path, rep = self.controller.swarm.export(bid)
                html_path = path.with_suffix(".html")
                self._append_terminal(self.t("msg_report_saved", path=html_path, done=rep["files"], total=rep["total_files"]), "success")
                webbrowser.open(str(html_path.resolve()))
            except Exception as err:
                self._append_terminal(str(err), "error")
        elif cmd == "demo":
            if arg == "math":
                job = self.controller.create_job("demo", 1, 0.1, 0.1)
                self._append_terminal(f"demo #{job.job_id} -> {job.node_name}" if job else self.t("msg_no_nodes"), "success" if job else "error")
            else:
                self._ui_examples_modal()
        elif cmd == "run":
            if not arg:
                self._ui_run_task()
                return
            if "::" in arg:
                target, command = [p.strip().strip('"') for p in arg.split("::", 1)]
            else:
                target, command = arg.strip('"'), ""
            try:
                p = Path(target)
                if p.exists():
                    job = self.controller.create_job("exec", 1, 0.2, 0.1, command=command, target_path=p)
                else:
                    job = self.controller.create_job("exec", 1, 0.2, 0.1, command=arg)
                self._append_terminal(f"task #{job.job_id} -> {job.node_name}" if job else self.t("msg_no_nodes"), "success" if job else "error")
            except Exception as err:
                self._append_terminal(str(err), "error")
        elif cmd == "host":
            tokens = arg.split(maxsplit=1)
            port = int(tokens[0]) if tokens and tokens[0].isdigit() else 8080
            rest = tokens[1].strip().strip('"') if len(tokens) > 1 else ""
            target = Path(rest) if rest and Path(rest).exists() else None
            command = "" if target else rest
            try:
                job = self.controller.create_job("host", 1, 0.2, 0.1, command=command, target_path=target, port=port)
                self._append_terminal(self.t("msg_host_started", jid=job.job_id, url=job.url) if job else self.t("msg_host_no_node"), "success" if job else "error")
            except Exception as err:
                self._append_terminal(str(err), "error")
        elif cmd == "stop":
            ok = self.controller.stop_service(arg)
            self._append_terminal(self.t("msg_stop_sent") if ok else self.t("msg_not_found"), "warning" if ok else "error")
        elif cmd == "revoke":
            ok = self.controller.revoke_device(arg)
            self._append_terminal(self.t("msg_revoked", nid=arg) if ok else self.t("msg_not_found"), "warning" if ok else "error")
        else:
            self._append_terminal(self.t("msg_unknown_cmd", cmd=cmd), "error")

    def _poll_ui(self):
        while not self.log_queue.empty():
            msg, lvl = self.log_queue.get_nowait()
            self._append_terminal(msg, lvl)

        if self.mode != "IDLE" and hasattr(self, "sys_cpu_lbl") and self.sys_cpu_lbl.winfo_exists():
            stats = get_system_stats()
            self.sys_cpu_lbl.config(text=self.t("cpu_lbl", pct=stats["cpu_usage"], cores=stats["cpu_total"]))
            self.sys_cpu_bar["value"] = min(100, stats["cpu_usage"])

            used_ram = max(0.0, stats["ram_total"] - stats["ram_free"])
            ram_pct = (used_ram / stats["ram_total"] * 100) if stats["ram_total"] else 0
            self.sys_ram_lbl.config(text=self.t("ram_lbl", free=stats["ram_free"], total=stats["ram_total"]))
            self.sys_ram_bar["value"] = min(100, ram_pct)
            self.sys_disk_lbl.config(text=self.t("disk_lbl", free=stats["disk_free"]))
            bat = f"{stats['battery_percent']}%" if stats["battery_percent"] is not None else self.t("power_ac")
            self.sys_bat_lbl.config(text=self.t("power_lbl", bat=bat))

            if self.controller:
                nodes = list(self.controller.nodes.values())
                online_cnt = sum(1 for n in nodes if is_online(n))
                jobs = list(self.controller.jobs.values())
                hosts = [j for j in jobs if j.kind == "host" and j.state == "running"]
                self.summary_lbl.config(text=self.t("summary_txt", on=online_cnt, total=len(nodes), jobs=len(jobs), hosts=len(hosts)))
                self._refresh_requests(self.controller.trust.list_pending_requests())
                self._refresh_nodes(nodes)
                self._refresh_jobs(hosts)

        self.root.after(1000, self._poll_ui)

    def _refresh_requests(self, reqs: list[dict]):
        if not hasattr(self, "reqs_box") or not self.reqs_box.winfo_exists():
            return
        for w in self.reqs_box.winfo_children():
            w.destroy()
        if not reqs:
            return
        tk.Label(self.reqs_box, text=self.t("pending_reqs"), bg=CARD, fg=AMBER, font=("Consolas", 9, "bold")).pack(anchor="w", pady=(0, 4))
        for r in reqs:
            row = tk.Frame(self.reqs_box, bg=SURFACE, padx=10, pady=6)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{r['name']} ({r['ip']})", bg=SURFACE, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left")
            make_pill(row, self.t("btn_decline"), lambda rid=r["id"]: self.controller.decline_request(rid), bg=CARD, fg=RED, font=("Segoe UI", 8, "bold"), padx=8, pady=2).pack(side="right", padx=(4, 0))
            make_pill(row, self.t("btn_approve"), lambda rid=r["id"]: self.controller.approve_request(rid), bg=CARD, fg=GREEN, font=("Segoe UI", 8, "bold"), padx=10, pady=2).pack(side="right")

    def _refresh_nodes(self, nodes):
        for w in self.nodes_inner.winfo_children():
            w.destroy()
        if not nodes:
            tk.Label(self.nodes_inner, text=self.t("no_nodes"), bg=CARD, fg=MUTED, font=FONT_UI, justify="left").pack(anchor="w")
            return
        for n in nodes:
            online = is_online(n)
            row_card = RoundedCard(self.nodes_inner, bg=SURFACE, border=BORDER, outer_bg=CARD, radius=14, pad=10)
            row_card.configure(height=68)
            row_card.pack(fill="x", pady=3)
            row = row_card.inner

            hdr = tk.Frame(row, bg=SURFACE)
            hdr.pack(fill="x")
            tk.Label(hdr, text="●", bg=SURFACE, fg=GREEN if online else RED, font=FONT_BOLD).pack(side="left")
            tk.Label(hdr, text=f" {n.name} · {n.state}", bg=SURFACE, fg=TEXT, font=FONT_BOLD).pack(side="left")
            tk.Button(
                hdr, text="✕", bg=SURFACE, fg=MUTED, relief="flat", cursor="hand2",
                command=lambda nid=n.node_id: self._run_cmd(f"/revoke {nid}"),
            ).pack(side="right")
            rts = ", ".join(n.runtimes[:5])
            tk.Label(
                row,
                text=f"{n.address} · CPU {n.cpu_usage}% · RAM {n.ram_free}GB\n{rts}",
                bg=SURFACE, fg=MUTED, font=("Consolas", 8), justify="left",
            ).pack(anchor="w", pady=(2, 0))

    def _refresh_jobs(self, hosts):
        for w in self.jobs_box.winfo_children():
            w.destroy()
        for j in hosts[-3:]:
            row = tk.Frame(self.jobs_box, bg=SURFACE, padx=10, pady=6)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"#{j.job_id} :{j.port}", bg=SURFACE, fg=GREEN, font=("Consolas", 9, "bold")).pack(side="left")
            make_pill(row, self.t("btn_stop"), lambda jid=j.job_id: self._run_cmd(f"/stop {jid}"), bg=CARD, fg=RED, font=("Segoe UI", 8), padx=8, pady=2).pack(side="right")
            if j.url:
                make_pill(row, self.t("btn_open"), lambda u=j.url: webbrowser.open(u), bg=CARD, fg=ACCENT, font=("Segoe UI", 8), padx=8, pady=2).pack(side="right", padx=4)

    def _stop_all(self):
        for w in self.workers:
            w.stop()
        self.workers.clear()
        if self.controller:
            self.controller.stop()
            self.controller = None

    def _on_close(self):
        self._stop_all()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
