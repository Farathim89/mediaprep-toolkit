"""main() - builds the window: the left navigation sidebar (ui/sidebar.py),
the page header + pages (the tabs), theme handling, the window icon, the
status bar (job registry / queue badge / version) and the info bars (update
available, missing ffmpeg / packages).
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
from .ui.icons import decorate
from .ui.pageview import PageView
from .ui.sidebar import Sidebar
from .ui.tkthread import _call_tk, _drain_tk_calls
from .ui.widgets import (InfoBar, InfoTab, RecommendedTab, ScrollFrame, add_tooltip,
                         make_root, show_help_index)
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
WINDOW_SIZE = "1280x760"
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
            root.iconbitmap(default=ico)    # dialogs + help windows too
            return
    except Exception:
        pass
    try:
        if os.path.exists(png):
            root._icon_img = tk.PhotoImage(file=png)   # keep a ref so it survives
            root.iconphoto(True, root._icon_img)
    except Exception:
        pass


# page key -> (sidebar icon, title, one-line description shown under the title)
_PAGES = (
    ("templates", "templates", N_("Templates"),
     N_("Mark the intro and credits of an episode and cut them into reusable templates.")),
    ("cut", "cut", N_("Cut / Edit"),
     N_("Find and remove intros and credits in whole seasons, or cut files by hand.")),
    ("audio", "audio", N_("Audio"),
     N_("Export theme songs and even out the loudness of your episodes.")),
    ("inspect", "inspect", N_("Inspect"),
     N_("Play two videos side by side, compare tracks and quality, check files for errors.")),
    ("log", "log", N_("Log"), N_("Everything the app did in this session.")),
    ("info", "info", N_("Info"),
     N_("Encoding notes, recommended settings and the help of every page.")),
)


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
        if e.widget is root and "page_view" in _late:
            _late["page_view"].window_resized((e.width, e.height))
    _late = {}                # widgets made further down that the binding uses
    root.bind("<Configure>", _track_geometry, add="+")

    # themes + fonts first: everything below is built with them
    theme_var = themes.init(root, saved.get("theme", themes.DEFAULT_THEME))
    px = themes.px

    # Tk-thread dispatcher for the job registry / notifications / updater
    jobs.set_dispatcher(_call_tk)
    root.after(50, _drain_tk_calls, root)
    notify.install(root, _call_tk)

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
                           command=lambda: QueueDialog.open_(root, _stop_all_jobs))
    decorate(queue_btn, "queue")
    queue_btn.pack(side="right")
    add_tooltip(queue_btn, tr("Jobs waiting to run one after another - remove, reorder, "
                              "pause/resume, Stop all"))
    badge = tk.Label(status, text="", font="MPSemiboldSmall", borderwidth=0,
                     padx=px(6), pady=0)
    themes.on_palette(badge, lambda p: badge.configure(bg=p["accent"], fg=p["accent_fg"]))
    status_prog = ttk.Progressbar(status, style="Chrome.Horizontal.TProgressbar",
                                  mode="indeterminate", length=px(110))
    ttk.Label(status, textvariable=status_var, style="Chrome.TLabel", anchor="w").pack(
        side="left", fill="x", expand=True)
    _status_ui = {"busy": False, "n": 0}

    # ---- shell: sidebar | content (info bars, page header, pages) ----
    shell = ttk.Frame(root)
    shell.pack(fill="both", expand=True)
    content = ttk.Frame(shell)
    banners = ttk.Frame(content)
    banners.pack(fill="x")
    # page header: title + its one-line description on the same line
    header = ttk.Frame(content, padding=themes.pad(20, 8, 20, 2))
    header.pack(fill="x")
    title_var = tk.StringVar()
    desc_var = tk.StringVar()
    ttk.Label(header, textvariable=title_var, style="Title.TLabel").pack(side="left")
    ttk.Label(header, textvariable=desc_var, style="Hint.TLabel").pack(
        side="left", anchor="s", padx=themes.pad(14, 0), pady=themes.pad(0, 4))
    # fixed-size pages in a 2-D scroll view (ui/pageview.py): the design
    # size (the page area of the default window) is set once the sidebar
    # exists; a window resize only moves the viewport
    page_view = PageView(content, inset=(px(12), px(4)))
    page_view.pack(fill="both", expand=True)
    _late["page_view"] = page_view
    page_host = page_view.canvas

    def _banner(text, kind, **kw):
        bar = InfoBar(banners, text, kind, **kw)
        bar.pack(fill="x", padx=themes.pad(20, 16), pady=themes.pad(10, 0))
        return bar

    # warn up front if ffmpeg/ffprobe are missing (most actions need them)
    # and which optional packages aren't installed (checked once)
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    missing_pkgs = _missing_packages()
    if missing:
        tools = " and ".join(missing)
        applog.record(f"[startup] {tools} not found on PATH - install ffmpeg (ffmpeg.org) "
                      "so cutting and detection work.")
        _banner(tr("{tools} not found on PATH - install ffmpeg (ffmpeg.org) so cutting and "
                   "detection work.", tools=" / ".join(missing)), "error")
    if missing_pkgs:
        names = ", ".join(n for n, _w in missing_pkgs)
        whats = ", ".join(dict.fromkeys(w for _n, w in missing_pkgs))
        applog.record(f"[startup] Optional packages missing: {names} ({whats} won't work) - "
                      "run Install Requirements.bat.")
        whats = ", ".join(dict.fromkeys(tr(w) for _n, w in missing_pkgs))
        _banner(tr("Optional packages missing: {names} ({features} won't work) - run "
                   "Install Requirements.bat.", names=names, features=whats), "warn")

    # main pages grouped by workflow: Templates, Cut / Edit, Audio
    # (Theme Audio, Audio Gain), Inspect (Dual Player, Compare, Check), Log,
    # Info (Info / Settings, Recommended). The tab objects and their attribute
    # names are the same as before - only their parent changed: a page is
    # shown by the sidebar (packed into page_host), a group's sub-tabs are a
    # notebook drawn as a flat tab bar with an accent underline.
    _main_pages = {}          # settings key -> main page widget
    _sub_books = {}           # settings key -> nested notebook
    _holders = []

    def scrolled(factory, title, book=None):
        """Build a tab inside a ScrollFrame so it scrolls when the window is
        smaller than the tab's content. The factory gets (parent, bottom):
        `bottom` is a non-scrolling strip pinned to the window bottom (used
        for progress bars that must stay visible). book = the notebook to add
        it to (default: a main page of its own). Returns the tab instance."""
        holder = ScrollFrame(book or page_host)
        tab = factory(holder.interior, holder.bottom)
        tab.pack(fill="both", expand=True)
        if book is not None:
            book.add(holder, text=_tab_text(title))
        _holders.append(holder)
        return tab

    def group(key, title):
        """A main page holding a nested notebook (the group's sub-tabs)."""
        sub = ttk.Notebook(page_host)
        _main_pages[key] = sub
        _sub_books[key] = sub
        return sub

    def _tab_text(title):
        """Notebook tab caption: the translated title with a little air."""
        return f"  {tr(title)}  "

    def _last_page(key):
        _main_pages[key] = _holders[-1]

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

    # ---- sidebar navigation ----
    _cur = {"page": None}
    _page_info = {key: (icon, title, desc) for key, icon, title, desc in _PAGES}

    def show_page(key):
        """Show main page `key` (hide the others, so their widgets don't
        re-layout on every resize and the players' tab-wide shortcuts only
        act on the visible page)."""
        page = _main_pages.get(key)
        if page is None:
            return
        page_view.show(page)
        _cur["page"] = key
        _icon, title, desc = _page_info[key]
        title_var.set(tr(title))
        desc_var.set(tr(desc))
        sidebar.select(key)

    def _toggle_sidebar(collapsed):
        _sb_state["pref"] = collapsed

    sidebar = Sidebar(shell, on_select=show_page, on_toggle=_toggle_sidebar,
                      title=APP_NAME)
    for key, icon, title, _desc in _PAGES:
        sidebar.add_page(key, icon, tr(title))
    sidebar.add_action("theme", "sun", tr("Light theme"), themes.toggle_dark)
    sidebar.add_action("cleanup", "broom", tr("Clean up folders..."),
                       lambda: CleanupDialog(root, is_busy=_any_busy),
                       tip=tr("Empty the template/videos/output/temp folders - contents are "
                              "moved to Data\\temp\\trash, not deleted, so they can be "
                              "recovered"))
    sidebar.add_action("settings", "settings", tr("Settings..."),
                       lambda: SettingsDialog.open_(root, persist, show_update,
                                                    restart=_restart),
                       tip=tr("Language, theme, notifications, update check and log "
                              "retention"))
    sidebar.add_action("help", "help", tr("Help"), lambda: show_help_index(root),
                       tip=tr("Help for every tab"))
    sidebar.pack(side="left", fill="y")
    content.pack(side="left", fill="both", expand=True)

    def _theme_item(_name=None, pal=None):
        dark = (pal or themes.current()).get("dark")
        sidebar.set_item("theme", icon="sun" if dark else "moon",
                         label=tr("Light theme") if dark else tr("Dark theme"))
    _theme_item()
    themes.subscribe(_theme_item)

    # collapsed state: the user's choice (☰), remembered
    _sb_state = {"pref": bool(saved.get("ui_sidebar_collapsed", False))}
    sidebar.set_collapsed(_sb_state["pref"], notify=False)
    # every page is laid out at least at the page area of the default window
    # (expanded sidebar, header, status bar) - and larger if its content
    # needs it; smaller windows scroll
    def _design():
        """The page area the default window (WINDOW_SIZE, expanded sidebar)
        would have, derived from the current window and view sizes."""
        dw, dh = (int(v) * themes.scale() for v in WINDOW_SIZE.lower().split("x"))
        side = sidebar.winfo_width() - sidebar.expanded_width()
        vw = page_view.winfo_width() + int(dw) - root.winfo_width() + side
        vh = page_view.winfo_height() + int(dh) - root.winfo_height()
        # (minus one scrollbar width, so a tall page scrolls only vertically)
        return (max(vw - 2 * px(12) - px(12), px(600)), max(vh - 2 * px(4), px(380)))
    page_view.design_fn = _design

    # Ctrl+1..6 jump to the pages
    for n, (key, *_rest) in enumerate(_PAGES, 1):
        root.bind(f"<Control-Key-{n}>", lambda e, k=key: (show_page(k), "break")[1])

    def _restore_tabs():
        """Re-select last session's main page and each group's sub-tab."""
        subs = saved.get("ui_sub_tabs")
        if isinstance(subs, dict):
            for key, book in _sub_books.items():
                try:
                    i = int(subs.get(key, 0))
                    if 0 <= i < len(book.tabs()):
                        book.select(i)
                except (TypeError, ValueError, tk.TclError):
                    pass
        key = saved.get("ui_main_tab")
        show_page(key if key in _main_pages else "templates")

    def _tabs_snapshot():
        d = {}
        try:
            if _cur["page"]:
                d["ui_main_tab"] = _cur["page"]
            d["ui_sub_tabs"] = {key: book.index("current")
                                for key, book in _sub_books.items()}
        except tk.TclError:
            pass
        d["ui_sidebar_collapsed"] = bool(_sb_state["pref"])
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

    # ---- update info bar (shown only when a newer release exists) ----
    _banner_w = {"w": None}

    def show_update(ver, url):
        if _banner_w["w"] is not None:
            try:
                _banner_w["w"].destroy()
            except tk.TclError:
                pass
        _banner_w["w"] = _banner(
            tr("Version {new} is available (you have {old}).", new=ver, old=APP_VERSION),
            "info", link_text=tr("Open download page"), on_link=lambda: _open_url(url))

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

    # handles for scripted UI checks (tools / tests drive the window with them)
    root._mp_app = dict(show_page=show_page, sidebar=sidebar, pages=_main_pages,
                        sub_books=_sub_books, persist=persist, restart=_restart,
                        show_update=show_update, stop_all=_stop_all_jobs, is_busy=_any_busy,
                        page_view=page_view,
                        tabs=dict(templates=template_tab, cut=remover_tab, theme=theme_tab,
                                  gain=gain_tab, dual=dual_tab, compare=compare_tab,
                                  check=check_tab))
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
