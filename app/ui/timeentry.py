"""The HH:MM:SS:mmm time box (TimeEntry) of the cut / template pages."""
import tkinter as tk
from tkinter import ttk

from . import themes
from .tooltips import Tooltip


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
        self._warn = None                  # the ⚠ label (set_warning), made on demand
        self._warn_texts = None            # box texts the warning was given for
        for var in self.vars:
            var.trace_add("write", lambda *_a: self._warn_check())

    # ---- "approximate edge" warning ----
    def set_warning(self, text=None):
        """Show a small ⚠ after the boxes with `text` as its tooltip (e.g. a
        boundary detection placed by sound / a fade, not on an exact frame);
        None hides it. Call it after filling the box: the mark goes away by
        itself as soon as the value changes (Set, Snap, typing, a new fill)."""
        if not text:
            if self._warn is not None:
                self._warn.grid_remove()
            self._warn_texts = None
            return
        if self._warn is None:
            self._warn = ttk.Label(self, text="⚠", style="Bare.TLabel")
            themes.on_palette(self._warn, lambda p: self._warn.configure(
                foreground=p.get("warn", "#d08000")))
            self._warn_tip = Tooltip(self._warn, text, delay=200)
        self._warn_tip.text = text
        self._warn.grid(row=0, column=7, padx=(themes.px(3), 0))
        self._warn_texts = self._texts()

    def _warn_check(self):
        if self._warn_texts is not None and self._texts() != self._warn_texts:
            self.set_warning(None)

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

    def value_for(self, fps):
        """(engine seconds | None, ok) for a file of frame rate `fps`: like
        get_value(), but on THAT file's frame grid (frametime.shown_to_excl)
        - e.g. one shown To time applied to many files whose rates differ,
        or by a box that has no fps of its own (no row selected)."""
        sec, ok = self.get_seconds()
        if not self.is_end or sec is None or not ok:
            return sec, ok
        from .frametime import shown_to_excl
        return shown_to_excl(sec, fps), True

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
