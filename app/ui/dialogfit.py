"""Dialogs that fit their monitor.

At a high display scaling a monitor has few logical pixels (a 4K screen at
300 % shows ~720 lines), so a long dialog could put its OK / Cancel off the
bottom of the screen. Every dialog is therefore capped to the work area (the
monitor minus the taskbar) of the monitor it opens on:

    ScrollBody   a dialog's body - its content goes into `.interior`. At
                 its natural size it is exactly that content (no scrollbar,
                 nothing looks different); when the dialog is capped it
                 scrolls vertically (scrollbar + mouse wheel). The dialog's
                 button row is packed OUTSIDE it (before it, side="bottom"),
                 so the buttons are always on screen.
    fit_dialog   the size a dialog gets on a work area: its natural size
                 when that fits, else capped - its ScrollBodies scroll then
                 (a dialog with a `_mp_compact(on)` method may first try a
                 more compact layout, e.g. Settings in two columns).
    refit/watch  the same for a dialog that is already open (after the
                 display scaling changed - ui/dpi.py).
    cap_window   size + position of a window with a size of its own (the
                 help windows), kept on its monitor's work area.

ui/dialogs.place_dialog() applies fit_dialog to every dialog it shows."""
import tkinter as tk
from tkinter import ttk

from . import themes


def decoration():
    """(width, height) the borders and the title bar add to a dialog's
    inner size, at the current UI scale."""
    d = int(round(themes.scale() * 96))
    try:
        import ctypes
        f = ctypes.windll.user32.GetSystemMetricsForDpi
        cap = f(4, d)                              # SM_CYCAPTION
        bx, by, pad = f(32, d), f(33, d), f(92, d)  # SM_CXFRAME, SM_CYFRAME, SM_CXPADDEDBORDER
        if cap > 0:
            return 2 * (bx + pad), cap + 2 * (by + pad)
    except Exception:
        pass
    return themes.px(16), themes.px(40)


def work_area(win):
    """(left, top, right, bottom) of the work area of the monitor `win` (a
    toplevel) is on - the screen when that can't be asked."""
    from . import dpi
    h = dpi.frame_of(win)
    area = None
    if h:
        area = dpi._work_area(h)
    else:
        try:
            x, y = win.winfo_rootx(), win.winfo_rooty()
            area = dpi._rect_work_area((x, y, x + max(1, win.winfo_width()),
                                        y + max(1, win.winfo_height())))
        except tk.TclError:
            area = None
    if area is None:
        area = (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
    return area


# ---------------------------------------------------------------- the body
def _bodies(top):
    out = []
    for b in getattr(top, "_mp_scrollbodies", ()):
        try:
            if b.winfo_exists():
                out.append(b)
        except tk.TclError:
            pass
    try:
        top._mp_scrollbodies = out
    except Exception:
        pass
    return out


class ScrollBody(ttk.Frame):
    """The body of a dialog (see the module docstring): build the content in
    `.interior`, pack the button row before this frame (side="bottom")."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, width=1, height=1, highlightthickness=0, borderwidth=0,
                                takefocus=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.interior = ttk.Frame(self.canvas)
        self._item = self.canvas.create_window(0, 0, window=self.interior, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrolling = False
        self._req = None
        self.interior.bind("<Configure>", lambda e: self.sync(), add="+")
        self.canvas.bind("<Configure>", self._on_canvas, add="+")
        top = self.winfo_toplevel()
        try:
            top._mp_scrollbodies = _bodies(top) + [self]
        except Exception:
            pass
        # the wheel event goes to the focused widget: the dialog's own tag
        # (a bindtag of every widget in it) sees it - see _on_wheel
        top.bind("<MouseWheel>", self._on_wheel, add="+")

    def sync(self):
        """Request the content's natural size (the canvas doesn't pass it
        up by itself) and update the scroll region."""
        try:
            w, h = self.interior.winfo_reqwidth(), self.interior.winfo_reqheight()
        except tk.TclError:
            return
        if (w, h) != self._req:
            self._req = (w, h)
            self.canvas.configure(width=w, height=h, scrollregion=(0, 0, w, h))
        self._on_canvas()

    def _on_canvas(self, _e=None):
        # wider than the content (a stretched dialog): the content follows
        # the width; else it keeps its own (a width given to the item would
        # hide later changes of the content's width)
        try:
            cw = self.canvas.winfo_width()
            want = cw if self._req and cw > self._req[0] else 0
            if str(self.canvas.itemcget(self._item, "width")) != str(want):
                self.canvas.itemconfigure(self._item, width=want)
        except tk.TclError:
            pass

    def set_scroll(self, on):
        """Show / hide the scrollbar (fit_dialog decides)."""
        on = bool(on)
        if on == self.scrolling:
            return
        self.scrolling = on
        if on:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        else:
            self.vsb.pack_forget()
            self.canvas.yview_moveto(0)

    def _on_wheel(self, e):
        if not self.scrolling:
            return None
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return None
        if w is None:
            return None
        me = str(self)
        if str(w) != me and not str(w).startswith(me + "."):
            return None
        from .widgets import _wheel_should_pass
        if _wheel_should_pass(w, self.canvas):
            return None
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"


# ---------------------------------------------------------------- fitting
def _natural(dlg, bodies):
    for b in bodies:
        b.set_scroll(False)
    dlg.update_idletasks()
    for b in bodies:
        b.sync()
    dlg.update_idletasks()
    return dlg.winfo_reqwidth(), dlg.winfo_reqheight()


def fit_dialog(dlg, area):
    """The inner size for `dlg` on work area `area` = (left, top, right,
    bottom): (w, h, capped). capped=True: smaller than the natural size -
    the dialog must be given that size (its ScrollBodies now scroll)."""
    bodies = _bodies(dlg)
    compact = getattr(dlg, "_mp_compact", None)
    if callable(compact):
        compact(False)
    w, h = _natural(dlg, bodies)
    dw, dh = decoration()
    left, top, right, bottom = area
    aw, ah = max(1, right - left - dw), max(1, bottom - top - dh)
    if h > ah and callable(compact) and compact(True):
        w2, h2 = _natural(dlg, bodies)
        if w2 <= aw:
            w, h = w2, h2
        else:                                    # too wide that way: one column + scroll
            compact(False)
            w, h = _natural(dlg, bodies)
    if h <= ah and w <= aw:
        return w, h, False
    if h > ah and bodies:
        for b in bodies:
            b.set_scroll(True)
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth()
    return min(w, aw), min(h, ah), True


def refit(dlg):
    """Re-fit an open dialog (e.g. after the display scaling changed): its
    natural size when that fits now, else capped to its monitor (top-left
    corner kept, moved onto the monitor if needed)."""
    try:
        if not dlg.winfo_exists() or dlg.state() != "normal":
            return
        area = work_area(dlg)
        w, h, capped = fit_dialog(dlg, area)
        dw, dh = decoration()
        left, top, right, bottom = area
        x0, y0 = dlg.winfo_x(), dlg.winfo_y()
        x = max(left, min(x0, right - w - dw))
        y = max(top, min(y0, bottom - h - dh))
        if capped:
            dlg.geometry(f"{w}x{h}+{x}+{y}")
        elif getattr(dlg, "_mp_capped", False):
            dlg.geometry("")                     # back to the natural size
            dlg.geometry(f"+{x}+{y}")
        elif (x, y) != (x0, y0):                 # grown (e.g. two columns): back on screen
            dlg.geometry(f"+{x}+{y}")
        dlg._mp_capped = capped
    except tk.TclError:
        pass


def watch(dlg):
    """Re-fit `dlg` after every display scaling change while it is open
    (once its rescaled widgets have been laid out)."""
    if getattr(dlg, "_mp_fit_watch", False):
        return
    dlg._mp_fit_watch = True
    from . import dpi
    dpi.on_change(dlg, lambda _r: refit(dlg), late=True)


def cap_window(win, w, h, x, y, area=None):
    """(w, h, x, y) for a window with a size of its own (inner size w x h,
    top-left corner x, y): no larger than the work area `area` (default:
    the monitor `win` is on) and on it."""
    if area is None:
        area = work_area(win)
    left, top, right, bottom = area
    dw, dh = decoration()
    w = max(1, min(w, right - left - dw))
    h = max(1, min(h, bottom - top - dh))
    x = max(left, min(x, right - w - dw))
    y = max(top, min(y, bottom - h - dh))
    return w, h, x, y
