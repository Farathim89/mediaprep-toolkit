"""The Fluent info bar (InfoBar) - the banners at the top of the window
(update available, missing ffmpeg / packages, settings problems)."""
import tkinter as tk

from . import colors, themes
from .widgets import auto_wrap


# ======================= Fluent info bar =======================
class InfoBar(tk.Frame):
    """A Windows 11 style InfoBar: tinted strip with a coloured accent edge,
    a severity icon, the message (wraps), an optional link and a close ×.
    kind = "error" / "warn" / "info"."""
    _ICONS = {"error": "error", "warn": "warning", "info": "info"}

    def __init__(self, master, text, kind="info", link_text=None, on_link=None,
                 closable=True, on_close=None, links=None):
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
        # links = [(text, callback), ...] shown left to right; link_text /
        # on_link is the older single-link form
        items = list(links or ())
        if link_text:
            items.append((link_text, on_link))
        self.links = []
        for text_, cb in reversed(items):           # packed from the right
            lbl = tk.Label(self, text=text_, cursor="hand2", borderwidth=0,
                           font="MPSemibold", padx=themes.px(6))
            lbl.pack(side="right", anchor="n", pady=themes.px(8))
            if cb is not None:
                lbl.bind("<Button-1>", lambda e, cb=cb: cb())
            self.links.insert(0, lbl)
        if self.links:
            self.link = self.links[-1]
        self.msg = tk.Label(self, text=text, justify="left", anchor="w", borderwidth=0,
                            font="TkDefaultFont")
        self.msg.pack(side="left", fill="x", expand=True, pady=themes.px(8))
        auto_wrap(self.msg, margin=8, width=820)
        themes.on_palette(self, self._repaint)
        # the bar spans the window: the message wraps at the room it really
        # has (a narrow window, or the few logical pixels of a 4K screen at
        # 300 %), never under the links / close button
        self._wrap_w = None
        self.bind("<Configure>", self._rewrap, add="+")
        from . import dpi
        dpi.on_change(self, self._dpi_rewrap, late=True)

    def _dpi_rewrap(self, _ratio):
        self._wrap_w = None
        self._rewrap()

    def _rewrap(self, e=None):
        try:
            others = sum(w.winfo_reqwidth() for w in self.pack_slaves() if w is not self.msg)
            room = self.winfo_width() - others - themes.px(8)
        except tk.TclError:
            return
        if room > themes.px(120) and room != self._wrap_w:
            self._wrap_w = room
            self.msg.configure(wraplength=room)

    def _repaint(self, p):
        k = self.kind
        bg, fg = p[f"banner_{k}_bg"], p[f"banner_{k}_fg"]
        acc = p[f"banner_{k}_accent"]
        self.configure(bg=bg, highlightbackground=colors.mix(bg, p["fg"], 0.12),
                       highlightcolor=colors.mix(bg, p["fg"], 0.12))
        self.edge.configure(bg=acc)
        self.icon.configure(bg=bg, fg=acc)
        self.msg.configure(bg=bg, fg=fg)
        for lbl in self.links:
            lbl.configure(bg=bg, fg=fg, font="MPSemibold")
        if self.close_lbl is not None:
            self.close_lbl.configure(bg=bg, fg=fg)

    def close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass
        if callable(self._on_close):
            self._on_close()
