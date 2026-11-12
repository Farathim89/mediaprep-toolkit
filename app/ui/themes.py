"""GUI themes: one colour palette per theme, everything else derived from it.

    PALETTES[name]   the colours of a theme (ui/palettes.py - the data)
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
drop-downs and menus) to recolour the plain-tk ones.

Look: Windows 11 / Fluent. Each palette's base colours are extended with
derived design tokens in palettes._complete() (card = surface_alt, stroke,
control / hover / pressed fills, accent hover, focus ring, sidebar, info
bars ...; the colour maths is ui/colors.py); ui/fluent.py draws the rounded ttk element images from them (Pillow), the
flat 'clam' styling below stays as the fallback when Pillow is missing.
Widgets inside a Labelframe ("card") get the Card.<style> variant of their
style, so they sit on the card colour (see _cardify)."""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .colors import _rgb, mix
from .palettes import DEFAULT_THEME, FOLLOW_WINDOWS, PALETTES, THEME_NAMES


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
    scale = 1.0                   # display DPI factor (1.0 = 100 %)
    font_scale = 1.0              # the user's font size (Settings, 1.0 = 100 %)
    fonts = {}                    # role -> font family in use
    font_objs = {}                # the app's own named fonts (kept alive)
    px_fonts = {}                 # pixel-sized named font -> (size, at scale, last set)
    card_styles = set()           # Card.<style> variants created so far
    last_light = "Light"          # the sidebar's sun/moon toggle goes back to these
    last_dark = DEFAULT_THEME


_S = _State()
WATCH_MS = 10000                  # Follow Windows re-check interval


def current():
    return PALETTES.get(_S.applied, PALETTES[DEFAULT_THEME])


def current_name():
    return _S.name


def is_dark():
    return bool(current().get("dark"))


def scale():
    """The display DPI factor (1.0 at 100 %, 1.5 at 150 % ...) - of the
    monitor the app is on; it changes when the window moves to a monitor
    with another scaling (ui/dpi.py)."""
    return _S.scale


def px(n):
    """n pixels at 100 % scaling -> pixels at the current DPI (ints)."""
    return max(0, int(round(n * _S.scale))) if n else 0


# Settings -> Font size: every UI font (and what is sized from text: list
# rows and columns) grows / shrinks by this factor. Applied at startup.
FONT_SCALES = (0.9, 1.0, 1.1, 1.2, 1.3)


def font_scale():
    """The font size factor in use (1.0 = 100 %)."""
    return _S.font_scale


def fpx(n):
    """Like px(), for sizes that hold text (list rows / columns): also
    follows the font size setting."""
    return px(n * _S.font_scale) if n else 0


def pad(*vals):
    """Scaled padding tuple, e.g. pad(12, 8)."""
    return tuple(px(v) for v in vals)


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


# ---------------------------------------------------------------- fonts
# role -> candidate families (first installed wins). Segoe UI Variable is
# Windows 11's UI font (GDI lists its named instances as families - the
# Display Semibold name is cut at 31 characters by Windows); Windows 10
# falls back to Segoe UI.
_FONT_CANDIDATES = {
    "body": ("Segoe UI Variable Text", "Segoe UI"),
    "semibold": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "display": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
    "mono": ("Cascadia Mono", "Consolas", "Courier New"),
    "icons": ("Segoe Fluent Icons", "Segoe MDL2 Assets"),
}


def _setup_fonts(root):
    """Configure Tk's named fonts (body 10 pt, captions 9 pt) and create the
    app's own: MPSemibold, MPSemiboldSmall, MPTitle, MPSubtitle, MPCaption,
    MPMono, MPIcon, MPIconSmall, MPIconLarge."""
    try:
        fams = set(tkfont.families(root))
    except tk.TclError:
        fams = set()
    pick = {}
    for role, cands in _FONT_CANDIDATES.items():
        pick[role] = next((f for f in cands if f in fams), None)
    _S.fonts = pick
    body = pick["body"]
    semi = pick["semibold"]
    weight = "normal" if semi else "bold"
    semi = semi or body or "TkDefaultFont"
    disp = pick["display"] or semi
    mono = pick["mono"] or "Courier"

    def named(name, **kw):
        if "size" in kw:            # the font size setting (whole points)
            kw["size"] = max(6, int(round(kw["size"] * _S.font_scale)))
        try:
            tkfont.nametofont(name, root=root).configure(**kw)
        except tk.TclError:
            # keep a reference: a collected Font object deletes its named font
            _S.font_objs[name] = tkfont.Font(root=root, name=name, exists=False, **kw)

    if body:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            named(name, family=body, size=10)
        named("TkTooltipFont", family=body, size=9)
        named("TkSmallCaptionFont", family=body, size=9)
        named("TkCaptionFont", family=semi, size=10, weight=weight)
        named("TkHeadingFont", family=semi, size=10, weight=weight)
    else:
        body = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    if pick["mono"]:
        named("TkFixedFont", family=mono, size=9)
    named("MPSemibold", family=semi, size=10, weight=weight)
    named("MPSemiboldSmall", family=semi, size=9, weight=weight)
    named("MPSubtitle", family=semi, size=12, weight=weight)
    named("MPTitle", family=disp, size=15,
          weight="normal" if pick["display"] or pick["semibold"] else "bold")
    named("MPCaption", family=body, size=9)
    named("MPMono", family=mono, size=9)
    icons = pick["icons"] or body
    named("MPIcon", family=icons, size=12)
    named("MPIconSmall", family=icons, size=10)
    named("MPIconLarge", family=icons, size=16)


def _refresh_fonts(ratio):
    """'tk scaling' changed (ui/dpi.py): realise every named font again, so
    its point size gives the new pixel size (Tk keeps the old pixels until
    a font is reconfigured). A font given in pixels (negative size) is
    scaled by ratio - from the size it started at, so no drift."""
    root = _S.root
    try:
        names = root.tk.splitlist(root.tk.call("font", "names"))
    except tk.TclError:
        return
    for name in names:
        try:
            size = int(root.tk.call("font", "configure", name, "-size"))
            if size < 0:
                rec = _S.px_fonts.get(name)
                if rec is None or rec[2] != size:
                    rec = (size, _S.scale / ratio, size)      # taken at the old scale
                size = min(-1, int(round(rec[0] * _S.scale / rec[1])))
                _S.px_fonts[name] = (rec[0], rec[1], size)
            root.tk.call("font", "configure", name, "-size", size)
        except (tk.TclError, ValueError):
            pass


def rescale(scale):
    """The display DPI changed and 'tk scaling' was set for it (ui/dpi.py):
    the px() factor, every font and every style for the new scale (factor
    1.0 = 100 %)."""
    old = _S.scale
    _S.scale = scale
    if _S.root is None:
        return
    _refresh_fonts(scale / old if old else 1.0)
    if _S.style is not None:
        apply(_S.root, _S.style, _S.applied)


def font_family(role):
    """Installed family for a role (body / semibold / display / mono /
    icons), or None when none of the candidates is installed."""
    return _S.fonts.get(role)


def mono_font(size=9):
    """(family, size) of the log / monospace font (Cascadia Mono -> Consolas)."""
    return (_S.fonts.get("mono") or "Consolas", max(6, int(round(size * _S.font_scale))))


def has_icon_font():
    return bool(_S.fonts.get("icons"))


def init(root, name=DEFAULT_THEME, font_scale=1.0):
    """Create the shared theme variable, apply `name` and start the Windows
    watch. font_scale = the font size setting (FONT_SCALES). Returns the
    StringVar the theme pickers bind to."""
    _S.root = root
    try:
        _S.font_scale = min(max(float(font_scale), FONT_SCALES[0]), FONT_SCALES[-1])
    except (TypeError, ValueError):
        _S.font_scale = 1.0
    _S.style = ttk.Style(root)
    try:
        _S.scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except (tk.TclError, ValueError):
        _S.scale = 1.0
    _setup_fonts(root)
    _install_tree_striping()
    _install_tree_columns()
    if name not in THEME_NAMES:
        name = DEFAULT_THEME
    _S.var = tk.StringVar(master=root, value=name)
    _S.var.trace_add("write", lambda *_a: set_theme(_S.var.get()))
    set_theme(name, force=True)
    # every new window (dialogs, help, tooltips) gets the palette once it's
    # mapped - covers its plain-tk children and the title bar colours
    root.bind_class("Toplevel", "<Map>", _on_map, add="+")
    from .widgets import bind_window
    bind_window(root, "<Map>", _on_root_map)      # the window itself, not every widget
    # widgets mapped later inside a card get their Card.<style> variant
    for cls in _CARD_CLASSES:
        root.bind_class(cls, "<Map>", _on_widget_map, add="+")
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
    # from a timer, not inside <Map> (see _on_widget_map)
    w.after(1, lambda: w.winfo_exists() and _walk(w, current(), _in_card(w)))


def _on_root_map(e):
    if e.widget is _S.root:
        _chrome(_S.root, current())


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
    if name != FOLLOW_WINDOWS:
        if PALETTES[concrete].get("dark"):
            _S.last_dark = name
        else:
            _S.last_light = name
    if _S.root is not None:
        apply(_S.root, _S.style, concrete)
    for fn in list(_S.listeners):
        try:
            fn(name, current())
        except Exception:
            pass


def toggle_dark():
    """The sidebar's sun / moon button: switch between the last light and
    the last dark theme used (Light / Dark by default)."""
    set_theme(_S.last_light if is_dark() else _S.last_dark)


def _watch():
    """Follow Windows: switch Light/Dark when the Windows app mode changes."""
    try:
        if _S.name == FOLLOW_WINDOWS and resolve(FOLLOW_WINDOWS) != _S.applied:
            set_theme(FOLLOW_WINDOWS, force=True)
        _S.root.after(WATCH_MS, _watch)
    except Exception:
        _S.watching = False


# ---------------------------------------------------------------- applying
def _style_flat(style, p):
    """The flat 'clam' look - the base under the Fluent images, and the whole
    look when Pillow is missing."""
    bg, field, fg = p["bg"], p["field"], p["fg"]
    accent, hov, prs = p["accent"], p["control_hover"], p["control_pressed"]
    border = p["stroke"]
    ctl = p["control"]
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
    style.map("TLabel", foreground=[("disabled", dis)])
    btn = dict(background=ctl, foreground=fg, bordercolor=border,
               lightcolor=ctl, darkcolor=ctl, focuscolor=accent, arrowcolor=fg,
               padding=pad(10, 3))
    btn_map = dict(background=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   lightcolor=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   darkcolor=[("disabled", bg), ("pressed", prs), ("active", hov)],
                   foreground=[("disabled", dis)],
                   bordercolor=[("focus", accent)],
                   arrowcolor=[("disabled", dis)])
    for w in ("TButton", "TMenubutton", "Subtle.TButton"):
        style.configure(w, **btn)
        style.map(w, **btn_map)
    style.configure("Subtle.TButton", background=bg, bordercolor=bg, lightcolor=bg,
                    darkcolor=bg, padding=pad(6, 3))
    acc = dict(btn, background=accent, foreground=p["accent_fg"], bordercolor=accent,
               lightcolor=accent, darkcolor=accent)
    style.configure("Accent.TButton", **acc)
    style.map("Accent.TButton",
              background=[("disabled", bg), ("pressed", p["accent_pressed"]),
                          ("active", p["accent_hover"])],
              lightcolor=[("disabled", bg), ("pressed", p["accent_pressed"]),
                          ("active", p["accent_hover"])],
              darkcolor=[("disabled", bg), ("pressed", p["accent_pressed"]),
                         ("active", p["accent_hover"])],
              foreground=[("disabled", dis)], bordercolor=[("focus", p["focus"])])
    # clam draws a 10 px indicator not scaled with the display DPI - size it
    # like the text (13 px at 100 %) and give it a visible edge
    ind = px(13)
    ind_margin = pad(2, 2, 6, 2)
    ind_edge = p["check_stroke"]
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
                    bordercolor=p["field_border"], lightcolor=field, darkcolor=field,
                    selectbackground=sb, selectforeground=sf, padding=pad(4, 2))
    style.map("TEntry", **field_map)
    for w in ("TCombobox", "TSpinbox"):
        style.configure(w, fieldbackground=field, foreground=fg, insertcolor=fg,
                        background=ctl, arrowcolor=fg, bordercolor=border,
                        lightcolor=field, darkcolor=field,
                        selectbackground=sb, selectforeground=sf)
        style.map(w, background=[("disabled", bg), ("pressed", prs), ("active", hov)],
                  arrowcolor=[("disabled", dis)],
                  selectbackground=[("readonly", "!focus", field)],
                  selectforeground=[("readonly", "!focus", fg)], **field_map)
    style.configure("TNotebook", background=bg, bordercolor=border,
                    lightcolor=bg, darkcolor=bg)
    style.configure("TNotebook.Tab", background=bg, foreground=p["muted"], bordercolor=bg,
                    lightcolor=bg, darkcolor=bg, focuscolor=accent, padding=pad(12, 5))
    style.map("TNotebook.Tab",
              background=[("selected", bg), ("active", p["control_hover"])],
              lightcolor=[("selected", accent), ("active", hov)],
              foreground=[("selected", fg), ("active", fg), ("disabled", dis)])
    style.configure("Treeview", background=field, fieldbackground=field, foreground=fg,
                    bordercolor=border, lightcolor=field, darkcolor=field,
                    rowheight=fpx(28))
    style.map("Treeview", background=[("selected", sb)], foreground=[("selected", sf)])
    style.configure("Treeview.Heading", background=p["heading_bg"], foreground=p["muted"],
                    bordercolor=border, lightcolor=p["heading_bg"],
                    darkcolor=p["heading_bg"], font="MPCaption")
    style.map("Treeview.Heading", background=[("active", p["heading_hover"])])
    for w in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(w, background=p["scroll_thumb"], troughcolor=bg, bordercolor=bg,
                        arrowcolor=fg, lightcolor=p["scroll_thumb"],
                        darkcolor=p["scroll_thumb"], gripcount=0, arrowsize=px(12))
        style.map(w, background=[("pressed", p["scroll_thumb_hover"]),
                                 ("active", p["scroll_thumb_hover"])],
                  arrowcolor=[("disabled", dis)])
    for w in ("TProgressbar", "Horizontal.TProgressbar", "Vertical.TProgressbar"):
        style.configure(w, background=accent, troughcolor=p["progress_track"],
                        bordercolor=bg, lightcolor=accent, darkcolor=accent,
                        thickness=px(6))
    style.configure("TLabelframe", background=bg, bordercolor=p["card_stroke"],
                    lightcolor=bg, darkcolor=bg, padding=pad(14, 10, 14, 12))
    style.configure("TLabelframe.Label", background=bg, foreground=fg, font="MPSemibold")
    style.configure("Cardbox.TFrame", background=p["card"], padding=pad(14, 10, 14, 12))
    for w in ("TScale", "Horizontal.TScale", "Vertical.TScale"):
        style.configure(w, background=ctl, troughcolor=p["progress_track"],
                        bordercolor=border, lightcolor=ctl, darkcolor=ctl)
        style.map(w, background=[("pressed", prs), ("active", hov)])
    style.configure("TSeparator", background=border)
    # TimeEntry: one bordered box around border-less entries
    style.configure("TimeBox.TFrame", background=field, borderwidth=1, relief="solid",
                    bordercolor=p["field_border"], lightcolor=field, darkcolor=field)
    style.configure("Bare.TEntry", fieldbackground=field, background=field, borderwidth=0,
                    bordercolor=field, lightcolor=field, darkcolor=field, padding=pad(2, 2))
    style.configure("Bare.TLabel", background=field, foreground=p["muted"])
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", background=border)


def _style_common(style, p):
    """App styles shared by the Fluent and the flat look."""
    bg, fg, muted = p["bg"], p["fg"], p["muted"]
    style.configure("Hint.TLabel", background=bg, foreground=muted, font="MPCaption")
    style.configure("Title.TLabel", background=bg, foreground=fg, font="MPTitle")
    style.configure("Subtitle.TLabel", background=bg, foreground=fg, font="MPSubtitle")
    style.configure("Strong.TLabel", background=bg, foreground=fg, font="MPSemibold")
    style.configure("Mono.TLabel", background=bg, foreground=fg, font="MPMono")
    # the ⓘ info icons (tooltips.info_icon): muted, accent on hover / focus
    style.configure("Info.TLabel", background=bg, foreground=muted, padding=pad(4, 0))
    style.map("Info.TLabel", foreground=[("active", p["accent"]), ("focus", p["accent"])])
    # the window chrome: sidebar + status bar surface
    cb = p["sidebar_bg"]
    style.configure("Chrome.TFrame", background=cb)
    style.configure("Chrome.TLabel", background=cb, foreground=fg)
    style.configure("Chrome.Hint.TLabel", background=cb, foreground=muted, font="MPCaption")
    style.configure("Chrome.Subtle.TButton", background=cb)
    style.configure("Chrome.TButton", background=cb)
    style.configure("Chrome.Horizontal.TProgressbar", background=cb)
    # a pill bar (e.g. the player transport): a card-coloured strip
    style.configure("Pill.TFrame", background=p["card"])
    style.configure("Pill.Subtle.TButton", background=p["card"])
    style.configure("Pill.TLabel", background=p["card"], foreground=fg)
    style.configure("Pill.Hint.TLabel", background=p["card"], foreground=muted,
                    font="MPCaption")
    style.configure("Pill.Horizontal.TScale", background=p["card"])
    style.configure("Pill.TCheckbutton", background=p["card"])
    # widgets inside cards
    for name in sorted(_S.card_styles):
        _card_configure(style, name, p)


def _card_configure(style, name, p):
    card = p["card"]
    try:
        style.configure(name, background=card)
        if name.endswith("TLabelframe"):
            style.configure(name + ".Label", background=card)
        elif name.endswith("TNotebook"):
            style.configure(name + ".Tab", background=card)
        elif name.endswith(("TCheckbutton", "TRadiobutton")):
            style.map(name, background=[])
    except tk.TclError:
        pass


def _style(style, p):
    _style_flat(style, p)
    from . import fluent
    if fluent.available():
        try:
            fluent.apply(style, p, _S.scale)
        except Exception as exc:       # never let the look break the app
            import sys
            sys.stderr.write(f"[themes] Fluent styling failed, flat look kept: {exc}\n")
            style.theme_use("clam")
    _style_common(style, p)


def _options(root, p):
    """Option database: colours for plain-tk widgets created from now on."""
    o = root.option_add
    pri = "interactive"
    for pat, val in (("*TCombobox*Listbox.background", p["card"]),
                     ("*TCombobox*Listbox.foreground", p["fg"]),
                     ("*TCombobox*Listbox.selectBackground", p["select_bg"]),
                     ("*TCombobox*Listbox.selectForeground", p["select_fg"]),
                     ("*TCombobox*Listbox.font", "TkDefaultFont"),
                     ("*TCombobox*Listbox.borderWidth", 0),
                     ("*Toplevel.background", p["bg"]),
                     ("*Text.background", p["field"]),
                     ("*Text.foreground", p["fg"]),
                     ("*Text.insertBackground", p["fg"]),
                     ("*Text.selectBackground", p["select_bg"]),
                     ("*Text.selectForeground", p["select_fg"]),
                     ("*Text.relief", "flat"),
                     ("*Text.highlightThickness", 1),
                     ("*Text.highlightBackground", p["stroke"]),
                     ("*Text.highlightColor", p["accent"]),
                     ("*Listbox.background", p["field"]),
                     ("*Listbox.foreground", p["fg"]),
                     ("*Listbox.selectBackground", p["select_bg"]),
                     ("*Listbox.selectForeground", p["select_fg"]),
                     ("*Listbox.relief", "flat"),
                     ("*Canvas.background", p["bg"]),
                     ("*Menu.background", p["card"]),
                     ("*Menu.foreground", p["fg"]),
                     ("*Menu.activeBackground", p["select_bg"]),
                     ("*Menu.activeForeground", p["select_fg"]),
                     ("*Menu.selectColor", p["fg"]),
                     ("*Menu.relief", "flat"),
                     ("*Menu.activeBorderWidth", 0)):
        o(pat, val, pri)


# ---------------------------------------------------------------- window chrome
def _colorref(c):
    r, g, b = _rgb(c)
    return r | (g << 8) | (b << 16)


def _chrome(win, p, caption=None):
    """Windows 10/11 title bar following the theme (best effort): dark / light
    mode (DWMWA_USE_IMMERSIVE_DARK_MODE 20, 19 on old Windows 10 builds) and on
    Windows 11 the caption, caption-text and border colours (35, 36, 34).
    Mica (DWMWA_SYSTEMBACKDROP_TYPE 38) is not used: Tk paints its client
    area opaque, so the backdrop would only show in the title bar."""
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi

        def put(attr, value):
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        for attr in (20, 19):
            if put(attr, 1 if p["dark"] else 0) == 0:
                break
        if _accent_title_bars():
            return      # the user wants the Windows accent colour there - keep it
        if caption is None:
            caption = p["sidebar_bg"] if win is _S.root else p["bg"]
        put(35, _colorref(caption))
        put(36, _colorref(p["fg"]))
        put(34, _colorref(p["stroke"] if not p.get("hc") else p["border"]))
    except Exception:
        pass


_ACCENT_BARS = []


def _accent_title_bars():
    """True when Windows' 'Show accent colour on title bars' is on (DWM
    ColorPrevalence) - Windows then ignores the caption colour anyway."""
    if not _ACCENT_BARS:
        val = False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\DWM") as k:
                val = bool(winreg.QueryValueEx(k, "ColorPrevalence")[0])
        except Exception:
            pass
        _ACCENT_BARS.append(val)
    return _ACCENT_BARS[0]


# ---------------------------------------------------------------- cards
# ttk classes that get a Card.<style> variant inside a card (Labelframe)
_CARD_CLASSES = ("TFrame", "TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry",
                 "TCombobox", "TSpinbox", "TScale", "TProgressbar", "TScrollbar",
                 "TSeparator", "TLabelframe", "TNotebook", "TMenubutton", "TPanedwindow")
_ORIENTED = ("TScale", "TProgressbar", "TScrollbar")


def _is_card_root(w):
    try:
        cls = w.winfo_class()
        if cls == "TLabelframe":
            return True
        if cls == "TFrame":
            return str(w.cget("style")).endswith("Cardbox.TFrame")
    except (tk.TclError, AttributeError):
        pass
    return False


def _in_card(w):
    """True if one of w's ancestors is a card (cached on the widgets)."""
    m = getattr(w, "master", None)
    while m is not None:
        v = getattr(m, "_mp_cardroot", None)
        if v is None:
            v = _is_card_root(m)
            try:
                m._mp_cardroot = v
            except Exception:
                pass
        if v:
            return True
        m = getattr(m, "master", None)
    return False


def card_style(name):
    """The Card.<name> variant (created on first use) - for widgets placed on
    a card."""
    new = name if name.startswith("Card.") else "Card." + name
    if new not in _S.card_styles:
        _S.card_styles.add(new)
        if _S.style is not None:
            _card_configure(_S.style, new, current())
    return new


def _cardify(w, cls):
    if getattr(w, "_mp_carded", False):
        return
    try:
        w._mp_carded = True
        st = str(w.cget("style"))
        if st.startswith(("Card.", "Pill.", "Chrome.", "Bare.")):
            return
        if not st:
            st = cls
            if cls in _ORIENTED:
                st = ("Vertical." if str(w.cget("orient")) == "vertical"
                      else "Horizontal.") + cls
        w.configure(style=card_style(st))
        if getattr(w, "_mp_icon", None) is not None:
            from .icons import redecorate      # icon flattened onto the card
            redecorate(w)
    except (tk.TclError, AttributeError):
        pass


_PENDING = []


def _on_widget_map(e):
    """A ttk widget got mapped: queue it for the card check. The restyle runs
    from a timer, NOT inside the <Map> event - changing a widget's style while
    Tk is still placing its siblings can send ttk::labelframe geometry into
    an endless loop."""
    w = e.widget
    if isinstance(w, str) or getattr(w, "_mp_cc", False):
        return
    try:
        w._mp_cc = True
    except Exception:
        return
    _PENDING.append(w)
    if len(_PENDING) == 1 and _S.root is not None:
        _S.root.after(1, _flush_pending)


def _flush_pending():
    items = _PENDING[:]
    del _PENDING[:]
    for w in items:
        try:
            if w.winfo_exists() and _in_card(w):
                _cardify(w, w.winfo_class())
        except Exception:
            pass


# ---------------------------------------------------------------- tree rows
def _install_tree_striping():
    """Subtle zebra rows on every ttk.Treeview: the top-level rows get the
    'mp_odd' tag every other row, re-applied (once per idle) after inserts,
    deletes, moves and tag changes."""
    if getattr(ttk.Treeview, "_mp_striped", False):
        return
    ttk.Treeview._mp_striped = True
    orig = {n: getattr(ttk.Treeview, n) for n in ("insert", "delete", "move", "detach",
                                                   "reattach", "item")}

    def restripe(tree):
        tree._mp_stripe_job = None
        try:
            if not tree.winfo_exists():
                return
            tree.tag_configure("mp_odd", background=current()["tree_alt"])
            for i, iid in enumerate(tree.get_children("")):
                tags = [t for t in orig["item"](tree, iid, "tags") if t != "mp_odd"]
                if i % 2:
                    tags.append("mp_odd")
                orig["item"](tree, iid, tags=tags)
        except tk.TclError:
            pass

    def schedule(tree):
        if getattr(tree, "_mp_stripe_job", None) is None:
            try:
                tree._mp_stripe_job = tree.after_idle(restripe, tree)
            except (tk.TclError, RuntimeError):
                pass

    def wrap(name):
        fn = orig[name]

        def method(self, *a, **kw):
            out = fn(self, *a, **kw)
            if name != "item" or "tags" in kw:
                schedule(self)
            return out
        method.__name__ = name
        method.__doc__ = fn.__doc__
        return method
    for n in orig:
        setattr(ttk.Treeview, n, wrap(n))


def _install_tree_columns():
    """Column widths given to ttk.Treeview.column() follow the font size
    setting (the columns hold text): width / minwidth are scaled once, when
    they are set."""
    if getattr(ttk.Treeview, "_mp_cols", False):
        return
    ttk.Treeview._mp_cols = True
    orig = ttk.Treeview.column

    def column(self, column, option=None, **kw):
        f = _S.font_scale
        if f != 1.0:
            for k in ("width", "minwidth"):
                if isinstance(kw.get(k), int):
                    kw[k] = int(round(kw[k] * f))
        return orig(self, column, option, **kw)
    column.__doc__ = orig.__doc__
    ttk.Treeview.column = column


# ---------------------------------------------------------------- the walk
def _paint(w, p, card=False):
    """Recolour one widget by its class (plain-tk ones; ttk ones get their
    Card.<style> inside cards)."""
    cls = w.winfo_class()
    if card and cls in _CARD_CLASSES:
        _cardify(w, cls)
    if cls == "Text":
        w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                    selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                    highlightbackground=p["stroke"], highlightcolor=p["accent"])
    elif cls == "Listbox":
        w.configure(bg=p["field"], fg=p["fg"], selectbackground=p["select_bg"],
                    selectforeground=p["select_fg"], highlightbackground=p["stroke"],
                    highlightcolor=p["accent"], disabledforeground=p["disabled_fg"])
    elif cls in ("Entry", "Spinbox"):
        w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                    selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                    disabledbackground=p["bg"], disabledforeground=p["disabled_fg"],
                    readonlybackground=p["bg"], highlightbackground=p["stroke"],
                    highlightcolor=p["accent"])
        if cls == "Spinbox":
            w.configure(buttonbackground=p["panel"])
    elif cls == "Canvas":
        w.configure(bg=p["card"] if card else p["bg"], highlightbackground=p["stroke"])
    elif cls == "Menu":
        w.configure(bg=p["card"], fg=p["fg"], activebackground=p["select_bg"],
                    activeforeground=p["select_fg"], selectcolor=p["fg"],
                    disabledforeground=p["disabled_fg"])
    elif cls == "Treeview":
        w.tag_configure("mp_odd", background=p["tree_alt"])
    elif cls == "Toplevel" or w is w.winfo_toplevel():
        try:
            if not w.wm_overrideredirect():
                w.configure(bg=p["bg"])
                _chrome(w, p)
        except tk.TclError:
            pass
    elif cls == "TCombobox":
        # the drop-down list is a Tcl-only child tkinter doesn't know about
        lb = f"{w}.popdown.f.l"
        if w.tk.call("winfo", "exists", lb):
            w.tk.call(lb, "configure", "-background", p["card"], "-foreground", p["fg"],
                      "-selectbackground", p["select_bg"],
                      "-selectforeground", p["select_fg"])
    for fn in list(getattr(w, "_palette_hooks", ()) or ()):
        try:
            fn(p)
        except Exception:
            pass


def _walk(w, p, card=False):
    try:
        _paint(w, p, card)
    except tk.TclError:
        pass
    try:
        kids = w.winfo_children()
    except tk.TclError:
        return
    if not card:
        v = getattr(w, "_mp_cardroot", None)
        if v is None:
            v = _is_card_root(w)
            try:
                w._mp_cardroot = v
            except Exception:
                pass
        card = v
    for c in kids:
        _walk(c, p, card)


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
    _walk(widget, current(), _in_card(widget))
