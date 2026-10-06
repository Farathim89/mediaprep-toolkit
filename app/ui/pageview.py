"""The page area of the main window: fixed-size pages in a 2-D scroll view.

Pages do NOT reflow when the window is resized. Each page is laid out once
at a fixed size - its natural (requested) size, at least the `design` size
(the page area of the default window) - and sits top-left in a canvas. A
window resize only moves the canvas viewport: horizontal / vertical
scrollbars appear while the window is smaller than the page and hide again
otherwise; a larger window shows the theme background around the page.

Mouse wheel = vertical, Shift+wheel = horizontal. Wheel events over widgets
that scroll themselves (lists, text boxes, tree views, scrolling canvases)
are left to them.
"""
import tkinter as tk
from tkinter import ttk

from . import themes
from .widgets import _wheel_should_pass


class PageView(ttk.Frame):
    def __init__(self, master, design=(0, 0), inset=(0, 0), design_fn=None):
        super().__init__(master)
        self.design = design
        # design_fn() -> (w, h): called once the view has its real size (the
        # page area of the default window, derived from the current one)
        self.design_fn = design_fn
        self.inset = inset
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
        self._sizes = {}            # page widget -> (w, h) it is laid out at
        self._cur = None
        self._shown = {"v": False, "h": False}
        self._view = None
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self.canvas.bind_all("<Shift-MouseWheel>", self._wheel_h, add="+")
        themes.on_palette(self.canvas, lambda p: self.canvas.configure(bg=p["bg"]))

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
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def fit(self, page=None):
        """(Re)measure the page's natural size; it only ever grows, so a page
        never jumps smaller while it is used."""
        page = page or self._cur
        if page is None:
            return
        try:
            page.update_idletasks()
            w = max(page.winfo_reqwidth(), self.design[0])
            h = max(page.winfo_reqheight(), self.design[1])
        except tk.TclError:
            return
        ow, oh = self._sizes.get(page, (0, 0))
        w, h = max(w, ow), max(h, oh)
        if (w, h) != (ow, oh):
            self._sizes[page] = (w, h)
            self.canvas.itemconfigure(self._items[page], width=w, height=h)
        self._update()

    # ------------------------------------------------------------ live resize
    def window_resized(self, size):
        """Called with the toplevel's new size on every <Configure>. While
        the window is being DRAGGED (a stream of size changes) the page is
        hidden: Windows repaints every visible widget of the window on each
        resize step, which is what made dragging lag. 150 ms after the last
        change the page shows again (it never re-lays out - it has a fixed
        size). A single change (maximize / restore) keeps the page shown."""
        import time
        now = time.monotonic()
        last, self._rz_t = getattr(self, "_rz_t", 0.0), now
        if size == getattr(self, "_rz_size", None):
            return
        first = getattr(self, "_rz_size", None) is None
        self._rz_size = size
        if first or now - last > 0.15 and not getattr(self, "_rz_hidden", False):
            return
        if not getattr(self, "_rz_hidden", False) and self._cur is not None:
            self._rz_hidden = True
            self._snapshot()
            self.canvas.itemconfigure(self._items[self._cur], state="hidden")
        job = getattr(self, "_rz_job", None)
        if job is not None:
            self.after_cancel(job)
        self._rz_job = self.after(150, self._resize_done)

    def _snapshot(self):
        """Show a still picture of the page while it is hidden, so a drag
        doesn't blank the page (one screen grab; skipped without Pillow)."""
        self._snap = None
        try:
            from PIL import ImageGrab, ImageTk
            c = self.canvas
            x, y = c.winfo_rootx(), c.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + c.winfo_width(), y + c.winfo_height()),
                                 all_screens=True)
            self._snap = ImageTk.PhotoImage(img, master=c)
            c.create_image(c.canvasx(0), c.canvasy(0), image=self._snap, anchor="nw",
                           tags=("snapshot",))
        except Exception:
            self._snap = None

    def _resize_done(self):
        self._rz_job = None
        if getattr(self, "_rz_hidden", False):
            self._rz_hidden = False
            if self._cur is not None:
                self.canvas.itemconfigure(self._items[self._cur], state="normal")
            self.canvas.delete("snapshot")
            self._snap = None

    # ------------------------------------------------------------ viewport
    def _on_configure(self, e):
        if (e.width, e.height) != self._view:
            self._view = (e.width, e.height)
            self._update()

    def _update(self):
        """Scroll region + scrollbars for the current viewport (cheap: no
        widget inside the page is touched)."""
        page = self._cur
        if page is None:
            return
        if self.design_fn is not None and self.canvas.winfo_width() > 1:
            fn, self.design_fn = self.design_fn, None
            try:
                self.design = fn()
            except Exception:
                pass
            for pg in list(self._sizes):
                self._sizes.pop(pg)
            self.fit(page)
            return
        pw, ph = self._sizes.get(page, (0, 0))
        tw, th = pw + 2 * self.inset[0], ph + 2 * self.inset[1]
        vw = self.canvas.winfo_width() + (self.vsb.winfo_width() if self._shown["v"] else 0)
        vh = self.canvas.winfo_height() + (self.hsb.winfo_height() if self._shown["h"] else 0)
        need_h = tw > vw
        need_v = th > vh
        # a scrollbar takes room from the other direction
        sb = themes.px(12)
        need_h = need_h or (need_v and tw > vw - sb)
        need_v = need_v or (need_h and th > vh - sb)
        self._toggle("v", need_v, self.vsb, dict(row=0, column=1, sticky="ns"))
        self._toggle("h", need_h, self.hsb, dict(row=1, column=0, sticky="ew"))
        self.canvas.configure(scrollregion=(0, 0, tw, th))
        if not need_h:
            self.canvas.xview_moveto(0)
        if not need_v:
            self.canvas.yview_moveto(0)

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
