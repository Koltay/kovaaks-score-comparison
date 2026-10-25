from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import queue
import re
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlparse
import requests

# KovaaK's & Steam Endpoints
KOVAAK_LOGIN_URL = "https://kovaaks.com/auth/webapp/login"
BENCHMARKS_LIST_URL = "https://kovaaks.com/webapp-backend/benchmarks/player-progress-rank"
BENCHMARK_PAGE_SIZE = 300
BENCHMARK_SCORE_URL = "https://kovaaks.com/webapp-backend/benchmarks/player-progress-rank-benchmark"
STEAM_PROFILE_URL = "https://steamcommunity.com/profiles/{}/?xml=1"
STEAM_ID_URL = "https://steamcommunity.com/id/{}/?xml=1"

CONFIG_FILE = Path(__file__).resolve().with_name("config.json")
MAX_WORKERS = 3
MIN_REQUEST_INTERVAL_SECONDS = 0.75
MAX_RATE_LIMIT_RETRIES = 10
RATE_LIMIT_BACKOFF_SECONDS = 2
DEFAULT_CONFIG = {"player_a": "", "player_b": ""}


class _ComparisonStopped(Exception):
    pass


class KovaaKsAuthenticationError(Exception):
    pass


# Theme Colors (Modern Dark Aesthetic)
THEME = {
    "bg_dark": "#121212",
    "bg_card": "#1e1e1e",
    "bg_input": "#252526",
    "border": "#333333",
    "accent": "#007acc",
    "accent_hover": "#0098ff",
    "green": "#10b981",
    "red": "#ef4444",
    "fg": "#e1e1e1",
    "fg_sub": "#8e8e93",
    "table_bg": "#18181b",
    "table_alt": "#202023",
    "tree_select": "#264f78",
    "toast_bg": "#2d2d30",
    "toast_fg": "#ffffff",
    "toast_border": "#007acc"
}


def clean_header_value(text: str) -> str:
    if not text:
        return ""
    text = text.replace("Bearer ", "").replace("bearer ", "").strip()
    text = text.encode("latin-1", errors="ignore").decode("latin-1")
    return text.strip()


def create_browser_session(auth_token: str = "") -> requests.Session:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://kovaaks.com/kovaaks/benchmarks",
        "Origin": "https://kovaaks.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    }
    clean_token = clean_header_value(auth_token)
    if clean_token:
        headers["Authorization"] = "Bearer " + clean_token

    session.headers.update(headers)
    return session


def retrieve_kovaaks_auth_token(username: str, password: str) -> str:
    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    response = requests.post(
        KOVAAK_LOGIN_URL,
        headers={"Authorization": f"Basic {credentials}"},
        timeout=10,
    )
    if response.status_code in (401, 403):
        raise KovaaKsAuthenticationError(
            "KovaaK's login was rejected. Check your username/email and password."
        )
    response.raise_for_status()

    try:
        token = response.json()["auth"]["jwt"]
    except (KeyError, TypeError, ValueError) as error:
        raise KovaaKsAuthenticationError(
            "KovaaK's login response did not include an auth token."
        ) from error
    if not isinstance(token, str) or not token.strip():
        raise KovaaKsAuthenticationError(
            "KovaaK's login response did not include a valid auth token."
        )
    return token.strip()


def resolve_to_steam_64(user_input: str) -> tuple[str | None, str | None]:
    """
    Takes a Steam64 ID, Steam profile URL, or EVXL profile URL
    and resolves it to a tuple: (steam64_id, persona_name)
    Returns (None, None) if invalid/unresolvable.
    """
    text = user_input.strip().rstrip("/")
    if not text:
        return None, None

    parsed_url = urlparse(text)
    evxl_steam_id = None
    if parsed_url.netloc.lower() in ("evxl.app", "www.evxl.app"):
        match = re.match(r"^/u/(\d{17})(?:/|$)", parsed_url.path)
        if parsed_url.scheme.lower() != "https" or not match:
            return None, None
        evxl_steam_id = match.group(1)
        url = STEAM_PROFILE_URL.format(evxl_steam_id)
    elif "steamcommunity.com/id/" in text:
        vanity_name = text.split("steamcommunity.com/id/")[-1].split("/")[0]
        url = STEAM_ID_URL.format(vanity_name)
    elif "steamcommunity.com/profiles/" in text:
        steam_id = text.split("steamcommunity.com/profiles/")[-1].split("/")[0]
        url = STEAM_PROFILE_URL.format(steam_id)
    elif re.match(r"^\d{17}$", text):
        url = STEAM_PROFILE_URL.format(text)
    else:
        url = STEAM_ID_URL.format(text)

    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            s64_match = re.search(r"<steamID64>(\d{17})</steamID64>", resp.text)
            name_match = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", resp.text)
            if not name_match:
                name_match = re.search(r"<steamID>(.*?)</steamID>", resp.text)

            steam_64 = s64_match.group(1) if s64_match else None
            persona_name = name_match.group(1).strip() if name_match else None

            if not steam_64 and re.match(r"^\d{17}$", text):
                steam_64 = text

            if steam_64:
                return steam_64, persona_name or steam_64
    except Exception:
        pass

    if evxl_steam_id:
        return evxl_steam_id, evxl_steam_id
    return None, None


class ModernKovaaKApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("KovaaK's Benchmark Inspector")
        self.geometry("1180x820")
        self.minsize(900, 600)
        self.configure(bg=THEME["bg_dark"])

        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.comparison_running = False
        self.token_request_running = False
        self.auth_token = ""
        self.master_results = []
        self._raw_results = []
        self.sort_state = {}
        self._debug_roots = []
        self._debug_children = {}
        self._debug_filter_state = ("", "All messages")
        self.debug_search_var = None
        self.debug_filter_var = None

        self.name_a = "Player A"
        self.name_b = "Player B"
        self._toast_timer = None

        self.setup_styles()
        self.setup_ui()
        self.load_config()
        self.after(100, self.process_queue)

    def setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=THEME["bg_dark"])
        style.configure("Card.TFrame", background=THEME["bg_card"], relief="flat")

        style.configure("TLabel", background=THEME["bg_dark"], foreground=THEME["fg"], font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=THEME["bg_card"], foreground=THEME["fg"], font=("Segoe UI", 9))
        style.configure("Sub.TLabel", background=THEME["bg_card"], foreground=THEME["fg_sub"], font=("Segoe UI", 8))
        style.configure("Preview.TLabel", background=THEME["bg_card"], foreground=THEME["fg_sub"], font=("Segoe UI", 8))
        style.configure("PreviewSuccess.TLabel", background=THEME["bg_card"], foreground=THEME["green"], font=("Segoe UI", 8))
        style.configure("PreviewWarning.TLabel", background=THEME["bg_card"], foreground="#fbbf24", font=("Segoe UI", 8))
        style.configure("Muted.TLabel", background=THEME["bg_dark"], foreground=THEME["fg_sub"], font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=THEME["bg_dark"], foreground="#ffffff", font=("Segoe UI", 14, "bold"))

        style.configure(
            "TEntry",
            fieldbackground=THEME["bg_input"],
            foreground=THEME["fg"],
            insertcolor=THEME["fg"],
            bordercolor=THEME["border"],
            lightcolor=THEME["border"],
            darkcolor=THEME["border"],
            padding=5,
        )

        style.configure(
            "TCombobox",
            fieldbackground=THEME["bg_input"],
            background=THEME["bg_input"],
            foreground=THEME["fg"],
            bordercolor=THEME["border"],
            arrowcolor=THEME["fg_sub"],
            padding=4,
        )
        style.map("TCombobox", fieldbackground=[("readonly", THEME["bg_input"])], foreground=[("readonly", THEME["fg"])])

        style.configure(
            "TCheckbutton",
            background=THEME["bg_card"],
            foreground=THEME["fg"],
            font=("Segoe UI", 9),
        )
        style.map("TCheckbutton", background=[("active", THEME["bg_card"])])

        style.configure(
            "Accent.TButton",
            background=THEME["accent"],
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
            padding=(15, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", THEME["accent_hover"]), ("disabled", THEME["border"])],
            foreground=[("disabled", THEME["fg_sub"])],
        )
        style.configure(
            "Stop.TButton",
            background=THEME["red"],
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            padding=(12, 8),
        )
        style.map(
            "Stop.TButton",
            background=[("active", "#dc2626"), ("disabled", THEME["border"])],
            foreground=[("disabled", THEME["fg_sub"])],
        )

        style.configure("TNotebook", background=THEME["bg_dark"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=THEME["bg_card"],
            foreground=THEME["fg_sub"],
            padding=(15, 8),
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", THEME["accent"])],
            foreground=[("selected", "#ffffff")],
        )

        style.configure(
            "Treeview",
            background=THEME["table_bg"],
            fieldbackground=THEME["table_bg"],
            foreground=THEME["fg"],
            borderwidth=0,
            rowheight=28,
            font=("Segoe UI", 9),
        )
        style.map("Treeview", background=[("selected", THEME["tree_select"])], foreground=[("selected", "#ffffff")])

        style.configure(
            "Treeview.Heading",
            background=THEME["bg_card"],
            foreground=THEME["fg"],
            font=("Segoe UI", 9, "bold"),
            borderwidth=1,
            relief="flat",
            padding=6,
        )
        style.map("Treeview.Heading", background=[("active", THEME["border"])])

    def setup_ui(self):
        main_container = tk.Frame(self, bg=THEME["bg_dark"])
        main_container.pack(fill="both", expand=True)

        content_frame = tk.Frame(main_container, bg=THEME["bg_dark"])
        content_frame.pack(fill="both", expand=True)

        title_bar = ttk.Frame(content_frame, padding=(20, 15, 20, 10))
        title_bar.pack(fill="x")

        ttk.Label(title_bar, text="KovaaK's Benchmark Inspector", style="Header.TLabel").pack(side="left")

        self.notebook = ttk.Notebook(content_frame)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        main_tab = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(main_tab, text=" Main ")

        card_controls = ttk.Frame(main_tab, style="Card.TFrame", padding=15)
        card_controls.pack(fill="x", pady=(10, 10))

        self.btn_compare = ttk.Button(
            card_controls,
            text="Compare Scores",
            style="Accent.TButton",
            command=self.start_comparison,
        )
        self.btn_compare.grid(row=0, column=0, padx=5, pady=(0, 8), sticky="w")

        self.btn_stop = ttk.Button(
            card_controls,
            text="Stop",
            style="Stop.TButton",
            command=self.stop_comparison,
            state="disabled",
        )
        self.btn_stop.grid(row=0, column=1, padx=5, pady=(0, 8), sticky="w")

        self.lbl_stats = ttk.Label(card_controls, text="", style="Sub.TLabel")
        self.lbl_stats.grid(row=0, column=2, sticky="e", padx=5)
        card_controls.columnconfigure(2, weight=1)

        self.progress_bar = ttk.Progressbar(card_controls, mode="determinate", maximum=100)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=4)
        self.lbl_progress = ttk.Label(card_controls, text="0 / 0 (—%)", style="Sub.TLabel")
        self.lbl_progress.grid(row=1, column=2, sticky="e", padx=5, pady=4)

        ttk.Label(card_controls, text="Performance Filter", style="Sub.TLabel").grid(
            row=2, column=0, sticky="w", padx=5, pady=(8, 2)
        )
        self.filter_better_var = tk.StringVar(value="All Results")
        self.cmb_filter_better = ttk.Combobox(
            card_controls,
            textvariable=self.filter_better_var,
            state="readonly",
            values=["All Results", "Player A Better", "Player B Better", "Tie / Equal Score"],
            width=22,
        )
        self.cmb_filter_better.grid(row=3, column=0, sticky="w", padx=5)
        self.cmb_filter_better.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

        self.filter_both_scored_var = tk.BooleanVar(value=False)
        self.chk_both_scored = ttk.Checkbutton(
            card_controls,
            text="Only show scenarios where both players scored",
            variable=self.filter_both_scored_var,
            command=self.apply_filters,
            style="TCheckbutton",
        )
        self.chk_both_scored.grid(row=3, column=1, columnspan=2, sticky="w", padx=10)

        self.lbl_status = ttk.Label(
            main_tab,
            text="Ready. Set up player profiles and retrieve a KovaaK's token in Config.",
            style="Muted.TLabel",
        )
        self.lbl_status.pack(fill="x", pady=(0, 8))

        table_tab = ttk.Frame(main_tab, padding=0)

        columns = ("benchmark", "scenario", "score_a", "score_b", "diff")
        self.tree = ttk.Treeview(table_tab, columns=columns, show="headings", style="Treeview")

        self.tree.heading("benchmark", text="Benchmark Group  ▲▼", command=lambda: self.sort_column("benchmark"))
        self.tree.heading("scenario", text="Scenario Name  ▲▼ (Click to Copy)", command=lambda: self.sort_column("scenario"))
        self.tree.heading("score_a", text="Player A  ▲▼", command=lambda: self.sort_column("score_a"))
        self.tree.heading("score_b", text="Player B  ▲▼", command=lambda: self.sort_column("score_b"))
        self.tree.heading("diff", text="Difference  ▲▼", command=lambda: self.sort_column("diff"))

        self.tree.column("benchmark", width=220, anchor="w")
        self.tree.column("scenario", width=320, anchor="w")
        self.tree.column("score_a", width=130, anchor="e")
        self.tree.column("score_b", width=130, anchor="e")
        self.tree.column("diff", width=130, anchor="center")

        self.tree.bind("<Button-1>", self.on_tree_click)

        table_scroll = ttk.Scrollbar(table_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=table_scroll.set)

        self.tree.pack(side="left", fill="both", expand=True)
        table_scroll.pack(side="right", fill="y")
        table_tab.pack(fill="both", expand=True)

        self.tree.tag_configure("pos_diff", foreground=THEME["green"])
        self.tree.tag_configure("neg_diff", foreground=THEME["red"])
        self.tree.tag_configure("even_row", background=THEME["table_bg"])
        self.tree.tag_configure("odd_row", background=THEME["table_alt"])

        self.config_tab = ttk.Frame(self.notebook, padding=20)
        self.notebook.add(self.config_tab, text=" Config ")

        player_card = ttk.Frame(self.config_tab, style="Card.TFrame", padding=15)
        player_card.pack(fill="x", pady=(0, 14))
        ttk.Label(player_card, text="Compare players", style="Card.TLabel", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4)
        )
        ttk.Label(
            player_card,
            text=(
                "Enter a Steam ID or profile URL, or an EVXL profile URL "
                "(https://evxl.app/u/<SteamID>)."
            ),
            style="Sub.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 14))
        ttk.Label(player_card, text="PLAYER A", style="Sub.TLabel").grid(
            row=2, column=0, sticky="w", padx=6, pady=(0, 4)
        )
        ttk.Label(player_card, text="PLAYER B", style="Sub.TLabel").grid(
            row=2, column=1, sticky="w", padx=6, pady=(0, 4)
        )
        self.player_a_var = tk.StringVar()
        self.player_b_var = tk.StringVar()
        self.entry_player_a = ttk.Entry(player_card, textvariable=self.player_a_var)
        self.entry_player_a.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 2), ipady=4)
        self.entry_player_b = ttk.Entry(player_card, textvariable=self.player_b_var)
        self.entry_player_b.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 2), ipady=4)
        self.player_a_preview = ttk.Label(
            player_card,
            text="Paste a Steam ID or profile URL to check the user.",
            style="Preview.TLabel",
        )
        self.player_a_preview.grid(row=4, column=0, sticky="w", padx=6, pady=(0, 8))
        self.player_b_preview = ttk.Label(
            player_card,
            text="Paste a Steam ID or profile URL to check the user.",
            style="Preview.TLabel",
        )
        self.player_b_preview.grid(row=4, column=1, sticky="w", padx=6, pady=(0, 8))
        self._player_lookup_generation = {"a": 0, "b": 0}
        self._player_lookup_after = {"a": None, "b": None}
        self.player_a_var.trace_add("write", lambda *_args: self.schedule_player_preview("a"))
        self.player_b_var.trace_add("write", lambda *_args: self.schedule_player_preview("b"))
        player_card.columnconfigure(0, weight=1)
        player_card.columnconfigure(1, weight=1)

        auth_card = ttk.Frame(self.config_tab, style="Card.TFrame", padding=15)
        auth_card.pack(fill="x")
        ttk.Label(auth_card, text="KovaaK's authorization", style="Card.TLabel", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4)
        )
        ttk.Label(
            auth_card,
            text=(
                "Credentials are sent directly to KovaaK's over HTTPS only when you request a token. "
                "They are cleared as soon as the request starts and are never saved. "
                "The token stays in memory for this app session only."
            ),
            style="Sub.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 14))
        ttk.Label(auth_card, text="USERNAME OR EMAIL", style="Sub.TLabel").grid(
            row=2, column=0, sticky="w", padx=6, pady=(0, 4)
        )
        ttk.Label(auth_card, text="PASSWORD", style="Sub.TLabel").grid(
            row=2, column=1, sticky="w", padx=6, pady=(0, 4)
        )
        self.entry_username = ttk.Entry(auth_card)
        self.entry_username.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 12), ipady=4)
        self.entry_password = ttk.Entry(auth_card, show="*")
        self.entry_password.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 12), ipady=4)
        self.btn_get_token = ttk.Button(
            auth_card,
            text="Get token",
            style="Accent.TButton",
            command=self.start_token_retrieval,
        )
        self.btn_get_token.grid(row=3, column=2, sticky="ew", padx=6, pady=(0, 12))
        ttk.Label(auth_card, text="AUTH TOKEN", style="Sub.TLabel").grid(
            row=4, column=0, sticky="w", padx=6, pady=(0, 4)
        )
        self.auth_token_var = tk.StringVar(value="")
        self.entry_token = ttk.Entry(
            auth_card,
            textvariable=self.auth_token_var,
            state="readonly",
            show="*",
        )
        self.entry_token.grid(row=5, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6), ipady=4)
        self.lbl_token_status = ttk.Label(
            auth_card,
            text="No token retrieved",
            style="Sub.TLabel",
        )
        self.lbl_token_status.grid(row=6, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))
        auth_card.columnconfigure(0, weight=1)
        auth_card.columnconfigure(1, weight=1)
        auth_card.columnconfigure(2, weight=0)

        debug_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(debug_tab, text=" Debug ")

        debug_toolbar = ttk.Frame(debug_tab, style="Card.TFrame", padding=(10, 8))
        debug_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(debug_toolbar, text="Debug messages", style="Card.TLabel").pack(side="left", padx=(4, 8))
        self.debug_filter_var = tk.StringVar(value="All messages")
        self.cmb_debug_filter = ttk.Combobox(
            debug_toolbar,
            textvariable=self.debug_filter_var,
            values=["All messages", "Errors only"],
            state="readonly",
            width=14,
        )
        self.cmb_debug_filter.pack(side="right", padx=(8, 4))
        self.cmb_debug_filter.bind("<<ComboboxSelected>>", lambda _event: self.filter_debug_entries())
        ttk.Label(debug_toolbar, text="Filter", style="Sub.TLabel").pack(side="right")
        self.debug_search_var = tk.StringVar(value="")
        self.entry_debug_search = ttk.Entry(debug_toolbar, textvariable=self.debug_search_var, width=30)
        self.entry_debug_search.pack(side="right", padx=(8, 4), ipady=3)
        self.debug_search_var.trace_add("write", lambda *_args: self.filter_debug_entries())
        ttk.Label(debug_toolbar, text="Search", style="Sub.TLabel").pack(side="right")

        self.debug_tree = ttk.Treeview(
            debug_tab,
            columns=("detail",),
            show="tree",
            style="Treeview",
        )
        self.debug_tree.column("#0", width=920, minwidth=400, stretch=True)
        self.debug_tree.heading("#0", text="Activity and benchmark details")
        debug_scroll_y = ttk.Scrollbar(debug_tab, orient="vertical", command=self.debug_tree.yview)
        debug_scroll_x = ttk.Scrollbar(debug_tab, orient="horizontal", command=self.debug_tree.xview)
        self.debug_tree.configure(yscrollcommand=debug_scroll_y.set, xscrollcommand=debug_scroll_x.set)

        debug_scroll_y.pack(side="right", fill="y")
        debug_scroll_x.pack(side="bottom", fill="x")
        self.debug_tree.pack(side="left", fill="both", expand=True)
        self.debug_tree.tag_configure("debug_error", foreground=THEME["red"])

        self.toast_frame = tk.Frame(
            main_container,
            bg=THEME["toast_bg"],
            highlightbackground=THEME["toast_border"],
            highlightthickness=1,
            padx=12,
            pady=8
        )
        self.toast_label = tk.Label(
            self.toast_frame,
            text="",
            bg=THEME["toast_bg"],
            fg=THEME["toast_fg"],
            font=("Segoe UI", 9, "bold")
        )
        self.toast_label.pack(side="left")

    def show_toast(self, message: str, duration_ms: int = 2500):
        if self._toast_timer is not None:
            self.after_cancel(self._toast_timer)

        self.toast_label.config(text=f"📋 Copied: {message}")
        self.toast_frame.place(relx=1.0, rely=1.0, x=-25, y=-25, anchor="se")
        self.toast_frame.lift()

        self._toast_timer = self.after(duration_ms, self.hide_toast)

    def hide_toast(self):
        self.toast_frame.place_forget()
        self._toast_timer = None

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            column = self.tree.identify_column(event.x)
            if column == "#2":
                item_id = self.tree.identify_row(event.y)
                if item_id:
                    values = self.tree.item(item_id, "values")
                    if len(values) > 1:
                        scenario_name = values[1]
                        self.clipboard_clear()
                        self.clipboard_append(scenario_name)
                        self.show_toast(scenario_name)

    def load_config(self):
        try:
            if not CONFIG_FILE.exists():
                CONFIG_FILE.write_text(
                    json.dumps(DEFAULT_CONFIG, indent=4),
                    encoding="utf-8",
                )

            with CONFIG_FILE.open("r", encoding="utf-8") as config_file:
                data = json.load(config_file)
            if not isinstance(data, dict):
                raise ValueError("config.json must contain a JSON object.")

            player_a = data.get("player_a", "")
            player_b = data.get("player_b", "")
            if player_a:
                self.entry_player_a.insert(0, player_a)
            if player_b:
                self.entry_player_b.insert(0, player_b)

            if "enc_auth_token" in data:
                self.save_config(player_a, player_b)
                self.log_debug(
                    "Removed a previously saved auth token from config.json. "
                    "Retrieve a new token from the Config tab."
                )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            self.log_debug(f"Failed to load config.json: {type(error).__name__}")

    def save_config(self, input_a: str, input_b: str):
        payload = {"player_a": input_a, "player_b": input_b}
        CONFIG_FILE.write_text(json.dumps(payload, indent=4), encoding="utf-8")

    def log_debug(self, message: str):
        self.msg_queue.put(("LOG", f"{message}\n"))

    @staticmethod
    def debug_timestamp() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    @staticmethod
    def is_debug_error(text: str) -> bool:
        lowered = text.lower()
        return (
            "error" in lowered
            or "failed" in lowered
            or "failure" in lowered
            or "http 4" in lowered
            or "http 5" in lowered
            or "could not" in lowered
            or "unavailable" in lowered
            or "timed out" in lowered
            or "rejected" in lowered
        )

    def filter_debug_entries(self):
        if not self.debug_filter_var or not self.debug_search_var:
            return
        query = self.debug_search_var.get().strip().casefold()
        errors_only = self.debug_filter_var.get() == "Errors only"
        filter_state = (query, self.debug_filter_var.get())
        if filter_state == self._debug_filter_state and not query and not errors_only:
            return

        for root in self._debug_roots:
            root_text = self.debug_tree.item(root, "text")
            root_is_error = "debug_error" in self.debug_tree.item(root, "tags")
            children = self._debug_children.get(root, [])
            child_matches = []
            for child in children:
                child_text = self.debug_tree.item(child, "text")
                child_is_error = "debug_error" in self.debug_tree.item(child, "tags")
                passes_filter = not errors_only or child_is_error or root_is_error
                matches_query = not query or query in child_text.casefold()
                if passes_filter and matches_query:
                    child_matches.append(child)

            root_passes_filter = not errors_only or root_is_error
            root_matches_query = not query or query in root_text.casefold()
            show_root = (
                root_passes_filter
                and root_matches_query
            ) or bool(child_matches)

            for child in children:
                self.debug_tree.detach(child)
            self.debug_tree.detach(root)
            if show_root:
                self.debug_tree.move(root, "", "end")
                for child in children:
                    if root_matches_query and root_passes_filter or child in child_matches:
                        self.debug_tree.move(child, root, "end")
        self._debug_filter_state = filter_state

    def add_benchmark_debug_detail(self, detail: dict):
        status = (
            f"Player A: {detail['player_a_status']} | "
            f"Player B: {detail['player_b_status']}"
        )
        parent_text = f"[{self.debug_timestamp()}] {detail['name']} | {status}"
        has_scenarios = bool(detail["scenarios"])
        has_error = not detail["success"]
        parent = self.debug_tree.insert(
            "",
            "end",
            text=parent_text,
            open=False,
            tags=("debug_error",) if has_error else (),
        )

        if has_scenarios:
            child_items = []
            for scenario in detail["scenarios"]:
                score_a = f"{scenario['score_a']:.2f}" if scenario["score_a"] is not None else "N/A"
                score_b = f"{scenario['score_b']:.2f}" if scenario["score_b"] is not None else "N/A"
                child_items.append(
                    self.debug_tree.insert(
                        parent,
                        "end",
                        text=(
                            f"{scenario['name']} | "
                            f"Player A: {score_a} | Player B: {score_b}"
                        ),
                    )
                )
        else:
            child_items = [
                self.debug_tree.insert(
                    parent, "end", text="No scenario scores were returned."
                )
            ]
        self._debug_children[parent] = child_items
        self._debug_roots.append(parent)
        self.filter_debug_entries()
        self.debug_tree.yview_moveto(1.0)

    def add_debug_log_entry(self, log_text: str):
        item = self.debug_tree.insert(
            "",
            "end",
            text=f"[{self.debug_timestamp()}] {log_text}",
            tags=("debug_error",) if self.is_debug_error(log_text) else (),
        )
        self._debug_roots.append(item)
        self._debug_children[item] = []
        self.filter_debug_entries()
        self.debug_tree.yview_moveto(1.0)

    def schedule_player_preview(self, player: str):
        variable = self.player_a_var if player == "a" else self.player_b_var
        label = self.player_a_preview if player == "a" else self.player_b_preview
        self._player_lookup_generation[player] += 1
        generation = self._player_lookup_generation[player]
        pending = self._player_lookup_after[player]
        if pending is not None:
            self.after_cancel(pending)
            self._player_lookup_after[player] = None

        value = variable.get().strip()
        if not value:
            label.config(
                text="Paste a Steam ID or profile URL to check the user.",
                style="Preview.TLabel",
            )
            return

        label.config(text="Checking for a Steam user...", style="Preview.TLabel")
        self._player_lookup_after[player] = self.after(
            450,
            lambda: self.lookup_player_preview(player, generation, value),
        )

    def lookup_player_preview(self, player: str, generation: int, value: str):
        self._player_lookup_after[player] = None

        def lookup():
            try:
                steam_id, username = resolve_to_steam_64(value)
                result = ("PLAYER_PREVIEW", (player, generation, value, steam_id, username, None))
            except Exception as error:
                result = (
                    "PLAYER_PREVIEW",
                    (player, generation, value, None, None, type(error).__name__),
                )
            self.msg_queue.put(result)

        try:
            threading.Thread(target=lookup, daemon=True).start()
        except RuntimeError as error:
            label = self.player_a_preview if player == "a" else self.player_b_preview
            label.config(
                text=f"Could not check this profile ({type(error).__name__}).",
                style="PreviewWarning.TLabel",
            )

    def update_player_preview(
        self,
        player: str,
        generation: int,
        value: str,
        steam_id: str | None,
        username: str | None,
        error: str | None,
    ):
        variable = self.player_a_var if player == "a" else self.player_b_var
        label = self.player_a_preview if player == "a" else self.player_b_preview
        if generation != self._player_lookup_generation[player] or variable.get().strip() != value:
            return

        if error:
            label.config(
                text=f"Could not check for a Steam user ({error}). Verify the connection and input.",
                style="PreviewWarning.TLabel",
            )
        elif not steam_id:
            label.config(
                text="No Steam user detected. Check the Steam ID or profile URL.",
                style="PreviewWarning.TLabel",
            )
        elif not username or username == steam_id:
            label.config(
                text=f"Steam ID detected: {steam_id}. Username could not be retrieved.",
                style="PreviewWarning.TLabel",
            )
        else:
            label.config(
                text=f"Steam user: {username} ({steam_id})",
                style="PreviewSuccess.TLabel",
            )

    def process_queue(self):
        results_changed = False
        final_status = None
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "STATUS":
                    self.lbl_status.config(text=data)
                elif msg_type == "LOG":
                    self.add_debug_log_entry(data.rstrip())
                elif msg_type == "BENCHMARK_DETAIL":
                    self.add_benchmark_debug_detail(data)
                elif msg_type == "PLAYER_PREVIEW":
                    self.update_player_preview(*data)
                elif msg_type == "HEADERS":
                    self.name_a, self.name_b = data
                    self.tree.heading("score_a", text=f"{self.name_a}  ▲▼", command=lambda: self.sort_column("score_a"))
                    self.tree.heading("score_b", text=f"{self.name_b}  ▲▼", command=lambda: self.sort_column("score_b"))
                elif msg_type == "PROGRESS":
                    completed, total, benchmark_results = data
                    self._raw_results.extend(benchmark_results)
                    self.master_results = self.dedupe_results(self._raw_results)
                    percentage = int(completed / total * 100) if total else 0
                    self.progress_bar.configure(value=percentage)
                    total_label = str(total) if total else "0"
                    percentage_label = f"{percentage}%" if total else "—%"
                    self.lbl_progress.config(
                        text=f"{completed} / {total_label} ({percentage_label})"
                    )
                    self.lbl_status.config(
                        text=f"Fetching benchmark scores: {completed} of {total} benchmarks ({percentage_label})."
                    )
                    results_changed = True
                elif msg_type == "ERROR":
                    messagebox.showerror("Error", f"Failed to retrieve data:\n\n{data}", parent=self)
                elif msg_type == "TOKEN_SUCCESS":
                    self.auth_token = data
                    self.auth_token_var.set(data)
                    self.btn_get_token.config(state="normal")
                    self.token_request_running = False
                    self.lbl_token_status.config(text="Token retrieved and ready for this session.")
                    self.lbl_status.config(text="KovaaK's token retrieved. Ready to compare.")
                    self.log_debug("KovaaK's token retrieved; credentials cleared and token not saved.")
                elif msg_type == "TOKEN_ERROR":
                    self.auth_token = ""
                    self.auth_token_var.set("")
                    self.btn_get_token.config(state="normal")
                    self.token_request_running = False
                    self.lbl_token_status.config(text=data)
                    self.lbl_status.config(text="Could not retrieve a KovaaK's token.")
                    messagebox.showerror("Token Retrieval Failed", data, parent=self)
                elif msg_type == "FINISHED":
                    self.comparison_running = False
                    self.btn_compare.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    completed = data["completed"]
                    total = data["total"]
                    if data["failed"]:
                        final_status = "Error occurred. Check the Debug tab."
                    elif data["cancelled"]:
                        final_status = (
                            f"Stopped after {completed} of {total} benchmarks. "
                            f"Kept {len(self.master_results)} unique scenarios."
                        )
                    elif total == 0:
                        final_status = "Finished. No benchmarks were available."
                    else:
                        final_status = (
                            f"Finished fetching {completed} of {total} benchmarks. "
                            f"Showing {len(self.master_results)} unique scenarios."
                        )
        except queue.Empty:
            pass
        finally:
            if results_changed:
                self.apply_filters()
            if final_status:
                self.lbl_status.config(text=final_status)
            self.after(100, self.process_queue)

    def start_comparison(self):
        input_a = self.entry_player_a.get().strip()
        input_b = self.entry_player_b.get().strip()

        if not input_a or not input_b:
            messagebox.showwarning(
                "Missing Player",
                "Enter a Steam ID, Steam profile URL, or EVXL profile URL for both players in the Config tab.",
                parent=self,
            )
            self.notebook.select(self.config_tab)
            return
        if self.token_request_running:
            messagebox.showwarning(
                "Token Request In Progress",
                "Wait for the KovaaK's token request to finish before comparing.",
                parent=self,
            )
            self.notebook.select(self.config_tab)
            return
        if not self.auth_token:
            messagebox.showwarning(
                "KovaaK's Token Required",
                "Retrieve an auth token in the Config tab before comparing scores.",
                parent=self,
            )
            self.notebook.select(self.config_tab)
            return

        try:
            self.save_config(input_a, input_b)
        except OSError as error:
            messagebox.showerror(
                "Could Not Save Settings",
                f"Could not save player IDs to config.json:\n{type(error).__name__}",
                parent=self,
            )
            return

        self.btn_compare.config(state="disabled")

        for item in self._debug_roots:
            self.debug_tree.delete(item)
        self._debug_roots.clear()
        self._debug_children.clear()

        for row in self.tree.get_children():
            self.tree.delete(row)

        self.lbl_stats.config(text="")
        self.stop_event.clear()
        self.comparison_running = True
        self.master_results = []
        self._raw_results = []
        self.progress_bar.configure(value=0)
        self.lbl_progress.config(text="0 / 0 (—%)")
        self.btn_stop.config(state="normal")
        threading.Thread(
            target=self.bg_worker,
            args=(input_a, input_b, self.auth_token),
            daemon=True,
        ).start()

    def start_token_retrieval(self):
        if self.token_request_running:
            return
        if self.comparison_running:
            messagebox.showwarning(
                "Comparison In Progress",
                "Wait for the current comparison to finish before retrieving a new token.",
                parent=self,
            )
            return

        username = self.entry_username.get().strip()
        password = self.entry_password.get()
        if not username or not password:
            self.lbl_token_status.config(text="Enter both username/email and password.")
            messagebox.showwarning(
                "Missing Credentials",
                "Enter your KovaaK's web-app username/email and password.",
                parent=self,
            )
            return

        self.entry_username.delete(0, "end")
        self.entry_password.delete(0, "end")
        self.auth_token = ""
        self.auth_token_var.set("")
        self.token_request_running = True
        self.btn_get_token.config(state="disabled")
        self.lbl_token_status.config(text="Requesting token... Credentials have been cleared.")
        self.lbl_status.config(text="Authenticating with KovaaK's...")
        self.log_debug("Requesting KovaaK's auth token.")

        def retrieve_token(request_username: str, request_password: str):
            try:
                token = retrieve_kovaaks_auth_token(request_username, request_password)
            except KovaaKsAuthenticationError as error:
                self.msg_queue.put(("TOKEN_ERROR", str(error)))
            except requests.Timeout:
                self.msg_queue.put(
                    ("TOKEN_ERROR", "The login request timed out. Check your connection and try again.")
                )
            except requests.ConnectionError:
                self.msg_queue.put(
                    ("TOKEN_ERROR", "Could not connect to KovaaK's. Check your internet connection and try again.")
                )
            except requests.HTTPError as error:
                status_code = error.response.status_code if error.response is not None else None
                if status_code == 429:
                    message = "KovaaK's temporarily rate-limited the login. Wait a moment and try again."
                elif status_code is not None and status_code >= 500:
                    message = "KovaaK's login service is unavailable. Try again later."
                else:
                    message = "KovaaK's rejected the login request. Check your credentials and try again."
                self.msg_queue.put(("TOKEN_ERROR", message))
            except requests.RequestException:
                self.msg_queue.put(
                    ("TOKEN_ERROR", "A network error interrupted the login request. Please try again.")
                )
            except Exception as error:
                self.log_debug(f"Unexpected token retrieval error ({type(error).__name__}).")
                self.msg_queue.put(
                    ("TOKEN_ERROR", "An unexpected error occurred while retrieving the token.")
                )
            else:
                self.msg_queue.put(("TOKEN_SUCCESS", token))

        try:
            threading.Thread(
                target=retrieve_token,
                args=(username, password),
                daemon=True,
            ).start()
        except RuntimeError as error:
            self.token_request_running = False
            self.btn_get_token.config(state="normal")
            self.lbl_token_status.config(text="Could not start the token request.")
            self.log_debug(f"Could not start token retrieval ({type(error).__name__}).")
            messagebox.showerror(
                "Token Retrieval Failed",
                "Could not start the token request. Please try again.",
                parent=self,
            )

    def stop_comparison(self):
        if self.comparison_running and not self.stop_event.is_set():
            self.stop_event.set()
            self.btn_stop.config(state="disabled")
            self.lbl_status.config(text="Stop requested. Waiting for active requests to finish...")

    def parse_scenarios_from_json(self, raw_json) -> dict:
        scenarios = {}
        if not raw_json or not isinstance(raw_json, dict):
            return scenarios

        categories = raw_json.get("categories", {})
        if isinstance(categories, dict):
            for cat_name, cat_data in categories.items():
                if isinstance(cat_data, dict):
                    scen_dict = cat_data.get("scenarios", {})
                    if isinstance(scen_dict, dict):
                        for s_name, s_info in scen_dict.items():
                            if isinstance(s_info, dict):
                                score_val = s_info.get("score")
                                if score_val is None:
                                    score_val = s_info.get("maxScore")
                                if score_val is None:
                                    score_val = s_info.get("highScore")
                                if score_val is not None:
                                    try:
                                        # Divide score by 100 here
                                        scenarios[str(s_name)] = float(score_val) / 100.0
                                    except (ValueError, TypeError):
                                        pass

        return scenarios

    @staticmethod
    def dedupe_results(all_results: list[dict]) -> list[dict]:
        unique_scenarios = {}
        for item in all_results:
            scen_name = item["scenario"]
            score_a = item["score_a_val"] or 0.0
            score_b = item["score_b_val"] or 0.0
            total_combined_score = score_a + score_b

            if scen_name not in unique_scenarios:
                unique_scenarios[scen_name] = (total_combined_score, item)
            else:
                existing_total, _ = unique_scenarios[scen_name]
                if total_combined_score > existing_total:
                    unique_scenarios[scen_name] = (total_combined_score, item)

        return [entry[1] for entry in unique_scenarios.values()]

    def bg_worker(self, raw_input_a: str, raw_input_b: str, auth_token: str):
        completed = 0
        total = 0
        cancelled = False
        failed = False
        request_lock = threading.Lock()
        next_request_time = 0.0
        cooldown_until = 0.0

        def check_for_stop():
            if self.stop_event.is_set():
                raise _ComparisonStopped()

        def wait_for_request_slot():
            nonlocal next_request_time
            while True:
                check_for_stop()
                with request_lock:
                    now = time.monotonic()
                    wait_seconds = max(next_request_time, cooldown_until) - now
                    if wait_seconds <= 0:
                        next_request_time = now + MIN_REQUEST_INTERVAL_SECONDS
                        return
                time.sleep(min(wait_seconds, 0.2))

        def request_with_rate_limit(send_request, request_name: str):
            nonlocal cooldown_until
            for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
                check_for_stop()
                wait_for_request_slot()
                response = send_request()
                if response.status_code != 429:
                    return response

                retry_after = self.retry_after_seconds(response)
                delay = (
                    retry_after
                    if retry_after is not None
                    else RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt)
                )
                with request_lock:
                    cooldown_until = max(cooldown_until, time.monotonic() + delay)
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    self.log_debug(
                        f"{request_name} still returned HTTP 429 after "
                        f"{MAX_RATE_LIMIT_RETRIES} retries; pausing other requests "
                        f"for {delay:.1f}s."
                    )
                    return response
                self.log_debug(
                    f"{request_name} returned HTTP 429; pausing requests for "
                    f"{delay:.1f}s before retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES}."
                )
            raise RuntimeError("Rate-limit retry loop exited unexpectedly.")

        try:
            session = create_browser_session(auth_token)

            self.msg_queue.put(("STATUS", "Validating & resolving Steam profiles..."))
            self.log_debug("=== Resolving Steam Profiles ===")

            check_for_stop()
            steam_a, name_a = resolve_to_steam_64(raw_input_a)
            check_for_stop()
            if not steam_a:
                raise ValueError("Could not resolve the Player A Steam profile.")

            steam_b, name_b = resolve_to_steam_64(raw_input_b)
            check_for_stop()
            if not steam_b:
                raise ValueError("Could not resolve the Player B Steam profile.")

            self.msg_queue.put(("HEADERS", (name_a, name_b)))

            self.log_debug("=== Starting Benchmark Comparison ===")
            self.log_debug("Resolved both Steam profiles.")

            benchmarks = []
            page = 0
            while True:
                check_for_stop()
                self.msg_queue.put(
                    ("STATUS", f"Fetching benchmark list: page {page + 1}...")
                )
                params = {
                    "username": steam_a,
                    "page": page,
                    "max": BENCHMARK_PAGE_SIZE,
                }
                resp = request_with_rate_limit(
                    lambda: session.get(BENCHMARKS_LIST_URL, params=params, timeout=10),
                    f"Benchmark list page {page + 1}",
                )
                self.log_debug(
                    f"Benchmark list page {page + 1}: HTTP {resp.status_code} "
                    f"({resp.reason or 'no status text'})."
                )
                check_for_stop()
                if resp.status_code != 200:
                    raise requests.HTTPError(
                        f"Benchmark list page {page + 1} failed with HTTP "
                        f"{resp.status_code} ({resp.reason or 'no status text'}).",
                        response=resp,
                    )

                raw_data = resp.json()
                items = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data
                if not isinstance(items, list):
                    raise ValueError(
                        f"Unexpected response format for benchmark list page {page + 1}."
                    )

                for item in items:
                    check_for_stop()
                    if not isinstance(item, dict):
                        continue
                    b_id = item.get("benchmarkId") or item.get("id")
                    if b_id:
                        b_name = item.get("benchmarkName") or item.get("name") or "Unnamed benchmark"
                        benchmarks.append({"id": b_id, "name": b_name})

                self.log_debug(
                    f"Page {page + 1} returned {len(items)} entries; "
                    f"collected {len(benchmarks)} benchmarks."
                )
                if len(items) < BENCHMARK_PAGE_SIZE:
                    break
                page += 1

            self.msg_queue.put(("STATUS", "Step 2/2: Querying scenarios & scores..."))
            total = len(benchmarks)
            self.msg_queue.put(("PROGRESS", (completed, total, [])))

            def fetch_benchmark_scenarios(b_info):
                b_id = b_info["id"]
                b_name = b_info["name"]

                params_a = {"benchmarkId": b_id, "steamId": steam_a}
                params_b = {"benchmarkId": b_id, "steamId": steam_b}

                dict_a, dict_b = {}, {}
                status_a = "Not fetched"
                status_b = "Not fetched"

                try:
                    check_for_stop()
                    req_a = session.prepare_request(requests.Request("GET", BENCHMARK_SCORE_URL, params=params_a))
                    r_a = request_with_rate_limit(
                        lambda: session.send(req_a, timeout=8),
                        f"{b_name} / Player A",
                    )
                    if r_a.status_code == 200:
                        parsed_json = r_a.json()
                        dict_a = self.parse_scenarios_from_json(parsed_json)
                        status_a = "HTTP 200"
                    else:
                        status_a = f"HTTP {r_a.status_code} ({r_a.reason or 'no status text'})"
                except _ComparisonStopped:
                    return None
                except Exception as ex:
                    status_a = self.describe_fetch_exception(ex)

                try:
                    check_for_stop()
                    req_b = session.prepare_request(requests.Request("GET", BENCHMARK_SCORE_URL, params=params_b))
                    r_b = request_with_rate_limit(
                        lambda: session.send(req_b, timeout=8),
                        f"{b_name} / Player B",
                    )
                    if r_b.status_code == 200:
                        parsed_json = r_b.json()
                        dict_b = self.parse_scenarios_from_json(parsed_json)
                        status_b = "HTTP 200"
                    else:
                        status_b = f"HTTP {r_b.status_code} ({r_b.reason or 'no status text'})"
                except _ComparisonStopped:
                    return None
                except Exception as ex:
                    status_b = self.describe_fetch_exception(ex)

                self.log_debug(
                    f"{b_name} fetch result — Player A: {status_a}; "
                    f"Player B: {status_b}; scenarios: "
                    f"{len(set(dict_a) | set(dict_b))}."
                )

                all_scenario_names = sorted(set(dict_a.keys()) | set(dict_b.keys()))

                b_results = []
                for s_name in all_scenario_names:
                    raw_sc_a = dict_a.get(s_name)
                    raw_sc_b = dict_b.get(s_name)

                    num_diff = 0.0
                    if raw_sc_a is None and raw_sc_b is None:
                        diff_str = "/"
                    elif raw_sc_a is not None and raw_sc_b is None:
                        num_diff = raw_sc_a
                        diff_str = f"+{raw_sc_a:.2f}"
                    elif raw_sc_a is None and raw_sc_b is not None:
                        num_diff = -raw_sc_b
                        diff_str = f"-{raw_sc_b:.2f}"
                    else:
                        num_diff = raw_sc_a - raw_sc_b
                        if abs(num_diff) < 1e-6:
                            diff_str = "/"
                        elif num_diff > 0:
                            diff_str = f"+{num_diff:.2f}"
                        else:
                            diff_str = f"-{abs(num_diff):.2f}"

                    b_results.append({
                        "benchmark": b_name,
                        "scenario": s_name,
                        "score_a_val": raw_sc_a,
                        "score_b_val": raw_sc_b,
                        "score_a": f"{raw_sc_a:.2f}" if raw_sc_a is not None else "N/A",
                        "score_b": f"{raw_sc_b:.2f}" if raw_sc_b is not None else "N/A",
                        "num_diff": num_diff,
                        "diff": diff_str,
                    })

                self.msg_queue.put(
                    (
                        "BENCHMARK_DETAIL",
                        {
                            "name": b_name,
                            "player_a_status": status_a,
                            "player_b_status": status_b,
                            "success": status_a == "HTTP 200" and status_b == "HTTP 200",
                            "scenarios": [
                                {
                                    "name": scenario["scenario"],
                                    "score_a": scenario["score_a_val"],
                                    "score_b": scenario["score_b_val"],
                                }
                                for scenario in b_results
                            ],
                        },
                    )
                )
                return b_results

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(fetch_benchmark_scenarios, b) for b in benchmarks]
                for future in as_completed(futures):
                    if self.stop_event.is_set():
                        for pending_future in futures:
                            pending_future.cancel()
                    if future.cancelled():
                        continue

                    benchmark_results = future.result()
                    if benchmark_results is None:
                        continue

                    completed += 1
                    self.msg_queue.put(("PROGRESS", (completed, total, benchmark_results)))

            cancelled = self.stop_event.is_set()
        except _ComparisonStopped:
            cancelled = True
        except Exception as err:
            failed = True
            error_message = self.describe_fetch_exception(err)
            self.log_debug(f"Comparison failed ({error_message}).")
            self.msg_queue.put(("ERROR", error_message))
        finally:
            self.msg_queue.put(
                (
                    "FINISHED",
                    {
                        "completed": completed,
                        "total": total,
                        "cancelled": cancelled,
                        "failed": failed,
                    },
                )
            )

    @staticmethod
    def retry_after_seconds(response: requests.Response) -> float | None:
        retry_after = getattr(response, "headers", {}).get("Retry-After")
        if not retry_after:
            return None
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.astimezone()
            return max(0.0, (retry_at - datetime.now().astimezone()).total_seconds())

    @staticmethod
    def describe_fetch_exception(error: Exception) -> str:
        if isinstance(error, requests.HTTPError):
            response = error.response
            if response is not None:
                status = response.status_code
                reason = response.reason or "Unknown status"
                if status == 401:
                    return f"HTTP 401 {reason}. Retrieve a fresh KovaaK's token."
                if status == 403:
                    return f"HTTP 403 {reason}. The account may not have access to this data."
                if status == 429:
                    return f"HTTP 429 {reason}. The service is rate-limiting requests; wait and try again."
                if status >= 500:
                    return f"HTTP {status} {reason}. The service may be temporarily unavailable."
                return f"HTTP {status} {reason}."
            return str(error) or "The server returned an HTTP error."
        if isinstance(error, requests.Timeout):
            return "The request timed out. Check your connection and try again."
        if isinstance(error, requests.ConnectionError):
            return "Could not connect to the service. Check your internet connection."
        if isinstance(error, requests.RequestException):
            return f"Network request failed ({type(error).__name__})."
        if isinstance(error, KovaaKsAuthenticationError):
            return str(error)
        return str(error) if isinstance(error, ValueError) else type(error).__name__

    def apply_filters(self):
        filtered = list(self.master_results)

        if self.filter_both_scored_var.get():
            filtered = [
                item for item in filtered
                if item["score_a_val"] is not None and item["score_b_val"] is not None
            ]

        better_selection = self.filter_better_var.get()
        if better_selection == "Player A Better":
            filtered = [item for item in filtered if item["num_diff"] > 1e-6]
        elif better_selection == "Player B Better":
            filtered = [item for item in filtered if item["num_diff"] < -1e-6]
        elif better_selection == "Tie / Equal Score":
            filtered = [item for item in filtered if abs(item["num_diff"]) <= 1e-6]

        self.render_tree_items(filtered)

    def render_tree_items(self, items: list):
        for row in self.tree.get_children():
            self.tree.delete(row)

        wins_a = sum(1 for i in self.master_results if i["num_diff"] > 1e-6)
        wins_b = sum(1 for i in self.master_results if i["num_diff"] < -1e-6)

        self.lbl_stats.config(
            text=f"Total Unique Scenarios: {len(self.master_results)}  |  {self.name_a} Wins: {wins_a}  |  {self.name_b} Wins: {wins_b}"
        )

        for idx, item in enumerate(items):
            tags = ["even_row" if idx % 2 == 0 else "odd_row"]
            if item["num_diff"] > 1e-6:
                tags.append("pos_diff")
            elif item["num_diff"] < -1e-6:
                tags.append("neg_diff")

            self.tree.insert(
                "",
                "end",
                values=(item["benchmark"], item["scenario"], item["score_a"], item["score_b"], item["diff"]),
                tags=tags
            )

        if not self.comparison_running:
            self.lbl_status.config(
                text=f"Displaying {len(items)} of {len(self.master_results)} unique scenario results."
            )

    def sort_column(self, col: str):
        if not self.master_results:
            return

        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        def sort_key(item):
            if col == "diff":
                return item["num_diff"]
            elif col == "score_a":
                return item["score_a_val"] if item["score_a_val"] is not None else -1e9
            elif col == "score_b":
                return item["score_b_val"] if item["score_b_val"] is not None else -1e9
            else:
                return str(item.get(col, "")).lower()

        self.master_results.sort(key=sort_key, reverse=reverse)
        self.apply_filters()


if __name__ == "__main__":
    app = ModernKovaaKApp()
    app.mainloop()