"""The main window's status bar (app.py): the running job(s) + an
indeterminate progress bar, the queue badge and Queue... button, the
version; refreshed once a second (it also lets the job queue start its next
job) and the job registry's start / end events go to the session log."""
import tkinter as tk
from tkinter import ttk

from .config import APP_VERSION
from .i18n import tr
from .ui import themes
from .ui.dialogs import QueueDialog, _fmt_elapsed
from .ui.icons import decorate
from .ui.tooltips import add_tooltip
from .ui.widgets import elide_label
from . import applog, jobs


class StatusBar:
    """Built at the bottom of `root` (call it before the rest of the window
    is packed). stop_all: the app's Stop all (the Queue window's button)."""

    def __init__(self, root, stop_all):
        self.root = root
        px = themes.px
        # ---- status bar (bottom): running job + progress, queue badge, version ----
        status = ttk.Frame(root, style="Chrome.TFrame", padding=themes.pad(14, 3, 8, 3))
        status.pack(side="bottom", fill="x")
        status_line = tk.Frame(root, height=1, borderwidth=0)
        status_line.pack(side="bottom", fill="x")
        themes.on_palette(status_line, lambda p: status_line.configure(bg=p["stroke"]))
        status_var = tk.StringVar(value=tr("Idle"))
        ttk.Label(status, text=f"v{APP_VERSION}", style="Chrome.Hint.TLabel").pack(
            side="right", padx=themes.pad(10, 6))
        queue_btn = ttk.Button(status, text=tr("Queue..."), style="Chrome.Subtle.TButton",
                               command=lambda: QueueDialog.open_(root, stop_all))
        decorate(queue_btn, "queue")
        queue_btn.pack(side="right")
        add_tooltip(queue_btn, tr("Jobs waiting to run one after another - remove, reorder, "
                                  "pause/resume, Stop all"))
        badge = tk.Label(status, text="", font="MPSemiboldSmall", borderwidth=0,
                         padx=px(6), pady=0)
        themes.on_palette(badge, lambda p: badge.configure(bg=p["accent"], fg=p["accent_fg"]))
        status_prog = ttk.Progressbar(status, style="Chrome.Horizontal.TProgressbar",
                                      mode="indeterminate", length=px(110))
        # the text takes the room left of the right-hand side (packed first,
        # so it always keeps its place); a text too long for it is cut at
        # the END with "…" - never at the start - and its size never depends
        # on the text (the bar and the window don't grow with it)
        elide_label(status, textvariable=status_var, style="Chrome.TLabel").pack(
            side="left", fill="x", expand=True)
        self._status_ui = {"busy": False, "n": 0}
        self.status_var, self.queue_btn = status_var, queue_btn
        self.badge, self.status_prog = badge, status_prog
        self._legacy_busy_tabs = lambda: []
        self._tab_titles = {}

    def start(self, legacy_busy_tabs, tab_titles):
        """Start the refresh loop (legacy_busy_tabs(): the tabs whose Stop
        button is enabled; tab_titles: tab -> its title) and log the jobs."""
        self._legacy_busy_tabs = legacy_busy_tabs
        self._tab_titles = tab_titles
        jobs.subscribe(_on_job_event)
        self.root.after(500, self._update_status)

    def _update_status(self):
        root = self.root
        _status_ui = self._status_ui
        status_var, queue_btn = self.status_var, self.queue_btn
        badge, status_prog = self.badge, self.status_prog
        _tab_titles = self._tab_titles
        try:
            run = jobs.running()
            names = [j["name"] for j in run]
            reg_tabs = {id(j.get("tab")) for j in run}
            for t in self._legacy_busy_tabs():
                if id(t) not in reg_tabs:
                    names.append(tr("{tab} job", tab=_tab_titles.get(t) or tr("a tab")))
            nq = len(jobs.queued())
            if names:
                txt = "▶ " + tr("Running: {jobs}", jobs=", ".join(dict.fromkeys(names)))
                if run:
                    txt += f"  ({_fmt_elapsed(max(j['elapsed'] for j in run))})"
            else:
                txt = tr("Idle")
            if nq:
                txt += "  (" + (tr("{n} more queued, PAUSED", n=nq) if jobs.is_paused()
                                else tr("{n} more queued", n=nq)) + ")"
            status_var.set(txt)
            busy = bool(names)
            if busy != _status_ui["busy"]:
                _status_ui["busy"] = busy
                if busy:
                    status_prog.pack(side="right", padx=themes.pad(10, 10), after=queue_btn)
                    status_prog.start(18)
                else:
                    status_prog.stop()
                    status_prog.pack_forget()
            if nq != _status_ui["n"]:
                _status_ui["n"] = nq
                if nq:
                    badge.configure(text=str(nq))
                    badge.pack(side="right", padx=themes.pad(0, 2), before=queue_btn)
                else:
                    badge.pack_forget()
            jobs.pump()      # start the next queued job once legacy tabs are idle
        except Exception:
            pass
        root.after(1000, self._update_status)


def _on_job_event(event, info):
    if event == "begin" and info:
        applog.record(f"[jobs] started: {info['name']}")
    elif event == "end" and info:
        state = ("stopped" if info.get("stopped") else
                 "finished" if info.get("ok") else "FAILED")
        summ = f" - {info['summary']}" if info.get("summary") else ""
        applog.record(f"[jobs] {state}: {info['name']} after "
                      f"{_fmt_elapsed(info.get('elapsed', 0))}{summ}")
