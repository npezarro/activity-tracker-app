"""The app: tray / menu-bar item, main window, and the background loop that ties
capture, ingest, the vision worker, summaries, pausing and updates together.

Threads never touch Tk: they put ("name", args...) on ``ui_q``, which the main thread
drains every 200 ms."""
import datetime as dt
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import (__version__, autostart, blocklist, config, ingest, keystore, models, pause, paths, store,
               summary, ui, update, vision)

log = logging.getLogger(__name__)
IS_MAC, IS_WIN = sys.platform == "darwin", sys.platform == "win32"
APP_TITLE = "Activity Tracker"

VISION_LABELS = [
    ("off", "Off: keep the on-screen text only (read on this computer)"),
    ("local", "Local model: nothing leaves this computer"),
    ("online", "Online model: every captured window is uploaded"),
    ("hybrid", "Mix: local model first, online only for non-sensitive windows"),
]
SUMMARY_LABELS = [
    ("off", "No model: plain summary from the timeline"),
    ("local", "Local model (Ollama)"),
    ("online", "Online model (same provider as above)"),
    ("claude", "Claude Code (your Claude login)"),
    ("codex", "Codex CLI (your ChatGPT login)"),
]
SCOPE_LABELS = [("window", "Focused window only"), ("display", "Whole screen")]


def _label(pairs, key):
    return dict(pairs).get(key, pairs[0][1])


def _key(pairs, label):
    return next((k for k, v in pairs if v == label), pairs[0][0])


def open_path(path):
    if IS_WIN:
        os.startfile(path)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def make_capture(get_cfg):
    if IS_MAC:
        from . import capture_mac

        return capture_mac.Capture(get_cfg)
    if IS_WIN:
        from . import capture_win

        return capture_win.Capture(get_cfg)
    return None  # Linux: no capture backend yet; the app still shows data from a spool


class App:
    def __init__(self, smoke_out=None):
        self.smoke_out = smoke_out
        self.cfg = config.load()
        self.ui_q = queue.Queue()
        self.root = tk.Tk()
        ui.init(self.root)
        self.root.title(APP_TITLE)
        try:
            self._icon = tk.PhotoImage(file=paths.resource("assets", "icon.png"))
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self.root.geometry("%dx%d" % (ui.px(980), ui.px(680)))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        if IS_MAC:  # clicking the Dock icon brings the window back; Dock > Quit really quits
            self.root.createcommand("::tk::mac::ReopenApplication", lambda *a: self.show())
            self.root.createcommand("::tk::mac::Quit", lambda *a: self.quit())
        self.spool = config.spool_dir(self.cfg)
        blocklist.user_path(self.spool)
        self.capture = make_capture(self.get_cfg)
        self.ingestor = ingest.Ingestor(self.spool)
        self.vision = vision.VisionWorker(self.get_cfg, keystore.get)
        self.last_nudge = 0
        self.nudge_win = None
        self.tray = None
        self.menubar = None
        self._menu_sig = None
        self._stop = threading.Event()
        self.state = self._load_state()
        self._build_window()
        self._start_tray()

    # ---------------- settings access ----------------
    def get_cfg(self):
        return self.cfg

    def _state_path(self):
        return os.path.join(paths.data_dir(), "state.json")

    def _load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(self.state, f)

    # ---------------- lifecycle ----------------
    def run(self):
        if self.smoke_out:
            self.root.after(300, self._smoke)
        else:
            if self.capture and self.cfg.get("capture_enabled", True):
                self.capture.start()
            self.vision.start()
            threading.Thread(target=self._background, name="background", daemon=True).start()
            threading.Thread(target=self._summary_loop, name="summaries", daemon=True).start()
            if not self.cfg.get("setup_seen"):
                self.cfg["setup_seen"] = True
                config.save(self.cfg)
                self.show("models")
            elif not self.tray and not self.menubar:
                self.show()  # no tray available: the window is the only way in
            else:
                self.root.withdraw()
            if self.cfg.get("auto_update_check") and time.time() - self.cfg.get("last_update_check", 0) > 86400:
                self.root.after(15000, lambda: self.check_updates(quiet=True))
        self.root.after(200, self._pump)
        self.refresh()
        self.root.mainloop()

    def quit(self):
        self._stop.set()
        self.vision.stop()
        if self.capture:
            self.capture.stop()
        if self.tray:
            self.tray.stop()
        if self.menubar:
            self.menubar.remove()
        self.root.destroy()

    def show(self, tab=None):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if tab:
            self.notebook.select(self.tabs[tab])
        self.refresh_timeline()

    def hide(self):
        if self.tray or self.menubar:
            self.root.withdraw()
        else:
            self.root.iconify()

    def _pump(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                getattr(self, msg[0])(*msg[1:])
        except queue.Empty:
            pass
        except Exception:
            log.exception("ui message failed")
        if not self._stop.is_set():
            self.root.after(200, self._pump)

    # ---------------- background loop ----------------
    def _background(self):
        last_retention = 0
        while not self._stop.is_set():
            now = time.time()
            try:
                self.ingestor.spool = config.spool_dir(self.cfg)
                self.ingestor.poll()
            except Exception:
                log.exception("ingest failed")
            st = pause.state(self.ingestor.spool)
            if pause.auto_resume_due(st, now):
                pause.resume(self.ingestor.spool)
                log.info("timed pause ended; capture resumed")
            elif pause.nudge_due(st, now, self.last_nudge, self.cfg.get("nudge_after_min", 15),
                                 self.cfg.get("nudge_repeat_min", 15), self.cfg.get("nudge_timed_pauses", False)):
                self.last_nudge = now
                self.ui_q.put(("show_nudge",))
            if now - last_retention > 6 * 3600:
                last_retention = now
                try:
                    store.run_retention(self.cfg["retention_days"], self.cfg["text_retention_days"])
                except Exception:
                    log.exception("retention failed")
            self.ui_q.put(("refresh",))
            self._stop.wait(5)

    def _summary_loop(self):
        """Separate from the main background loop: a model can take minutes to answer,
        and pausing, nudges and ingest must not wait for it."""
        while not self._stop.wait(30):
            try:
                self._summaries(dt.datetime.now().astimezone())
            except Exception:
                log.exception("summary failed")

    def _summaries(self, now):
        interval = int(self.cfg.get("summary_interval_min") or 0)
        if interval > 0:
            last = self.state.get("last_period_end")
            last = dt.datetime.fromisoformat(last) if last else None
            if last is None:  # first run: start counting from now
                self.state["last_period_end"] = now.isoformat()
                self._save_state()
            elif (now - last).total_seconds() >= interval * 60:
                start = last if last and (now - last).total_seconds() < 6 * 3600 else now - dt.timedelta(minutes=interval)
                if start.date() != now.date():
                    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if store.count_since(start) > 0:
                    summary.write_summary(self.cfg, keystore.get, start, now, "%s to %s" % (
                        start.strftime("%H:%M"), now.strftime("%H:%M")),
                        "%s to %s" % (start.strftime("%H:%M"), now.strftime("%H:%M")))
                self.state["last_period_end"] = now.isoformat()
                self._save_state()
        today = now.date().isoformat()
        if now.hour >= int(self.cfg.get("daily_summary_hour", 23)) and self.state.get("last_daily") != today:
            start, _ = summary.day_bounds(now)
            summary.write_summary(self.cfg, keystore.get, start, now, "the whole day", "The day")
            self.state["last_daily"] = today
            self._save_state()

    # ---------------- pause / resume ----------------
    def pause_for(self, minutes):
        pause.pause(config.spool_dir(self.cfg), minutes)
        self.last_nudge = 0
        self.close_nudge()
        self.refresh()

    def resume(self):
        pause.resume(config.spool_dir(self.cfg))
        self.close_nudge()
        self.refresh()

    def pause_state(self):
        return pause.state(config.spool_dir(self.cfg))

    def status_text(self):
        st = self.pause_state()
        text = pause.describe(st, time.time())
        if not self.cfg.get("capture_enabled", True):
            text = "Capture is off (Settings)"
        problem = ""
        if self.capture:
            problem = self.capture.status().get("problem", "")
        elif not st:
            problem = "no capture on this platform"
        if self.vision.status:
            problem = (problem + "; " if problem else "") + self.vision.status
        return text, problem

    def show_nudge(self):
        st = self.pause_state()
        if not st:
            return
        mins = int((time.time() - st["since"]) // 60)
        msg = "Activity Tracker has been paused for %d minutes.\nResume recording?" % mins
        if self.tray:
            try:
                self.tray.notify(msg.replace("\n", " "), APP_TITLE)
            except Exception:
                pass
        if self.nudge_win and self.nudge_win.winfo_exists():
            self.nudge_msg.config(text=msg)
            self.nudge_win.lift()
            return
        w = tk.Toplevel(self.root)
        w.title("Still paused")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=ui.px(16))
        frm.pack(fill="both", expand=True)
        self.nudge_msg = ttk.Label(frm, text=msg, justify="left")
        self.nudge_msg.pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(ui.px(12), 0))
        ttk.Button(row, text="Resume now", command=self.resume).pack(side="left")
        repeat = int(self.cfg.get("nudge_repeat_min") or 15)
        ttk.Button(row, text="Remind me in %d min" % repeat, command=self.close_nudge).pack(side="left", padx=ui.px(6))
        ttk.Button(row, text="Pause 1 hour", command=lambda: self.pause_for(60)).pack(side="left")
        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_width() - ui.px(40)
        y = w.winfo_screenheight() - w.winfo_height() - ui.px(110)
        w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        w.protocol("WM_DELETE_WINDOW", self.close_nudge)
        self.nudge_win = w

    def close_nudge(self):
        if self.nudge_win is not None:
            try:
                self.nudge_win.destroy()
            except tk.TclError:
                pass
            self.nudge_win = None

    # ---------------- tray / menu bar ----------------
    def _menu_entries(self):
        st = self.pause_state()
        text, problem = self.status_text()
        entries = [(text, None)]
        if problem:
            entries.append(("⚠ " + problem[:70], None))
        entries.append(None)
        if st:
            entries.append(("Resume recording", lambda: self.ui_q.put(("resume",))))
        entries.append(("Pause", [("For 15 minutes", lambda: self.ui_q.put(("pause_for", 15))),
                                  ("For 1 hour", lambda: self.ui_q.put(("pause_for", 60))),
                                  ("For 4 hours", lambda: self.ui_q.put(("pause_for", 240))),
                                  ("Until I resume", lambda: self.ui_q.put(("pause_for", 0)))]))
        entries += [None,
                    ("Open Activity Tracker", lambda: self.ui_q.put(("show",))),
                    ("Write a summary now", lambda: self.ui_q.put(("summary_now",))),
                    ("Settings…", lambda: self.ui_q.put(("show", "models"))),
                    ("Check for updates…", lambda: self.ui_q.put(("check_updates",))),
                    None,
                    ("Quit", lambda: self.ui_q.put(("quit",)))]
        return entries

    def _start_tray(self):
        if IS_MAC:
            try:
                from .menubar_mac import MenuBar

                self.menubar = MenuBar("●")
                self._refresh_menubar()
            except Exception:
                log.exception("menu-bar item unavailable; using the window only")
                self.menubar = None
            return
        try:
            import pystray

            from . import icons
        except Exception:
            log.info("pystray unavailable; the window is the only way in")
            return

        def build(entries):
            items = []
            for e in entries:
                if e is None:
                    items.append(pystray.Menu.SEPARATOR)
                elif isinstance(e[1], list):
                    items.append(pystray.MenuItem(e[0], pystray.Menu(*build(e[1]))))
                elif e[1] is None:
                    items.append(pystray.MenuItem(e[0], None, enabled=False))
                else:
                    items.append(pystray.MenuItem(e[0], (lambda f: lambda icon, item: f())(e[1]),
                                                  default=e[0] == "Open Activity Tracker"))
            return items

        self._pystray_build = lambda: pystray.Menu(*build(self._menu_entries()))
        self._icons = icons
        self.tray = pystray.Icon("ActivityTracker", icons.tray_image("recording"), APP_TITLE,
                                 self._pystray_build())
        self.tray.run_detached()

    def _refresh_menubar(self):
        text, problem = self.status_text()
        st = self.pause_state()
        sig = (text, problem, bool(st))
        if sig == self._menu_sig:
            return
        self._menu_sig = sig
        if self.menubar:
            self.menubar.set_title("⏸" if st else ("⚠" if problem else "●"))
            self.menubar.set_items(self._menu_entries())
        if self.tray:
            self.tray.icon = self._icons.tray_image("paused" if st else "problem" if problem else "recording")
            self.tray.title = "%s: %s" % (APP_TITLE, text)
            self.tray.menu = self._pystray_build()
            try:
                self.tray.update_menu()
            except Exception:
                pass

    # ---------------- window ----------------
    def _build_window(self):
        outer = ttk.Frame(self.root, padding=ui.px(10))
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x")
        self.status_var = tk.StringVar()
        self.problem_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, font=("TkDefaultFont", 13, "bold")).pack(side="left")
        self.resume_btn = ttk.Button(top, text="Resume", command=self.resume)
        self.resume_btn.pack(side="right")
        pm = ttk.Menubutton(top, text="Pause ▾")
        menu = tk.Menu(pm, tearoff=False)
        for label, mins in (("For 15 minutes", 15), ("For 1 hour", 60), ("For 4 hours", 240), ("Until I resume", 0)):
            menu.add_command(label=label, command=lambda m=mins: self.pause_for(m))
        pm["menu"] = menu
        pm.pack(side="right", padx=ui.px(6))
        ttk.Label(outer, textvariable=self.problem_var, foreground="#b35c00", wraplength=ui.px(900)).pack(fill="x")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, pady=(ui.px(8), 0))
        self.tabs = {}
        for key, title, build in (("today", "Today", self._build_today), ("models", "Models", self._build_models),
                                  ("capture", "Capture & privacy", self._build_capture),
                                  ("about", "About & updates", self._build_about)):
            frame = ttk.Frame(self.notebook, padding=ui.px(10))
            self.notebook.add(frame, text=title)
            self.tabs[key] = frame
            build(frame if key == "today" else self._scrollable(frame))
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(ui.px(8), 0))
        self.saved_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.saved_var).pack(side="left")
        ttk.Button(bottom, text="Save settings", command=self.save_settings).pack(side="right")
        self.load_settings()

    def _scrollable(self, parent):
        """A frame inside a canvas, so long settings pages scroll instead of being cut off."""
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(event):
            if str(canvas.winfo_containing(event.x_root, event.y_root)).startswith(str(canvas)):
                step = -1 * (event.delta // 120 if IS_WIN else event.delta) if event.delta else (1 if event.num == 5 else -1)
                canvas.yview_scroll(step, "units")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, wheel, add="+")
        return inner

    def _build_today(self, f):
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh_timeline).pack(side="left")
        ttk.Button(bar, text="Write a summary now", command=self.summary_now).pack(side="left", padx=ui.px(6))
        ttk.Button(bar, text="Open summaries folder", command=lambda: open_path(paths.sub("summaries"))).pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=28)
        ent.pack(side="right")
        ent.bind("<Return>", lambda e: self.refresh_timeline())
        ttk.Label(bar, text="Search:").pack(side="right", padx=ui.px(4))
        pane = ttk.PanedWindow(f, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(ui.px(8), 0))
        cols = ("time", "app", "window", "dur", "what")
        tv = ttk.Treeview(pane, columns=cols, show="headings", height=14)
        for c, title, w in (("time", "Time", 60), ("app", "App", 120), ("window", "Window", 220),
                            ("dur", "Time on it", 80), ("what", "What you were doing", 480)):
            tv.heading(c, text=title)
            tv.column(c, width=ui.px(w), stretch=c in ("window", "what"))
        self.timeline = tv
        pane.add(tv, weight=3)
        self.summary_text = tk.Text(pane, height=10, wrap="word", relief="flat")
        pane.add(self.summary_text, weight=2)

    def _row(self, parent, label, widget, note=None):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=ui.px(3))
        ttk.Label(r, text=label, width=24).pack(side="left", anchor="n")
        widget(r).pack(side="left", fill="x", expand=True)
        if note:
            ttk.Label(parent, text=note, foreground="#666", wraplength=ui.px(820), justify="left").pack(
                fill="x", padx=(ui.px(190), 0))

    def _build_models(self, f):
        v = self.vars = {}
        for k in ("vision_mode", "local_endpoint", "local_vision_model", "local_text_model", "online_provider",
                  "online_model", "online_base_url", "api_key", "summary_engine", "claude_model", "codex_path",
                  "claude_path"):
            v[k] = tk.StringVar()
        for k in ("summary_interval_min", "daily_summary_hour", "frame_interval_s", "idle_seconds",
                  "retention_days", "text_retention_days", "nudge_after_min", "nudge_repeat_min"):
            v[k] = tk.IntVar()
        for k in ("capture_enabled", "diff_include_text", "nudge_timed_pauses", "start_at_login",
                  "auto_update_check"):
            v[k] = tk.BooleanVar()
        v["capture_scope"] = tk.StringVar()
        v["sensitive_keywords"] = tk.StringVar()

        box = ttk.LabelFrame(f, text="Describe each capture with", padding=ui.px(8))
        box.pack(fill="x")
        for key, label in VISION_LABELS:
            ttk.Radiobutton(box, text=label, value=key, variable=v["vision_mode"],
                            command=self._update_egress).pack(anchor="w")
        self.egress_var = tk.StringVar()
        ttk.Label(box, textvariable=self.egress_var, foreground="#1f5fa8", wraplength=ui.px(880),
                  justify="left").pack(fill="x", pady=(ui.px(6), 0))

        loc = ttk.LabelFrame(f, text="Local model (Ollama, free, offline)", padding=ui.px(8))
        loc.pack(fill="x", pady=(ui.px(8), 0))
        self._row(loc, "Ollama address", lambda p: ttk.Entry(p, textvariable=v["local_endpoint"]))
        self.local_models_cb = None

        def vision_combo(p):
            self.local_models_cb = ttk.Combobox(p, textvariable=v["local_vision_model"])
            return self.local_models_cb
        self._row(loc, "Vision model", vision_combo,
                  "Install Ollama from ollama.com, then run: ollama pull qwen2.5vl:3b (fast) or qwen2.5vl:7b (better).")
        self._row(loc, "Text model (optional)", lambda p: ttk.Entry(p, textvariable=v["local_text_model"]),
                  "Used for summaries and for merging in Mix mode. Blank = the vision model.")
        ttk.Button(loc, text="Test local model", command=lambda: self.test("local")).pack(anchor="e")

        onl = ttk.LabelFrame(f, text="Online model", padding=ui.px(8))
        onl.pack(fill="x", pady=(ui.px(8), 0))
        labels = [p["label"] for p in models.ONLINE_PRESETS.values()]
        self._row(onl, "Provider", lambda p: ttk.Combobox(p, textvariable=v["online_provider"], values=labels,
                                                          state="readonly"))
        v["online_provider"].trace_add("write", lambda *a: self._update_key_state())
        self._row(onl, "Model", lambda p: ttk.Entry(p, textvariable=v["online_model"]),
                  "Blank = the provider's default (OpenAI gpt-5.4-mini, Gemini gemini-3.8-flash, Groq qwen/qwen3.8-27b). "
                  "Must accept images.")
        self._row(onl, "Server address", lambda p: ttk.Entry(p, textvariable=v["online_base_url"]),
                  "Custom provider only, e.g. http://localhost:1234/v1 for LM Studio.")
        self.key_state = tk.StringVar()
        self._row(onl, "API key", lambda p: ttk.Entry(p, textvariable=v["api_key"], show="•"))
        kr = ttk.Frame(onl)
        kr.pack(fill="x")
        ttk.Label(kr, textvariable=self.key_state, foreground="#666").pack(side="left", padx=(ui.px(190), 0))
        ttk.Button(kr, text="Get a key", command=self._open_key_page).pack(side="right")
        ttk.Button(kr, text="Test online model", command=lambda: self.test("online")).pack(side="right",
                                                                                         padx=ui.px(6))

        summ = ttk.LabelFrame(f, text="Write summaries with", padding=ui.px(8))
        summ.pack(fill="x", pady=(ui.px(8), 0))
        self._row(summ, "Summaries", lambda p: ttk.Combobox(p, textvariable=v["summary_engine"],
                                                            values=[l for _, l in SUMMARY_LABELS], state="readonly"))
        v["summary_engine"].trace_add("write", lambda *a: self._update_egress())
        self._row(summ, "Claude model", lambda p: ttk.Entry(p, textvariable=v["claude_model"]),
                  "For Claude Code: haiku, sonnet or opus.")
        self._row(summ, "Every (minutes)", lambda p: ttk.Spinbox(p, from_=0, to=1440, textvariable=v["summary_interval_min"], width=8),
                  "0 = only the end-of-day summary.")
        self._row(summ, "Day summary at (hour)", lambda p: ttk.Spinbox(p, from_=0, to=23, textvariable=v["daily_summary_hour"], width=8))
        tr = ttk.Frame(summ)
        tr.pack(fill="x")
        ttk.Button(tr, text="Test summary engine", command=lambda: self.test("summary")).pack(side="right")

    def _build_capture(self, f):
        v = self.vars
        cap = ttk.LabelFrame(f, text="Capture", padding=ui.px(8))
        cap.pack(fill="x")
        ttk.Checkbutton(cap, text="Capture activity", variable=v["capture_enabled"]).pack(anchor="w")
        self._row(cap, "What to capture", lambda p: ttk.Combobox(p, textvariable=v["capture_scope"],
                                                                 values=[l for _, l in SCOPE_LABELS], state="readonly"))
        self._row(cap, "Describe at most every (s)", lambda p: ttk.Spinbox(p, from_=5, to=600, textvariable=v["frame_interval_s"], width=8),
                  "Only when a model is chosen. A window is captured again only when it changed.")
        self._row(cap, "Stop when idle for (s)", lambda p: ttk.Spinbox(p, from_=30, to=3600, textvariable=v["idle_seconds"], width=8))

        pz = ttk.LabelFrame(f, text="Pause reminder", padding=ui.px(8))
        pz.pack(fill="x", pady=(ui.px(8), 0))
        self._row(pz, "Remind me after (min)", lambda p: ttk.Spinbox(p, from_=0, to=480, textvariable=v["nudge_after_min"], width=8),
                  "When recording has been paused this long, a reminder offers to resume. 0 = never.")
        self._row(pz, "Then every (min)", lambda p: ttk.Spinbox(p, from_=1, to=480, textvariable=v["nudge_repeat_min"], width=8))
        ttk.Checkbutton(pz, text='Also remind during timed pauses ("pause for 1 hour")',
                        variable=v["nudge_timed_pauses"]).pack(anchor="w")

        pv = ttk.LabelFrame(f, text="Privacy", padding=ui.px(8))
        pv.pack(fill="x", pady=(ui.px(8), 0))
        ttk.Label(pv, wraplength=ui.px(880), justify="left", text=(
            "Blocked apps and windows are never captured: no title, no text, no screenshot. The default list covers "
            "password managers, sign-in pages, banks and private browsing windows. Add your own in the blocklist file.")
        ).pack(fill="x")
        br = ttk.Frame(pv)
        br.pack(fill="x", pady=ui.px(4))
        ttk.Button(br, text="Edit blocklist", command=lambda: open_path(blocklist.user_path(config.spool_dir(self.cfg)))).pack(side="left")
        ttk.Button(br, text="Restart capture", command=self.restart_capture).pack(side="left", padx=ui.px(6))
        ttk.Button(br, text="Open data folder", command=lambda: open_path(paths.data_dir())).pack(side="left")
        ttk.Checkbutton(pv, text="Show the lines that appeared on screen in the timeline (scrubbed of keys and passwords)",
                        variable=v["diff_include_text"]).pack(anchor="w")
        self._row(pv, "Extra sensitive words", lambda p: ttk.Entry(p, textvariable=v["sensitive_keywords"]),
                  "Mix mode: a capture whose description mentions any of these (comma-separated) stays on this computer.")
        self._row(pv, "Keep timeline for (days)", lambda p: ttk.Spinbox(p, from_=1, to=3650, textvariable=v["retention_days"], width=8))
        self._row(pv, "Keep screen text for (days)", lambda p: ttk.Spinbox(p, from_=1, to=3650, textvariable=v["text_retention_days"], width=8))
        if IS_MAC:
            mp = ttk.LabelFrame(f, text="macOS permissions", padding=ui.px(8))
            mp.pack(fill="x", pady=(ui.px(8), 0))
            ttk.Label(mp, wraplength=ui.px(880), justify="left", text=(
                "Activity Tracker needs Screen Recording (to read the focused window) and Accessibility (for window "
                "titles). Turn both on for Activity Tracker, then press Restart capture.")).pack(fill="x")
            from . import capture_mac
            ttk.Button(mp, text="Open Screen Recording settings",
                       command=lambda: capture_mac.open_permission_settings("screen")).pack(side="left")
            ttk.Button(mp, text="Open Accessibility settings",
                       command=lambda: capture_mac.open_permission_settings("accessibility")).pack(side="left", padx=ui.px(6))

    def _build_about(self, f):
        v = self.vars
        ttk.Label(f, text="Activity Tracker %s" % __version__, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(f, text="Data folder: %s" % paths.data_dir(), foreground="#666").pack(anchor="w")
        ttk.Checkbutton(f, text="Start Activity Tracker when I sign in", variable=v["start_at_login"]).pack(anchor="w", pady=(ui.px(8), 0))
        ttk.Checkbutton(f, text="Check for updates automatically (once a day)", variable=v["auto_update_check"]).pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=ui.px(8))
        ttk.Button(r, text="Check for updates now", command=self.check_updates).pack(side="left")
        ttk.Button(r, text="Releases page", command=lambda: webbrowser.open(update.RELEASES_PAGE)).pack(side="left", padx=ui.px(6))
        self.update_var = tk.StringVar()
        ttk.Label(f, textvariable=self.update_var, wraplength=ui.px(880), justify="left").pack(fill="x")

    # ---------------- settings form ----------------
    def load_settings(self):
        c, v = self.cfg, self.vars
        v["vision_mode"].set(c["vision_mode"])
        for k in ("local_endpoint", "local_vision_model", "local_text_model", "online_model", "online_base_url",
                  "claude_model", "codex_path", "claude_path"):
            v[k].set(c.get(k) or "")
        v["online_provider"].set(models.ONLINE_PRESETS.get(c["online_provider"], models.ONLINE_PRESETS["openai"])["label"])
        v["summary_engine"].set(_label(SUMMARY_LABELS, c["summary_engine"]))
        v["capture_scope"].set(_label(SCOPE_LABELS, c["capture_scope"]))
        for k in ("summary_interval_min", "daily_summary_hour", "frame_interval_s", "idle_seconds", "retention_days",
                  "text_retention_days", "nudge_after_min", "nudge_repeat_min"):
            v[k].set(int(c[k]))
        for k in ("capture_enabled", "diff_include_text", "nudge_timed_pauses", "auto_update_check"):
            v[k].set(bool(c[k]))
        v["start_at_login"].set(autostart.is_enabled())
        v["sensitive_keywords"].set(", ".join(c.get("sensitive_keywords") or []))
        v["api_key"].set("")
        self._update_key_state()
        self._update_egress()

    def _provider_key(self):
        label = self.vars["online_provider"].get()
        return next((k for k, p in models.ONLINE_PRESETS.items() if p["label"] == label), "openai")

    def _update_key_state(self):
        p = models.ONLINE_PRESETS[self._provider_key()]
        if not p["key_name"]:
            self.key_state.set("No key needed: uses the Codex CLI's ChatGPT sign-in (run `codex login` once).")
        else:
            have = bool(keystore.get(p["key_name"]))
            self.key_state.set(("A key is saved for this provider (type a new one to replace it)." if have
                                else "No key saved for this provider yet.")
                               + " Keys are kept in the system keychain.")

    def _open_key_page(self):
        url = models.ONLINE_PRESETS[self._provider_key()]["key_url"]
        if url:
            webbrowser.open(url)

    def _update_egress(self):
        mode = self.vars["vision_mode"].get()
        provider = self.vars["online_provider"].get()
        eng = _key(SUMMARY_LABELS, self.vars["summary_engine"].get())
        lines = {
            "off": "Captures: on-screen text is read on this computer. Nothing is sent anywhere.",
            "local": "Captures: described by the local model. Nothing leaves this computer.",
            "online": "Captures: a screenshot of every captured window is uploaded to %s." % provider,
            "hybrid": ("Captures: the local model sees every window first. Windows it judges sensitive (passwords, "
                       "bank details, private messages...) stay on this computer; the rest are also uploaded to "
                       "%s. If the local model is not running, nothing is uploaded." % provider),
        }
        summ = {"off": "Summaries: written on this computer without a model.",
                "local": "Summaries: written by the local model on this computer.",
                "online": "Summaries: the text timeline (not screenshots) is sent to %s." % provider,
                "claude": "Summaries: the text timeline is sent to Anthropic through Claude Code.",
                "codex": "Summaries: the text timeline is sent to OpenAI through the Codex CLI."}
        self.egress_var.set(lines.get(mode, "") + "\n" + summ.get(eng, ""))

    def collect(self):
        c, v = dict(self.cfg), self.vars
        c["vision_mode"] = v["vision_mode"].get() or "off"
        for k in ("local_endpoint", "local_vision_model", "local_text_model", "online_model", "online_base_url",
                  "claude_model", "codex_path", "claude_path"):
            c[k] = v[k].get().strip()
        c["local_endpoint"] = c["local_endpoint"] or config.DEFAULTS["local_endpoint"]
        c["local_vision_model"] = c["local_vision_model"] or config.DEFAULTS["local_vision_model"]
        c["online_provider"] = self._provider_key()
        c["summary_engine"] = _key(SUMMARY_LABELS, v["summary_engine"].get())
        c["capture_scope"] = _key(SCOPE_LABELS, v["capture_scope"].get())
        for k in ("summary_interval_min", "daily_summary_hour", "frame_interval_s", "idle_seconds", "retention_days",
                  "text_retention_days", "nudge_after_min", "nudge_repeat_min"):
            try:
                c[k] = int(v[k].get())
            except (tk.TclError, ValueError):
                pass
        for k in ("capture_enabled", "diff_include_text", "nudge_timed_pauses", "auto_update_check"):
            c[k] = bool(v[k].get())
        c["sensitive_keywords"] = [w.strip() for w in v["sensitive_keywords"].get().split(",") if w.strip()]
        return c

    def save_settings(self):
        new = self.collect()
        key = self.vars["api_key"].get().strip()
        if key:
            name = models.ONLINE_PRESETS[new["online_provider"]]["key_name"]
            if name:
                where = keystore.set(name, key)
                self.vars["api_key"].set("")
                if where == "file":
                    messagebox.showwarning(APP_TITLE, "No system keychain is available, so the key was saved in "
                                                      "the data folder (secrets.json).")
        if new["vision_mode"] in ("online", "hybrid") and self.cfg["vision_mode"] not in ("online", "hybrid"):
            if not messagebox.askyesno(APP_TITLE, "With this setting, screenshots of the windows you use are sent to "
                                                  "an online model (see the note under the options). Continue?"):
                return
        try:
            if bool(self.vars["start_at_login"].get()) != autostart.is_enabled():
                autostart.set_enabled(bool(self.vars["start_at_login"].get()))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Couldn't change start-at-login: %s" % exc)
        restart = any(new[k] != self.cfg[k] for k in ("vision_mode", "frame_interval_s", "idle_seconds",
                                                       "capture_scope", "capture_enabled"))
        self.cfg = new
        config.save(new)
        if self.capture and restart:
            if new["capture_enabled"]:
                self.capture.stop()
                self.capture.start()
            else:
                self.capture.stop()
        self._update_key_state()
        self._update_egress()
        self.saved_var.set("Saved at %s" % time.strftime("%H:%M:%S"))
        self.refresh()

    def restart_capture(self):
        if self.capture:
            self.capture.stop()
            if self.cfg.get("capture_enabled", True):
                self.capture.start()
        self.saved_var.set("Capture restarted")

    def test(self, kind):
        cfg = self.collect()
        key = self.vars["api_key"].get().strip()

        def get_key(name):
            return key or keystore.get(name)

        if kind == "summary":
            kind = cfg["summary_engine"]
            if kind == "off":
                messagebox.showinfo(APP_TITLE, "No model is used for summaries.")
                return
        self.saved_var.set("Testing %s…" % kind)

        def work():
            ok, msg = models.test_connection(kind, cfg, get_key)
            names = None
            if kind == "local":
                try:
                    names = models.ollama_models(cfg)
                except Exception:
                    pass
            self.ui_q.put(("_test_done", kind, ok, msg, names))
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, kind, ok, msg, names):
        self.saved_var.set("")
        if names and self.local_models_cb is not None:
            self.local_models_cb["values"] = names
        (messagebox.showinfo if ok else messagebox.showerror)(APP_TITLE, "%s: %s" % (kind, msg))

    # ---------------- timeline ----------------
    def refresh(self):
        text, problem = self.status_text()
        self.status_var.set(text)
        self.problem_var.set(problem)
        if self.pause_state():
            self.resume_btn.state(["!disabled"])
        else:
            self.resume_btn.state(["disabled"])
        if self.menubar or self.tray:
            self._refresh_menubar()

    def refresh_timeline(self):
        tv = self.timeline
        tv.delete(*tv.get_children())
        q = self.search_var.get().strip()
        if q:
            rows = [(store.from_utc_text(e["timestamp"]), e) for e in store.search(q)]
            for t, e in rows:
                m = e["meta"]
                tv.insert("", "end", values=(t.strftime("%m-%d %H:%M"), m.get("proc", ""), m.get("title", "")[:80],
                                             e["event_type"], " ".join(e["content"].split())[:300]))
        else:
            start, end = summary.day_bounds(dt.datetime.now().astimezone())
            sess = summary.sessions(store.events_between(start, end, ("focus", "focus_end", "ocr", "vision")))
            for s in reversed(sess):
                what = s["notes"][-1][1] if s["notes"] else " ".join(s["text"].split())[:200]
                tv.insert("", "end", values=(s["start"].strftime("%H:%M"), s["proc"], s["title"][:80],
                                             summary.fmt_duration(s["seconds"]), what[:300]))
        path = summary.summary_path(dt.date.today())
        self.summary_text.config(state="normal")
        self.summary_text.delete("1.0", "end")
        try:
            with open(path, encoding="utf-8") as fh:
                self.summary_text.insert("1.0", fh.read())
        except OSError:
            self.summary_text.insert("1.0", "No summary yet today. Summaries are written every %s minutes and at the "
                                            "end of the day; press 'Write a summary now' for one." %
                                     self.cfg.get("summary_interval_min"))
        self.summary_text.config(state="disabled")

    def summary_now(self):
        self.saved_var.set("Writing a summary…")
        cfg = dict(self.cfg)

        def work():
            now = dt.datetime.now().astimezone()
            start, _ = summary.day_bounds(now)
            try:
                summary.write_summary(cfg, keystore.get, start, now, "today so far",
                                      "Today so far (%s)" % now.strftime("%H:%M"))
                self.ui_q.put(("_summary_done", ""))
            except Exception as exc:
                self.ui_q.put(("_summary_done", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _summary_done(self, error):
        self.saved_var.set("Summary failed: %s" % error[:100] if error else "Summary written")
        self.show("today")

    # ---------------- updates ----------------
    def check_updates(self, quiet=False):
        self.update_var.set("Checking GitHub for a newer version…")

        def work():
            try:
                info = update.check()
                self.ui_q.put(("_update_checked", info, None, quiet))
            except Exception as exc:
                self.ui_q.put(("_update_checked", None, str(exc), quiet))
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, info, error, quiet):
        self.cfg["last_update_check"] = time.time()
        config.save(self.cfg)
        if error:
            self.update_var.set("Couldn't check for updates: %s" % error)
            return
        if not info["available"]:
            self.update_var.set("You have the latest version (%s)." % info["current"])
            if not quiet:
                messagebox.showinfo(APP_TITLE, "You have the latest version (%s)." % info["current"])
            return
        self.update_var.set("Version %s is available (you have %s)." % (info["latest"], info["current"]))
        if info["kind"] == "source" or not info["asset"]:
            if messagebox.askyesno(APP_TITLE, "Activity Tracker %s is available. Open the download page?" % info["latest"]):
                webbrowser.open(info["page"])
            return
        notes = (info["notes"] or "")[:600]
        if messagebox.askyesno(APP_TITLE, "Activity Tracker %s is available (you have %s).\n\n%s\n\nInstall it now? "
                                          "The app restarts; your data and settings are kept."
                                          % (info["latest"], info["current"], notes)):
            def work():
                try:
                    done = update.apply(info, lambda msg, frac: self.ui_q.put(
                        ("_update_progress", "%s %d%%" % (msg, frac * 100))))
                    self.ui_q.put(("_update_applied", done, None))
                except Exception as exc:
                    self.ui_q.put(("_update_applied", False, str(exc)))
            threading.Thread(target=work, daemon=True).start()

    def _update_progress(self, text):
        self.update_var.set(text)
        self.saved_var.set(text)

    def _update_applied(self, done, error):
        if error:
            messagebox.showerror(APP_TITLE, "Update failed: %s" % error)
        elif done:
            self.quit()

    # ---------------- CI smoke test ----------------
    def _smoke(self):
        result = {"ok": False}
        try:
            self.show("today")
            self.refresh_timeline()
            self.notebook.select(self.tabs["models"])
            self.root.update()
            pause.pause(config.spool_dir(self.cfg), 0, now=time.time() - 3600)
            self.show_nudge()
            self.root.update()
            result["nudge_shown"] = bool(self.nudge_win and self.nudge_win.winfo_exists())
            self.resume()
            result["resumed"] = self.pause_state() is None
            result["tray"] = "pystray" if self.tray else "menubar" if self.menubar else "none"
            result["menu"] = [e[0] for e in self._menu_entries() if e]
            result["ok"] = result["nudge_shown"] and result["resumed"] and (IS_MAC or IS_WIN) == (result["tray"] != "none")
        except Exception:
            import traceback

            result["error"] = traceback.format_exc()
        with open(self.smoke_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        self.root.after(500, self.quit)


def main(smoke_out=None):
    App(smoke_out).run()
    return 0
