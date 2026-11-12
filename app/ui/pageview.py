"""The page area of the main window: fixed-size pages in a 2-D scroll view.

Pages don't follow every resize step. Each page is laid out at a fixed size
- the `design` size (the page area of the default window), but never smaller
than its natural (requested) size and, in a window smaller than the default
one, only as large as the visible area (still never below its natural size)
- and sits top-left in a canvas. The size is re-checked when the page is
shown, when the window resize has settled and whenever the page's content
needs more room (a video loaded, a list filled, a longer text, another DPI /
font): content is never clipped. Horizontal / vertical scrollbars appear
while the window is smaller than the page's natural size and hide again
otherwise; a larger window shows the theme background around the page.

Mouse wheel = vertical, Shift+wheel = horizontal. Wheel events over widgets
that scroll themselves (lists, text boxes, tree views, scrolling canvases)
are left to them.
"""
import sys
import time
import tkinter as tk
from tkinter import ttk

from . import themes
from .widgets import _wheel_should_pass


if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes as _wt
        _RedrawWindow = ctypes.WinDLL("user32").RedrawWindow
        _RedrawWindow.argtypes = [_wt.HWND, ctypes.c_void_p, _wt.HANDLE, _wt.UINT]
    except Exception:
        _RedrawWindow = None
else:
    _RedrawWindow = None

_FAST = {"done": False, "hook": None}


def _sizing():
    """True while Windows' modal move / size loop runs for the main window
    (the user drags its border, a corner or the title bar) - the _SizeHook
    sees WM_ENTERSIZEMOVE / WM_EXITSIZEMOVE. Only this window's own drag
    counts: a mouse button held in another program doesn't."""
    hook = _FAST["hook"]
    return bool(hook is not None and hook.alive and hook.sizing)


def fast_resize(root):
    """Windows: make resizing the main window cheap. Call it once the window
    exists (mapped); later calls do nothing.

    1. Tk registers its window classes - TkTopLevel (the frame of a window)
       and TkChild (every widget) - with CS_HREDRAW | CS_VREDRAW: a window
       that changes size is invalidated together with ALL its child windows.
       A resize step resizes the frame and the nested containers of the page
       area, so every widget was repainted about five times per step. The two
       styles are removed (per process and class: every window and dialog);
       Tk still redraws each widget it resizes or re-lays out.
    2. Even so, Windows (DWM) invalidates every child window whenever a
       top-level window changes size, although their pixels are kept. A
       small window-procedure hook on the main window's frame (_SizeHook)
       adds SWP_NOREDRAW to size changes that keep the window's top-left
       corner in place and invalidates only the strips the window grew by.
    Measured at 150 % scaling: 170-850 ms per resize step before, ~15 ms
    after (see PageView.window_resized for the page side)."""
    if _FAST["done"] or sys.platform != "win32":
        return
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32")
        get = getattr(u32, "GetClassLongPtrW", None) or u32.GetClassLongW
        put = getattr(u32, "SetClassLongPtrW", None) or u32.SetClassLongW
        get.restype = put.restype = ctypes.c_size_t
        get.argtypes = [ctypes.c_void_p, ctypes.c_int]
        put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        gcl_style, redraw = -26, 0x0001 | 0x0002        # CS_VREDRAW | CS_HREDRAW
        frame = int(root.wm_frame(), 16)
        if not frame or frame == root.winfo_id():
            return                                      # no frame (wrapper) yet
        for hwnd in (frame, root.winfo_id()):
            style = get(hwnd, gcl_style)
            if style & redraw:
                put(hwnd, gcl_style, style & ~redraw)
        _FAST["done"] = True
        _FAST["hook"] = _SizeHook(frame)
    except Exception:
        pass


class _SizeHook:
    """Window procedure hook on a Tk frame (wrapper) window.

    Size changes that keep the window's top-left corner where it is (a
    right / bottom edge drag; no show / hide / frame change) get
    SWP_NOREDRAW, so Windows doesn't invalidate every child window: the
    pixels it keeps are still in the right place, and the strips the client
    area grew by are invalidated here (with their children). Tk redraws the
    widgets it resizes or moves. Everything else - a left / top edge drag, a
    snap, a move + resize, maximise / restore - passes unchanged: there the
    content moves on screen and Windows has to repaint it (cheap enough
    without the class redraw styles). `sizing` is True while Windows' modal
    move / size loop runs (WM_ENTERSIZEMOVE .. WM_EXITSIZEMOVE).

    WM_DPICHANGED (the window moved to a monitor with another scaling) goes
    to `dpi_handler` (ui/dpi.py: takes the suggested size, queues the UI
    rescale) instead of Tk, which doesn't know the message.

    The hook never calls into Tk (it runs inside Tk's own message
    dispatch). Every message goes to Tk's window procedure exactly once;
    the hook removes itself on WM_NCDESTROY."""

    dpi_handler = None      # fn(hwnd, wparam, lparam) -> True when handled
    minmax_handler = None   # fn(hwnd, lparam) after Tk answered WM_GETMINMAXINFO
    display_handler = None  # fn(hwnd) on WM_DISPLAYCHANGE (monitors / resolution changed)

    def __init__(self, hwnd):
        import ctypes
        from ctypes import wintypes as wt
        self._c, self._wt = ctypes, wt
        u32 = ctypes.WinDLL("user32")
        lres = ctypes.c_ssize_t
        self._proto = ctypes.WINFUNCTYPE(lres, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        setp = getattr(u32, "SetWindowLongPtrW", None) or u32.SetWindowLongW
        setp.restype = ctypes.c_void_p
        setp.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        self._call = u32.CallWindowProcW
        self._call.restype = lres
        self._call.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self._client = u32.GetClientRect
        self._client.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self._wrect = u32.GetWindowRect
        self._wrect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self._redraw = u32.RedrawWindow
        self._redraw.argtypes = [wt.HWND, ctypes.c_void_p, wt.HANDLE, wt.UINT]
        self._setp = setp

        class WINDOWPOS(ctypes.Structure):
            _fields_ = [("hwnd", wt.HWND), ("after", wt.HWND), ("x", ctypes.c_int),
                        ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int),
                        ("flags", wt.UINT)]
        self._pos = ctypes.POINTER(WINDOWPOS)
        self.hwnd = hwnd
        self.alive = False
        self.sizing = False
        self._old_size = None
        self._fn = self._proto(self._proc)          # keep a reference (GC = crash)
        self._old = setp(hwnd, -4, ctypes.cast(self._fn, ctypes.c_void_p))   # GWLP_WNDPROC
        self.alive = bool(self._old)

    def _size(self, hwnd):
        rc = self._wt.RECT()
        self._client(hwnd, self._c.byref(rc))
        return rc.right - rc.left, rc.bottom - rc.top

    def _changing(self, hwnd, lp):
        """WM_WINDOWPOSCHANGING (after Tk has seen it): SWP_NOREDRAW for a
        plain size change that keeps the top-left corner in place."""
        pos = self._c.cast(lp, self._pos).contents
        # SWP_NOSIZE | NOREDRAW | FRAMECHANGED | SHOWWINDOW | HIDEWINDOW: as is
        if pos.flags & (0x0001 | 0x0008 | 0x0020 | 0x0040 | 0x0080):
            return
        if not pos.flags & 0x0002:                              # no SWP_NOMOVE
            rc = self._wt.RECT()
            if (not self._wrect(hwnd, self._c.byref(rc))
                    or (pos.x, pos.y) != (rc.left, rc.top)):
                return          # the content moves on screen: Windows repaints it
        pos.flags |= 0x0008                                     # SWP_NOREDRAW
        self._old_size = self._size(hwnd)

    def _grown(self, hwnd):
        """WM_WINDOWPOSCHANGED after a SWP_NOREDRAW change: invalidate the
        strips the client area grew by (with their child windows)."""
        ow, oh = self._old_size
        self._old_size = None
        w, h = self._size(hwnd)
        for rc in ((ow, 0, w, h), (0, oh, w, h)):
            if rc[0] < rc[2] and rc[1] < rc[3]:
                r = self._wt.RECT(*rc)
                # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN
                self._redraw(hwnd, self._c.byref(r), None, 0x1 | 0x4 | 0x80)

    def _proc(self, hwnd, msg, wp, lp):
        old = self._old
        if msg == 0x0024 and _SizeHook.minmax_handler is not None:   # WM_GETMINMAXINFO
            res = self._call(old, hwnd, msg, wp, lp)
            try:
                _SizeHook.minmax_handler(hwnd, lp)
            except Exception:
                pass
            return res
        if msg == 0x0046:                                       # WM_WINDOWPOSCHANGING
            self._old_size = None
            res = self._call(old, hwnd, msg, wp, lp)
            try:
                self._changing(hwnd, lp)
            except Exception:
                self._old_size = None
            return res
        try:
            if msg == 0x0047:                                   # WM_WINDOWPOSCHANGED
                if self._old_size is not None:
                    self._grown(hwnd)
            elif msg == 0x0231:                                 # WM_ENTERSIZEMOVE
                self.sizing = True
            elif msg == 0x0232:                                 # WM_EXITSIZEMOVE
                self.sizing = False
            elif msg == 0x02E0:                                 # WM_DPICHANGED
                fn = _SizeHook.dpi_handler
                if fn is not None and fn(hwnd, wp, lp):
                    return 0
            elif msg == 0x007E:                                 # WM_DISPLAYCHANGE
                fn = _SizeHook.display_handler        # (Tk still gets it)
                if fn is not None:
                    fn(hwnd)
            elif msg == 0x0082:                                 # WM_NCDESTROY
                self.alive = self.sizing = False
                self._setp(hwnd, -4, old)
        except Exception:
            self._old_size = None
        return self._call(old, hwnd, msg, wp, lp)


class PageView(ttk.Frame):
    def __init__(self, master, design=(0, 0), inset=(0, 0), design_fn=None):
        super().__init__(master)
        self.design = design
        # design_fn() -> (w, h): called once the view has its real size (the
        # page area of the default window, derived from the current one)
        self.design_fn = design_fn
        self.inset = inset
        # (inset, design) and the DPI factor they are for: re-derived from
        # these on a display scaling change (no drift back and forth)
        self._inset0 = (inset, themes.scale())
        self._design0 = (design, themes.scale())
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                xscrollincrement=themes.px(24),
                                yscrollincrement=themes.px(24))
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.hsb.set, yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._items = {}            # page widget -> canvas window item
        self._sizes = {}            # page widget -> (w, h, viewport) it is laid out at
        self._cur = None
        self._shown = {"v": False, "h": False}
        self._view = None
        self._live = False          # the window is being dragged to a new size
        self._rz_t = 0.0            # time of the last toplevel <Configure>
        self._rz_size = None        # the toplevel size seen last
        self._rz_job = None
        self._flush_job = None      # _flush_paints() at idle
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self.canvas.bind_all("<Shift-MouseWheel>", self._wheel_h, add="+")
        themes.on_palette(self.canvas, lambda p: self.canvas.configure(bg=p["bg"]))
        from . import dpi
        dpi.on_change(self, self._dpi_changed)
        dpi.on_change(self, self._dpi_refit, late=True)
        self.after(self.WATCH_MS, self._watch)

    # ------------------------------------------------------------ DPI
    def _dpi_changed(self, _ratio):
        """The display scaling changed (ui/dpi.py): margins and the design
        size for the new DPI; a drag in progress is over."""
        s = themes.scale()
        (ix, iy), s0 = self._inset0
        self.inset = (int(round(ix * s / s0)), int(round(iy * s / s0)))
        (dw, dh), s0 = self._design0
        self.design = (int(round(dw * s / s0)), int(round(dh * s / s0)))
        if self._live:
            self._live = False
            self._freeze(False)

    def _dpi_refit(self, _ratio):
        """... and every page laid out again from scratch (once the
        rescaled widgets report their new natural sizes)."""
        self._sizes.clear()
        self._view = None
        if self._cur is not None:
            self.fit(self._cur, idle=False)
        self._flush_soon()

    # how often the shown page's natural size is re-checked (ms)
    WATCH_MS = 250

    # ------------------------------------------------------------ pages
    def add(self, page):
        x, y = self.inset
        self._items[page] = self.canvas.create_window(x, y, window=page, anchor="nw",
                                                      state="hidden")

    def current(self):
        return self._cur

    def show(self, page):
        """Show `page` (hide the shown one) at its fixed size."""
        if page not in self._items:
            self.add(page)
        if self._cur is not None and self._cur is not page:
            self.canvas.itemconfigure(self._items[self._cur], state="hidden")
        self._cur = page
        self.fit(page)
        self.canvas.itemconfigure(self._items[page], state="normal")
        self._flush_soon()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def _view_total(self):
        """The whole page area (canvas + the scrollbars shown now)."""
        return (self.canvas.winfo_width() + (self.vsb.winfo_width() if self._shown["v"] else 0),
                self.canvas.winfo_height() + (self.hsb.winfo_height() if self._shown["h"] else 0))

    def _bars(self, pw, ph, cw, ch):
        """(need_h, need_v, visible w, visible h) for a page of pw x ph in a
        page area of cw x ch. The inset (margin around the page) is given up
        before a scrollbar is needed."""
        sb = self._sbw()
        need_h = pw > cw
        need_v = ph > ch
        need_h = need_h or (need_v and pw > cw - sb)
        need_v = need_v or (need_h and ph > ch - sb)
        return need_h, need_v, cw - (sb if need_v else 0), ch - (sb if need_h else 0)

    def _page_size(self, page):
        """The size `page` is laid out at for the current viewport."""
        rw, rh = page.winfo_reqwidth(), page.winfo_reqheight()
        dw, dh = self.design
        cw, ch = self._view_total()
        if cw <= 1 or ch <= 1:                  # not on screen yet
            return max(rw, dw), max(rh, dh)
        ix, iy = self.inset
        _nh, _nv, vw, vh = self._bars(rw, rh, cw, ch)
        return max(rw, min(dw, vw - 2 * ix)), max(rh, min(dh, vh - 2 * iy))

    def fit(self, page=None, idle=True):
        """(Re)size the page for its content and the current viewport. While
        the viewport stays the same the page only grows (no jumping back and
        forth); after a resize it takes the new target size - never smaller
        than its content."""
        page = page or self._cur
        if page is None:
            return
        try:
            if idle:
                page.update_idletasks()
            w, h = self._page_size(page)
        except tk.TclError:
            return
        view = (self.canvas.winfo_width(), self.canvas.winfo_height())
        ow, oh, oview = self._sizes.get(page, (0, 0, None))
        if oview == view:
            w, h = max(w, ow), max(h, oh)
        if (w, h, view) != (ow, oh, oview):
            self._sizes[page] = (w, h, view)
            if (w, h) != (ow, oh):
                self.canvas.itemconfigure(self._items[page], width=w, height=h)
        self._update()

    def refit(self, page=None):
        """Lay `page` out again from scratch (its content got SMALLER, e.g.
        a player picked a smaller video size): fit() alone only grows while
        the viewport stays the same."""
        page = page or self._cur
        if page is None:
            return
        self._sizes.pop(page, None)
        if page is self._cur:
            self.fit(page)

    def _watch(self):
        """The page is a canvas window item of a fixed size, so it doesn't
        follow its content by itself: when content grows later (a video
        loaded, a longer translated status, a list filled in, a font that
        came out wider at this DPI) the page would clip it at the right /
        bottom edge. A cheap check of the requested size (no layout pass)
        grows the page - and the scroll region - as soon as that happens."""
        try:
            page = self._cur
            if page is not None and not self._live:
                ow, oh = self._sizes.get(page, (0, 0, None))[:2]
                if page.winfo_reqwidth() > ow or page.winfo_reqheight() > oh:
                    self.fit(page, idle=False)
        except tk.TclError:
            return
        self.after(self.WATCH_MS, self._watch)

    # ------------------------------------------------------------ live resize
    # how long the window size must stay unchanged (and Windows' size loop
    # be over) before a drag counts as finished (ms)
    SETTLE_MS = 120

    def window_resized(self, size):
        """Called with the toplevel's new size on its <Configure>.

        Every size change of a window makes Windows (DWM) invalidate ALL its
        child windows, although their pixels are kept: each resize step
        repainted every widget of the page (200-500 ms per step on the big
        pages at 150 % scaling - the "resize lag"). Size changes that keep
        the window's top-left corner go through _SizeHook, which prevents
        that; the others (left / top edge, snap, maximise / restore) are
        repainted by Windows, because their content moves on screen.

        While the window is being DRAGGED (Windows' size loop runs, or a
        stream of size steps arrives) the page also keeps its size AND
        position - only the scroll region and scrollbars follow - so nothing
        inside it is laid out or moved; it is fitted to the new size once,
        when the drag has ended. A single change (maximise, restore, snap) is
        fitted right away by _on_configure."""
        now = time.monotonic()
        last, self._rz_t = self._rz_t, now
        if size == self._rz_size:
            return
        first = self._rz_size is None
        self._rz_size = size
        if first:
            return
        if not self._live and (_sizing() or now - last < 0.15):
            self._live = True
            self._freeze(True)
        if not self._live:
            self._flush_soon()          # a single change: laid out at idle
        elif self._rz_job is None:
            self._rz_job = self.after(self.SETTLE_MS, self._live_check)

    def live_resizing(self):
        """True while the window is being dragged to a new size."""
        return self._live

    def _live_check(self):
        self._rz_job = None
        if _sizing() or time.monotonic() - self._rz_t < self.SETTLE_MS / 1000.0:
            self._rz_job = self.after(self.SETTLE_MS // 2, self._live_check)
            return
        if self._live:
            self._live = False
            self._freeze(False)
            self.fit(idle=False)       # the settled window size
            self._flush_paints()

    def _flush_soon(self):
        """_flush_paints() once the pending layout has been done (idle)."""
        if not getattr(self, "_flush_job", None):
            self._flush_job = self.after_idle(self._flush_paints)

    def _flush_paints(self):
        """Paint - right now - every window of the main window that Windows
        still has marked for repainting (RDW_UPDATENOW; nothing new is
        invalidated, so it costs next to nothing when all is painted).

        A safety net after a resize or a page switch: with the size changed
        from outside Windows' own size loop (a snap, another program, a
        page switched during the change) Tk's event loop was seen to go to
        sleep with a whole page still waiting for its WM_PAINT - blank or
        stale until some other message (the mouse moving over the window)
        woke it up. The pending layout is done first, so the windows it
        moves are included."""
        self._flush_job = None
        if _RedrawWindow is None:
            return
        try:
            self.update_idletasks()
            top = self.winfo_toplevel()
            frame = int(top.wm_frame(), 16) or top.winfo_id()
        except (tk.TclError, ValueError):
            return
        try:
            _RedrawWindow(frame, None, None, 0x0080 | 0x0100)  # RDW_ALLCHILDREN|UPDATENOW
        except Exception:
            pass

    def _freeze(self, on):
        """During a drag the page area keeps its size: it doesn't stretch
        with the window (the room beyond it shows the same background), so
        the canvas is not redrawn and no clipped edge of the page is
        uncovered strip by strip - each such strip made the big containers
        along that edge redraw completely. It still shrinks with a smaller
        window (that uncovers nothing)."""
        try:
            if on:
                if self.winfo_manager() != "pack":
                    return
                info = self.pack_info()
                self._frozen = {k: info.get(k) for k in ("fill", "anchor")}
                self.configure(width=self.winfo_width(), height=self.winfo_height())
                self.grid_propagate(False)
                self.pack_configure(fill="none", anchor="nw")
            elif getattr(self, "_frozen", None):
                old, self._frozen = self._frozen, None
                self.grid_propagate(True)
                self.configure(width=0, height=0)
                self.pack_configure(**old)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ viewport
    def _on_configure(self, e):
        if (e.width, e.height) != self._view:
            self._view = (e.width, e.height)
            if self._live:
                self._update(live=True)     # dragging: fitted once it ends
            else:
                self.fit(idle=False)

    def _update(self, live=False):
        """Scroll region + scrollbars for the current viewport (cheap: no
        widget inside the page is touched). live = during a window drag: the
        page also keeps its position."""
        page = self._cur
        if page is None:
            return
        if self.design_fn is not None and self.canvas.winfo_width() > 1:
            fn, self.design_fn = self.design_fn, None
            try:
                self.design = fn()
                self._design0 = (self.design, themes.scale())
            except Exception:
                pass
            for pg in list(self._sizes):
                self._sizes.pop(pg)
            self.fit(page)
            return
        pw, ph = self._sizes.get(page, (0, 0, None))[:2]
        ix, iy = self.inset
        cw, ch = self._view_total()
        need_h, need_v, vw, vh = self._bars(pw, ph, cw, ch)
        # the margin shrinks (down to 0) before a scrollbar is needed
        x = ix if need_h else max(0, min(ix, vw - pw))
        y = iy if need_v else max(0, min(iy, vh - ph))
        try:
            at = tuple(int(float(v)) for v in self.canvas.coords(self._items[page]))
            if live:
                x, y = at
            elif at != (x, y):
                self.canvas.coords(self._items[page], x, y)
        except (tk.TclError, ValueError):
            pass
        self._toggle("v", need_v, self.vsb, dict(row=0, column=1, sticky="ns"))
        self._toggle("h", need_h, self.hsb, dict(row=1, column=0, sticky="ew"))
        self.canvas.configure(scrollregion=(0, 0, pw + 2 * ix if need_h else vw,
                                            ph + 2 * iy if need_v else vh))
        if not need_h:
            self.canvas.xview_moveto(0)
        if not need_v:
            self.canvas.yview_moveto(0)

    def _sbw(self):
        """Thickness of a scrollbar (the room it takes when shown)."""
        return max(themes.px(12), self.vsb.winfo_reqwidth(), self.hsb.winfo_reqheight())

    def _toggle(self, key, on, bar, grid):
        if on and not self._shown[key]:
            bar.grid(**grid)
        elif not on and self._shown[key]:
            bar.grid_remove()
        self._shown[key] = on

    # ------------------------------------------------------------ wheel
    def _target(self, e):
        try:
            if not self.canvas.winfo_viewable():
                return None
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return None
        if w is None or not str(w).startswith(str(self)):
            return None
        if _wheel_should_pass(w, self.canvas):
            return None
        return w

    def _wheel(self, e):
        if (e.state & 0x1) or self._target(e) is None or not self._shown["v"]:
            return
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _wheel_h(self, e):
        if self._target(e) is None or not self._shown["h"]:
            return
        self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units")
