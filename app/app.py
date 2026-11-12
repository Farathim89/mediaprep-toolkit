"""main() - builds the window: the left navigation sidebar (ui/sidebar.py),
the page header + pages (the tabs), theme handling, the window icon, the
status bar (job registry / queue badge / version) and the info bars (update
available, missing ffmpeg / packages).
The Queue / Settings dialogs live in ui/dialogs.py; the window's shell
helpers (geometry, icon, restart) in window.py, the info bars in banners.py,
the status bar in statusbar.py."""
import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from .config import (APP_NAME, APP_VERSION, LOGS_DIR, PREFS, WORK_DIRS, load_prefs,
                     load_settings, prefs_snapshot, save_settings)
from .tabs.audio_gain import AudioGainTab
from .tabs.check import CheckTab
from .tabs.compare import CompareTab
from .tabs.convert import ConvertTab
from .tabs.cut_edit import RemoverTab
from .tabs.log import LogTab
from .tabs.templates import TemplateTab
from .tabs.theme_audio import ThemeAudioTab
from .ui.cleanup import CleanupDialog
from .ui.dialogs import SettingsDialog, install_default_parent
from .ui import bgmode, dpi, palettes, themepicker, themes
from .ui.dualplayer import DualPlayerTab
from .ui.pageview import PageView, fast_resize
from .ui.sidebar import Sidebar
from .ui.tkthread import _call_tk, _drain_tk_calls
from .ui.helpwin import InfoTab, RecommendedTab, show_help_index
from .ui.widgets import ScrollFrame, bind_window, make_root
from .banners import Banners
from .statusbar import StatusBar
from .window import (_enable_dpi_awareness, _geo_rect, _restart_command, _restart_env,
                     _restore_geometry, _set_window_icon)
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

# page key -> (sidebar icon, title, one-line description shown under the title)
_PAGES = (
    ("templates", "templates", N_("Templates"),
     N_("Mark the intro and credits of an episode and cut them into reusable templates.")),
    ("cut", "cut", N_("Cut / Edit"),
     N_("Find and remove intros and credits in whole seasons, or cut files by hand.")),
    ("convert", "convert", N_("Convert"),
     N_("Re-encode videos: codec, quality, size, crop, filters, audio and subtitle tracks.")),
    ("audio", "audio", N_("Audio"),
     N_("Export theme songs and even out the loudness of your episodes.")),
    ("inspect", "inspect", N_("Inspect"),
     N_("Play two videos side by side, compare tracks and quality, check files for errors.")),
    ("log", "log", N_("Log"), N_("Everything the app did in this session.")),
    ("info", "info", N_("Info"),
     N_("Encoding notes, recommended settings and the help of every page.")),
)


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
    bgmode.remember_foreground()   # MEDIAPREP_BACKGROUND test runs: user's window
    root = make_root()   # drag-and-drop capable if tkinterdnd2 is installed
    root.title(f"{APP_NAME} v{APP_VERSION}")
    _set_window_icon(root)
    root.resizable(True, True)
    # follow each monitor's scaling (ui/dpi.py); the UI is built at the
    # system DPI and rescaled when the window is on another monitor
    dpi.install(root)
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
    # saved on a monitor with another scaling: rescale once everything is
    # built (below) - hidden until then, so it never shows at the wrong size
    start_dpi = dpi.startup_dpi(_geo_rect(start_geo))
    if start_dpi and not bgmode.enabled():
        root.withdraw()
    if bgmode.enabled():
        # scripted test run: open off-screen and never steal the user's focus
        root.withdraw()
        bgmode.apply(root)
        bgmode.place_offscreen(root, start_geo.split("+")[0] if start_geo else default_size)
        root.deiconify()
        for ms in (0, 50, 200, 600, 1500, 3000):     # Tk activates on (re)map
            root.after(ms, lambda: bgmode.give_back_focus(root))

    # remember the last NORMAL (not maximized) geometry, so a maximized
    # window still restores to a sensible size when un-maximized next time
    normal_geo = {"g": start_geo}

    def _track_geometry(e):
        if e.widget is not root:
            return
        if (root.state() == "normal" and root.winfo_width() > 200
                and not bgmode.enabled()):         # don't remember the off-screen spot
            normal_geo["g"] = root.geometry()
        if "page_view" in _late:
            fast_resize(root)        # once the window frame exists (no-op after)
            _late["page_view"].window_resized((e.width, e.height))
    _late = {}                # widgets made further down that the binding uses
    # (the window itself only - root.bind() would run this for every widget
    # inside it that is laid out)
    bind_window(root, "<Configure>", _track_geometry)

    # themes + fonts first: everything below is built with them
    theme_var = themes.init(root, saved.get("theme", palettes.DEFAULT_THEME),
                            font_scale=PREFS.get("font_scale", 1.0))
    px = themes.px
    # message boxes / file dialogs without parent= open over the active
    # window (e.g. over Clean up), not behind it
    install_default_parent(root)

    # Tk-thread dispatcher for the job registry / notifications / updater
    jobs.set_dispatcher(_call_tk)
    root.after(50, _drain_tk_calls, root)
    notify.install(root, _call_tk)

    # ---- status bar (bottom): running job + progress, queue badge, version ----
    status_bar = StatusBar(root, stop_all=lambda: _stop_all_jobs())

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
    # the fixed footer of the content area (the "tracker"): each page's
    # status text + progress bar + Stop are built here, outside the
    # scrolling page, and the footer shows the visible page's strip - so a
    # running job's progress is on screen from any scroll position. Its
    # size is set by _footer_sync only (pack_propagate off): a status text
    # that changes many times a second can't resize anything around it.
    footer_line = tk.Frame(content, height=1, borderwidth=0)
    themes.on_palette(footer_line, lambda p: footer_line.configure(bg=p["stroke"]))
    footer = ttk.Frame(content)
    footer.pack_propagate(False)
    _footer = {"strip": None, "h": 0}

    # the info bars (app/banners.py); warn up front if ffmpeg/ffprobe are
    # missing and which optional packages aren't installed (checked once)
    infobars = Banners(root, banners, persist=lambda: persist(), any_busy=lambda: _any_busy(),
                       on_close=lambda: on_close())
    infobars.startup_problems()

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
        holder = ScrollFrame(book or page_host, bottom_master=footer)
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
    convert_tab = scrolled(lambda m, b: ConvertTab(m, saved=saved, bottom=b), N_("Convert"))
    _last_page("convert")
    audio_nb = group("audio", N_("Audio"))
    theme_tab = scrolled(lambda m, b: ThemeAudioTab(m, saved=saved, bottom=b),
                         N_("Theme Audio"), audio_nb)
    gain_tab = scrolled(lambda m, b: AudioGainTab(m, saved=saved, bottom=b), N_("Audio Gain"),
                        audio_nb)
    inspect_nb = group("inspect", N_("Inspect"))
    dual_tab = scrolled(lambda m, b: DualPlayerTab(m), N_("Dual Player"), inspect_nb)
    _dual_holder = len(_holders) - 1
    compare_tab = scrolled(lambda m, b: CompareTab(m, bottom=b), N_("Compare"), inspect_nb)
    check_tab = scrolled(lambda m, b: CheckTab(m, saved=saved, bottom=b), N_("Check"),
                         inspect_nb)
    scrolled(lambda m, b: LogTab(m, LOGS_DIR), N_("Log"))
    _last_page("log")
    _tab_titles = {template_tab: tr("Templates"), remover_tab: tr("Cut / Edit"),
                   convert_tab: tr("Convert"),
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
        _footer_sync()

    def _visible_holder():
        """The ScrollFrame of the page on screen (a group's selected sub-tab)."""
        page = _main_pages.get(_cur["page"])
        if page is not None and page in _sub_books.values():
            try:
                page = page.nametowidget(page.select())
            except (tk.TclError, KeyError):
                return None
        return page if page in _holders else None

    def _footer_sync():
        """Show the visible page's strip in the footer (hide the footer when
        the page has none) and size the footer to the strip's height."""
        holder = _visible_holder()
        strip = getattr(holder, "bottom", None)
        try:
            if strip is not None and not strip.winfo_children():
                strip = None
            if strip is not _footer["strip"]:
                if _footer["strip"] is not None:
                    _footer["strip"].pack_forget()
                _footer["strip"] = strip
                if strip is not None:
                    strip.pack(fill="x", padx=(px(12), px(12)))
            h = (strip.winfo_reqheight() + px(4)) if strip is not None else 0
            if h != _footer["h"]:
                _footer["h"] = h
                if h:
                    footer.configure(height=h)
                    footer_line.pack(side="bottom", fill="x", before=page_view)
                    footer.pack(side="bottom", fill="x", before=footer_line)
                else:
                    footer.pack_forget()
                    footer_line.pack_forget()
        except tk.TclError:
            pass

    def _footer_tick():
        # a page can change its strip's rows (e.g. Templates shows the
        # Auto-detect row on that sub-tab) - follow its height
        _footer_sync()
        root.after(300, _footer_tick)

    def _toggle_sidebar(collapsed):
        _sb_state["pref"] = collapsed

    sidebar = Sidebar(shell, on_select=show_page, on_toggle=_toggle_sidebar,
                      title=APP_NAME)
    for key, icon, title, _desc in _PAGES:
        sidebar.add_page(key, icon, tr(title))
    _theme_tip = tr("Choose a theme - right-click: switch between your light and dark theme")
    # left click opens the theme list (with colour swatches) under the mouse
    sidebar.add_action("theme", "palette", tr("Theme:"),
                       lambda: themepicker.popup(sidebar, sidebar.winfo_pointerx(),
                                                 sidebar.winfo_pointery()),
                       tip=_theme_tip, context=lambda _x, _y: themes.toggle_dark())
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
    _help_buttons = {"list": None}      # (Check for updates - set further down)
    sidebar.add_action("help", "help", tr("Help"),
                       lambda: show_help_index(root, buttons=_help_buttons["list"]),
                       tip=tr("Help for every tab"))
    sidebar.pack(side="left", fill="y")
    content.pack(side="left", fill="both", expand=True)

    def _theme_item(_name=None, pal=None):
        sidebar.set_item("theme", icon="palette",
                         label=f"{tr('Theme:')} {palettes.label(themes.theme_var().get())}",
                         tip=_theme_tip)
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
        root.update_idletasks()      # the footer may have just been packed
        dw, dh = (int(v) * themes.scale() for v in WINDOW_SIZE.lower().split("x"))
        side = sidebar.winfo_width() - sidebar.expanded_width()
        vw = page_view.winfo_width() + int(dw) - root.winfo_width() + side
        vh = page_view.winfo_height() + int(dh) - root.winfo_height()
        # leave room for the tallest footer strip (pages with a shorter one,
        # or none, just get a little air below - never a needless scrollbar)
        fmax = max((h.bottom.winfo_reqheight() + px(4) + 1 for h in _holders
                    if h.bottom.winfo_children()), default=0)
        vh -= max(0, fmax - (_footer["h"] + 1 if _footer["h"] else 0))
        # (minus one scrollbar width, so a tall page scrolls only vertically)
        return (max(vw - 2 * px(12) - px(12), px(600)), max(vh - 2 * px(4), px(380)))
    page_view.design_fn = _design

    # Ctrl+1..7 jump to the pages
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
    for _book in _sub_books.values():
        _book.bind("<<NotebookTabChanged>>", lambda e: _footer_sync(), add="+")
    root.after(300, _footer_tick)

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
    convert_tab.unload_players_hook = _unload_all_players

    def _open_dual(path_a, path_b):
        """Convert -> Preview: source segment (A) and sample (B) side by side."""
        show_page("inspect")
        for i, tab_id in enumerate(inspect_nb.tabs()):
            if inspect_nb.nametowidget(tab_id) is _holders[_dual_holder]:
                inspect_nb.select(i)
        dual_tab.load_pair(path_a, path_b)
    convert_tab.open_dual = _open_dual
    remover_tab.template_tab = template_tab   # presets read its detect settings

    _top_tabs = [template_tab, remover_tab, convert_tab, theme_tab, gain_tab, dual_tab,
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

    status_bar.start(_legacy_busy_tabs, _tab_titles)

    # ---- update info bar (shown only when a newer release exists) ----
    show_update = infobars.show_update
    update_now = infobars.update_now
    check_for_updates = infobars.check_for_updates
    _help_buttons["list"] = [(tr("Check for updates"), check_for_updates)]

    # the previous run started an update: say how it went; the settings
    # file was damaged at startup - say so once
    infobars.last_update()
    infobars.settings_damaged()

    def persist():
        d = prefs_snapshot()                # app-wide prefs (notify / update / logs)
        d.update(remover_tab.snapshot())
        d.update(template_tab.snapshot())   # Templates tab's Auto-detect settings
        d.update(theme_tab.snapshot())      # Theme Audio format/name/fade/output
        d.update(gain_tab.snapshot())       # Audio Gain batch + single settings
        d.update(check_tab.snapshot())      # Check depth
        d.update(convert_tab.snapshot())    # Convert settings / preset / sub-tab
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
        try:
            infobars.check_save_error()    # a failed save: one banner per new error
        except Exception:
            pass

    # re-walk now that every tab exists, then persist each later theme change
    # (from the top bar, the Settings dialog or Follow Windows)
    themes.set_theme(theme_var.get(), force=True)
    themes.subscribe(lambda _name, _pal: persist())

    # background update check (at most once a day, silent on any failure)
    updater.check_async(PREFS, on_newer=lambda v, url: _call_tk(lambda: show_update(v, url)),
                        save=lambda: _call_tk(persist))

    remover_tab.save_hook = persist   # persist whenever Start is pressed
    template_tab.save_hook = persist  # persist whenever Detect is pressed
    convert_tab.save_hook = persist   # ... and whenever Convert -> Start is pressed

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
            subprocess.Popen(_restart_command(), cwd=os.getcwd(), close_fds=True,
                             env=_restart_env())
        except OSError as exc:
            applog.record(f"[settings] restart failed: {exc}")
            return False
        applog.record("[settings] restarting to switch the language")
        on_close()
        return True

    # handles for scripted UI checks (tools / tests drive the window with them)
    root._mp_app = dict(show_page=show_page, sidebar=sidebar, pages=_main_pages,
                        sub_books=_sub_books, persist=persist, restart=_restart,
                        show_update=show_update, update_now=update_now,
                        check_for_updates=check_for_updates, stop_all=_stop_all_jobs, is_busy=_any_busy,
                        page_view=page_view, footer=footer,
                        tabs=dict(templates=template_tab, cut=remover_tab, convert=convert_tab,
                                  theme=theme_tab,
                                  gain=gain_tab, dual=dual_tab, compare=compare_tab,
                                  check=check_tab))
    root.protocol("WM_DELETE_WINDOW", on_close)
    if start_dpi:
        # the window opens on a monitor with another scaling than the one
        # the UI was built for: rescale before it is shown
        dpi.manager.rescale(start_dpi)
        if not bgmode.enabled():
            root.deiconify()
    # safety net: the UI follows the monitor the window really opened on
    root.after(1500, dpi.manager.check_main)
    root.mainloop()
