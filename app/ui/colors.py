"""Colour maths of the themes (ui/themes.py, ui/palettes.py): mixing, WCAG
luminance / contrast, a readable text colour on a background."""


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


def _readable(c, bgs, target=4.5, toward=None):
    """c nudged toward `toward` until it reaches `target` contrast on every
    colour in bgs (WCAG body text)."""
    for _i in range(20):
        if all(contrast(c, b) >= target for b in bgs):
            return c
        c = mix(c, toward, 0.12)
    return c
