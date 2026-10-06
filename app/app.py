"""main() - builds the window, the four tabs, theme handling and the window icon."""
import os
import shutil
import threading
import tkinter as tk
from tkinter import ttk

from .config import (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR,
                     VIDEO_DIR, OUTPUT_DIR, AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT,
                     THEMES, apply_theme, load_settings, save_settings)
from .widgets import InfoTab, RecommendedTab, ScrollFrame, add_tooltip, make_root
from .cleanup import CleanupDialog
from .tabs import TemplateTab, RemoverTab
from .audiotools import ThemeAudioTab, AudioGainTab
from .dualplayer import DualPlayerTab
from .compare import CompareTab
from .checktab import CheckTab
from .logtab import LogTab
from . import applog

# assets/ sits next to the app/ package (i.e. in the project root)
_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

# ==========================================================================
#  WINDOW SIZE  -  change these two lines to resize the app window.
#  WINDOW_SIZE is the opening size as "WIDTHxHEIGHT" in pixels.
#  MIN_SIZE is the smallest it can be dragged to (width, height).
# ==========================================================================
WINDOW_SIZE = "1180x710"
MIN_SIZE = (1180, 710)


def _set_window_icon(root):
    """Prefer the .ico (best on Windows); fall back to the .png everywhere else."""
    ico = os.path.join(_ASSETS, "icon.ico")
    png = os.path.join(_ASSETS, "icon.png")
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


def _center_window(root, size):
    """Place the window of the given "WIDTHxHEIGHT" size in the screen centre."""
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        root.geometry(size)
        return
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2 - 50)   # 50px above dead centre
    root.geometry(f"{w}x{h}+{x}+{y}")


def main():
    # keep full CPU speed when the window is minimized / in the background
    # (Windows 11 otherwise power-throttles the whole process tree)
    from .media import prevent_power_throttling
    prevent_power_throttling()

    # premade working folders next to the launcher
    for d in (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR, VIDEO_DIR, OUTPUT_DIR,
              AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT, "logs"):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    applog.init("logs")

    saved = load_settings()

    root = make_root()   # drag-and-drop capable if tkinterdnd2 is installed
    root.title("MediaPrep Toolkit")
    _set_window_icon(root)
    root.resizable(True, True)
    root.minsize(*MIN_SIZE)
    _center_window(root, WINDOW_SIZE)   # open centered at the size set above

    # warn up front if ffmpeg/ffprobe are missing (most actions need them)
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        tk.Label(root, text="  \u26a0  " + " and ".join(missing) +
                 " not found on PATH - install ffmpeg (ffmpeg.org) so cutting and detection work.",
                 bg="#b00020", fg="white", anchor="w", padx=8, pady=3).pack(fill="x")

    style = ttk.Style(root)

    # top bar with theme selector
    top = ttk.Frame(root, padding=(10, 6, 10, 0))
    top.pack(fill="x")
    ttk.Label(top, text="Theme:").pack(side="left")
    theme_saved = saved.get("theme", "Light")
    theme_var = tk.StringVar(value=theme_saved if theme_saved in THEMES else "Light")
    theme_box = ttk.Combobox(top, textvariable=theme_var, values=list(THEMES.keys()),
                             state="readonly", width=14)
    theme_box.pack(side="left", padx=6)
    cleanup_btn = ttk.Button(top, text="Clean up folders...",
                             command=lambda: CleanupDialog(root, is_busy=_any_busy))
    cleanup_btn.pack(side="right")
    add_tooltip(cleanup_btn, "Empty the template/videos/output/temp folders - contents are "
                             "moved to temp\\trash, not deleted, so they can be recovered")

    nb = ttk.Notebook(root)

    def scrolled(factory, title):
        """Build a tab inside a ScrollFrame so it scrolls when the window is
        smaller than the tab's content. The factory gets (parent, bottom):
        `bottom` is a non-scrolling strip pinned to the window bottom (used
        for progress bars that must stay visible). Returns the tab instance."""
        holder = ScrollFrame(nb)
        tab = factory(holder.interior, holder.bottom)
        tab.pack(fill="both", expand=True)
        nb.add(holder, text=title)
        return tab

    template_tab = scrolled(lambda m, b: TemplateTab(m, saved=saved, bottom=b),
                            "  Template Cutter  ")
    remover_tab = scrolled(lambda m, b: RemoverTab(m, saved=saved, bottom=b),
                           "  Cut / Edit  ")
    theme_tab = scrolled(lambda m, b: ThemeAudioTab(m, saved=saved), "  Theme Audio  ")
    gain_tab = scrolled(lambda m, b: AudioGainTab(m, saved=saved), "  Audio Gain  ")
    dual_tab = scrolled(lambda m, b: DualPlayerTab(m), "  Dual Player  ")
    compare_tab = scrolled(lambda m, b: CompareTab(m), "  Compare  ")
    check_tab = scrolled(lambda m, b: CheckTab(m, saved=saved), "  Check  ")
    scrolled(lambda m, b: LogTab(m, "logs"), "  Log  ")
    nb.add(InfoTab(nb), text="  Info / Settings  ")        # scrolls on its own
    nb.add(RecommendedTab(nb), text="  Recommended  ")     # scrolls on its own
    nb.pack(fill="both", expand=True)

    # let the batch free any video a preview player is holding open, so its
    # 'move finished to done' can move the source (Windows locks open files)
    _players = [template_tab.player, remover_tab.player,
                getattr(remover_tab, "multi_player", None), theme_tab.player,
                getattr(dual_tab, "pA", None), getattr(dual_tab, "pB", None)]

    def _unload_all_players():
        for p in _players:
            if p is not None:
                try:
                    p.unload()
                except Exception:
                    pass

    remover_tab.unload_players_hook = _unload_all_players

    _job_tabs = [template_tab, remover_tab, theme_tab, gain_tab, dual_tab,
                 compare_tab, check_tab]

    def _any_busy():
        """True if any tab has a job running (its Stop button is enabled)."""
        for t in _job_tabs:
            for name, w in vars(t).items():
                if name.endswith("stop_btn"):
                    try:
                        if w.instate(["!disabled"]):
                            return True
                    except Exception:
                        pass
        return False

    def _stop_all_jobs():
        """Set every tab's stop event(s) (stop_event, detect_stop, cut_stop, ...)
        so the workers terminate their ffmpeg processes."""
        for t in _job_tabs:
            for name, ev in vars(t).items():
                if "stop" in name and isinstance(ev, threading.Event):
                    ev.set()

    def persist():
        d = remover_tab.snapshot()
        d.update(template_tab.snapshot())   # Template Cutter's auto-detect settings
        d.update(theme_tab.snapshot())      # Theme Audio format/name/fade/output
        d.update(gain_tab.snapshot())       # Audio Gain batch + single settings
        d.update(check_tab.snapshot())      # Check depth
        d["theme"] = theme_var.get()
        d["last_video"] = (template_tab.file_var.get().strip()
                           or remover_tab.sel_var.get().strip())
        save_settings(d)  # theme + settings + last video; window size is fixed in code

    def on_theme_change(_evt=None):
        apply_theme(root, style, theme_var.get())
        persist()

    theme_box.bind("<<ComboboxSelected>>", on_theme_change)
    apply_theme(root, style, theme_var.get())

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

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
