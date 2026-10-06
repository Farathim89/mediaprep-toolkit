"""Fluent (Windows 11) look for ttk, drawn at runtime.

Every rounded control surface (buttons, fields, check boxes, cards, tabs,
scrollbars, progress bars, sliders, list headers) is an ttk *image element*
whose pictures are painted with Pillow in the current theme's colours and at
the display's DPI. The element images keep fixed Tk names; on a theme change
only their pixels are replaced (PhotoImage.configure(data=...)), so every
widget follows instantly without re-creating anything.

    available()            True when Pillow is installed (else themes.py keeps
                           the plain flat 'clam' look)
    apply(style, p, s)     build (once) + recolour the 'mpfluent' ttk theme for
                           palette p at DPI factor s (1.0 = 100 %)

Styles added on top of the standard ones:
    Accent.TButton   accent-filled primary action button
    Subtle.TButton   transparent button (toolbars, the player transport)
    Card.<style>     the variant of any style used INSIDE a card (a
                     Labelframe): same look on the card background
    Cardbox.TFrame   a frame drawn as a card (rounded, card fill, stroke)
"""
import base64
import io
import tkinter as tk

THEME = "mpfluent"
SS = 4                      # supersampling factor for anti-aliased edges
# Width of the stretched middle band of every 9-slice picture. ttk TILES that
# band (it can't scale), one blit per tile - a 2 px band over a 1500 px
# wide card meant thousands of alpha blits per redraw (a frozen window, and
# the lag while resizing). A wide band keeps it to a handful.
M = 64
MB = 256                    # ... for the big surfaces (cards, long bars)


class _State:
    images = {}             # key -> tk.PhotoImage (fixed names)
    created_for = None      # interpreter the elements/layouts were made in
    scale = None


_F = _State()


def available():
    try:
        from PIL import Image, ImageDraw
        return bool(Image and ImageDraw)
    except Exception:
        return False


# ---------------------------------------------------------------- colours
def _rgba(c, a=1.0):
    if isinstance(c, tuple):
        return c
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(round(255 * a)))


CLEAR = (0, 0, 0, 0)


# ---------------------------------------------------------------- drawing
def _new(w, h):
    from PIL import Image
    return Image.new("RGBA", (w * SS, h * SS), CLEAR)


def _down(im, w, h):
    from PIL import Image
    # premultiplied, so the transparent surroundings don't darken the edges
    return im.convert("RGBa").resize((w, h), Image.LANCZOS).convert("RGBA")


def _rr(im, box, r, fill):
    """Anti-aliased rounded rectangle (box/r in FINAL pixels, floats ok)."""
    from PIL import ImageDraw
    x0, y0, x1, y1 = (int(round(v * SS)) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    rr = max(0, min(int(round(r * SS)), (x1 - x0) // 2, (y1 - y0) // 2))
    ImageDraw.Draw(im).rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=rr, fill=fill)


def _ellipse(im, box, fill):
    from PIL import ImageDraw
    x0, y0, x1, y1 = (int(round(v * SS)) for v in box)
    ImageDraw.Draw(im).ellipse((x0, y0, x1 - 1, y1 - 1), fill=fill)


def _lines(im, pts, width, fill):
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    p = [(x * SS, y * SS) for x, y in pts]
    w = max(1, int(round(width * SS)))
    d.line(p, fill=fill, width=w, joint="curve")
    r = w / 2.0                                   # round caps
    for x, y in (p[0], p[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _control(w, h, r, fill, stroke, sw, bottom=None, bw=None):
    """Rounded control surface: `stroke` outline (sw px) whose bottom edge is
    `bottom` (bw px) when given - the Fluent elevation / text-field
    underline - filled with `fill`."""
    im = _new(w, h)
    _rr(im, (0, 0, w, h), r, stroke)
    if bottom is not None:
        bw = bw or sw
        mask = _new(w, h)
        _rr(mask, (0, 0, w, h), r, (255, 255, 255, 255))
        im = _band_over(im, mask, bottom, h, bw)
    else:
        bw = sw
    _rr(im, (sw, sw, w - sw, h - bw), max(0, r - sw), fill)
    return _down(im, w, h)


def _band_over(im, mask, color, h, bw):
    """Paint the bottom `bw` px of the rounded outline in `color`."""
    from PIL import Image
    band = Image.new("RGBA", im.size, CLEAR)
    y0 = int(round((h - bw) * SS))
    band.paste(color, (0, y0, im.size[0], im.size[1]))
    alpha = mask.split()[3]
    band.putalpha(Image.composite(band.split()[3], Image.new("L", im.size, 0), alpha))
    out = im.copy()
    out.alpha_composite(band)
    return out


def mix_hex(a, b, t):
    ra, rb = _rgba(a), _rgba(b)
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(ra[:3], rb[:3]))


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


# the surfaces a control can sit on: the page (bg), a card (also the player's
# pill bar) and the window chrome (sidebar / status bar). Every picture is
# flattened onto each of them: an opaque photo is a plain blit for Tk, one
# with an alpha channel is blended pixel by pixel on every redraw (that made
# resizing twice as slow).
SURFACES = (("", "bg"), ("_card", "card"), ("_chrome", "sidebar_bg"))
_SURF = {}


def _flat(img, color):
    from PIL import Image
    base = Image.new("RGB", img.size, _rgba(color)[:3])
    base.paste(img, mask=img.split()[3])
    return base


def _store(key, img):
    data = _png(img)
    ph = _F.images.get(key)
    if ph is None:
        ph = tk.PhotoImage(name="mpf_" + key, data=data)
        _F.images[key] = ph
    else:
        ph.configure(data=data)
    return ph


def _put(key, img, on=None):
    """Create or re-fill the fixed-name PhotoImages `mpf_<key>[_card|_chrome]`
    (img flattened onto each surface; on="card" -> only that one, under
    the plain key)."""
    if on is not None:
        return _store(key, _flat(img, _SURF[on]))
    for sfx, surf in SURFACES:
        _store(key + sfx, _flat(img, _SURF[surf]))


# ---------------------------------------------------------------- the pictures
def _u(s):
    return lambda n: max(1, int(round(n * s)))


def _paint_all(p, s):
    """(Re)draw every element picture for palette p at DPI factor s."""
    u = _u(s)
    _SURF.update(bg=p["bg"], card=p["card"], sidebar_bg=p["sidebar_bg"])
    sw = u(1)
    R = u(4)                          # control corner radius
    fg = p["fg"]
    card, field, ctl = p["card"], p["field"], p["control"]

    # ---- buttons (standard / accent / subtle) ----
    B = R + sw + u(2)
    W = H = 2 * B + M
    elev = p["control_bottom"]
    _put("btn_n", _control(W, H, R, _rgba(ctl), _rgba(p["stroke"]), sw, _rgba(elev), sw))
    _put("btn_h", _control(W, H, R, _rgba(p["control_hover"]), _rgba(p["stroke"]), sw,
                           _rgba(elev), sw))
    _put("btn_p", _control(W, H, R, _rgba(p["control_pressed"]), _rgba(p["stroke"]), sw))
    _put("btn_d", _control(W, H, R, _rgba(p["control_disabled"]),
                           _rgba(p["stroke_subtle"]), sw))
    _put("btn_f", _control(W, H, R, _rgba(ctl), _rgba(p["focus"]), u(2)))
    acc = p["accent"]
    acc_edge = p["accent_edge"]
    _put("acc_n", _control(W, H, R, _rgba(acc), _rgba(acc_edge), sw))
    _put("acc_h", _control(W, H, R, _rgba(p["accent_hover"]), _rgba(acc_edge), sw))
    _put("acc_p", _control(W, H, R, _rgba(p["accent_pressed"]), _rgba(p["accent_pressed"]), sw))
    _put("acc_d", _control(W, H, R, _rgba(p["control_disabled"]),
                           _rgba(p["stroke_subtle"]), sw))
    _put("acc_f", _control(W, H, R, _rgba(acc), _rgba(p["focus"]), u(2)))
    sub = _new(W, H)
    _put("sub_n", _down(sub, W, H))
    _put("sub_h", _control(W, H, R, _rgba(fg, 0.075), CLEAR, sw))
    _put("sub_p", _control(W, H, R, _rgba(fg, 0.045), CLEAR, sw))
    _put("sub_f", _control(W, H, R, CLEAR, _rgba(p["focus"]), u(2)))
    # selected toggle (e.g. a pill-bar button that is "on")
    _put("sub_s", _control(W, H, R, _rgba(fg, 0.11), CLEAR, sw))

    # ---- text fields (entry / editable combobox / spinbox) ----
    E = R + u(3)
    FW, FH = 2 * E + M, 2 * E + M
    # inputs: field fill + a 1 px border with >= 3:1 against the surface
    # (field_border); focus = accent border with a 2 px accent bottom
    fb = p["field_border"]
    fbh = mix_hex(fb, fg, 0.25)
    _put("fld_n", _control(FW, FH, R, _rgba(field), _rgba(fb), sw))
    _put("fld_h", _control(FW, FH, R, _rgba(p["field_hover"]), _rgba(fbh), sw))
    _put("fld_f", _control(FW, FH, R, _rgba(field), _rgba(p["accent"]), sw,
                           _rgba(p["accent"]), u(2)))
    _put("fld_d", _control(FW, FH, R, _rgba(p["control_disabled"]),
                           _rgba(p["stroke"]), sw))
    # read-only drop-down: a control fill, but the same input border
    _put("cmb_n", _control(FW, FH, R, _rgba(ctl), _rgba(fb), sw))
    _put("cmb_h", _control(FW, FH, R, _rgba(p["control_hover"]), _rgba(fbh), sw))
    _put("cmb_p", _control(FW, FH, R, _rgba(p["control_pressed"]), _rgba(fb), sw))
    _put("cmb_f", _control(FW, FH, R, _rgba(ctl), _rgba(p["accent"]), sw,
                           _rgba(p["accent"]), u(2)))

    # chevrons (combobox arrow, spinbox arrows)
    def chevron(w, h, up, color, cw=None):
        im = _new(w, h)
        cw = cw or u(8)
        ch = cw / 2.0
        cx, cy = w / 2.0, h / 2.0
        if up:
            pts = [(cx - ch, cy + ch / 2), (cx, cy - ch / 2), (cx + ch, cy + ch / 2)]
        else:
            pts = [(cx - ch, cy - ch / 2), (cx, cy + ch / 2), (cx + ch, cy - ch / 2)]
        _lines(im, pts, max(1.0, 1.15 * s), color)
        return _down(im, w, h)
    arrow_w = u(22)
    _put("chev_d", chevron(arrow_w, u(16), False, _rgba(p["muted"])))
    _put("chev_dd", chevron(arrow_w, u(16), False, _rgba(p["disabled_fg"])))
    _put("spin_u", chevron(u(20), u(10), True, _rgba(p["muted"]), u(7)))
    _put("spin_d", chevron(u(20), u(10), False, _rgba(p["muted"]), u(7)))
    _put("spin_uh", chevron(u(20), u(10), True, _rgba(fg), u(7)))
    _put("spin_dh", chevron(u(20), u(10), False, _rgba(fg), u(7)))

    # ---- check boxes / radio buttons ----
    C = u(18)
    gap = u(8)
    box_stroke = p["check_stroke"]

    def check(state):
        im = _new(C + gap, C)
        if state in ("on", "on_h", "on_p", "on_d", "mixed", "mixed_d"):
            fill = {"on": p["accent"], "on_h": p["accent_hover"],
                    "on_p": p["accent_pressed"], "on_d": p["disabled_fg"],
                    "mixed": p["accent"], "mixed_d": p["disabled_fg"]}[state]
            _rr(im, (0, 0, C, C), R, _rgba(fill))
            mark = _rgba(p["accent_fg"] if not state.endswith("_d") else p["bg"])
            if state.startswith("mixed"):
                _lines(im, [(C * 0.28, C * 0.5), (C * 0.72, C * 0.5)], 1.6 * s, mark)
            else:
                _lines(im, [(C * 0.26, C * 0.52), (C * 0.43, C * 0.68),
                            (C * 0.75, C * 0.33)], 1.5 * s, mark)
        else:
            edge = {"off": box_stroke, "off_h": box_stroke, "off_p": box_stroke,
                    "off_d": p["stroke_subtle"]}[state]
            inner = {"off": p["check_fill"], "off_h": p["check_fill_hover"],
                     "off_p": p["check_fill_hover"], "off_d": p["check_fill"]}[state]
            _rr(im, (0, 0, C, C), R, _rgba(edge))
            _rr(im, (sw, sw, C - sw, C - sw), R - sw, _rgba(inner))
        return _down(im, C + gap, C)
    for st in ("off", "off_h", "off_p", "off_d", "on", "on_h", "on_p", "on_d",
               "mixed", "mixed_d"):
        _put("chk_" + st, check(st))

    def radio(state):
        im = _new(C + gap, C)
        if state.startswith("on"):
            fill = {"on": p["accent"], "on_h": p["accent_hover"], "on_p": p["accent_pressed"],
                    "on_d": p["disabled_fg"]}[state]
            _ellipse(im, (0, 0, C, C), _rgba(fill))
            dot = {"on": 0.44, "on_h": 0.52, "on_p": 0.36, "on_d": 0.44}[state] * C
            o = (C - dot) / 2.0
            _ellipse(im, (o, o, o + dot, o + dot),
                     _rgba(p["accent_fg"] if state != "on_d" else p["bg"]))
        else:
            edge = p["stroke_subtle"] if state == "off_d" else box_stroke
            inner = p["check_fill_hover"] if state in ("off_h", "off_p") else p["check_fill"]
            _ellipse(im, (0, 0, C, C), _rgba(edge))
            _ellipse(im, (sw, sw, C - sw, C - sw), _rgba(inner))
        return _down(im, C + gap, C)
    for st in ("off", "off_h", "off_p", "off_d", "on", "on_h", "on_p", "on_d"):
        _put("rad_" + st, radio(st))

    # ---- notebook tabs (flat, accent underline when selected) ----
    T = u(12)
    TB = u(7)
    TW, TH = 2 * T + M, u(4) + TB + 24
    _put("tab_n", _down(_new(TW, TH), TW, TH))
    im = _new(TW, TH)
    _rr(im, (0, u(2), TW, TH - u(3)), R, _rgba(fg, 0.06))
    _put("tab_h", _down(im, TW, TH))
    im = _new(TW, TH)
    pill = u(3)
    _rr(im, (u(8), TH - pill, TW - u(8), TH), pill / 2.0, _rgba(p["accent"]))
    _put("tab_s", _down(im, TW, TH))

    # ---- cards (Labelframe border / Cardbox frame) ----
    CR = u(6)
    CB = CR + sw + 1
    CW = CH = 2 * CB + MB
    _put("card", _control(CW, CH, CR, _rgba(card), _rgba(p["card_stroke"]), sw), on="bg")
    _put("card_in", _control(CW, CH, CR, _rgba(p["card_alt"]), _rgba(p["card_stroke"]), sw),
         on="card")

    # ---- scrollbars (thin pill thumb, no arrows) ----
    SB = u(12)
    TP = u(6)
    for orient in ("v", "h"):
        for st, thick, col in (("n", u(4), p["scroll_thumb"]),
                               ("h", u(6), p["scroll_thumb_hover"]),
                               ("p", u(6), p["scroll_thumb_hover"])):
            if orient == "v":
                w, h = SB, 2 * TP + MB
                im = _new(w, h)
                x0 = (SB - thick) / 2.0
                _rr(im, (x0, u(1), x0 + thick, h - u(1)), thick / 2.0, _rgba(col))
            else:
                w, h = 2 * TP + MB, SB
                im = _new(w, h)
                y0 = (SB - thick) / 2.0
                _rr(im, (u(1), y0, w - u(1), y0 + thick), thick / 2.0, _rgba(col))
            _put(f"sb{orient}_{st}", _down(im, w, h))
        if orient == "v":
            _put("sbv_trough", _down(_new(SB, MB + 2), SB, MB + 2))
        else:
            _put("sbh_trough", _down(_new(MB + 2, SB), MB + 2, SB))

    # ---- progress bar (slim track + rounded accent bar) ----
    PH = u(4)
    PB = u(3)
    PW = 2 * PB + MB
    im = _new(PW, PH)
    _rr(im, (0, 0, PW, PH), PH / 2.0, _rgba(p["progress_track"]))
    _put("pb_trough", _down(im, PW, PH))
    im = _new(PW, PH)
    _rr(im, (0, 0, PW, PH), PH / 2.0, _rgba(p["accent"]))
    _put("pb_bar", _down(im, PW, PH))
    im = _new(PH, PW)
    _rr(im, (0, 0, PH, PW), PH / 2.0, _rgba(p["progress_track"]))
    _put("pbv_trough", _down(im, PH, PW))
    im = _new(PH, PW)
    _rr(im, (0, 0, PH, PW), PH / 2.0, _rgba(p["accent"]))
    _put("pbv_bar", _down(im, PH, PW))

    # ---- slider (scale) ----
    K = u(18)
    LB = u(3)
    LW = 2 * LB + MB
    im = _new(LW, K)
    line = u(4)
    _rr(im, (0, (K - line) / 2.0, LW, (K + line) / 2.0), line / 2.0,
        _rgba(p["progress_track"]))
    _put("sc_trough", _down(im, LW, K))
    for st, dot in (("n", 0.5), ("h", 0.62), ("p", 0.42), ("d", 0.5)):
        im = _new(K, K)
        _ellipse(im, (0, 0, K, K), _rgba(p["stroke"]))
        _ellipse(im, (sw, sw, K - sw, K - sw), _rgba(ctl))
        d = K * dot
        o = (K - d) / 2.0
        _ellipse(im, (o, o, o + d, o + d),
                 _rgba(p["disabled_fg"] if st == "d" else p["accent"]))
        _put("sc_" + st, _down(im, K, K))

    # ---- tree view (hairline frame, flat header) ----
    HW, HH = u(3) + 2 + M, 2 * u(7) + 24
    for st, fill in (("n", p["heading_bg"]), ("h", p["heading_hover"])):
        im = _new(HW, HH)
        _rr(im, (0, 0, HW, HH), 0, _rgba(fill))
        _rr(im, (0, HH - sw, HW, HH), 0, _rgba(p["stroke"]))
        _rr(im, (HW - sw, u(5), HW, HH - u(5)), 0, _rgba(p["stroke"]))
        _put("th_" + st, _down(im, HW, HH), on="bg")
    return dict(B=B, E=E, T=T, TB=TB, CB=CB, TP=TP, PB=PB, LB=LB, sw=sw, u7=u(7), u3=u(3),
                SB=SB, PH=PH, K=K)


# ---------------------------------------------------------------- elements
def _img(key):
    return _F.images[key]


def _create(style, g, s):
    """Create the theme, its image elements and layouts (once per interpreter)."""
    u = _u(s)
    names = style.theme_names()
    if THEME not in names:
        style.theme_create(THEME, parent="clam")
    style.theme_use(THEME)
    single = {"Fl.Card.border", "Fl.CardIn.border", "Fl.Heading.cell"}

    def vname(name, v):
        return name if not v or name in single else name.replace("Fl.", f"Fl.{v}.", 1)

    def ec(name, kind, key, *states, **kw):
        """One element per surface (see SURFACES); states = (state.., key)."""
        for sfx, _surf in SURFACES:
            if sfx and name in single:
                continue
            if name in single:
                def pick(k):
                    return _img(k)
            else:
                def pick(k, sfx=sfx):
                    return _img(k + sfx)
            st = [tuple(x[:-1]) + (pick(x[-1]),) for x in states]
            style.element_create(vname(name, sfx[1:]), kind, pick(key), *st, **kw)

    def vspec(spec, v):
        return [(vname(n, v), dict(o, children=vspec(o["children"], v))
                 if "children" in o else o) for n, o in spec]

    def lay(name, spec):
        """The layout of `name` and of its Card. / Pill. / Chrome. variants."""
        for v, prefixes in (("", ("",)), ("card", ("Card.", "Pill.")),
                            ("chrome", ("Chrome.",))):
            for pre in prefixes:
                style.layout(pre + name, vspec(spec, v))
    B, E, T, TB, CB, TP = g["B"], g["E"], g["T"], g["TB"], g["CB"], g["TP"]

    ec("Fl.Button.bg", "image", "btn_n",
       ("disabled", "btn_d"), ("pressed", "btn_p"),
       ("active", "btn_h"), ("focus", "btn_f"),
       border=B, padding=(u(3), u(2)), sticky="nsew", width=2 * B + 2, height=2 * B + 2)
    ec("Fl.Accent.bg", "image", "acc_n",
       ("disabled", "acc_d"), ("pressed", "acc_p"),
       ("active", "acc_h"), ("focus", "acc_f"),
       border=B, padding=(u(3), u(2)), sticky="nsew", width=2 * B + 2, height=2 * B + 2)
    ec("Fl.Subtle.bg", "image", "sub_n",
       ("disabled", "sub_n"), ("pressed", "sub_p"),
       ("selected", "sub_s"), ("active", "sub_h"), ("focus", "sub_f"),
       border=B, padding=(u(3), u(2)), sticky="nsew", width=2 * B + 2, height=2 * B + 2)
    for name, el in (("TButton", "Fl.Button.bg"), ("Accent.TButton", "Fl.Accent.bg"),
                     ("Subtle.TButton", "Fl.Subtle.bg")):
        lay(name, [(el, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
    # (no drop-down indicator: the app's menu buttons show their own ▾)
    lay("TMenubutton", [("Fl.Button.bg", {"sticky": "nsew", "children": [
        ("Menubutton.padding", {"sticky": "we", "children": [
            ("Menubutton.label", {"side": "left", "sticky": ""})]})]})])

    ec("Fl.Entry.field", "image", "fld_n",
       ("disabled", "fld_d"), ("focus", "fld_f"), ("hover", "fld_h"),
       border=E, padding=(u(3), u(2)), sticky="nsew", width=2 * E + 2,
       height=2 * E + 2)
    lay("TEntry", [("Fl.Entry.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [
            ("Entry.textarea", {"sticky": "nsew"})]})]})])

    # TimeEntry: one field box (the frame) holding border-less entries
    ec("Fl.TimeBox.field", "image", "fld_n",
       ("disabled", "fld_d"), ("focus", "fld_f"), ("hover", "fld_h"),
       border=E, padding=(u(3), u(1)), sticky="nsew", width=2 * E + 2,
       height=2 * E + 2)
    lay("TimeBox.TFrame", [("Fl.TimeBox.field", {"sticky": "nsew"})])
    style.layout("Bare.TEntry", [("Entry.padding", {"sticky": "nsew", "children": [
        ("Entry.textarea", {"sticky": "nsew"})]})])

    ec("Fl.Combobox.field", "image", "fld_n",
       ("disabled", "fld_d"),
       ("readonly", "pressed", "cmb_p"),
       ("readonly", "focus", "cmb_f"),
       ("readonly", "hover", "cmb_h"),
       ("readonly", "cmb_n"),
       ("focus", "fld_f"), ("hover", "fld_h"),
       border=E, padding=(u(3), u(2)), sticky="nsew", width=2 * E + 2,
       height=2 * E + 2)
    ec("Fl.Combobox.arrow", "image", "chev_d",
       ("disabled", "chev_dd"), sticky="")
    lay("TCombobox", [("Fl.Combobox.field", {"sticky": "nsew", "children": [
        ("Fl.Combobox.arrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
            ("Combobox.textarea", {"sticky": "nsew"})]})]})])

    ec("Fl.Spinbox.field", "image", "fld_n",
       ("disabled", "fld_d"), ("focus", "fld_f"), ("hover", "fld_h"),
       border=E, padding=(u(3), u(2)), sticky="nsew", width=2 * E + 2,
       height=2 * E + 2)
    ec("Fl.Spinbox.up", "image", "spin_u",
       ("pressed", "spin_uh"), ("active", "spin_uh"), sticky="")
    ec("Fl.Spinbox.down", "image", "spin_d",
       ("pressed", "spin_dh"), ("active", "spin_dh"), sticky="")
    lay("TSpinbox", [("Fl.Spinbox.field", {"sticky": "nsew", "children": [
        ("null", {"side": "right", "sticky": "", "children": [
            ("Fl.Spinbox.up", {"side": "top", "sticky": "e"}),
            ("Fl.Spinbox.down", {"side": "bottom", "sticky": "e"})]}),
        ("Spinbox.padding", {"sticky": "nsew", "children": [
            ("Spinbox.textarea", {"sticky": "nsew"})]})]})])

    ec("Fl.Check.ind", "image", "chk_off",
       ("disabled", "selected", "chk_on_d"),
       ("disabled", "alternate", "chk_mixed_d"),
       ("disabled", "chk_off_d"),
       ("pressed", "selected", "chk_on_p"),
       ("active", "selected", "chk_on_h"),
       ("selected", "chk_on"),
       ("alternate", "chk_mixed"),
       ("pressed", "chk_off_p"),
       ("active", "chk_off_h"), sticky="")
    lay("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
        ("Fl.Check.ind", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nsew"})]})]})])
    ec("Fl.Radio.ind", "image", "rad_off",
       ("disabled", "selected", "rad_on_d"),
       ("disabled", "rad_off_d"),
       ("pressed", "selected", "rad_on_p"),
       ("active", "selected", "rad_on_h"),
       ("selected", "rad_on"),
       ("pressed", "rad_off_p"),
       ("active", "rad_off_h"), sticky="")
    lay("TRadiobutton", [("Radiobutton.padding", {"sticky": "nsew", "children": [
        ("Fl.Radio.ind", {"side": "left", "sticky": ""}),
        ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Radiobutton.label", {"sticky": "nsew"})]})]})])

    ec("Fl.Tab", "image", "tab_n",
       ("selected", "tab_s"), ("active", "tab_h"),
       border=(T, u(4), T, TB), padding=(u(2), u(2), u(2), u(4)), sticky="nsew",
       width=2 * T + 2, height=u(4) + TB + 2)
    lay("TNotebook.Tab", [("Fl.Tab", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nsew", "children": [
            ("Notebook.label", {"side": "top", "sticky": ""})]})]})])

    ec("Fl.Card.border", "image", "card", border=CB, padding=0, sticky="nsew",
       width=2 * CB + 2, height=2 * CB + 2)
    ec("Fl.CardIn.border", "image", "card_in", border=CB, padding=0,
       sticky="nsew", width=2 * CB + 2, height=2 * CB + 2)
    style.layout("TLabelframe", [("Fl.Card.border", {"sticky": "nsew"})])
    style.layout("Cardbox.TFrame", [("Fl.Card.border", {"sticky": "nsew"})])
    style.layout("Pill.TFrame", [("Fl.Card.border", {"sticky": "nsew"})])
    style.layout("Card.Cardbox.TFrame", [("Fl.CardIn.border", {"sticky": "nsew"})])
    style.layout("Card.TLabelframe", [("Fl.CardIn.border", {"sticky": "nsew"})])

    ec("Fl.Vsb.trough", "image", "sbv_trough", border=(0, 1, 0, 1), sticky="ns",
       width=g["SB"], height=4)
    ec("Fl.Vsb.thumb", "image", "sbv_n",
       ("pressed", "sbv_p"), ("active", "sbv_h"),
       border=(0, TP, 0, TP), sticky="nsew", width=g["SB"], height=2 * TP + 2)
    ec("Fl.Hsb.trough", "image", "sbh_trough", border=(1, 0, 1, 0), sticky="ew",
       width=4, height=g["SB"])
    ec("Fl.Hsb.thumb", "image", "sbh_n",
       ("pressed", "sbh_p"), ("active", "sbh_h"),
       border=(TP, 0, TP, 0), sticky="nsew", width=2 * TP + 2, height=g["SB"])
    lay("Vertical.TScrollbar", [("Fl.Vsb.trough", {"sticky": "ns", "children": [
        ("Fl.Vsb.thumb", {"expand": "1", "sticky": "nsew"})]})])
    lay("Horizontal.TScrollbar", [("Fl.Hsb.trough", {"sticky": "ew", "children": [
        ("Fl.Hsb.thumb", {"expand": "1", "sticky": "nsew"})]})])

    pw = 2 * g["PB"]
    ph = g["PH"]
    ec("Fl.Pb.trough", "image", "pb_trough", border=(pw // 2, 0, pw // 2, 0),
       sticky="ew", width=pw + 2, height=ph)
    ec("Fl.Pb.pbar", "image", "pb_bar", border=(pw // 2, 0, pw // 2, 0), sticky="nsew",
       width=pw + 2, height=ph)
    lay("Horizontal.TProgressbar", [("Fl.Pb.trough", {"sticky": "ew", "children": [
        ("Fl.Pb.pbar", {"side": "left", "sticky": "ns"})]})])
    ec("Fl.Pbv.trough", "image", "pbv_trough", border=(0, pw // 2, 0, pw // 2),
       sticky="ns", width=ph, height=pw + 2)
    ec("Fl.Pbv.pbar", "image", "pbv_bar", border=(0, pw // 2, 0, pw // 2),
       sticky="nsew", width=ph, height=pw + 2)
    lay("Vertical.TProgressbar", [("Fl.Pbv.trough", {"sticky": "ns", "children": [
        ("Fl.Pbv.pbar", {"side": "bottom", "sticky": "we"})]})])

    lw = 2 * g["LB"]
    ec("Fl.Scale.trough", "image", "sc_trough", border=(lw // 2, 0, lw // 2, 0),
       sticky="ew", width=lw + 2, height=g["K"])
    ec("Fl.Scale.slider", "image", "sc_n",
       ("disabled", "sc_d"), ("pressed", "sc_p"), ("active", "sc_h"),
       sticky="")
    lay("Horizontal.TScale", [("Fl.Scale.trough", {"sticky": "ew", "children": [
        ("Fl.Scale.slider", {"side": "left", "sticky": ""})]})])

    ec("Fl.Heading.cell", "image", "th_n", ("active", "th_h"),
       border=(2, g["u7"], g["u3"] + 2, g["u7"]), sticky="nsew",
       width=g["u3"] + 6, height=2 * g["u7"] + 2)
    style.layout("Treeview.Heading", [("Fl.Heading.cell", {"sticky": "nsew", "children": [
        ("Treeheading.padding", {"sticky": "nsew", "children": [
            ("Treeheading.image", {"side": "right", "sticky": ""}),
            ("Treeheading.text", {"sticky": "we"})]})]})])


def apply(style, p, s):
    """Make 'mpfluent' the ttk theme and configure it for palette p.
    Called by themes._style AFTER the flat clam options were set (those stay
    as the fallback colours of every non-image element)."""
    s = max(1.0, float(s or 1.0))
    g = _paint_all(p, s)
    interp = str(style.tk)
    if _F.created_for != interp:
        _create(style, g, s)
        _F.created_for = interp
        _F.scale = s
    style.theme_use(THEME)
    _configure(style, p, s)


def _configure(style, p, s):
    u = _u(s)
    bg, fg, field = p["bg"], p["fg"], p["field"]
    dis = p["disabled_fg"]
    sb, sf = p["select_bg"], p["select_fg"]
    accent = p["accent"]
    style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                    bordercolor=p["stroke"], lightcolor=bg, darkcolor=bg, troughcolor=bg,
                    selectbackground=sb, selectforeground=sf, insertcolor=fg,
                    focuscolor=accent, arrowcolor=fg, font="TkDefaultFont")
    style.map(".", foreground=[("disabled", dis)], background=[])
    bpad = (u(9), u(4), u(9), u(4))
    for name in ("TButton", "Subtle.TButton", "TMenubutton"):
        style.configure(name, background=bg, foreground=fg, padding=bpad, anchor="center",
                        focuscolor=accent, relief="flat", borderwidth=0)
        style.map(name, foreground=[("disabled", dis)], background=[])
    style.configure("Subtle.TButton", padding=(u(5), u(4), u(5), u(4)))
    style.configure("Accent.TButton", background=bg, foreground=p["accent_fg"],
                    padding=bpad, anchor="center", font="MPSemibold")
    style.map("Accent.TButton", foreground=[("disabled", dis)], background=[])
    epad = (u(5), u(4), u(5), u(4))
    style.configure("TEntry", background=bg, fieldbackground=field, foreground=fg,
                    insertcolor=fg, padding=epad, selectbackground=sb, selectforeground=sf,
                    insertwidth=max(1, u(1)))
    style.map("TEntry", foreground=[("disabled", dis)], fieldbackground=[], background=[],
              bordercolor=[], lightcolor=[])
    for name in ("TCombobox", "TSpinbox"):
        style.configure(name, background=bg, fieldbackground=field, foreground=fg,
                        insertcolor=fg, padding=epad, arrowsize=u(10),
                        selectbackground=sb, selectforeground=sf)
        style.map(name, foreground=[("disabled", dis), ("readonly", fg)],
                  fieldbackground=[("readonly", p["control"])], background=[],
                  bordercolor=[], lightcolor=[], arrowcolor=[],
                  selectbackground=[("readonly", p["control"])],
                  selectforeground=[("readonly", fg)])
    style.configure("TSpinbox", padding=(u(5), u(4), u(1), u(4)))
    style.configure("Bare.TEntry", background=field, fieldbackground=field,
                    padding=(u(2), u(2), u(2), u(2)))
    style.configure("Bare.TLabel", background=field, foreground=p["muted"], padding=0)
    style.configure("TimeBox.TFrame", background=bg, padding=(u(3), u(1)))
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=bg, foreground=fg, padding=(0, u(2), u(2), u(2)),
                        focuscolor=bg, focusthickness=0)
        style.map(name, background=[], foreground=[("disabled", dis)],
                  indicatorbackground=[], indicatorforeground=[])
    style.configure("TNotebook", background=bg, tabmargins=(0, 0, 0, u(4)), padding=0,
                    borderwidth=0, bordercolor=bg, lightcolor=bg, darkcolor=bg)
    style.configure("TNotebook.Tab", background=bg, foreground=p["muted"],
                    padding=(u(12), u(4), u(12), u(6)), focuscolor=bg, borderwidth=0)
    style.map("TNotebook.Tab", foreground=[("selected", fg), ("active", fg),
                                           ("disabled", dis)],
              background=[], lightcolor=[], expand=[("selected", (0, 0, 0, 0))])
    style.configure("TLabelframe", background=bg, labeloutside=True,
                    labelmargins=(u(2), 0, 0, u(6)), padding=(u(12), u(8), u(12), u(10)),
                    borderwidth=0)
    style.configure("TLabelframe.Label", background=bg, foreground=fg, font="MPSemibold")
    style.configure("Cardbox.TFrame", background=bg, padding=(u(12), u(8), u(12), u(10)))
    style.configure("Treeview", background=field, fieldbackground=field, foreground=fg,
                    rowheight=u(28), padding=0, borderwidth=1, bordercolor=p["stroke"],
                    lightcolor=field, darkcolor=field)
    style.map("Treeview", background=[("selected", sb)], foreground=[("selected", sf)])
    style.configure("Treeview.Heading", background=p["heading_bg"], foreground=p["muted"],
                    padding=(u(8), u(5), u(8), u(5)), font="MPCaption", relief="flat",
                    borderwidth=0)
    style.map("Treeview.Heading", background=[], foreground=[("active", fg)],
              relief=[])
    style.configure("Vertical.TScrollbar", background=bg, troughcolor=bg, borderwidth=0)
    style.configure("Horizontal.TScrollbar", background=bg, troughcolor=bg, borderwidth=0)
    style.map("Vertical.TScrollbar", background=[])
    style.map("Horizontal.TScrollbar", background=[])
    for name in ("Horizontal.TProgressbar", "Vertical.TProgressbar", "TProgressbar"):
        style.configure(name, background=bg, troughcolor=bg, borderwidth=0, thickness=u(4))
    style.configure("Horizontal.TScale", background=bg, borderwidth=0)
    style.map("Horizontal.TScale", background=[])
    style.configure("TSeparator", background=p["stroke"])

