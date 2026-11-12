"""The VideoPlayer's seek bar (TimelineBar) and the colours of the marked
segments on it, per theme."""
import tkinter as tk

from ..engine.formatting import fmt_time
from . import themes

# segment band colours on the timeline, per theme (palette marker_<kind>).
# Pass the KIND ("intro", ...) to set_markers so the bands follow theme
# changes; MARKER_COLORS (kind -> current colour) is kept for old callers.
MARKER_KINDS = ("preintro", "intro", "credits", "aftercredits")

MARKER_COLORS = {}


def _sync_marker_colors(_name=None, pal=None):
    pal = pal or themes.current()
    MARKER_COLORS.update({k: pal["marker_" + k] for k in MARKER_KINDS})


_sync_marker_colors()
themes.subscribe(_sync_marker_colors)


def marker_color(c, pal=None):
    """A marker colour given as a kind ("intro"), a palette key ("warn") or a
    literal colour -> the colour to draw with the current theme."""
    pal = pal or themes.current()
    if c in MARKER_KINDS:
        return pal["marker_" + c]
    if isinstance(c, str) and c.startswith("#"):
        return c
    return pal.get(c, pal["border"])


class TimelineBar(tk.Canvas):
    """A slim seek bar: rounded track with the played part in the accent
    colour, the marked segments as coloured range bands with small edge
    handles, a round playhead thumb and a time tooltip under the pointer.
    Click or drag to seek (calls on_seek(fraction)); on_release() is called
    when the mouse button is let go."""

    def __init__(self, master, on_seek=None, height=30, on_release=None, **kw):
        super().__init__(master, height=themes.px(height), highlightthickness=0,
                         borderwidth=0, **kw)
        self.on_seek = on_seek
        self.on_release = on_release
        self.duration = 0.0
        self.pos = 0.0
        self.markers = []   # list of (start_sec, end_sec, color)
        self._size = None
        self._thumb = None
        self._tip = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._click)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self._hide_tip())
        self._pal = themes.current()
        themes.on_palette(self, self._repaint)

    def _repaint(self, pal):
        self._pal = pal
        self._thumb = None
        self.configure(bg=pal["bg"])
        self._redraw()

    def _on_configure(self, e):
        # a redraw is cheap, but only do it when the size really changed
        if (e.width, e.height) != self._size:
            self._size = (e.width, e.height)
            self._redraw()

    def set_duration(self, dur):
        self.duration = max(0.0, dur or 0.0)
        self._redraw()

    def set_position(self, sec):
        self.pos = max(0.0, min(sec, self.duration)) if self.duration else 0.0
        self._place_head()

    def set_markers(self, markers):
        self.markers = markers or []
        self._redraw()

    # geometry: the track runs between two insets so the thumb never clips
    def _inset(self):
        return themes.px(8)

    def _x(self, sec):
        i = self._inset()
        w = max(1, self.winfo_width() - 2 * i)
        return i + (int((sec / self.duration) * w) if self.duration else 0)

    def _frac(self, x):
        i = self._inset()
        w = max(1, self.winfo_width() - 2 * i)
        return min(max((x - i) / w, 0.0), 1.0)

    def _click(self, e):
        if not self.duration or not self.on_seek:
            return
        self.on_seek(self._frac(e.x))
        self._hover(e)

    def _release(self, _e=None):
        if self.on_release:
            self.on_release()

    def _thumb_img(self):
        """Anti-aliased round thumb (Pillow), cached per theme."""
        if self._thumb is not None:
            return self._thumb or None
        self._thumb = False
        try:
            import base64
            import io
            from PIL import Image, ImageDraw
            pal = self._pal
            d = themes.px(14)
            ss = 4
            # opaque on the bar's background: a plain blit for Tk (alpha
            # photos are blended per pixel on every redraw)
            im = Image.new("RGB", (d * ss, d * ss), pal["bg"])
            dr = ImageDraw.Draw(im)
            dr.ellipse((0, 0, d * ss - 1, d * ss - 1), fill=pal["fg"])
            o = int(d * ss * 0.2)
            dr.ellipse((o, o, d * ss - 1 - o, d * ss - 1 - o), fill=pal["accent"])
            im = im.resize((d, d), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            self._thumb = tk.PhotoImage(master=self, data=base64.b64encode(buf.getvalue()))
        except Exception:
            self._thumb = False
        return self._thumb or None

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        mid = h // 2
        pal = self._pal
        i = self._inset()
        track = max(2, themes.px(4))
        # base track (round caps) + played part
        self.create_line(i, mid, max(i + 1, w - i), mid, fill=pal["progress_track"],
                         width=track, capstyle="round", tags=("track",))
        self.create_line(i, mid, i, mid, fill=pal["accent"], width=track, capstyle="round",
                         tags=("played",))
        # coloured marker bands with a small handle at each end
        band = themes.px(12)
        for s, e, color in self.markers:
            if self.duration and e > s:
                x0, x1 = self._x(s), self._x(e)
                color = marker_color(color, pal)
                self.create_rectangle(x0, mid - band // 2, max(x1, x0 + 2), mid + band // 2,
                                      fill=color, outline="", tags=("band",))
                hw = max(1, themes.px(2))
                for x in (x0, max(x1, x0 + 2)):
                    self.create_rectangle(x - hw // 2, mid - band // 2 - themes.px(3),
                                          x - hw // 2 + hw, mid + band // 2 + themes.px(3),
                                          fill=color, outline="", tags=("band",))
        img = self._thumb_img()
        if img is not None:
            self.create_image(i, mid, image=img, tags=("head",))
        else:
            r = themes.px(6)
            self.create_oval(i - r, mid - r, i + r, mid + r, fill=pal["accent"],
                             outline=pal["fg"], tags=("head",))
        self._place_head()

    def _place_head(self):
        """Move the played bar + thumb (cheap - runs on every played frame)."""
        try:
            h = max(1, self.winfo_height())
        except tk.TclError:
            return
        mid = h // 2
        i = self._inset()
        px = self._x(self.pos)
        self.coords("played", i, mid, max(i, px), mid)
        self.itemconfigure("played", state="normal" if px > i else "hidden")
        bb = self.bbox("head")
        if bb:
            cx = (bb[0] + bb[2]) // 2
            self.move("head", px - cx, 0)
        self.tag_raise("head")

    # hover: the time under the pointer
    def _hover(self, e):
        if not self.duration:
            self._hide_tip()
            return
        text = fmt_time(self._frac(e.x) * self.duration)
        pal = self._pal
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            try:
                self._tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            self._tip_lbl = tk.Label(self._tip, font="MPMono", borderwidth=0,
                                     padx=themes.px(6), pady=themes.px(2))
            self._tip_lbl.pack(padx=1, pady=1)
        self._tip.configure(bg=pal["stroke"])
        self._tip_lbl.configure(text=text, bg=pal["tooltip_bg"], fg=pal["tooltip_fg"])
        self._tip.update_idletasks()
        tw = self._tip.winfo_reqwidth()
        self._tip.wm_geometry(f"+{e.x_root - tw // 2}+{self.winfo_rooty() - self._tip.winfo_reqheight() - themes.px(4)}")

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
