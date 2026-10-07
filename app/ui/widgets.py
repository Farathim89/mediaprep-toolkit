"""Small shared widgets: the HH:MM:SS:mmm TimeEntry, the scrollable help tabs
and the "?" help buttons / help windows."""
import tkinter as tk
from tkinter import ttk

from . import themes
from ..i18n import N_, tr


class TimeEntry(ttk.Frame):
    """Four boxes (HH : MM : SS : mmm) so times are easy to type.
    Empty box counts as 0; the last box is milliseconds (a decimal seconds
    value like 30.5 in the seconds box still works too).
    All boxes empty = no time given (used for 'to end of file').

    end=True makes it an inclusive "To" box: it SHOWS the last frame a
    section removes, while get_value() / set_value() speak the engine's
    exclusive end (the first frame kept) - one frame later on the grid of
    `fps` (a number or a callable returning the file's fps, None = unknown).
    A value put in with set_value() comes back exactly (no rounding drift)
    as long as the boxes weren't edited. For a From box (end=False)
    get_value() / set_value() are get_seconds() / set_seconds()."""

    def __init__(self, master, on_focus=None, end=False, fps=None):
        # one field box (border, accent on focus) with the four border-less
        # boxes and their ':' separators inside
        super().__init__(master, style="TimeBox.TFrame", padding=themes.pad(4, 2))
        self._on_focus = on_focus          # called with self when a box is focused
        self.is_end = bool(end)
        self._fps = fps
        self._raw_end = None               # (box texts, engine end) of the last set_value
        self.h, self.m = tk.StringVar(), tk.StringVar()
        self.s, self.ms = tk.StringVar(), tk.StringVar()
        self.vars = (self.h, self.m, self.s, self.ms)
        self._entries = []
        # widths count '0' digits of the box font (so they follow the DPI and
        # the font): 2 / 2 / 2 / 3 digits + one spare for the cursor
        for i, (var, w) in enumerate(((self.h, 3), (self.m, 3), (self.s, 3), (self.ms, 4))):
            e = ttk.Entry(self, textvariable=var, width=w, justify="center", style="Bare.TEntry")
            e.grid(row=0, column=i * 2)
            self._entries.append(e)
            if i < 3:
                ttk.Label(self, text=":", style="Bare.TLabel").grid(row=0, column=i * 2 + 1)
            e.bind("<FocusIn>", lambda ev: self._focused(), add="+")
            e.bind("<FocusOut>", lambda ev: self.after_idle(self._focus_left), add="+")
        self.bind("<Enter>", lambda ev: self._state("hover", True), add="+")
        self.bind("<Leave>", lambda ev: self._state("hover", False), add="+")
        # auto-jump to the next box once its digits are typed (2 for H/M/S)
        for i in range(3):
            self._entries[i].bind(
                "<KeyRelease>",
                lambda ev, i=i: self._advance(ev, i))

    def _state(self, flag, on):
        try:
            self.state([flag] if on else ["!" + flag])
        except tk.TclError:
            pass

    def _focus_left(self):
        try:
            if self.focus_get() not in self._entries:
                self._state("focus", False)
                self._normalize()
        except (tk.TclError, KeyError):
            self._state("focus", False)

    def _normalize(self):
        """Once the box loses focus, show a typed value zero-padded
        (4 -> 04, 30.5 s -> 30 : 500); an invalid entry is left as typed."""
        sec, ok = self.get_seconds()
        if ok and sec is not None:
            self.set_seconds(sec)

    def _focused(self):
        self._state("focus", True)
        if callable(self._on_focus):
            self._on_focus(self)

    def _advance(self, ev, i):
        if ev.char.isdigit() and len(self._entries[i].get()) >= 2:
            self._entries[i + 1].focus_set()
            self._entries[i + 1].icursor("end")

    def set_seconds(self, sec):
        """Fill the H:M:S:ms boxes from a float second count (frame-precise),
        always zero-padded: 124.958 -> 00 : 02 : 04 : 958."""
        if sec is None or sec < 0:
            sec = 0.0
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        for var, text in ((self.h, f"{h:02d}"), (self.m, f"{m:02d}"),
                          (self.s, f"{s:02d}"), (self.ms, f"{ms:03d}")):
            if var.get() != text:
                var.set(text)

    # ---- engine values (exclusive end for a To box) ----
    def fps(self):
        f = self._fps() if callable(self._fps) else self._fps
        try:
            f = float(f) if f else None
        except (TypeError, ValueError):
            f = None
        return f if f and f > 0 else None

    def _texts(self):
        return tuple(v.get() for v in self.vars)

    def shown(self, t):
        """Engine time -> the time this box shows for it."""
        if not self.is_end or t is None:
            return t
        from .frametime import excl_to_shown
        return excl_to_shown(t, self.fps())

    def engine(self, t):
        """A shown time (e.g. the player frame) -> this box's engine value."""
        if not self.is_end or t is None:
            return t
        from .frametime import shown_to_excl
        return shown_to_excl(t, self.fps())

    def set_value(self, t):
        """Fill the box from an engine time (None = empty)."""
        if t is None:
            self.clear()
            return
        self.set_seconds(self.shown(t))
        self._raw_end = (self._texts(), float(t)) if self.is_end else None

    def get_value(self):
        """(engine seconds | None, ok) - like get_seconds(), but a To box
        returns the exclusive end (the frame after the one shown)."""
        sec, ok = self.get_seconds()
        if not self.is_end or sec is None or not ok:
            return sec, ok
        raw = self._raw_end
        if raw is not None and raw[0] == self._texts():
            return raw[1], True
        return self.engine(sec), True

    def clear(self):
        """Empty all four boxes (= no time given)."""
        for var in self.vars:
            if var.get():
                var.set("")

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
        super().__init__(master, padding=themes.pad(8, 12, 4, 8))
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
            # tr(): the module-level sections are N_()-marked English; the
            # helpdocs ones arrive translated (tr of a translation = itself)
            box = ttk.LabelFrame(inner, text=tr(title))
            box.pack(fill="x", expand=True, padx=themes.px(4), pady=themes.pad(6, 8))
            lbl = ttk.Label(box, text=tr(body), wraplength=580, justify="left")
            lbl.pack(anchor="w", fill="x")
            self._labels.append(lbl)

        def on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", on_inner)

        # re-wrapping every card costs a full relayout - do it once the
        # resize settles, and only when the width really changed
        state = {"w": None, "job": None}

        def rewrap():
            state["job"] = None
            w = state["w"]
            canvas.itemconfigure(inner_id, width=w)
            for lbl in self._labels:
                lbl.configure(wraplength=max(300, w - themes.px(48)))

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


# ======================= hover tooltips =======================
class Tooltip:
    """A small hover tooltip for any widget. Shows after a short delay and
    hides on leave or click. Tkinter has no built-in tooltip, so this is it."""

    def __init__(self, widget, text, delay=450, follow=False):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.follow = follow          # show at the pointer (per-cell tips)
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
        if self.follow:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
        else:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)      # no title bar
        self.tip.wm_geometry(f"+{x}+{y}")
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass
        pal = themes.current()
        edge = tk.Frame(self.tip, background=pal["stroke"], borderwidth=0)
        edge.pack()
        tk.Label(edge, text=self.text, justify="left",
                 background=pal["tooltip_bg"], foreground=pal["tooltip_fg"],
                 borderwidth=0, highlightthickness=0, font="TkTooltipFont",
                 padx=themes.px(8), pady=themes.px(5),
                 wraplength=themes.px(360)).pack(padx=1, pady=1)

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


def info_icon(parent, text):
    """A small ⓘ (Segoe Fluent Icons 'Info', muted; accent on hover / focus)
    that shows `text` - an explanation that doesn't need to be on screen all
    the time - in the themed tooltip: on hover (after 300 ms, stays while
    hovered), on keyboard focus (Tab) and on click. The caller places it."""
    from .icons import glyph
    g = glyph("info")
    lbl = ttk.Label(parent, text=g or "ⓘ", style="Info.TLabel", takefocus=1,
                    cursor="hand2", font="MPIconSmall" if g else "TkDefaultFont")
    tip = Tooltip(lbl, text, delay=300)

    def state(on):
        try:
            lbl.state(["active"] if on else ["!active"])
        except tk.TclError:
            pass
    lbl.bind("<Enter>", lambda e: state(True), add="+")
    lbl.bind("<Leave>", lambda e: state(False), add="+")
    lbl.bind("<ButtonRelease-1>", lambda e: (lbl.focus_set(), tip._show()), add="+")
    lbl.bind("<FocusIn>", lambda e: tip._show(), add="+")
    lbl.bind("<FocusOut>", lambda e: tip._hide(), add="+")
    lbl.bind("<Escape>", lambda e: tip._hide(), add="+")
    return lbl


# ======================= translated drop-downs =======================
class KeyedCombobox(ttk.Combobox):
    """A ttk.Combobox that SHOWS translated labels while its `textvariable`
    keeps the stable KEY (the English text the settings / dicts use).

        KeyedCombobox(parent, textvariable=self.codec_var,
                      values=list(CODECS), state="readonly", width=40)

    values = the keys; labels default to tr(key) (labels=[...] overrides,
    translate=False shows the keys as-is). var.get() / cb.get() return the
    key, var.set(key) / cb.set(key) select it. configure(values=...),
    cb["values"] = ... and cget("values") work with keys too. Text typed into
    an editable box that isn't a known label is passed through as the key, so
    free values (e.g. "-14") keep working."""

    def __init__(self, master=None, textvariable=None, values=(), labels=None,
                 translate=True, **kw):
        self._keyvar = textvariable if textvariable is not None else tk.StringVar(master=master)
        self._translate = translate
        self._keys, self._k2l, self._l2k = [], {}, {}
        self._sync = False
        super().__init__(master, **kw)
        self._disp = tk.StringVar(master=self)
        super().configure(textvariable=self._disp)
        self._set_values(values, labels)
        self._t_key = self._keyvar.trace_add("write", self._key_changed)
        self._disp.trace_add("write", self._disp_changed)
        self.bind("<Destroy>", self._untrace, add="+")
        self._key_changed()

    # -- mapping
    def _label(self, key):
        key = str(key)
        if not self._translate:
            return key
        from ..i18n import tr
        return tr(key)

    def _set_values(self, keys, labels=None):
        if isinstance(keys, str):
            keys = self.tk.splitlist(keys)
        self._keys = [str(k) for k in (keys or ())]
        labels = list(labels) if labels is not None else [self._label(k) for k in self._keys]
        self._k2l = dict(zip(self._keys, labels))
        self._l2k = {lbl: k for k, lbl in zip(self._keys, labels)}
        super().configure(values=labels)
        self._key_changed()

    def _key_changed(self, *_a):
        if self._sync:
            return
        k = self._keyvar.get()
        self._sync = True
        try:
            self._disp.set(self._k2l.get(k, k))
        finally:
            self._sync = False

    def _disp_changed(self, *_a):
        if self._sync:
            return
        d = self._disp.get()
        self._sync = True
        try:
            self._keyvar.set(self._l2k.get(d, d))
        finally:
            self._sync = False

    def _untrace(self, e):
        if e.widget is self and self._t_key is not None:
            try:
                self._keyvar.trace_remove("write", self._t_key)
            except (tk.TclError, ValueError):
                pass
            self._t_key = None

    # -- Combobox API with keys
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = dict(cnf, **kw)
            cnf = None
        handled = False
        if "values" in kw or "labels" in kw:
            self._set_values(kw.pop("values", self._keys), kw.pop("labels", None))
            handled = True
        if handled and cnf is None and not kw:
            return None
        return super().configure(cnf, **kw)

    config = configure

    def __setitem__(self, key, value):
        if key == "values":
            self._set_values(value)
        else:
            super().__setitem__(key, value)

    def cget(self, key):
        if key == "values":
            return tuple(self._keys)
        if key == "textvariable":
            return str(self._keyvar)
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def get(self):
        return self._keyvar.get()

    def set(self, value):
        self._keyvar.set(value)

    def label(self):
        """The text shown (translated)."""
        return self._disp.get()


def auto_wrap(label, margin=4, minimum=80, width=None):
    """Give a ttk/tk Label a FIXED wrap width so a long (translated) hint
    wraps instead of widening the page. Pages have a fixed layout (they
    scroll instead of reflowing when the window is small), so there is
    nothing to follow on <Configure>. width = px at 100 % scaling (default:
    the label's own wraplength if it has one, else 560)."""
    try:
        cur = int(float(str(label.cget("wraplength")) or 0))
    except (tk.TclError, ValueError):
        cur = 0
    w = themes.px(width) if width else (themes.px(cur) if cur > 0 else themes.px(560))
    try:
        label.configure(wraplength=max(minimum, w - margin))
    except tk.TclError:
        pass
    return label


# ======================= theme-aware status colours =======================
# result colours (OK / broken / warning) come from the theme palette (ok /
# error / warn), which keeps them readable (>= 4.5:1) on every theme's lists
_STATUS_KEYS = {"good": "ok", "bad": "error", "warn": "warn"}


def status_palette(_widget=None):
    """{"good", "bad", "warn"} -> colour for the current theme."""
    pal = themes.current()
    return {kind: pal[key] for kind, key in _STATUS_KEYS.items()}


def bind_status_colors(widget, mapping):
    """Colour Treeview tags by status kind, e.g. {"bad": "bad", "unknown":
    "warn"}, and re-colour them whenever the theme changes."""
    def apply(pal):
        for tag, kind in mapping.items():
            widget.tag_configure(tag, foreground=pal[_STATUS_KEYS[kind]])
    themes.on_palette(widget, apply)


# ======================= log Text size cap =======================
MAX_LOG_LINES = 20000


def trim_text_lines(text, max_lines=MAX_LOG_LINES, chunk=2000):
    """Keep a Text log from growing forever: once it holds more than
    max_lines, drop the oldest lines in one chunk (not one line per append)."""
    try:
        lines = int(text.index("end-1c").split(".")[0])
        if lines > max_lines + chunk:
            text.delete("1.0", f"{lines - max_lines}.0")
    except Exception:
        pass


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
    """A tab's container: `.interior` for the content, `.bottom` for a strip
    pinned below it (progress bars).

    Pages have a FIXED natural size: they don't reflow with the window. The
    window's page area (ui/pageview.PageView) scrolls the whole page - both
    directions, scrollbars only when the window is smaller than the page -
    so this frame no longer scrolls by itself; it just passes its content's
    natural size up. (The name and attributes are kept for the tabs.)"""

    def __init__(self, master, canvas_width=None, bottom_master=None, **kw):
        super().__init__(master, **kw)
        # bottom_master = the window's fixed footer (app.py): the strip is
        # built there - outside the scrolling page - and app.py shows the
        # strip of the visible page only. Without it the strip sits below
        # the content (scrolls with the page).
        if bottom_master is not None:
            self.bottom = ttk.Frame(bottom_master)
        else:
            self.bottom = ttk.Frame(self)
            self.bottom.pack(side="bottom", fill="x")
        self.interior = ttk.Frame(self)
        self.interior.pack(side="top", fill="both", expand=True)


def fixed_label(parent, samples, style="TLabel", anchor="w", **kw):
    """A ttk.Label inside a frame of FIXED size - the widest of `samples`
    (texts) in the label's font, measured with every digit 0-9 - so a text
    that changes many times a second (a time / frame counter, a percentage)
    never changes the label's size and never makes the layout around it
    shift. `samples` may be a callable returning the texts. Re-measured
    when the theme (fonts) changes, or by holder.remeasure() (e.g. once a
    longer value becomes possible). Returns (holder_frame, label); place
    the holder."""
    import tkinter.font as tkfont
    holder = ttk.Frame(parent)
    holder.pack_propagate(False)
    lbl = ttk.Label(holder, style=style, anchor=anchor, **kw)
    lbl.pack(fill="both", expand=True)

    def measure(_p=None):
        try:
            font = lbl.cget("font") or ttk.Style(lbl).lookup(style, "font") or "TkDefaultFont"
            f = tkfont.Font(root=lbl, font=font)
            pad = ttk.Style(lbl).lookup(style, "padding") or 0
            try:
                extra = sum(int(float(v)) for v in str(pad).split()[:1]) * 2
            except ValueError:
                extra = 0
            w = 0
            for s in (samples() if callable(samples) else samples):
                w = max(w, max(f.measure("".join(d if ch.isdigit() else ch for ch in s))
                               for d in "0123456789"))
            lbl.update_idletasks()
            holder.configure(width=w + extra + themes.px(4),
                             height=max(f.metrics("linespace") + themes.px(2),
                                        lbl.winfo_reqheight()))
        except tk.TclError:
            pass
    measure()
    themes.on_palette(holder, measure)
    holder.remeasure = measure
    return holder, lbl


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


def build_log_tab(notebook, text=None):
    """Add a '  Log  ' page to *notebook* holding a full-height, scrollable log
    box plus a 'Clear log' button. Returns the (read-only) Text widget so the
    caller can keep appending to it. Used by every tool tab so the log lives in
    its own sub-tab and the main view gets the whole window."""
    frame = ttk.Frame(notebook, padding=themes.pad(0, 10, 0, 6))
    notebook.add(frame, text=text if text is not None else f"  {tr('Log')}  ")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    # small requested height so this page doesn't force a hug-content notebook
    # tall; it still expands to fill via the row weight above
    box = tk.Text(frame, state="disabled", font="MPMono", wrap="none", height=8,
                  padx=themes.px(8), pady=themes.px(6))
    box.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=sb.set)
    sb.grid(row=0, column=1, sticky="ns")

    def _clear():
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")
    from .icons import decorate
    decorate(ttk.Button(frame, text=tr("Clear log"), command=_clear), "trash").grid(
        row=1, column=0, sticky="w", pady=themes.pad(8, 0))
    return box


# ======================= "?" help buttons / help windows =======================
_HELP_WINDOWS = {}      # key -> open Toplevel (one window per key)


def _theme_colors(_widget=None):
    """(bg, fg, text_bg, text_fg, accent) of the current theme."""
    p = themes.current()
    return p["bg"], p["fg"], p["field"], p["fg"], p["accent"]


def _open_help_window(parent, wkey, title, sections, index=False):
    """Themed, scrollable help window (re-used/raised if already open).
    sections = [(heading, text)]; index=True adds a clickable topic list."""
    old = _HELP_WINDOWS.get(wkey)
    if old is not None:
        try:
            if old.winfo_exists():
                old.deiconify()
                old.lift()
                old.focus_force()
                return old
        except tk.TclError:
            pass
    top = parent.winfo_toplevel()
    bg, fg, _tbg, tfg, _acc = _theme_colors(top)
    win = tk.Toplevel(top)
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
        win.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        win.geometry(f"{w}x{h}")
    win.minsize(int(320 * dpi), int(220 * dpi))

    outer = ttk.Frame(win, padding=themes.pad(16, 12, 16, 12))
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text=title, style="Subtitle.TLabel").pack(anchor="w",
                                                             pady=themes.pad(0, 10))
    bar = ttk.Frame(outer)
    bar.pack(side="bottom", fill="x", pady=themes.pad(12, 0))
    ttk.Button(bar, text=tr("Close"), command=win.destroy).pack(side="right")

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
    win.lift()
    # focus_set on a window that isn't mapped yet doesn't activate it
    win.after(50, lambda: win.winfo_exists() and win.focus_force())
    return win


def show_help(parent, key):
    """Open (or raise) the help window for a helpdocs key."""
    from .. import helpdocs
    title, body = helpdocs.get(key)
    return _open_help_window(parent, "help:" + key, tr("Help – {title}", title=title),
                             [(title, body)])


def show_help_index(parent):
    """Open (or raise) the help window listing every tab's help."""
    from .. import helpdocs
    return _open_help_window(parent, "help:index", tr("{app} – Help", app="MediaPrep Toolkit"),
                             helpdocs.sections(), index=True)


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


# ======================= Fluent info bar =======================
class InfoBar(tk.Frame):
    """A Windows 11 style InfoBar: tinted strip with a coloured accent edge,
    a severity icon, the message (wraps), an optional link and a close ×.
    kind = "error" / "warn" / "info"."""
    _ICONS = {"error": "error", "warn": "warning", "info": "info"}

    def __init__(self, master, text, kind="info", link_text=None, on_link=None,
                 closable=True, on_close=None):
        super().__init__(master, borderwidth=0, highlightthickness=1)
        from .icons import glyph
        self.kind = kind
        self._on_close = on_close
        self.edge = tk.Frame(self, width=themes.px(4), borderwidth=0)
        self.edge.pack(side="left", fill="y")
        ic = glyph(self._ICONS.get(kind, "info"))
        self.icon = tk.Label(self, text=ic or "\u26a0", font="MPIcon" if ic else "MPSemibold",
                             borderwidth=0, padx=themes.px(10))
        self.icon.pack(side="left", anchor="n", pady=themes.px(9))
        if closable:
            cg = glyph("close")
            self.close_lbl = tk.Label(self, text=cg or "\u2715", cursor="hand2",
                                      font="MPIconSmall" if cg else "TkDefaultFont",
                                      borderwidth=0, padx=themes.px(12))
            self.close_lbl.pack(side="right", anchor="n", pady=themes.px(10))
            self.close_lbl.bind("<Button-1>", lambda e: self.close())
        else:
            self.close_lbl = None
        self.link = None
        if link_text:
            self.link = tk.Label(self, text=link_text, cursor="hand2", borderwidth=0,
                                 font="MPSemibold", padx=themes.px(6))
            self.link.pack(side="right", anchor="n", pady=themes.px(8))
            if on_link is not None:
                self.link.bind("<Button-1>", lambda e: on_link())
        self.msg = tk.Label(self, text=text, justify="left", anchor="w", borderwidth=0,
                            font="TkDefaultFont")
        self.msg.pack(side="left", fill="x", expand=True, pady=themes.px(8))
        auto_wrap(self.msg, margin=8, width=820)
        themes.on_palette(self, self._repaint)

    def _repaint(self, p):
        k = self.kind
        bg, fg = p[f"banner_{k}_bg"], p[f"banner_{k}_fg"]
        acc = p[f"banner_{k}_accent"]
        self.configure(bg=bg, highlightbackground=themes.mix(bg, p["fg"], 0.12),
                       highlightcolor=themes.mix(bg, p["fg"], 0.12))
        self.edge.configure(bg=acc)
        self.icon.configure(bg=bg, fg=acc)
        self.msg.configure(bg=bg, fg=fg)
        if self.link is not None:
            self.link.configure(bg=bg, fg=fg, font="MPSemibold")
        if self.close_lbl is not None:
            self.close_lbl.configure(bg=bg, fg=fg)

    def close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass
        if callable(self._on_close):
            self._on_close()
