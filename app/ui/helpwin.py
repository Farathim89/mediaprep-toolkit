"""Help: the "?" help buttons and the help windows (one per helpdocs key,
plus the index of every page's help), and the Info / Recommended pages
(general encoding notes + the help of every tab, recommended settings)."""
import tkinter as tk
from tkinter import ttk

from . import themes
from ..i18n import N_, tr
from .tooltips import add_tooltip


# ======================= Info + Recommended help content =======================
INFO_SECTIONS = [
    (N_("Video codec"),
     N_("H.264 (libx264) - plays on everything (TVs, phones, old devices). "
     "Baseline choice. Biggest files of the modern codecs.\n\n"
     "H.265 (libx265) - roughly 30-50% smaller files than H.264 at the same "
     "visual quality. Needs a reasonably modern player/TV. Encoding is 2-4x "
     "slower than H.264.\n\n"
     "H.264 / H.265 NVENC - uses your NVIDIA GPU. 5-15x faster encoding, but "
     "at the same file size the quality is a bit lower than the CPU (libx26x) "
     "encoders. Great for bulk jobs where time matters more than megabytes.\n\n"
     "AV1 (libsvtav1) - smallest files of all (~20-30% below H.265), but slow "
     "to encode and only newer devices play it. Best for archiving on a "
     "machine you can leave running.")),
    (N_("Bit depth - 8 vs 10"),
     N_("10-bit stores brightness/color in finer steps. Even for 8-bit sources, "
     "encoding in 10-bit usually compresses ~5% BETTER (less rounding noise "
     "inside the encoder) and strongly reduces banding in smooth gradients - "
     "especially anime.\n\n"
     "Catches: 10-bit H.264 (Hi10P) has almost no hardware decoding support, "
     "so pair 10-bit with H.265 instead - modern devices decode HEVC main10 "
     "in hardware. H.264 NVENC cannot do 10-bit at all (the tool falls back "
     "to 8-bit and says so in the log).\n\n"
     "Auto (recommended) matches the source: 10-bit source -> 10-bit output. "
     "The log shows each source's codec, bit depth and bitrate before "
     "encoding, and warns when your settings will likely inflate the file.")),
    (N_("CRF / CQ - quality knob (0-51, LOWER = better quality = bigger file)"),
     N_("Logarithmic scale: +6 roughly halves the file size.\n\n"
     "H.264: 18 = visually lossless, 20-23 = great, 26+ = visible loss.\n"
     "H.265: 20 = visually lossless, 22-26 = great (x265 numbers sit ~2-4 "
     "higher than x264 for the same look).\n"
     "NVENC: CQ works the same idea; use ~2 lower than you would for CPU.\n"
     "AV1: 26-32 is the typical sweet spot.")),
    (N_("Preset - speed knob (does NOT change quality, changes file size)"),
     N_("With CRF, quality is fixed; the preset decides how hard the encoder "
     "works to COMPRESS that quality. Slower preset = same look, smaller "
     "file.\n\n"
     "ultrafast = biggest files, instant.  medium = the balanced default.  "
     "slow = ~5-10% smaller than medium at ~2x encode time (sweet spot).  "
     "veryslow = diminishing returns, mostly not worth it.")),
    (N_("Keyframe interval (Cut / Edit)"),
     N_("0 (default): a clean keyframe is forced exactly at every cut point - "
     "that is all a normal player needs. Set 2-5 s if your media server "
     "scrubs/skips inside the Cold Open / Post Credits and you want every "
     "seek frame-perfect.\n\n"
     "It only applies to the SHORT pieces that get re-encoded (cold open, "
     "post credits) - not to the main episode. Smaller interval = slightly "
     "bigger file.")),
    (N_("Min confidence (Cut / Edit)"),
     N_("How strong the audio match must be to accept an intro/credits "
     "detection.\n\n"
     "0.32 (default) works for most shows. Raise to 0.40+ if it cuts things "
     "it should not (false matches). Lower to ~0.25 if it misses intros it "
     "should find - check the per-template scores in the log to see how "
     "close it was.")),
    (N_("Run modes (Cut / Edit)"),
     N_("Cut: removes intro/credits, keeps cold open + post-credits, stitches "
     "the rest back together.\n\n"
     "Inject keyframes: keeps the whole video, only inserts keyframes at the "
     "detected boundaries (for media servers with their own skip buttons). "
     "Audio is copied as-is.\n\n"
     "Add chapter markers: keeps the whole video and adds chapters at the "
     "boundaries (Intro / Episode / Credits ...). Nothing is re-encoded - "
     "video, audio and subtitles are copied, so it is fast and lossless.")),
    (N_("Audio & subtitles (Cut / Edit)"),
     N_("Audio is re-encoded at a high bitrate in its OWN codec and channel "
     "layout (5.1 stays 5.1). The one exception is DTS: "
     "ffmpeg's DTS encoder is experimental, so DTS becomes E-AC3 at 640k.\n\n"
     "Subtitles are copied unchanged - unless the subtitle language filter is "
     "on, then only the chosen languages are kept. Inject keyframes and "
     "chapter markers copy the audio untouched.")),
    (N_("Templates (Templates tab)"),
     N_("Only the AUDIO of a template matters (matching is done on sound). "
     "Templates are saved as .mkv.\n\n"
     "IMPORTANT: Cut / Edit uses the template LENGTH as the cut length, so "
     "cut the WHOLE intro/credits, not just a recognizable part. Trim and "
     "Anchor change that length - re-check it after using them.")),
]
# per-tab help (Theme Audio, Audio Gain, Compare, Check, Log, Clean up, ...)
# lives in helpdocs.HELP - the Info tab appends it, so Info and the "?"
# buttons always show the same text


RECOMMENDED_SECTIONS = [
    (N_("Closest to original quality  (the tool's default)"),
     N_("H.264,  CRF 18,  preset slow,  bit depth Auto\n\n"
     "Visually indistinguishable from the source. The file lands near the "
     "original's size - sometimes a bit over, which is the price of "
     "re-encoding an already-compressed file. Use CRF 17 if you ever spot "
     "a difference in very dark scenes.")),
    (N_("Best balance  (recommended)"),
     N_("H.265,  CRF 22,  preset slow\n\n"
     "High quality + small files. Use preset 'medium' if encodes feel too slow.")),
    (N_("Anime / 10-bit sources (Hi10P)"),
     N_("H.265,  10-bit,  CRF 22-23,  preset slow\n\n"
     "Matches the efficiency of typical fansub Hi10P H.264 encodes. Keeping "
     "8-bit H.264 output for such sources can easily triple the file size.")),
    (N_("Max compatibility"),
     N_("H.264,  CRF 20,  preset medium\n\n"
     "For old TVs/devices, or when unsure what will play the files.")),
    (N_("Fast bulk job  (NVIDIA GPU)"),
     N_("H.265 NVENC,  CQ 24,  preset slow\n\n"
     "Encodes many episodes quickly with good (not maximal) efficiency.")),
    (N_("Smallest archive"),
     N_("AV1,  CRF 28,  preset slow\n\n"
     "Leave it running overnight; check that your players support AV1.")),
    (N_("Note on audio & subtitles"),
     N_("Audio keeps its codec and channels (re-encoded at a high bitrate; "
     "DTS becomes E-AC3 640k). Subtitles are copied "
     "unless the language filter is on. None of the settings above touch "
     "them.")),
]


class SectionList(ttk.Frame):
    """Scrollable list of titled cards (LabelFrames), reflows on resize."""
    def __init__(self, master, sections, height=430):
        # sizes in px at 100 % scaling (grown with the DPI)
        super().__init__(master, padding=themes.pad(8, 12, 4, 8))
        canvas = tk.Canvas(self, height=themes.px(height), width=themes.px(660),
                           highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._labels = []

        def colours(p):
            for lbl in self._labels:
                lbl.configure(bg=p["card"], fg=p["fg"])
        for title, body in sections:
            # tr(): the module-level sections are N_()-marked English; the
            # helpdocs ones arrive translated (tr of a translation = itself)
            box = ttk.LabelFrame(inner, text=tr(title))
            box.pack(fill="x", expand=True, padx=themes.px(4), pady=themes.pad(6, 8))
            # a classic Label: it wraps its (long) text once per change and
            # keeps the result - a ttk.Label wraps it again for every size
            # query and redraw, which made a theme / font / display scaling
            # change several times slower with these ~40 help texts
            lbl = tk.Label(box, text=tr(body), wraplength=themes.px(580), justify="left",
                           anchor="w", font="TkDefaultFont", borderwidth=0,
                           highlightthickness=0, padx=0, pady=0)
            lbl.pack(anchor="w", fill="x")
            self._labels.append(lbl)
        themes.on_palette(self, colours)

        def on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", on_inner)

        # re-wrapping every card costs a full relayout - do it once the
        # resize settles, and only when the width really changed
        state = {"w": None, "job": None}

        def rewrap():
            state["job"] = None
            w = state["w"]
            # re-laid out OUTSIDE the canvas: while a canvas manages the
            # frame, every size request of it is applied at once and all
            # ~40 cards are moved again for each card that changed
            # (~0.3 s instead of ~0.05 s)
            canvas.itemconfigure(inner_id, window="")
            try:
                for lbl in self._labels:
                    lbl.configure(wraplength=max(themes.px(300), w - themes.px(48)))
                inner.update_idletasks()
            finally:
                canvas.itemconfigure(inner_id, window=inner, width=w)
                on_inner()

        def on_canvas(e):
            if e.width == state["w"]:
                return
            first = state["w"] is None
            state["w"] = e.width
            if first:
                rewrap()
            elif state["job"] is None:
                state["job"] = canvas.after(80, rewrap)
        canvas.bind("<Configure>", on_canvas)

        # NOTE: no unbind_all here - that would kill the wheel binding of every
        # other scrollable tab. On Windows the wheel event goes to the FOCUSED
        # widget, so locate the widget under the pointer to decide if it's us.
        def on_wheel(e):
            try:
                if not canvas.winfo_exists():
                    return
                w = canvas.winfo_containing(e.x_root, e.y_root)
                if w is None:
                    return
                if w is not self and not str(w).startswith(str(self) + "."):
                    return
            except Exception:
                return
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        canvas.bind_all("<MouseWheel>", on_wheel, add="+")


class InfoTab(SectionList):
    """General encoding notes + the help of every tab (from helpdocs)."""
    def __init__(self, master):
        from .. import helpdocs
        tab_help = [(tr("Tab help – {title}", title=t), body)
                    for t, body in helpdocs.sections()]
        super().__init__(master, INFO_SECTIONS + tab_help)


class RecommendedTab(SectionList):
    def __init__(self, master):
        super().__init__(master, RECOMMENDED_SECTIONS)


# ======================= "?" help buttons / help windows =======================
_HELP_WINDOWS = {}      # key -> open Toplevel (one window per key)


def _theme_colors(_widget=None):
    """(bg, fg, text_bg, text_fg, accent) of the current theme."""
    p = themes.current()
    return p["bg"], p["fg"], p["field"], p["fg"], p["accent"]


def _open_help_window(parent, wkey, title, sections, index=False, buttons=None):
    """Themed, scrollable help window (re-used/raised if already open).
    sections = [(heading, text)]; index=True adds a clickable topic list;
    buttons = [(text, command(window))] at the bottom left."""
    from . import bgmode
    old = _HELP_WINDOWS.get(wkey)
    if old is not None:
        try:
            if old.winfo_exists():
                old.deiconify()
                old.lift()
                bgmode.focus(old)           # no activation in background mode
                return old
        except tk.TclError:
            pass
    top = parent.winfo_toplevel()
    bg, fg, _tbg, tfg, _acc = _theme_colors(top)
    win = tk.Toplevel(top)
    hidden = bgmode.enabled()       # scripted test run: build it unseen (see below)
    if hidden:
        win.withdraw()
    win.title(title)
    win.configure(bg=bg)
    win.transient(top)
    try:
        dpi = max(1.0, win.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        dpi = 1.0
    w, h = int((760 if index else 560) * dpi), int((560 if index else 460) * dpi)
    try:
        x = top.winfo_rootx() + max(0, (top.winfo_width() - w) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - h) // 3)
        # no larger than the work area of the main window's monitor (a high
        # display scaling leaves few pixels) and on it - the Close button
        # at the bottom stays visible
        from .dialogfit import cap_window, work_area
        w, h, x, y = cap_window(win, w, h, x, y, work_area(top))
        win.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        win.geometry(f"{w}x{h}")
    win.minsize(min(int(320 * dpi), w), min(int(220 * dpi), h))

    outer = ttk.Frame(win, padding=themes.pad(16, 12, 16, 12))
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text=title, style="Subtitle.TLabel").pack(anchor="w",
                                                             pady=themes.pad(0, 10))
    bar = ttk.Frame(outer)
    bar.pack(side="bottom", fill="x", pady=themes.pad(12, 0))
    ttk.Button(bar, text=tr("Close"), command=win.destroy).pack(side="right")
    for btext, bcmd in buttons or ():
        ttk.Button(bar, text=btext, command=lambda c=bcmd: c(win)).pack(
            side="left", padx=themes.pad(0, 6))

    body = ttk.Frame(outer)
    body.pack(fill="both", expand=True)
    p0 = themes.current()
    text = tk.Text(body, wrap="word", bg=p0["card"], fg=tfg, relief="flat",
                   font="TkDefaultFont", padx=themes.px(16), pady=themes.px(12),
                   borderwidth=0, highlightthickness=1, highlightbackground=p0["stroke"],
                   highlightcolor=p0["stroke"], selectbackground=p0["select_bg"],
                   insertbackground=tfg, cursor="arrow", spacing2=themes.px(2),
                   selectforeground=p0["select_fg"])
    sb = ttk.Scrollbar(body, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    if index:
        lb = tk.Listbox(body, exportselection=False, activestyle="none",
                        bg=bg, fg=tfg, selectbackground=p0["select_bg"],
                        selectforeground=p0["select_fg"], relief="flat", borderwidth=0,
                        highlightthickness=0, font="TkDefaultFont",
                        width=max(28, max((len(h) for h, _t in sections), default=0) + 1))
        lb.pack(side="left", fill="y", padx=themes.pad(0, 12))
    sb.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    text.tag_configure("h", font="MPSubtitle", foreground=fg,
                       spacing1=themes.px(14), spacing3=themes.px(6))

    def _repaint(p):
        # the walk in themes.apply recolours Text/Listbox; these are the
        # help window's own bits
        text.tag_configure("h", foreground=p["fg"])
        text.configure(bg=p["card"], highlightbackground=p["stroke"],
                       highlightcolor=p["stroke"], selectbackground=p["select_bg"],
                       selectforeground=p["select_fg"])
        if index:
            lb.configure(bg=p["bg"])
    themes.on_palette(win, _repaint)
    text.tag_configure("p", spacing1=1, spacing3=1, lmargin1=2, lmargin2=2)
    marks = []
    for i, (head, para) in enumerate(sections):
        mark = f"sec{i}"
        text.mark_set(mark, "end-1c")
        text.mark_gravity(mark, "left")
        marks.append(mark)
        text.insert("end", head + "\n", "h")
        text.insert("end", para.strip() + "\n\n", "p")
        if index:
            lb.insert("end", head)
    text.configure(state="disabled")

    if index:
        def jump(_e=None):
            sel = lb.curselection()
            if sel:
                text.yview(marks[sel[0]])
        lb.bind("<<ListboxSelect>>", jump)

    def wheel(e):
        text.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"
    text.bind("<MouseWheel>", wheel)
    win.bind("<Escape>", lambda e: win.destroy())

    def gone(e):
        if e.widget is win and _HELP_WINDOWS.get(wkey) is win:
            _HELP_WINDOWS.pop(wkey, None)
    win.bind("<Destroy>", gone, add="+")
    _HELP_WINDOWS[wkey] = win
    themes.recolor(win)
    if hidden:
        # invisible + non-activating like every window of a background run
        bgmode.apply(win)
        win.deiconify()
    win.lift()
    # focus_set on a window that isn't mapped yet doesn't activate it
    win.after(50, lambda: win.winfo_exists() and bgmode.focus(win))
    return win


def show_help(parent, key):
    """Open (or raise) the help window for a helpdocs key."""
    from .. import helpdocs
    title, body = helpdocs.get(key)
    return _open_help_window(parent, "help:" + key, tr("Help – {title}", title=title),
                             [(title, body)])


def show_help_index(parent, buttons=None):
    """Open (or raise) the help window listing every tab's help.
    buttons = extra [(text, command(window))] (e.g. Check for updates)."""
    from .. import helpdocs
    return _open_help_window(parent, "help:index", tr("{app} – Help", app="MediaPrep Toolkit"),
                             helpdocs.sections(), index=True, buttons=buttons)


def help_button(parent, key, text="?"):
    """A small '?' ttk.Button that opens the help for helpdocs.HELP[key].
    The caller places it (pack/grid) like any other widget."""
    from .. import helpdocs
    from . import icons
    if text == "?" and icons.available():
        btn = ttk.Button(parent, text="", style="Subtle.TButton",
                         command=lambda: show_help(parent, key))
        icons.decorate(btn, "help")
    else:
        btn = ttk.Button(parent, text=text, width=max(2, len(text) + 1),
                         command=lambda: show_help(parent, key))
    add_tooltip(btn, tr("Help: {title}", title=helpdocs.get(key)[0]))
    return btn
