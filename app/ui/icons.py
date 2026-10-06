"""One icon set for the whole app: Windows' own icon font.

Segoe Fluent Icons (Windows 11) with Segoe MDL2 Assets (Windows 10) as the
fallback - no image files to ship, crisp at any DPI, and the colour follows
the theme.

    glyph(name)            the character for a tk.Label / Canvas text drawn
                           with the MPIcon* fonts ("" without an icon font)
    image(name, ...)       a PhotoImage of the icon in a theme colour (Pillow),
                           redrawn in place on every theme change
    decorate(button, name) put the icon on a ttk.Button (left of its text;
                           icon only when the button has no text)
"""
import os

from . import themes

# name -> code point (identical in Segoe Fluent Icons and Segoe MDL2 Assets)
GLYPHS = {
    "menu": "\ue700", "templates": "\ueca5", "cut": "\ue8c6", "audio": "\ue8d6",
    "inspect": "\ue721", "log": "\ue9f9", "info": "\ue946", "settings": "\ue713",
    "help": "\ue897", "play": "\ue768", "pause": "\ue769", "stop": "\ue71a",
    "to_start": "\ue892", "to_end": "\ue893", "step_back": "\ue76b",
    "step_fwd": "\ue76c", "back10": "\ueb9e", "fwd10": "\ueb9d",
    "volume": "\ue767", "mute": "\ue74f", "folder": "\ued25", "load": "\ue8e5",
    "detect": "\uf4a5", "snap": "\ue8af", "set": "\ue718", "queue": "\ue8fd",
    "trash": "\ue74d", "add": "\ue710", "remove": "\ue738", "refresh": "\ue72c",
    "save": "\ue74e", "export": "\uede1", "sun": "\ue706", "moon": "\ue708",
    "close": "\ue711", "warning": "\ue7ba", "error": "\uea39", "success": "\ue930",
    "broom": "\uea99", "check": "\ue73e", "shield": "\uea18", "compare": "\ue8ab",
    "eject": "\uf847", "go": "\ue72a", "up": "\ue70e", "down": "\ue70d",
    "chevron_down": "\ue70d", "list": "\ue8fd", "copy": "\ue8c8", "link": "\ue71b",
    "filter": "\ue71c", "edit": "\ue70f", "import": "\ue8b5", "open": "\ue8e5",
    "headphones": "\ue7f6", "pin": "\ue718", "run": "\ue768", "flag": "\ue7c1",
}

_FONT_FILES = ("SegoeIcons.ttf", "segmdl2.ttf")


class _State:
    font_path = None
    checked = False
    images = {}          # key -> (PhotoImage, name, color_key, size)
    subscribed = False


_S = _State()


def _font_path():
    if not _S.checked:
        _S.checked = True
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for f in _FONT_FILES:
            p = os.path.join(windir, "Fonts", f)
            if os.path.isfile(p):
                _S.font_path = p
                break
    return _S.font_path


def available():
    """True when icons can be drawn as images (icon font + Pillow)."""
    if not _font_path():
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
        return bool(Image and ImageDraw and ImageFont)
    except Exception:
        return False


def glyph(name):
    """The icon character, or "" when no icon font is installed."""
    if not themes.has_icon_font():
        return ""
    return GLYPHS.get(name, "")


def _render(name, color, size_px, gap_px=0, back=None):
    """PNG (base64) of the glyph; on an opaque `back` colour when given (an
    opaque photo is a plain blit for Tk - alpha ones are blended per pixel
    on every redraw)."""
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(_font_path(), size_px)
    im = Image.new("RGBA", (size_px + gap_px, size_px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((size_px / 2.0, size_px / 2.0), GLYPHS[name], font=font, fill=color, anchor="mm")
    if back is not None:
        flat = Image.new("RGB", im.size, back)
        flat.paste(im, mask=im.split()[3])
        im = flat
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


def _color(pal, spec):
    """A palette key, a literal colour, or (key_a, key_b, t) = mix(a, b, t)."""
    if spec is None:
        return None
    if isinstance(spec, tuple):
        a, b, t = spec
        return themes.mix(pal.get(a, a), pal.get(b, b), t)
    return pal.get(spec, spec)


def image(name, color_key="fg", size=16, gap=0, back=None):
    """PhotoImage of icon `name` in palette colour `color_key` at `size` px
    (100 % scaling; grows with the DPI), with `gap` px of space on its
    right (room before a button's text), on the opaque background `back`
    (palette key or (a, b, t) mix; None = transparent). Same arguments ->
    same image, redrawn in place on theme changes. None when icons can't be
    drawn."""
    if name not in GLYPHS or not available():
        return None
    import tkinter as tk
    bk = "-".join(str(x) for x in back) if isinstance(back, tuple) else str(back)
    key = f"{name}_{color_key}_{size}_{gap}_{bk}".replace(".", "")
    hit = _S.images.get(key)
    if hit is not None:
        return hit[0]
    pal = themes.current()
    px = max(8, themes.px(size))
    gp = themes.px(gap)
    try:
        ph = tk.PhotoImage(name="mpi_" + key,
                           data=_render(name, pal.get(color_key, color_key), px, gp,
                                        _color(pal, back)))
    except Exception:
        return None
    _S.images[key] = (ph, name, color_key, px, gp, back)
    if not _S.subscribed:
        _S.subscribed = True
        themes.subscribe(_recolor)
    return ph


def _recolor(_name, pal):
    for ph, name, color_key, px, gp, back in list(_S.images.values()):
        try:
            ph.configure(data=_render(name, pal.get(color_key, color_key), px, gp,
                                      _color(pal, back)))
        except Exception:
            pass


# button style -> the fills under its icon per state (normal, disabled,
# pressed, hover); None = keep the icon transparent (surface unknown)
def _fills(style):
    base = style.split(".", 1)[1] if style.startswith(("Card.", "Pill.", "Chrome.")) else style
    if base in ("", "TButton", "TMenubutton"):
        return ("control", "control_disabled", "control_pressed", "control_hover")
    if base == "Accent.TButton":
        return ("accent", "control_disabled", "accent_pressed", "accent_hover")
    if base == "Subtle.TButton":
        surf = ("sidebar_bg" if style.startswith("Chrome.") else
                "card" if style.startswith(("Pill.", "Card.")) else "bg")
        return (surf, surf, (surf, "fg", 0.045), (surf, "fg", 0.075))
    return None


def decorate(button, name, size=16, color_key=None):
    """Show icon `name` on a ttk.Button, left of its text (icon only when the
    text is empty or "..."). Accent buttons get the on-accent colour. Returns
    the button."""
    try:
        style = str(button.cget("style"))
    except Exception:
        style = ""
    if color_key is None:
        color_key = "accent_fg" if "Accent." in style else "fg"
    try:
        text = str(button.cget("text"))
    except Exception:
        text = ""
    only = text.strip() in ("", "...")
    gap = 0 if only else 7
    fills = _fills(style) if themes.has_icon_font() else None
    if fills is None:
        img = image(name, color_key, size, gap)
        if img is None:
            return button
        dis = image(name, "disabled_fg", size, gap)
        spec = (img, "disabled", dis) if dis is not None else img
    else:
        n, d, pr, hv = fills
        img = image(name, color_key, size, gap, n)
        if img is None:
            return button
        spec = (img, "disabled", image(name, "disabled_fg", size, gap, d),
                "pressed", image(name, color_key, size, gap, pr),
                "active", image(name, color_key, size, gap, hv))
    try:
        button.configure(image=spec, compound="image" if only else "left")
        # the icon is flattened onto the surface of the button's style: when
        # the button later moves onto a card (Card.<style>, themes._cardify)
        # it is decorated again for that surface
        button._mp_icon = (name, size, color_key if color_key != "fg" else None)
    except Exception:
        pass
    return button


def redecorate(button):
    """Re-apply a decorate() icon after the button's style changed."""
    spec = getattr(button, "_mp_icon", None)
    if spec is not None:
        decorate(button, spec[0], spec[1], spec[2])


def set_icon(button, name, size=16, color_key=None):
    """Swap the icon of a decorated button (e.g. play <-> pause)."""
    return decorate(button, name, size, color_key)
