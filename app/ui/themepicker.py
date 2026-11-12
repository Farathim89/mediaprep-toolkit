"""Theme picker with colour swatches: a drop-down button (Settings) and a
pop-up menu (the sidebar's theme item, right-click).

Every entry shows a small swatch of its palette - background, panel, accent
and text colour as four squares - next to the name, grouped as in
palettes.picker_groups() ("Basic" first, then all other themes by name). The
list is a native Tk menu: keyboard navigation (arrows, Enter, Esc) comes
with it. Swatches are tiny PhotoImages filled with put() (no Pillow needed),
DPI-scaled, made on first use and cached; nothing is built at startup.
"""
import tkinter as tk
from tkinter import ttk

from . import colors, palettes, themes

_SWATCHES = {}          # theme -> PhotoImage (redrawn in place for another DPI)


def _swatch_colours(name):
    if name == palettes.FOLLOW_WINDOWS:
        lt, dk = palettes.PALETTES["Light"], palettes.PALETTES["Dark"]
        return [lt["bg"], lt["accent"], dk["bg"], dk["accent"]], colors.mix(lt["border"],
                                                                         dk["border"], 0.5)
    p = palettes.PALETTES[themes.resolve(name)]
    edge = p["border"] if p.get("hc") else colors.mix(p["bg"], p["fg"], 0.35)
    return [p["bg"], p["panel"], p["accent"], p["fg"]], edge


def swatch(widget, name):
    """The swatch PhotoImage of theme `name` (cached)."""
    img = _SWATCHES.get(name)
    if img is not None:
        return img
    img = tk.PhotoImage(master=widget.winfo_toplevel())
    _fill(img, name)
    if not _SWATCHES:
        # another display scaling (ui/dpi.py): the same images, new size
        from . import dpi
        dpi.on_change(None, lambda _r: [_fill(im, n) for n, im in _SWATCHES.items()])
    _SWATCHES[name] = img
    return img


def _fill(img, name):
    """Draw theme `name`'s swatch into img at the current DPI."""
    sq, gap = themes.px(11), max(1, themes.px(2))
    colours, edge = _swatch_colours(name)
    w = len(colours) * sq + (len(colours) - 1) * gap
    try:
        img.blank()
        img.configure(width=w, height=sq)
        for i, c in enumerate(colours):
            x = i * (sq + gap)
            img.put(edge, to=(x, 0, x + sq, sq))                     # 1 px frame
            img.put(c, to=(x + 1, 1, x + sq - 1, sq - 1))
    except tk.TclError:
        pass


def build_menu(parent):
    """A tk.Menu listing every theme (radio entries bound to the shared
    theme variable), grouped, with swatches."""
    menu = tk.Menu(parent, tearoff=0)
    var = themes.theme_var()
    groups = palettes.picker_groups()
    # one column unless the list would not fit on the screen
    try:
        import tkinter.font as tkfont
        row = max(tkfont.nametofont("TkMenuFont").metrics("linespace"), themes.px(11))
        tall = (sum(len(n) + 1 for _t, n in groups) + 1) * (row + themes.px(8))
        two_cols = tall > parent.winfo_screenheight() * 0.9
    except (tk.TclError, KeyError):
        two_cols = False
    for gi, (title, names) in enumerate(groups):
        if gi and not two_cols:
            menu.add_separator()
        menu.add_command(label=title, state="disabled", columnbreak=bool(gi and two_cols))
        for n in names:
            menu.add_radiobutton(label=palettes.label(n), image=swatch(parent, n),
                                 compound="left", variable=var, value=n)
    themes.recolor(menu)
    return menu


def popup(widget, x, y):
    """Show the theme menu at screen position x, y (built on first use)."""
    menu = getattr(widget, "_mp_theme_menu", None)
    if menu is None:
        menu = build_menu(widget)
        widget._mp_theme_menu = menu
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


class ThemePicker(ttk.Menubutton):
    """A drop-down button for the theme: the current theme's swatch and name;
    opens the swatch menu (also with Space, Enter or Down)."""

    def __init__(self, master, **kw):
        names = [n for _t, ns in palettes.picker_groups() for n in ns]
        kw.setdefault("width", max(len(palettes.label(n)) for n in names) + 2)
        kw.setdefault("style", "Picker.TMenubutton")     # field + chevron (ui/fluent.py)
        super().__init__(master, compound="left", **kw)
        self._menu = build_menu(self)
        self.configure(menu=self._menu)
        var = themes.theme_var()
        self._trace = var.trace_add("write", lambda *_a: self._show())
        self.bind("<Destroy>", self._untrace, add="+")
        for seq in ("<Return>", "<KP_Enter>", "<Down>"):
            self.bind(seq, lambda e: (self._post(), "break")[1])
        self._show()

    def _show(self):
        name = themes.theme_var().get()     # (this trace may run before set_theme)
        try:
            self.configure(text=" " + palettes.label(name), image=swatch(self, name))
        except tk.TclError:
            pass

    def _post(self):
        self.update_idletasks()
        self._menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())

    def _untrace(self, e):
        if e.widget is self and self._trace is not None:
            try:
                themes.theme_var().trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None
