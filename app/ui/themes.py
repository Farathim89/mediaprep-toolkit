"""GUI themes: one colour palette per theme, everything else derived from it.

    PALETTES[name]   the base colours of a theme (see _BASE_KEYS)
    THEME_NAMES      what the theme pickers list ("Follow Windows" first)
    init(root)       create the shared theme variable + start the Windows watch
    theme_var()      the tk.StringVar both pickers (top bar, Settings) share -
                     writing it applies the theme live to the whole app
    current()        the palette in use (resolved: Follow Windows -> Light/Dark)
    on_palette(w, fn)  call fn(palette) now and on every theme change for as
                     long as widget w lives (custom-coloured widgets use it)
    subscribe(fn)    app-wide listener fn(name, palette) (e.g. persist)

apply() styles every ttk widget class, sets the option database for widgets
created later, and walks ALL existing widgets (incl. open Toplevels, combobox
drop-downs and menus) to recolour the plain-tk ones."""
import tkinter as tk
from tkinter import ttk

from ..i18n import N_, tr

FOLLOW_WINDOWS = "Follow Windows"
DEFAULT_THEME = "Light"

# keys every palette defines; the rest is derived in _complete()
_BASE_KEYS = ("bg", "panel", "field", "fg", "muted", "accent", "accent_fg",
              "select_bg", "select_fg", "border", "ok", "warn", "error")

_MARKERS_LIGHT = dict(marker_preintro="#d9771f", marker_intro="#2f6fd6",
                      marker_credits="#2f9a4c", marker_aftercredits="#8e4fc8")
_MARKERS_DARK = dict(marker_preintro="#f0a050", marker_intro="#5b9bff",
                     marker_credits="#58d07a", marker_aftercredits="#b88af0")

PALETTES = {
    "Light": dict(
        bg="#f0f0f0", panel="#e1e1e1", field="#ffffff", fg="#1a1a1a", muted="#555555",
        accent="#2459b8", accent_fg="#ffffff", select_bg="#2f6fd6", select_fg="#ffffff",
        border="#a6a6a6", ok="#1e7d3a", warn="#8a4b00", error="#c62828",
        playhead="#d81b1b", **_MARKERS_LIGHT),
    "Dark": dict(
        bg="#2b2b2b", panel="#3c3c3c", field="#1e1e1e", fg="#e6e6e6", muted="#a8a8a8",
        accent="#6ea0ff", accent_fg="#0d1a33", select_bg="#3a66c4", select_fg="#ffffff",
        border="#5a5a5a", ok="#5cd17a", warn="#ffb347", error="#ff6b6b",
        dark=True, **_MARKERS_DARK),
    "High Contrast": dict(
        bg="#000000", panel="#1a1a1a", field="#000000", fg="#ffffff", muted="#00ffff",
        accent="#ffff00", accent_fg="#000000", select_bg="#ffff00", select_fg="#000000",
        border="#ffffff", ok="#00ff00", warn="#ffb000", error="#ff5c5c",
        hover="#333333", pressed="#4d4d4d", dark=True, playhead="#ff00ff",
        marker_preintro="#ff9900", marker_intro="#00b0ff", marker_credits="#00ff00",
        marker_aftercredits="#ff66ff"),
    "Plex": dict(
        bg="#141414", panel="#252525", field="#0b0b0b", fg="#ececec", muted="#a3a3a3",
        accent="#e5a00d", accent_fg="#000000", select_bg="#e5a00d", select_fg="#000000",
        border="#3d3d3d", ok="#62c96a", warn="#ff9f43", error="#ff6161",
        dark=True, **dict(_MARKERS_DARK, marker_intro="#e5a00d",
                          marker_preintro="#ff7b39")),
    "Gold on Dark": dict(
        bg="#121110", panel="#24201a", field="#0a0908", fg="#ffd100", muted="#c8ad6e",
        accent="#ffb400", accent_fg="#000000", select_bg="#ffd100", select_fg="#000000",
        border="#6b5a2e", ok="#7ed957", warn="#ff9a3c", error="#ff5f5f",
        tooltip_bg="#24201a", tooltip_fg="#ffd100", dark=True,
        **dict(_MARKERS_DARK, marker_intro="#ffd100")),
    "Midnight Blue": dict(
        bg="#0f1a2e", panel="#1b2b47", field="#0a1222", fg="#e1e8f5", muted="#9fb2d0",
        accent="#5aa9ff", accent_fg="#04101f", select_bg="#2e5fa8", select_fg="#ffffff",
        border="#2f4670", ok="#5fd38d", warn="#ffc35a", error="#ff7a7a",
        dark=True, **_MARKERS_DARK),
    "Nord": dict(
        bg="#2e3440", panel="#3b4252", field="#242933", fg="#eceff4", muted="#b4bccb",
        accent="#88c0d0", accent_fg="#2e3440", select_bg="#4a6a96", select_fg="#ffffff",
        border="#4c566a", ok="#a3be8c", warn="#ebcb8b", error="#e5868f",
        dark=True, marker_preintro="#d08770", marker_intro="#81a1c1",
        marker_credits="#a3be8c", marker_aftercredits="#b48ead"),
    "Dracula": dict(
        bg="#282a36", panel="#383a4a", field="#21222c", fg="#f8f8f2", muted="#b1b8d8",
        accent="#bd93f9", accent_fg="#21222c", select_bg="#6c4fb0", select_fg="#ffffff",
        border="#6272a4", ok="#50fa7b", warn="#ffb86c", error="#ff6e6e",
        dark=True, playhead="#ff79c6", marker_preintro="#ffb86c",
        marker_intro="#8be9fd", marker_credits="#50fa7b", marker_aftercredits="#bd93f9"),
    "Solarized Light": dict(
        bg="#fdf6e3", panel="#eee8d5", field="#fffcf4", fg="#073642", muted="#4f646b",
        accent="#1b6ea8", accent_fg="#ffffff", select_bg="#1b6ea8", select_fg="#ffffff",
        border="#c9c0a6", ok="#4f6600", warn="#9a4a00", error="#b8262a",
        playhead="#d33682", marker_preintro="#cb4b16", marker_intro="#268bd2",
        marker_credits="#859900", marker_aftercredits="#6c71c4"),
    "Solarized Dark": dict(
        bg="#002b36", panel="#073642", field="#00212b", fg="#eee8d5", muted="#a0acac",
        accent="#4aa3e8", accent_fg="#00212b", select_bg="#1d5f8f", select_fg="#ffffff",
        border="#2a5866", ok="#a4c400", warn="#e3a72f", error="#ff6f61",
        dark=True, playhead="#d33682", marker_preintro="#cb4b16",
        marker_intro="#268bd2", marker_credits="#859900", marker_aftercredits="#6c71c4"),
    "Forest": dict(
        bg="#1c2a1e", panel="#283c2b", field="#132015", fg="#e3eedf", muted="#a9c2a5",
        accent="#8fd16a", accent_fg="#0f1f0e", select_bg="#3e7a39", select_fg="#ffffff",
        border="#3e5c41", ok="#7ee08a", warn="#f0c05a", error="#ff7f6e",
        dark=True, **_MARKERS_DARK),
    "Sepia": dict(
        bg="#f4ecd8", panel="#e6d9bb", field="#fbf7ec", fg="#3b2f1e", muted="#5f5038",
        accent="#8b4513", accent_fg="#ffffff", select_bg="#8b5a2b", select_fg="#ffffff",
        border="#b5a27c", ok="#3d6a1d", warn="#8a4f00", error="#a8231b",
        playhead="#c0392b", marker_preintro="#c76a1c", marker_intro="#2f6fa8",
        marker_credits="#4f8a2b", marker_aftercredits="#8a4fa8"),
}

THEME_NAMES = [FOLLOW_WINDOWS] + list(PALETTES)

# the theme names as the pickers SHOW them (translated); the settings and
# theme_var() keep the English key. Listed here for the string extractor.
_THEME_LABELS = (N_("Follow Windows"), N_("Light"), N_("Dark"), N_("High Contrast"),
                 N_("Plex"), N_("Gold on Dark"), N_("Midnight Blue"), N_("Nord"),
                 N_("Dracula"), N_("Solarized Light"), N_("Solarized Dark"),
                 N_("Forest"), N_("Sepia"))


def label(name):
    """A theme name as shown in the pickers (translated)."""
    return tr(name)


# ---------------------------------------------------------------- colour maths
def _rgb(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def mix(a, b, t):
    """Colour a blended toward b by t (0 = a, 1 = b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex([x + (y - x) * t for x, y in zip(ra, rb)])


def luminance(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio of two colours (1 .. 21)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def on_color(bg):
    """Black or white - whichever reads better on bg."""
    return "#000000" if contrast(bg, "#000000") >= contrast(bg, "#ffffff") else "#ffffff"


def _complete(p):
    """Fill the derived keys of a base palette."""
    p = dict(p)
    dark = p.setdefault("dark", luminance(p["bg"]) < 0.2)
    p.setdefault("hover", mix(p["panel"], p["fg"], 0.14))
    p.setdefault("pressed", mix(p["panel"], p["fg"], 0.26))
    p.setdefault("disabled_fg", mix(p["fg"], p["bg"], 0.5))
    p.setdefault("tooltip_bg", "#ffffe0" if not dark else p["panel"])
    p.setdefault("tooltip_fg", "#1a1a1a" if not dark else p["fg"])
    p.setdefault("timeline_bg", p["field"])
    p.setdefault("timeline_track", mix(p["border"], p["fg"], 0.3))
    p.setdefault("playhead", "#ff3b3b" if dark else "#d81b1b")
    p.setdefault("video_bg", "#000000" if dark else mix(p["bg"], "#000000", 0.08))
    for k, v in (_MARKERS_DARK if dark else _MARKERS_LIGHT).items():
        p.setdefault(k, v)
    # banners (missing ffmpeg, missing packages, update available)
    p.setdefault("banner_error_bg", "#b00020" if not dark else "#8f1a1a")
    p.setdefault("banner_warn_bg", "#8a5a00" if not dark else "#7a5200")
    p.setdefault("banner_info_bg", p["accent"])
    for k in ("banner_error", "banner_warn", "banner_info"):
        p.setdefault(k + "_fg", on_color(p[k + "_bg"]))
    p["banner_info_fg"] = p["accent_fg"] if p["banner_info_bg"] == p["accent"] else p["banner_info_fg"]
    return p


PALETTES = {name: _complete(p) for name, p in PALETTES.items()}


# ---------------------------------------------------------------- Windows mode
def windows_prefers_light():
    """True if Windows' app mode is Light (registry AppsUseLightTheme); True
    as well when it can't be read (non-Windows, missing key)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            val, _typ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return bool(val)
    except Exception:
        return True


def resolve(name):
    """A theme name -> the concrete palette name ("Follow Windows" -> Light/Dark)."""
    if name == FOLLOW_WINDOWS:
        return "Light" if windows_prefers_light() else "Dark"
    return name if name in PALETTES else DEFAULT_THEME


def palette(name=None):
    if name is None:
        return current()
    return PALETTES[resolve(name)]


# ---------------------------------------------------------------- state
class _State:
    root = None
    style = None
    var = None
    name = DEFAULT_THEME          # what the user picked (may be Follow Windows)
    applied = DEFAULT_THEME       # the concrete palette applied last
    listeners = []
    watching = False


_S = _State()
WATCH_MS = 10000                  # Follow Windows re-check interval


def current():
    return PALETTES.get(_S.applied, PALETTES[DEFAULT_THEME])


def current_name():
    return _S.name


def subscribe(fn):
    """fn(name, palette) after every theme change (app-wide, not widget-bound)."""
    _S.listeners.append(fn)


def on_palette(widget, fn):
    """Call fn(palette) now and after each theme change while widget lives."""
    hooks = getattr(widget, "_palette_hooks", None)
    if hooks is None:
        hooks = []
        try:
            widget._palette_hooks = hooks
        except Exception:
            pass
    hooks.append(fn)
    try:
        fn(current())
    except Exception:
        pass
    return fn


def init(root, name=DEFAULT_THEME):
    """Create the shared theme variable, apply `name` and start the Follow
    Windows watch. Returns the StringVar the theme pickers bind to."""
    _S.root = root
    _S.style = ttk.Style(root)
    if name not in THEME_NAMES:
        name = DEFAULT_THEME
    _S.var = tk.StringVar(master=root, value=name)
    _S.var.trace_add("write", lambda *_a: set_theme(_S.var.get()))
    set_theme(name, force=True)
    # every new window (dialogs, help, tooltips) gets the palette once it's
    # mapped - covers its plain-tk children and the dark/light title bar
    root.bind_class("Toplevel", "<Map>", _on_map, add="+")
    if not _S.watching:
        _S.watching = True
        root.after(WATCH_MS, _watch)
    return _S.var


def _on_map(e):
    w = e.widget
    if not isinstance(w, tk.Misc) or getattr(w, "_themed_for", None) == _S.applied:
        return
    try:
        w._themed_for = _S.applied
    except Exception:
        pass
    _walk(w, current())


def theme_var():
    return _S.var


def set_theme(name, force=False):
    """Apply theme `name` (live, whole app) and tell the listeners."""
    if name not in THEME_NAMES:
        name = DEFAULT_THEME
    if _S.var is not None and _S.var.get() != name:
        _S.var.set(name)              # the trace calls back into set_theme
        return
    concrete = resolve(name)
    if not force and name == _S.name and concrete == _S.applied:
        return
    _S.name = name
    _S.applied = concrete
    if _S.root is not None:
        apply(_S.root, _S.style, concrete)
    for fn in list(_S.listeners):
        try:
            fn(name, current())
        except Exception:
            pass


def _watch():
    """Follow Windows: switch Light/Dark when the Windows app mode changes."""
    try:
        if _S.name == FOLLOW_WINDOWS and resolve(FOLLOW_WINDOWS) != _S.applied:
            set_theme(FOLLOW_WINDOWS, force=True)
        _S.root.after(WATCH_MS, _watch)
    except Exception:
        _S.watching = False


# ---------------------------------------------------------------- applying
def _style(style, p):
    bg, panel, field, fg = p["bg"], p["panel"], p["field"], p["fg"]
    border, accent, hov, prs = p["border"], p["accent"], p["hover"], p["pressed"]
    dis = p["disabled_fg"]
    sb, sf = p["select_bg"], p["select_fg"]
    style.theme_use("clam")
    style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                    bordercolor=border, lightcolor=bg, darkcolor=bg, troughcolor=field,
                    selectbackground=sb, selectforeground=sf, insertcolor=fg,
                    focuscolor=accent, arrowcolor=fg)
    style.map(".", foreground=[("disabled", dis)], background=[("disabled", bg)])
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("Hint.TLabel", background=bg, foreground=p["muted"])
    style.map("TLabel", foreground=[("disabled", dis)])
    btn = dict(background=panel, foreground=fg, bordercolor=border,
               lightcolor=panel, darkcolor=panel, focuscolor=accent, arrowcolor=fg)
    btn_map = dict(background=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   lightcolor=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   darkcolor=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   foreground=[("disabled", dis)],
                   bordercolor=[("focus", accent)],
                   arrowcolor=[("disabled", dis)])
    for w in ("TButton", "TMenubutton"):
        style.configure(w, **btn)
        style.map(w, **btn_map)
    # clam draws a 10 px indicator with a 1 px margin, not scaled with the
    # display DPI - tiny and cramped, and its border nearly vanishes on dark
    # backgrounds. Size it like the text (13 px at 100 %) with room to breathe
    # and give it a visible edge.
    try:
        dpi_f = max(1.0, float(style.tk.call("tk", "scaling")) / (96.0 / 72.0))
    except (tk.TclError, ValueError):
        dpi_f = 1.0
    ind = int(round(13 * dpi_f))
    ind_margin = (int(round(2 * dpi_f)), int(round(2 * dpi_f)),
                  int(round(6 * dpi_f)), int(round(2 * dpi_f)))
    ind_edge = mix(border, fg, 0.35) if p["dark"] else border
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, background=bg, foreground=fg, indicatorbackground=field,
                        indicatorforeground=fg, upperbordercolor=ind_edge,
                        lowerbordercolor=ind_edge, focuscolor=accent,
                        indicatorsize=ind, indicatormargin=ind_margin)
        style.map(w, background=[("active", bg)],
                  foreground=[("disabled", dis)],
                  indicatorbackground=[("disabled", bg), ("pressed", hov),
                                       ("active", mix(field, fg, 0.08))],
                  indicatorforeground=[("disabled", dis)])
    field_map = dict(fieldbackground=[("disabled", bg), ("readonly", field)],
                     foreground=[("disabled", dis), ("readonly", fg)],
                     bordercolor=[("focus", accent)],
                     lightcolor=[("focus", accent)])
    style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg,
                    bordercolor=border, lightcolor=field, darkcolor=field,
                    selectbackground=sb, selectforeground=sf)
    style.map("TEntry", **field_map)
    for w in ("TCombobox", "TSpinbox"):
        style.configure(w, fieldbackground=field, foreground=fg, insertcolor=fg,
                        background=panel, arrowcolor=fg, bordercolor=border,
                        lightcolor=field, darkcolor=field,
                        selectbackground=sb, selectforeground=sf)
        style.map(w, background=[("disabled", bg), ("pressed", prs), ("active", hov)],
                  arrowcolor=[("disabled", dis)],
                  selectbackground=[("readonly", "!focus", field)],
                  selectforeground=[("readonly", "!focus", fg)], **field_map)
    style.configure("TNotebook", background=bg, bordercolor=border,
                    lightcolor=bg, darkcolor=bg)
    style.configure("TNotebook.Tab", background=panel, foreground=fg, bordercolor=border,
                    lightcolor=panel, darkcolor=panel, focuscolor=accent)
    style.map("TNotebook.Tab",
              background=[("selected", bg), ("active", hov)],
              lightcolor=[("selected", accent), ("active", hov)],
              foreground=[("selected", fg), ("disabled", dis)])
    style.configure("Treeview", background=field, fieldbackground=field, foreground=fg,
                    bordercolor=border, lightcolor=field, darkcolor=field)
    style.map("Treeview", background=[("selected", sb)], foreground=[("selected", sf)])
    style.configure("Treeview.Heading", background=panel, foreground=fg,
                    bordercolor=border, lightcolor=panel, darkcolor=panel)
    style.map("Treeview.Heading", background=[("active", hov)])
    for w in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(w, background=panel, troughcolor=bg, bordercolor=border,
                        arrowcolor=fg, lightcolor=panel, darkcolor=panel, gripcount=0)
        style.map(w, background=[("pressed", prs), ("active", hov)],
                  lightcolor=[("pressed", prs), ("active", hov)],
                  darkcolor=[("pressed", prs), ("active", hov)],
                  arrowcolor=[("disabled", dis)])
    for w in ("TProgressbar", "Horizontal.TProgressbar", "Vertical.TProgressbar"):
        style.configure(w, background=accent, troughcolor=field, bordercolor=border,
                        lightcolor=accent, darkcolor=accent)
    style.configure("TLabelframe", background=bg, bordercolor=border,
                    lightcolor=bg, darkcolor=bg)
    style.configure("TLabelframe.Label", background=bg, foreground=accent)
    for w in ("TScale", "Horizontal.TScale", "Vertical.TScale"):
        style.configure(w, background=panel, troughcolor=field, bordercolor=border,
                        lightcolor=panel, darkcolor=panel)
        style.map(w, background=[("pressed", prs), ("active", hov)])
    style.configure("TSeparator", background=border)
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", background=border)


def _options(root, p):
    """Option database: colours for plain-tk widgets created from now on."""
    o = root.option_add
    pri = "interactive"
    for pat, val in (("*TCombobox*Listbox.background", p["field"]),
                     ("*TCombobox*Listbox.foreground", p["fg"]),
                     ("*TCombobox*Listbox.selectBackground", p["select_bg"]),
                     ("*TCombobox*Listbox.selectForeground", p["select_fg"]),
                     ("*Toplevel.background", p["bg"]),
                     ("*Text.background", p["field"]),
                     ("*Text.foreground", p["fg"]),
                     ("*Text.insertBackground", p["fg"]),
                     ("*Text.selectBackground", p["select_bg"]),
                     ("*Text.selectForeground", p["select_fg"]),
                     ("*Listbox.background", p["field"]),
                     ("*Listbox.foreground", p["fg"]),
                     ("*Listbox.selectBackground", p["select_bg"]),
                     ("*Listbox.selectForeground", p["select_fg"]),
                     ("*Canvas.background", p["bg"]),
                     ("*Menu.background", p["panel"]),
                     ("*Menu.foreground", p["fg"]),
                     ("*Menu.activeBackground", p["select_bg"]),
                     ("*Menu.activeForeground", p["select_fg"]),
                     ("*Menu.selectColor", p["fg"])):
        o(pat, val, pri)


def _dark_titlebar(win, dark):
    """Windows 10/11: dark or light title bar to match the theme (best effort)."""
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):            # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def _paint(w, p):
    """Recolour one plain-tk widget by its class."""
    cls = w.winfo_class()
    if cls == "Text":
        w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                    selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                    highlightbackground=p["border"], highlightcolor=p["accent"])
    elif cls == "Listbox":
        w.configure(bg=p["field"], fg=p["fg"], selectbackground=p["select_bg"],
                    selectforeground=p["select_fg"], highlightbackground=p["border"],
                    highlightcolor=p["accent"], disabledforeground=p["disabled_fg"])
    elif cls in ("Entry", "Spinbox"):
        w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                    selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                    disabledbackground=p["bg"], disabledforeground=p["disabled_fg"],
                    readonlybackground=p["bg"], highlightbackground=p["border"],
                    highlightcolor=p["accent"])
        if cls == "Spinbox":
            w.configure(buttonbackground=p["panel"])
    elif cls == "Canvas":
        w.configure(bg=p["bg"], highlightbackground=p["border"])
    elif cls == "Menu":
        w.configure(bg=p["panel"], fg=p["fg"], activebackground=p["select_bg"],
                    activeforeground=p["select_fg"], selectcolor=p["fg"],
                    disabledforeground=p["disabled_fg"])
    elif cls == "Toplevel" or w is w.winfo_toplevel():
        try:
            if not w.wm_overrideredirect():
                w.configure(bg=p["bg"])
                _dark_titlebar(w, p["dark"])
        except tk.TclError:
            pass
    elif cls == "TCombobox":
        # the drop-down list is a Tcl-only child tkinter doesn't know about
        lb = f"{w}.popdown.f.l"
        if w.tk.call("winfo", "exists", lb):
            w.tk.call(lb, "configure", "-background", p["field"], "-foreground", p["fg"],
                      "-selectbackground", p["select_bg"],
                      "-selectforeground", p["select_fg"])
    for fn in list(getattr(w, "_palette_hooks", ()) or ()):
        try:
            fn(p)
        except Exception:
            pass


def _walk(w, p):
    try:
        _paint(w, p)
    except tk.TclError:
        pass
    try:
        kids = w.winfo_children()
    except tk.TclError:
        return
    for c in kids:
        _walk(c, p)


def apply(root, style, name):
    """Apply the concrete palette `name` to the whole app right now."""
    p = PALETTES.get(name, PALETTES[DEFAULT_THEME])
    _style(style, p)
    _options(root, p)
    root.configure(bg=p["bg"])
    _walk(root, p)


def recolor(widget):
    """Re-apply the current palette to `widget` and its children (for a
    Toplevel built after the last theme change)."""
    _walk(widget, current())
