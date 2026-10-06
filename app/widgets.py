"""Small shared widgets: the HH:MM:SS:mmm TimeEntry and the scrollable help tabs."""
import tkinter as tk
from tkinter import ttk


class TimeEntry(ttk.Frame):
    """Four boxes (HH : MM : SS : mmm) so times are easy to type.
    Empty box counts as 0; the last box is milliseconds (a decimal seconds
    value like 30.5 in the seconds box still works too).
    All boxes empty = no time given (used for 'to end of file')."""

    def __init__(self, master, on_focus=None):
        super().__init__(master)
        self._on_focus = on_focus          # called with self when a box is focused
        self.h, self.m = tk.StringVar(), tk.StringVar()
        self.s, self.ms = tk.StringVar(), tk.StringVar()
        self.vars = (self.h, self.m, self.s, self.ms)
        self._entries = []
        for i, (var, w) in enumerate(((self.h, 3), (self.m, 3), (self.s, 3), (self.ms, 4))):
            e = ttk.Entry(self, textvariable=var, width=w, justify="center")
            e.grid(row=0, column=i * 2)
            self._entries.append(e)
            if i < 3:
                ttk.Label(self, text=":").grid(row=0, column=i * 2 + 1)
            e.bind("<FocusIn>", lambda ev: self._focused())
        # auto-jump to the next box once its digits are typed (2 for H/M/S)
        for i in range(3):
            self._entries[i].bind(
                "<KeyRelease>",
                lambda ev, i=i: self._advance(ev, i))

    def _focused(self):
        if callable(self._on_focus):
            self._on_focus(self)

    def _advance(self, ev, i):
        if ev.char.isdigit() and len(self._entries[i].get()) >= 2:
            self._entries[i + 1].focus_set()
            self._entries[i + 1].icursor("end")

    def set_seconds(self, sec):
        """Fill the H:M:S:ms boxes from a float second count (frame-precise)."""
        if sec is None or sec < 0:
            sec = 0.0
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        self.h.set(str(h) if h else "")
        self.m.set(f"{m:02d}" if (h or m) else "")
        self.s.set(str(s))
        self.ms.set(f"{ms:03d}" if ms else "")

    def nudge(self, delta):
        """Shift the current time by delta seconds (can be negative), clamped at
        0. An empty box counts as 0 so a nudge gives it a value. Helper for
        nudging a boundary that is off by a small amount (kept for reuse)."""
        cur, ok = self.get_seconds()
        if not ok:
            return
        base = cur if cur is not None else 0.0
        self.set_seconds(max(0.0, base + delta))
        self.flash()

    def flash(self):
        """Briefly highlight the boxes to confirm a value was captured."""
        for e in self._entries:
            try:
                e.select_range(0, "end")
            except Exception:
                pass
        self.after(500, self._unflash)

    def _unflash(self):
        for e in self._entries:
            try:
                e.selection_clear()
            except Exception:
                pass

    def get_seconds(self):
        """Returns (seconds_or_None, ok). None+ok=True means 'left empty'."""
        h = self.h.get().strip()
        m = self.m.get().strip()
        s = self.s.get().strip()
        ms = self.ms.get().strip()
        if not (h or m or s or ms):
            return None, True
        try:
            h = int(h) if h else 0
            m = int(m) if m else 0
            s = float(s) if s else 0.0
            # a bare "5" in the ms box means 5 ms, not 500 ms - plain int
            ms = int(ms) if ms else 0
            if h < 0 or not 0 <= m < 60 or not 0 <= s < 60 or not 0 <= ms < 1000:
                raise ValueError
            return h * 3600 + m * 60 + s + ms / 1000.0, True
        except ValueError:
            return None, False


# ======================= Info + Recommended help content =======================
INFO_SECTIONS = [
    ("Video codec",
     "H.264 (libx264) - plays on everything (TVs, phones, old devices). "
     "Baseline choice. Biggest files of the modern codecs.\n\n"
     "H.265 (libx265) - roughly 30-50% smaller files than H.264 at the same "
     "visual quality. Needs a reasonably modern player/TV. Encoding is 2-4x "
     "slower than H.264.\n\n"
     "H.264 / H.265 NVENC - uses your NVIDIA GPU. 5-15x faster encoding, but "
     "at the same file size the quality is a bit lower than the CPU (libx26x) "
     "encoders. Great for bulk jobs where time matters more than megabytes.\n\n"
     "AV1 (libsvtav1) - smallest files of all (~20-30% below H.265), but slow "
     "to encode and only newer devices play it. Best for archiving on a "
     "machine you can leave running."),
    ("Bit depth - 8 vs 10",
     "10-bit stores brightness/color in finer steps. Even for 8-bit sources, "
     "encoding in 10-bit usually compresses ~5% BETTER (less rounding noise "
     "inside the encoder) and strongly reduces banding in smooth gradients - "
     "especially anime.\n\n"
     "Catches: 10-bit H.264 (Hi10P) has almost no hardware decoding support, "
     "so pair 10-bit with H.265 instead - modern devices decode HEVC main10 "
     "in hardware. H.264 NVENC cannot do 10-bit at all (the tool falls back "
     "to 8-bit and says so in the log).\n\n"
     "Auto (recommended) matches the source: 10-bit source -> 10-bit output. "
     "The log shows each source's codec, bit depth and bitrate before "
     "encoding, and warns when your settings will likely inflate the file."),
    ("CRF / CQ - quality knob (0-51, LOWER = better quality = bigger file)",
     "Logarithmic scale: +6 roughly halves the file size.\n\n"
     "H.264: 18 = visually lossless, 20-23 = great, 26+ = visible loss.\n"
     "H.265: 20 = visually lossless, 22-26 = great (x265 numbers sit ~2-4 "
     "higher than x264 for the same look).\n"
     "NVENC: CQ works the same idea; use ~2 lower than you would for CPU.\n"
     "AV1: 26-32 is the typical sweet spot."),
    ("Preset - speed knob (does NOT change quality, changes file size)",
     "With CRF, quality is fixed; the preset decides how hard the encoder "
     "works to COMPRESS that quality. Slower preset = same look, smaller "
     "file.\n\n"
     "ultrafast = biggest files, instant.  medium = the balanced default.  "
     "slow = ~5-10% smaller than medium at ~2x encode time (sweet spot).  "
     "veryslow = diminishing returns, mostly not worth it."),
    ("Keyframe interval (Cut / Edit)",
     "0 (default): a clean keyframe is forced exactly at every cut point - "
     "that is all a normal player needs. Set 2-5 s if your media server "
     "scrubs/skips inside the Cold Open / Post Credits and you want every "
     "seek frame-perfect. Smaller interval = slightly bigger file (only "
     "affects those short segments)."),
    ("Min confidence (Cut / Edit)",
     "How strong the audio match must be to accept an intro/credits "
     "detection.\n\n"
     "0.32 (default) works for most shows. Raise to 0.40+ if it cuts things "
     "it should not (false matches). Lower to ~0.25 if it misses intros it "
     "should find - check the per-template scores in the log to see how "
     "close it was."),
    ("Modes",
     "Cut: removes intro/credits, keeps cold open + post-credits, stitches "
     "the rest back together.\n\n"
     "Inject: keeps the whole video, only inserts keyframes at the detected "
     "boundaries (for media servers with their own skip buttons)."),
    ("Templates (Template Cutter tab)",
     "Only the AUDIO of a template matters (matching is done on sound), so "
     "the template tab always uses fast x264 - no need to configure it.\n\n"
     "IMPORTANT: the Remover uses the template LENGTH as the cut length, so "
     "cut the WHOLE intro/credits, not just a recognizable part."),
]

RECOMMENDED_SECTIONS = [
    ("Closest to original quality  (the tool's default)",
     "H.264,  CRF 18,  preset slow,  bit depth Auto\n\n"
     "Visually indistinguishable from the source. The file lands near the "
     "original's size - sometimes a bit over, which is the price of "
     "re-encoding an already-compressed file. Use CRF 17 if you ever spot "
     "a difference in very dark scenes."),
    ("Best balance  (recommended)",
     "H.265,  CRF 22,  preset slow\n\n"
     "High quality + small files. Use preset 'medium' if encodes feel too slow."),
    ("Anime / 10-bit sources (Hi10P)",
     "H.265,  10-bit,  CRF 22-23,  preset slow\n\n"
     "Matches the efficiency of typical fansub Hi10P H.264 encodes. Keeping "
     "8-bit H.264 output for such sources can easily triple the file size."),
    ("Max compatibility",
     "H.264,  CRF 20,  preset medium\n\n"
     "For old TVs/devices, or when unsure what will play the files."),
    ("Fast bulk job  (NVIDIA GPU)",
     "H.265 NVENC,  CQ 24,  preset slow\n\n"
     "Encodes many episodes quickly with good (not maximal) efficiency."),
    ("Smallest archive",
     "AV1,  CRF 28,  preset slow\n\n"
     "Leave it running overnight; check that your players support AV1."),
    ("Note on audio & subtitles",
     "Audio is always re-encoded to the same codec/channels at high bitrate "
     "and subtitles are copied - none of the settings above touch them."),
]


class SectionList(ttk.Frame):
    """Scrollable list of titled boxes (LabelFrames), reflows on resize."""
    def __init__(self, master, sections, height=430):
        super().__init__(master, padding=(10, 8))
        canvas = tk.Canvas(self, height=height, width=660,
                           highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._labels = []
        for title, body in sections:
            box = ttk.LabelFrame(inner, text=f" {title} ", padding=(10, 6))
            box.pack(fill="x", expand=True, padx=4, pady=5)
            lbl = ttk.Label(box, text=body, wraplength=580, justify="left")
            lbl.pack(anchor="w", fill="x")
            self._labels.append(lbl)

        def on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", on_inner)

        def on_canvas(e):
            canvas.itemconfigure(inner_id, width=e.width)
            for lbl in self._labels:
                lbl.configure(wraplength=max(300, e.width - 60))
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
    def __init__(self, master):
        super().__init__(master, INFO_SECTIONS)


class RecommendedTab(SectionList):
    def __init__(self, master):
        super().__init__(master, RECOMMENDED_SECTIONS)


# ======================= hover tooltips =======================
class Tooltip:
    """A small hover tooltip for any widget. Shows after a short delay and
    hides on leave or click. Tkinter has no built-in tooltip, so this is it."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        if self.tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)      # no title bar
        self.tip.wm_geometry(f"+{x}+{y}")
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(self.tip, text=self.text, justify="left",
                 background="#ffffe0", foreground="#1a1a1a",
                 relief="solid", borderwidth=1,
                 font=("Segoe UI", 9), padx=7, pady=4, wraplength=320).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def add_tooltip(widget, text):
    """Attach a hover tooltip to widget and return the widget (for chaining)."""
    Tooltip(widget, text)
    return widget


# ======================= vertically scrollable frame =======================
def _wheel_should_pass(widget, own_canvas):
    """True if the mouse wheel over `widget` should be left to the widget
    itself (Text boxes, list boxes, tree views and other scrollable canvases
    scroll their own content)."""
    w = widget
    while w is not None and w is not own_canvas:
        try:
            cls = w.winfo_class()
        except Exception:
            return False
        if cls in ("Text", "Listbox", "Treeview", "TCombobox"):
            return True
        if cls == "Canvas":
            try:
                if w.cget("yscrollcommand"):
                    return True
            except Exception:
                pass
        w = getattr(w, "master", None)
    return False


class ScrollFrame(ttk.Frame):
    """A vertically scrollable container. Add child widgets to `.interior`.

    The interior always fills the visible area (so expanding layouts still
    stretch), and when the content is taller than the window a scrollbar
    appears and the mouse wheel scrolls it. Wheel events over Text boxes,
    lists etc. are left alone so they keep scrolling their own content."""

    def __init__(self, master, canvas_width=None, **kw):
        super().__init__(master, **kw)
        # non-scrolling strip pinned to the bottom (for progress bars etc.);
        # packed first so it always stays visible below the scrolled content
        self.bottom = ttk.Frame(self)
        self.bottom.pack(side="bottom", fill="x")
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                 yscrollincrement=30)
        if canvas_width:
            self._canvas.configure(width=canvas_width)
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vsb_shown = False

        self.interior = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self.interior, anchor="nw")

        self.interior.bind("<Configure>", self._fit, add="+")
        self._canvas.bind("<Configure>", self._fit, add="+")
        # one app-wide wheel binding per frame; the handler checks that the
        # pointer is inside THIS frame, so multiple ScrollFrames coexist
        self._canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        # sizes settle late (size requests propagate at idle time, and themes
        # restyle widgets after the tabs are built), so re-measure afterwards
        # and keep a light watchdog running - otherwise the frame can get
        # stuck clipped, with no scrollbar until the window is resized
        self.after_idle(self._fit)
        self.bind("<<ThemeChanged>>", lambda e: self.after_idle(self._fit), add="+")
        self._last_measure = None
        self.after(120, self._watch)

    def _watch(self):
        """Re-measure whenever the content's requested size or the canvas size
        changed without an event we could catch (late idle-time layout, theme
        restyles, DPI/font changes). Cheap: two winfo calls per tick."""
        try:
            if not self.winfo_exists():
                return
            cur = (self._canvas.winfo_width(), self._canvas.winfo_height(),
                   self.interior.winfo_reqheight())
            if cur != self._last_measure:
                self._last_measure = cur
                self._fit()
        except Exception:
            return
        self.after(400, self._watch)

    def _fit(self, _e=None):
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        rh = self.interior.winfo_reqheight()
        if rh >= ch:
            # content overflows: give the interior its NATURAL height (0), so
            # the canvas keeps following the interior's size requests - forcing
            # a height here would freeze it and clip late-arriving growth
            self._canvas.itemconfigure(self._win, width=cw, height=0)
        else:
            # content fits: stretch the interior to fill the visible area
            self._canvas.itemconfigure(self._win, width=cw, height=ch)
        self._canvas.configure(scrollregion=(0, 0, cw, max(ch, rh)))
        need = rh > ch
        if need and not self._vsb_shown:
            self._vsb.pack(side="right", fill="y", before=self._canvas)
            self._vsb_shown = True
        elif not need and self._vsb_shown:
            self._vsb.pack_forget()
            self._vsb_shown = False
            self._canvas.yview_moveto(0)

    def _wheel(self, e):
        # NOTE: on Windows the wheel event goes to the FOCUSED widget, so
        # e.widget is useless here - find the widget under the pointer instead.
        try:
            if not self._canvas.winfo_exists():
                return
            w = self.winfo_containing(e.x_root, e.y_root)
            if w is None:
                return
            # only react when the pointer is inside this frame
            if w is not self and not str(w).startswith(str(self) + "."):
                return
        except Exception:
            return
        if _wheel_should_pass(w, self._canvas):
            return
        if self.interior.winfo_reqheight() <= self._canvas.winfo_height():
            return   # nothing to scroll
        self._canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")


# ======================= drag-and-drop (optional) =======================
import re as _re

try:
    from tkinterdnd2 import TkinterDnD as _TkinterDnD, DND_FILES as _DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False


def make_root():
    """A drag-and-drop capable Tk root if tkinterdnd2 is installed, else plain Tk."""
    if _HAS_DND:
        try:
            return _TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def _first_dropped_path(data):
    parts = _re.findall(r"\{[^}]*\}|\S+", data or "")
    if not parts:
        return ""
    p = parts[0]
    return p[1:-1] if p.startswith("{") and p.endswith("}") else p


def enable_file_drop(widget, callback):
    """Let a video file be dropped onto `widget`; calls callback(path).
    No-op (returns False) when tkinterdnd2 isn't available."""
    if not _HAS_DND:
        return False
    try:
        widget.drop_target_register(_DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: callback(_first_dropped_path(e.data)))
        return True
    except Exception:
        return False


def enable_file_drop_deep(widget, callback):
    """Register file-drop on `widget` AND every descendant, so a drop anywhere
    inside a composite widget (e.g. the whole video player - canvas, timeline,
    buttons, empty space) works, not just on one sub-widget."""
    if not _HAS_DND:
        return False
    enable_file_drop(widget, callback)
    for child in widget.winfo_children():
        enable_file_drop_deep(child, callback)
    return True


def _all_dropped_paths(data):
    out = []
    for p in _re.findall(r"\{[^}]*\}|\S+", data or ""):
        out.append(p[1:-1] if p.startswith("{") and p.endswith("}") else p)
    return out


def enable_paths_drop(widget, callback):
    """Drop one or more files/folders onto `widget`; calls callback([paths]).
    No-op (returns False) when tkinterdnd2 isn't available."""
    if not _HAS_DND:
        return False
    try:
        widget.drop_target_register(_DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: callback(_all_dropped_paths(e.data)))
        return True
    except Exception:
        return False


def build_log_tab(notebook, text="  Log  "):
    """Add a '  Log  ' page to *notebook* holding a full-height, scrollable log
    box plus a 'Clear log' button. Returns the (read-only) Text widget so the
    caller can keep appending to it. Used by every tool tab so the log lives in
    its own sub-tab and the main view gets the whole window."""
    frame = ttk.Frame(notebook, padding=6)
    notebook.add(frame, text=text)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    # small requested height so this page doesn't force a hug-content notebook
    # tall; it still expands to fill via the row weight above
    box = tk.Text(frame, state="disabled", font=("Consolas", 9), wrap="none", height=8)
    box.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=sb.set)
    sb.grid(row=0, column=1, sticky="ns")

    def _clear():
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")
    ttk.Button(frame, text="Clear log", command=_clear).grid(
        row=1, column=0, sticky="w", pady=(4, 0))
    return box
