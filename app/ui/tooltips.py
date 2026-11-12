"""Hover tooltips (Tooltip, add_tooltip) and the small (i) info icons that
show an explanation on hover / focus / click (info_icon)."""
import tkinter as tk
from tkinter import ttk

from . import themes


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
