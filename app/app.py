"""main() - builds the window, the tabs, theme handling, the window icon,
the status bar (job registry / queue / help) and the update banner.
The Queue / Settings dialogs live in ui/dialogs.py."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from .config import (APP_NAME, APP_VERSION, LOGS_DIR, PREFS, WORK_DIRS, load_prefs,
                     load_settings, prefs_snapshot, resource_path, save_settings)
from .tabs.audio_gain import AudioGainTab
from .tabs.check import CheckTab
from .tabs.compare import CompareTab
from .tabs.cut_edit import RemoverTab
from .tabs.log import LogTab
from .tabs.templates import TemplateTab
from .tabs.theme_audio import ThemeAudioTab
from .ui.cleanup import CleanupDialog
from .ui.dialogs import QueueDialog, SettingsDialog, _fmt_elapsed
from .ui import themes
from .ui.dualplayer import DualPlayerTab
from .ui.tkthread import _call_tk, _drain_tk_calls
from .ui.widgets import (InfoTab, KeyedCombobox, RecommendedTab, ScrollFrame, add_tooltip,
                         auto_wrap, make_root, show_help_index)
from . import applog, i18n, jobs, migrate, notify, updater
from .i18n import N_, tr

# ==========================================================================
#  WINDOW SIZE  -  change these two lines to resize the app window.
#  WINDOW_SIZE is the first-run opening size as "WIDTHxHEIGHT" in pixels
#  (at 100% Windows scaling - it grows with the display scaling). After that
#  the last window size/position (and maximized state) is restored.
#  MIN_SIZE is the smallest it can be dragged to (width, height); the tabs
#  scroll when the window is smaller than their content.
# ==========================================================================
WINDOW_SIZE = "1180x710"
MIN_SIZE = (900, 600)

# optional Python packages: (import name, what breaks without it)
_OPTIONAL_PACKAGES = (
    ("tkinterdnd2", N_("drag & drop")),
    ("cv2", N_("video preview")),
    ("PIL", N_("video preview")),
    ("sounddevice", N_("preview sound")),
    ("librosa", N_("intro/credits detection, auto-align")),
    ("numpy", N_("detection")),
    ("scipy", N_("detection")),
)


def _enable_dpi_awareness():
    """Tell Windows the app handles DPI itself, so text is sharp instead of a
    blurry bitmap-stretched window on scaled (125%/150%) displays. Must run
    before the Tk root exists. Tk then sizes its point-based fonts from the
    real DPI on its own; pixel sizes are scaled in main()."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:
        pass


def _missing_packages():
    """[(name, purpose)] of optional packages that aren't installed (checked
    once at startup without importing them)."""
    out = []
    for name, why in _OPTIONAL_PACKAGES:
        try:
            if importlib.util.find_spec(name) is None:
                out.append((name, why))
        except (ImportError, ValueError):
            out.append((name, why))
    return out


def _restore_geometry(root, saved, default_size):
    """Re-open at the saved size/position (clamped onto the current screen);
    first run or an unusable value -> centred at default_size. Returns the
    normal (un-maximized) geometry string it set."""
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(saved.get("window_geometry", "")))
    if not m:
        return _center_window(root, default_size)
    w, h, x, y = (int(v) for v in m.groups())
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    mw, mh = root.minsize()
    w = max(min(w, sw), min(mw, sw))
    h = max(min(h, sh), min(mh, sh))
    x = min(max(0, x), max(0, sw - w))
    y = min(max(0, y), max(0, sh - h))
    geo = f"{w}x{h}+{x}+{y}"
    root.geometry(geo)
    if saved.get("window_zoomed"):
        def _zoom():
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        root.after(50, _zoom)     # once the window is up
    return geo


def _set_window_icon(root):
    """Prefer the .ico (best on Windows); fall back to the .png everywhere else."""
    ico = resource_path("assets/icon.ico")      # app/assets (or the exe bundle)
    png = resource_path("assets/icon.png")
    try:
        if os.path.exists(ico):
            root.iconbitmap(ico)
            return
    except Exception:
        pass
    try:
        if os.path.exists(png):
            root._icon_img = tk.PhotoImage(file=png)   # keep a ref so it survives
            root.iconphoto(True, root._icon_img)
    except Exception:
        pass


def _banner_label(parent, text, kind, **kw):
    """A tk.Label coloured as a banner of `kind` (error / warn / info) that
    follows theme changes."""
    opts = dict(anchor="w", padx=8, pady=3)
    opts.update(kw)
    lbl = tk.Label(parent, text=text, **opts)
    themes.on_palette(lbl, lambda p: lbl.configure(bg=p[f"banner_{kind}_bg"],
                                                   fg=p[f"banner_{kind}_fg"]))
    return lbl


def _center_window(root, size):
    """Place the window of the given "WIDTHxHEIGHT" size in the screen centre."""
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        root.geometry(size)
        return None
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2 - 50)   # 50px above dead centre
    root.geometry(f"{w}x{h}+{x}+{y}")
    return f"{w}x{h}+{x}+{y}"


def _restart_command():
    """argv that starts this app again (frozen exe or python + launcher)."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + sys.argv[1:]
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not script or not os.path.isfile(script):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "intro_credits_toolkit.py")
    return [sys.executable, script] + sys.argv[1:]


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def main():
    # keep full CPU speed when the window is minimized / in the background
    # (Windows 11 otherwise power-throttles the whole process tree)
    from .engine.process import prevent_power_throttling
    prevent_power_throttling()

    # move an old flat layout (videos\, input\, logs\, toolkit_settings.json
    # ...) into Media\ + Data\ BEFORE anything reads or creates those folders
    migrate_msgs = migrate.run()
    # premade working folders (absolute, under Media\ and Data\ next to the app)
    for d in WORK_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    saved = load_settings()
    load_prefs(saved)
    # the UI language (before any window text is built); "" = Windows language
    i18n.set_language(PREFS.get("language", ""))
    applog.init(LOGS_DIR, days=PREFS.get("log_keep_days", applog.LOG_KEEP_DAYS))
    for m in migrate_msgs:
        applog.record(m)

    _enable_dpi_awareness()   # before the Tk root is created
    root = make_root()   # drag-and-drop capable if tkinterdnd2 is installed
    root.title(f"{APP_NAME} v{APP_VERSION}")
    _set_window_icon(root)
    root.resizable(True, True)
    # pixel sizes are given for 100% scaling - grow them with the display DPI
    # (tk's own font scaling is left alone)
    try:
        dpi_f = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        dpi_f = 1.0
    min_w, min_h = (int(v * dpi_f) for v in MIN_SIZE)
    root.minsize(min(min_w, root.winfo_screenwidth()), min(min_h, root.winfo_screenheight()))
    dw, dh = (int(int(v) * dpi_f) for v in WINDOW_SIZE.lower().split("x"))
    default_size = (f"{min(dw, root.winfo_screenwidth())}x"
                    f"{min(dh, root.winfo_screenheight() - 80)}")
    start_geo = _restore_geometry(root, saved, default_size)

    # remember the last NORMAL (not maximized) geometry, so a maximized
    # window still restores to a sensible size when un-maximized next time
    normal_geo = {"g": start_geo}

    def _track_geometry(e):
        if e.widget is root and root.state() == "normal" and root.winfo_width() > 200:
            normal_geo["g"] = root.geometry()
    root.bind("<Configure>", _track_geometry, add="+")

    # warn up front if ffmpeg/ffprobe are missing (most actions need them)
    # and which optional packages aren't installed (checked once)
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    missing_pkgs = _missing_packages()
    if missing:
        tools = " and ".join(missing)
        applog.record(f"[startup] {tools} not found on PATH - install ffmpeg (ffmpeg.org) "
                      "so cutting and detection work.")
        msg = tr("{tools} not found on PATH - install ffmpeg (ffmpeg.org) so cutting and "
                 "detection work.", tools=" / ".join(missing))
        auto_wrap(_banner_label(root, "  \u26a0  " + msg, "error", justify="left"),
                  margin=20).pack(fill="x")
    if missing_pkgs:
        names = ", ".join(n for n, _w in missing_pkgs)
        whats = ", ".join(dict.fromkeys(w for _n, w in missing_pkgs))
        applog.record(f"[startup] Optional packages missing: {names} ({whats} won't work) - "
                      "run Install Requirements.bat.")
        whats = ", ".join(dict.fromkeys(tr(w) for _n, w in missing_pkgs))
        msg = tr("Optional packages missing: {names} ({features} won't work) - run "
                 "Install Requirements.bat.", names=names, features=whats)
        auto_wrap(_banner_label(root, "  \u26a0  " + msg, "warn", justify="left"),
                  margin=20).pack(fill="x")

    # themes: one shared variable for the top-bar picker and the Settings
    # dialog; writing it re-themes the whole app live (themes.set_theme)
    theme_var = themes.init(root, saved.get("theme", themes.DEFAULT_THEME))

    # top bar with theme selector
    top = ttk.Frame(root, padding=(10, 6, 10, 0))
    top.pack(fill="x")
    ttk.Label(top, text=tr("Theme:")).pack(side="left")
    theme_box = KeyedCombobox(top, textvariable=theme_var, values=themes.THEME_NAMES,
                              state="readonly", width=20)
    theme_box.pack(side="left", padx=6)
    add_tooltip(theme_box, tr("Colour theme - applies to every window right away. "
                              "'Follow Windows' switches Light / Dark with Windows."))
    cleanup_btn = ttk.Button(top, text=tr("Clean up folders..."),
                             command=lambda: CleanupDialog(root, is_busy=_any_busy))
    cleanup_btn.pack(side="right")
    add_tooltip(cleanup_btn, tr("Empty the template/videos/output/temp folders - contents are "
                                "moved to Data\\temp\\trash, not deleted, so they can be "
                                "recovered"))
    settings_btn = ttk.Button(top, text=tr("Settings..."),
                              command=lambda: SettingsDialog.open_(root, persist, show_update,
                                                                   restart=_restart))
    settings_btn.pack(side="right", padx=(0, 6))
    add_tooltip(settings_btn, tr("Language, theme, notifications, update check and log "
                                 "retention"))

    # Tk-thread dispatcher for the job registry / notifications / updater
    jobs.set_dispatcher(_call_tk)
    root.after(50, _drain_tk_calls, root)
    notify.install(root, _call_tk)

    # ---- status bar (bottom): running job, queue, help, version ----
    status = ttk.Frame(root, padding=(10, 2, 10, 3))
    status.pack(side="bottom", fill="x")
    ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")
    status_var = tk.StringVar(value=tr("Idle"))
    ttk.Label(status, textvariable=status_var, anchor="w").pack(side="left", fill="x",
                                                                expand=True)
    ttk.Label(status, text=f"v{APP_VERSION}", style="Hint.TLabel").pack(side="right")
    help_btn = ttk.Button(status, text=tr("Help"),
                          command=lambda: show_help_index(root))
    help_btn.pack(side="right", padx=(0, 8))
    add_tooltip(help_btn, tr("Help for every tab"))
    queue_btn = ttk.Button(status, text=tr("Queue..."),
                           command=lambda: QueueDialog.open_(root, _stop_all_jobs))
    queue_btn.pack(side="right", padx=(0, 6))
    add_tooltip(queue_btn, tr("Jobs waiting to run one after another - remove, reorder, "
                              "pause/resume, Stop all"))

    # main tabs grouped by workflow: Templates, Cut / Edit, Audio
    # (Theme Audio, Audio Gain), Inspect (Dual Player, Compare, Check), Log,
    # Info (Info / Settings, Recommended). The tab objects and their attribute
    # names are the same as before the grouping - only their parent changed.
    nb = ttk.Notebook(root)
    _main_pages = {}          # settings key -> main page widget
    _sub_books = {}           # settings key -> nested notebook

    def scrolled(factory, title, book=None):
        """Build a tab inside a ScrollFrame so it scrolls when the window is
        smaller than the tab's content. The factory gets (parent, bottom):
        `bottom` is a non-scrolling strip pinned to the window bottom (used
        for progress bars that must stay visible). book = the notebook to add
        it to (default: the main one). Returns the tab instance."""
        book = book or nb
        holder = ScrollFrame(book)
        tab = factory(holder.interior, holder.bottom)
        tab.pack(fill="both", expand=True)
        book.add(holder, text=_tab_text(title))
        return tab

    def group(key, title):
        """A main tab holding a nested notebook (the group's sub-tabs)."""
        sub = ttk.Notebook(nb)
        nb.add(sub, text=_tab_text(title))
        _main_pages[key] = sub
        _sub_books[key] = sub
        return sub

    def _tab_text(title):
        """Notebook tab caption: the translated title with a little air."""
        return f"  {tr(title)}  "

    def _last_page(key):
        _main_pages[key] = nb.tabs()[-1]

    template_tab = scrolled(lambda m, b: TemplateTab(m, saved=saved, bottom=b),
                            N_("Templates"))
    _last_page("templates")
    remover_tab = scrolled(lambda m, b: RemoverTab(m, saved=saved, bottom=b),
                           N_("Cut / Edit"))
    _last_page("cut")
    audio_nb = group("audio", N_("Audio"))
    theme_tab = scrolled(lambda m, b: ThemeAudioTab(m, saved=saved), N_("Theme Audio"),
                         audio_nb)
    gain_tab = scrolled(lambda m, b: AudioGainTab(m, saved=saved), N_("Audio Gain"),
                        audio_nb)
    inspect_nb = group("inspect", N_("Inspect"))
    dual_tab = scrolled(lambda m, b: DualPlayerTab(m), N_("Dual Player"), inspect_nb)
    compare_tab = scrolled(lambda m, b: CompareTab(m), N_("Compare"), inspect_nb)
    check_tab = scrolled(lambda m, b: CheckTab(m, saved=saved), N_("Check"), inspect_nb)
    scrolled(lambda m, b: LogTab(m, LOGS_DIR), N_("Log"))
    _last_page("log")
    _tab_titles = {template_tab: tr("Templates"), remover_tab: tr("Cut / Edit"),
                   theme_tab: tr("Theme Audio"), gain_tab: tr("Audio Gain"),
                   dual_tab: tr("Dual Player"), compare_tab: tr("Compare"),
                   check_tab: tr("Check")}
    info_nb = group("info", N_("Info"))
    info_nb.add(InfoTab(info_nb), text=_tab_text(N_("Info / Settings")))   # scrolls itself
    info_nb.add(RecommendedTab(info_nb), text=_tab_text(N_("Recommended")))
    nb.pack(fill="both", expand=True)

    def _restore_tabs():
        """Re-select last session's main tab and each group's sub-tab."""
        subs = saved.get("ui_sub_tabs")
        if isinstance(subs, dict):
            for key, book in _sub_books.items():
                try:
                    i = int(subs.get(key, 0))
                    if 0 <= i < len(book.tabs()):
                        book.select(i)
                except (TypeError, ValueError, tk.TclError):
                    pass
        page = _main_pages.get(saved.get("ui_main_tab"))
        if page is not None:
            try:
                nb.select(page)
            except tk.TclError:
                pass

    def _tabs_snapshot():
        d = {}
        try:
            cur = nb.select()
            for key, page in _main_pages.items():
                if str(page) == cur:
                    d["ui_main_tab"] = key
            d["ui_sub_tabs"] = {key: book.index("current")
                                for key, book in _sub_books.items()}
        except tk.TclError:
            pass
        return d

    _restore_tabs()

    # let the batch free any video a preview player is holding open, so its
    # 'move finished to done' can move the source (Windows locks open files)
    _players = [template_tab.player, getattr(template_tab, "tm_player", None),
                remover_tab.player, getattr(remover_tab, "multi_player", None),
                theme_tab.player, getattr(dual_tab, "pA", None),
                getattr(dual_tab, "pB", None)]

    def _unload_all_players():
        # the Audition window (Templates -> Auto-detect) is created on demand
        aud = getattr(template_tab, "_audition", None)
        extra = []
        try:
            if aud is not None and aud.winfo_exists():
                extra.append(getattr(aud, "player", None))
        except Exception:
            pass
        for p in _players + extra:
            if p is not None:
                try:
                    p.unload()
                except Exception:
                    pass

    remover_tab.unload_players_hook = _unload_all_players
    remover_tab.template_tab = template_tab   # presets read its detect settings

    _top_tabs = [template_tab, remover_tab, theme_tab, gain_tab, dual_tab,
                 compare_tab, check_tab]
    # jobs can also live on sub-tab objects (Compare's Quality pane is
    # compare_tab.quality) - include every widget attribute of a tab that has
    # its own Stop button / stop event, one level deep
    _job_tabs = list(_top_tabs)
    for _t in _top_tabs:
        for _v in vars(_t).values():
            if (isinstance(_v, tk.Misc) and _v not in _job_tabs
                    and any(n.endswith("stop_btn") or (
                        "stop" in n and isinstance(x, threading.Event))
                        for n, x in vars(_v).items())):
                _job_tabs.append(_v)
                _tab_titles[_v] = _tab_titles.get(_t, "")

    # Busy / stop: the job registry (jobs.begin/end) is the source of truth;
    # the old Stop-button / stop-event scraping stays as a fallback for tabs
    # not yet registering their jobs.
    def _legacy_busy_tabs():
        """Tabs whose Stop button is enabled (a job is running there)."""
        out = []
        for t in _job_tabs:
            for name, w in vars(t).items():
                if name.endswith("stop_btn"):
                    try:
                        if w.instate(["!disabled"]):
                            out.append(t)
                            break
                    except Exception:
                        pass
        return out

    def _any_busy():
        """True if any job is running (registry OR an enabled Stop button)."""
        return jobs.is_busy() or bool(_legacy_busy_tabs())

    def _stop_all_jobs():
        """Stop every registered job (and pause the queue), then set every
        tab's stop event(s) (stop_event, detect_stop, cut_stop, ...) so the
        workers terminate their ffmpeg processes."""
        jobs.stop_all()
        for t in _job_tabs:
            for name, ev in vars(t).items():
                if "stop" in name and isinstance(ev, threading.Event):
                    ev.set()

    # the queue must also wait for jobs of tabs that don't register yet
    jobs.set_busy_check(lambda: bool(_legacy_busy_tabs()))

    def _update_status():
        try:
            run = jobs.running()
            names = [j["name"] for j in run]
            reg_tabs = {id(j.get("tab")) for j in run}
            for t in _legacy_busy_tabs():
                if id(t) not in reg_tabs:
                    names.append(tr("{tab} job", tab=_tab_titles.get(t) or tr("a tab")))
            nq = len(jobs.queued())
            if names:
                txt = "\u25b6 " + tr("Running: {jobs}", jobs=", ".join(dict.fromkeys(names)))
                if run:
                    txt += f"  ({_fmt_elapsed(max(j['elapsed'] for j in run))})"
            else:
                txt = tr("Idle")
            if nq:
                txt += "  (" + (tr("{n} more queued, PAUSED", n=nq) if jobs.is_paused()
                                else tr("{n} more queued", n=nq)) + ")"
            status_var.set(txt)
            queue_btn.configure(text=tr("Queue ({n})...", n=nq) if nq else tr("Queue..."))
            jobs.pump()      # start the next queued job once legacy tabs are idle
        except Exception:
            pass
        root.after(1000, _update_status)

    def _on_job_event(event, info):
        if event == "begin" and info:
            applog.record(f"[jobs] started: {info['name']}")
        elif event == "end" and info:
            state = ("stopped" if info.get("stopped") else
                     "finished" if info.get("ok") else "FAILED")
            summ = f" - {info['summary']}" if info.get("summary") else ""
            applog.record(f"[jobs] {state}: {info['name']} after "
                          f"{_fmt_elapsed(info.get('elapsed', 0))}{summ}")
    jobs.subscribe(_on_job_event)
    root.after(500, _update_status)

    # ---- update banner (shown only when a newer release exists) ----
    _banner = {"w": None}

    def show_update(ver, url):
        if _banner["w"] is not None:
            try:
                _banner["w"].destroy()
            except tk.TclError:
                pass
        bar = tk.Frame(root)
        _banner_label(bar, "  \u2b06  " + tr("Version {new} is available (you have {old}).",
                                              new=ver, old=APP_VERSION),
                      "info").pack(side="left")
        link = _banner_label(bar, tr("Open download page"), "info", cursor="hand2",
                             font=("Segoe UI", 9, "underline"), padx=0, pady=0)
        link.pack(side="left", padx=6)
        link.bind("<Button-1>", lambda e: _open_url(url))
        close = _banner_label(bar, "\u2715", "info", cursor="hand2", pady=0)
        close.pack(side="right")
        themes.on_palette(bar, lambda p: bar.configure(bg=p["banner_info_bg"]))
        close.bind("<Button-1>", lambda e: bar.destroy())
        bar.pack(fill="x", before=top)
        _banner["w"] = bar

    def persist():
        d = prefs_snapshot()                # app-wide prefs (notify / update / logs)
        d.update(remover_tab.snapshot())
        d.update(template_tab.snapshot())   # Templates tab's Auto-detect settings
        d.update(theme_tab.snapshot())      # Theme Audio format/name/fade/output
        d.update(gain_tab.snapshot())       # Audio Gain batch + single settings
        d.update(check_tab.snapshot())      # Check depth
        d.update(_tabs_snapshot())          # selected main tab + sub-tabs
        d["theme"] = themes.current_name()
        d["last_video"] = (template_tab.file_var.get().strip()
                           or remover_tab.sel_var.get().strip())
        # window size/position + maximized state (clamped to the screen on load)
        try:
            d["window_zoomed"] = root.state() == "zoomed"
            geo = root.geometry() if root.state() == "normal" else normal_geo["g"]
            if geo:
                d["window_geometry"] = geo
        except tk.TclError:
            pass
        save_settings(d)  # theme + settings + last video + window geometry

    # re-walk now that every tab exists, then persist each later theme change
    # (from the top bar, the Settings dialog or Follow Windows)
    themes.set_theme(theme_var.get(), force=True)
    themes.subscribe(lambda _name, _pal: persist())

    # background update check (at most once a day, silent on any failure)
    updater.check_async(PREFS, on_newer=lambda v, url: _call_tk(lambda: show_update(v, url)),
                        save=lambda: _call_tk(persist))

    remover_tab.save_hook = persist   # persist whenever Start is pressed
    template_tab.save_hook = persist  # persist whenever Detect is pressed

    def on_close():
        try:
            persist()
        except Exception:
            pass
        _stop_all_jobs()
        _unload_all_players()
        # give the workers a moment to kill their ffmpeg before the window goes
        root.after(300 if _any_busy() else 0, root.destroy)

    def _restart():
        """Save everything, start a fresh copy of the app and close this one
        (used after the language changed). Running jobs are stopped."""
        if _any_busy():
            from tkinter import messagebox
            if not messagebox.askyesno(
                    tr("Restart now?"),
                    tr("A job is still running. Restarting stops it.\n\nRestart anyway?"),
                    parent=root):
                return False
        try:
            persist()
        except Exception:
            pass
        try:
            subprocess.Popen(_restart_command(), cwd=os.getcwd(), close_fds=True)
        except OSError as exc:
            applog.record(f"[settings] restart failed: {exc}")
            return False
        applog.record("[settings] restarting to switch the language")
        on_close()
        return True

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
