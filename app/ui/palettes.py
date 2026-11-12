"""The theme palettes (data): one colour palette per theme - its base
colours (_BASE_KEYS) extended with the derived design tokens in _complete()
(card = surface_alt, stroke, control / hover / pressed fills, accent hover,
focus ring, sidebar, info bars ...) - and the theme names the pickers list.
Applying a theme is ui/themes.py."""
from ..i18n import N_, tr
from .colors import _readable, contrast, luminance, mix

FOLLOW_WINDOWS = "Follow Windows"
DEFAULT_THEME = "Dark"

# keys every palette defines; the rest is derived in _complete()
_BASE_KEYS = ("bg", "panel", "field", "fg", "muted", "accent", "accent_fg",
              "select_bg", "select_fg", "border", "ok", "warn", "error")

_MARKERS_LIGHT = dict(marker_preintro="#d9771f", marker_intro="#2f6fd6",
                      marker_credits="#2f9a4c", marker_aftercredits="#8e4fc8")
_MARKERS_DARK = dict(marker_preintro="#f0a050", marker_intro="#5b9bff",
                     marker_credits="#58d07a", marker_aftercredits="#b88af0")

PALETTES = {
    "Light": dict(
        bg="#f3f3f3", panel="#e9e9e9", field="#ffffff", fg="#1b1b1b", muted="#5d5d5d",
        accent="#005fb8", accent_fg="#ffffff", select_bg="#cce4f7", select_fg="#1b1b1b",
        border="#c4c4c4", ok="#0f7b0f", warn="#8a4b00", error="#c42b1c",
        card="#fbfbfb", sidebar_bg="#ebebeb", playhead="#d81b1b", **_MARKERS_LIGHT),
    "Dark": dict(
        bg="#202020", panel="#2d2d2d", field="#1b1b1b", fg="#ffffff", muted="#c5c5c5",
        accent="#60cdff", accent_fg="#000000", select_bg="#1e4e78", select_fg="#ffffff",
        border="#4a4a4a", ok="#6ccb5f", warn="#fce100", error="#ff99a4",
        card="#2b2b2b", sidebar_bg="#1a1a1a", dark=True, **_MARKERS_DARK),
    "High Contrast": dict(
        bg="#000000", panel="#1a1a1a", field="#000000", fg="#ffffff", muted="#00ffff",
        accent="#ffff00", accent_fg="#000000", select_bg="#ffff00", select_fg="#000000",
        border="#ffffff", ok="#00ff00", warn="#ffb000", error="#ff5c5c",
        hover="#333333", pressed="#4d4d4d", dark=True, hc=True, playhead="#ff00ff",
        card="#000000", sidebar_bg="#000000", stroke="#ffffff", stroke_subtle="#8f8f8f",
        card_stroke="#ffffff", control="#000000", control_hover="#333333",
        control_pressed="#4d4d4d", control_disabled="#000000", control_bottom="#ffffff",
        field_bottom="#ffffff", check_stroke="#ffffff", check_fill="#000000",
        check_fill_hover="#333333", scroll_thumb="#ffffff", scroll_thumb_hover="#ffff00",
        progress_track="#8f8f8f", heading_bg="#000000", heading_hover="#333333",
        focus="#ffff00", card_alt="#000000", tree_alt="#000000",
        sidebar_hover="#333333", sidebar_sel="#1a1a1a",
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
        border="#4c566a", ok="#a3be8c", warn="#ebcb8b", error="#e79097",
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
    # ---- popular editor themes: their official palettes; a colour that
    # missed a contrast rule is nudged just enough (the comment names the
    # official one)
    "Catppuccin Mocha": dict(
        bg="#1e1e2e", panel="#313244", field="#181825", fg="#cdd6f4", muted="#a6adc8",
        accent="#cba6f7", accent_fg="#11111b", select_bg="#45475a", select_fg="#cdd6f4",
        border="#45475a", ok="#a6e3a1", warn="#f9e2af", error="#f38ba8",
        sidebar_bg="#181825", dark=True, playhead="#f38ba8", marker_preintro="#fab387",
        marker_intro="#89b4fa", marker_credits="#a6e3a1", marker_aftercredits="#cba6f7"),
    "Catppuccin Latte": dict(
        bg="#eff1f5", panel="#e6e9ef", field="#ffffff", fg="#4c4f69", muted="#5c5f77",
        accent="#8839ef", accent_fg="#ffffff", select_bg="#ccd0da", select_fg="#4c4f69",
        border="#bcc0cc", ok="#317921", warn="#945f13",     # official #40a02b, #df8e1d
        error="#d20f39", sidebar_bg="#e6e9ef", playhead="#d20f39",
        marker_preintro="#fe640b", marker_intro="#1e66f5", marker_credits="#40a02b",
        marker_aftercredits="#8839ef"),
    "Gruvbox Dark": dict(
        bg="#282828", panel="#3c3836", field="#1d2021", fg="#ebdbb2", muted="#bdae93",
        accent="#fe8019", accent_fg="#282828", select_bg="#504945", select_fg="#ebdbb2",
        border="#665c54", ok="#b8bb26", warn="#fabd2f",
        error="#fb6a59",                                    # official #fb4934
        sidebar_bg="#1d2021", dark=True, playhead="#fb4934", marker_preintro="#fe8019",
        marker_intro="#83a598", marker_credits="#b8bb26", marker_aftercredits="#d3869b"),
    "Gruvbox Light": dict(
        bg="#fbf1c7", panel="#ebdbb2", field="#f9f5d7", fg="#3c3836", muted="#665c54",
        accent="#af3a03", accent_fg="#fbf1c7", select_bg="#d5c4a1", select_fg="#3c3836",
        border="#bdae93", ok="#746f0d", warn="#94600f",     # official #79740e, #b57614
        error="#9d0006", sidebar_bg="#f2e5bc", playhead="#9d0006",
        marker_preintro="#af3a03", marker_intro="#076678", marker_credits="#79740e",
        marker_aftercredits="#8f3f71"),
    "Tokyo Night": dict(
        bg="#1a1b26", panel="#24283b", field="#16161e", fg="#c0caf5", muted="#a9b1d6",
        accent="#7aa2f7", accent_fg="#1a1b26", select_bg="#33467c", select_fg="#c0caf5",
        border="#3b4261", ok="#9ece6a", warn="#e0af68", error="#f7768e",
        sidebar_bg="#16161e", dark=True, playhead="#f7768e", marker_preintro="#ff9e64",
        marker_intro="#7aa2f7", marker_credits="#9ece6a", marker_aftercredits="#bb9af7"),
    "One Dark": dict(
        bg="#282c34", panel="#2c313a", field="#21252b", fg="#abb2bf", muted="#9da5b4",
        accent="#61afef", accent_fg="#282c34", select_bg="#3e4451", select_fg="#d7dae0",
        border="#3e4451", ok="#98c379", warn="#e5c07b",
        error="#e37d85",                                    # official #e06c75
        sidebar_bg="#21252b", dark=True, playhead="#e06c75", marker_preintro="#d19a66",
        marker_intro="#61afef", marker_credits="#98c379", marker_aftercredits="#c678dd"),
    "Monokai": dict(
        bg="#272822", panel="#3e3d32", field="#1e1f1c", fg="#f8f8f2", muted="#a59f85",
        accent="#a6e22e", accent_fg="#272822", select_bg="#49483e", select_fg="#f8f8f2",
        border="#49483e", ok="#a6e22e", warn="#e6db74",
        error="#ff6188",                                    # Monokai Pro's red (#f92672)
        sidebar_bg="#1e1f1c", dark=True, playhead="#f92672", marker_preintro="#fd971f",
        marker_intro="#66d9ef", marker_credits="#a6e22e", marker_aftercredits="#ae81ff"),
    "Rosé Pine": dict(
        bg="#191724", panel="#26233a", field="#1f1d2e", fg="#e0def4", muted="#908caa",
        accent="#ebbcba", accent_fg="#191724", select_bg="#403d52", select_fg="#e0def4",
        border="#524f67", ok="#9ccfd8", warn="#f6c177", error="#eb6f92",
        dark=True, playhead="#eb6f92", marker_preintro="#f6c177",
        marker_intro="#9ccfd8", marker_credits="#31748f", marker_aftercredits="#c4a7e7"),
    "Everforest": dict(
        bg="#2d353b", panel="#343f44", field="#232a2e", fg="#d3c6aa", muted="#9da9a0",
        accent="#a7c080", accent_fg="#2d353b", select_bg="#475258", select_fg="#d3c6aa",
        border="#4f585e", ok="#a7c080", warn="#dbbc7f",
        error="#e98d8f",                                    # official #e67e80
        sidebar_bg="#232a2e", dark=True, playhead="#e67e80", marker_preintro="#e69875",
        marker_intro="#7fbbb3", marker_credits="#a7c080", marker_aftercredits="#d699b6"),
    "Kanagawa": dict(
        bg="#1f1f28", panel="#2a2a37", field="#16161d", fg="#dcd7ba", muted="#938aa9",
        accent="#7e9cd8", accent_fg="#1f1f28", select_bg="#2d4f67", select_fg="#dcd7ba",
        border="#54546d", ok="#98bb6c", warn="#e6c384", error="#e46876",
        sidebar_bg="#16161d", dark=True, playhead="#ff5d62", marker_preintro="#ffa066",
        marker_intro="#7e9cd8", marker_credits="#98bb6c", marker_aftercredits="#957fb8"),
    "GitHub Dark": dict(
        bg="#0d1117", panel="#161b22", field="#010409", fg="#e6edf3", muted="#9198a1",
        accent="#1f6feb", accent_fg="#ffffff", select_bg="#264f78", select_fg="#ffffff",
        border="#30363d", ok="#3fb950", warn="#d29922", error="#f85149",
        sidebar_bg="#010409", dark=True, playhead="#f85149", marker_preintro="#db6d28",
        marker_intro="#4493f8", marker_credits="#3fb950", marker_aftercredits="#a371f7"),
    "Synthwave '84": dict(
        bg="#262335", panel="#34294f", field="#241b2f", fg="#ffffff", muted="#848bbd",
        accent="#ff7edb", accent_fg="#262335", select_bg="#463465", select_fg="#ffffff",
        border="#495495", ok="#72f1b8", warn="#fede5d",
        error="#fe6670",                                    # official #fe4450
        sidebar_bg="#1e1a2c", dark=True, playhead="#fe4450", marker_preintro="#ff8b39",
        marker_intro="#36f9f6", marker_credits="#72f1b8", marker_aftercredits="#b893ce"),
}

THEME_NAMES = [FOLLOW_WINDOWS] + list(PALETTES)

# the theme names as the pickers SHOW them (translated); the settings and
# theme_var() keep the English key. Listed here for the string extractor.
_THEME_LABELS = (N_("Follow Windows"), N_("Light"), N_("Dark"), N_("High Contrast"),
                 N_("Plex"), N_("Gold on Dark"), N_("Midnight Blue"), N_("Nord"),
                 N_("Dracula"), N_("Solarized Light"), N_("Solarized Dark"),
                 N_("Forest"), N_("Sepia"))
# published themes: proper names, shown as they are in every language
_PROPER_NAMES = frozenset(("Catppuccin Mocha", "Catppuccin Latte", "Gruvbox Dark",
                           "Gruvbox Light", "Tokyo Night", "One Dark", "Monokai",
                           "Rosé Pine", "Everforest", "Kanagawa", "GitHub Dark",
                           "Synthwave '84"))
# the pickers list these first ("Basic"), then every other theme by name
BASIC_THEMES = (FOLLOW_WINDOWS, "Light", "Dark", "High Contrast")
_GROUP_LABELS = (N_("Basic"), N_("More themes"))


def label(name):
    """A theme name as shown in the pickers (translated, except the proper
    names of published themes)."""
    return name if name in _PROPER_NAMES else tr(name)


def picker_groups():
    """[(group title (translated), [theme keys])]: the basic themes first,
    then all the others sorted by their shown name."""
    rest = sorted((n for n in THEME_NAMES if n not in BASIC_THEMES),
                  key=lambda n: label(n).casefold())
    return [(tr(_GROUP_LABELS[0]), list(BASIC_THEMES)), (tr(_GROUP_LABELS[1]), rest)]


def _complete(p):
    """Fill the derived keys (design tokens) of a base palette."""
    p = dict(p)
    dark = p.setdefault("dark", luminance(p["bg"]) < 0.2)
    p.setdefault("hc", False)
    bg, fg, field = p["bg"], p["fg"], p["field"]
    ink = "#000000" if not dark else "#ffffff"      # 'more contrast' direction
    p.setdefault("hover", mix(p["panel"], fg, 0.14))
    p.setdefault("pressed", mix(p["panel"], fg, 0.26))
    p.setdefault("disabled_fg", mix(fg, bg, 0.5))
    # ---- surfaces: content (bg) / cards (surface_alt) / sidebar + status bar
    p.setdefault("card", mix(bg, fg, 0.05) if dark else mix(bg, "#ffffff", 0.7))
    card = p["card"]
    p.setdefault("surface", bg)
    p.setdefault("surface_alt", card)
    p.setdefault("sidebar_bg", mix(bg, "#000000", 0.16) if dark else mix(bg, "#000000", 0.035))
    p.setdefault("card_alt", mix(card, fg, 0.03))           # a card inside a card
    # ---- strokes
    p.setdefault("stroke", mix(card, fg, 0.13 if dark else 0.12))
    p.setdefault("stroke_subtle", mix(card, fg, 0.07))
    p.setdefault("card_stroke", p["stroke"])
    # ---- control fills (buttons, read-only drop-downs)
    p.setdefault("control", mix(card, fg, 0.06) if dark else mix(card, "#ffffff", 0.8))
    ctl = p["control"]
    p.setdefault("control_hover", mix(ctl, fg, 0.05) if dark else mix(ctl, "#000000", 0.025))
    p.setdefault("control_pressed", mix(ctl, bg, 0.45) if dark else mix(ctl, "#000000", 0.045))
    p.setdefault("control_disabled", mix(card, fg, 0.025) if dark else mix(card, "#000000", 0.02))
    p.setdefault("control_bottom", p["stroke"] if dark else mix(p["stroke"], fg, 0.2))
    p.setdefault("field_hover", mix(field, fg, 0.03))
    p.setdefault("field_bottom", mix(field, fg, 0.5))
    # input boxes: a 1 px border with >= 3:1 against the page and card
    # surfaces (WCAG 1.4.11 non-text contrast), still lighter than the text
    p.setdefault("field_border", _readable(mix(field, fg, 0.3), (bg, card), 3.0, toward=fg))
    # ---- accent states, focus ring
    # hover / pressed accent fills: toward the page colour, unless that would
    # take the on-accent text below 4.5:1 - then toward more contrast
    away = "#000000" if luminance(p["accent_fg"]) > 0.5 else "#ffffff"
    for k, t in (("accent_hover", 0.12), ("accent_pressed", 0.24)):
        c = mix(p["accent"], bg, t)
        if contrast(p["accent_fg"], c) < 4.5:
            c = mix(p["accent"], away, t * 0.8)
        p.setdefault(k, c)
    p.setdefault("accent_edge", mix(p["accent"], "#000000" if not dark else "#ffffff", 0.1))
    p.setdefault("focus", p["accent"])
    p.setdefault("radius", 4)
    # ---- check boxes, scrollbars, progress, list headers, zebra rows
    p.setdefault("check_stroke", _readable(mix(card, fg, 0.5), (bg, card), 3.0, toward=fg))
    p.setdefault("check_fill", mix(card, "#000000", 0.12) if dark else mix(card, "#000000", 0.02))
    p.setdefault("check_fill_hover", mix(card, fg, 0.08))
    p.setdefault("scroll_thumb", mix(bg, fg, 0.38))
    p.setdefault("scroll_thumb_hover", mix(bg, fg, 0.58))
    p.setdefault("progress_track", mix(card, fg, 0.22))
    p.setdefault("heading_bg", field)
    p.setdefault("heading_hover", mix(field, fg, 0.05))
    p.setdefault("tree_alt", mix(field, fg, 0.03))
    # ---- sidebar navigation
    p.setdefault("sidebar_hover", mix(p["sidebar_bg"], fg, 0.06))
    p.setdefault("sidebar_sel", mix(p["sidebar_bg"], fg, 0.095))
    # captions / secondary text stay WCAG-readable on every surface
    p["muted"] = _readable(p["muted"], (bg, card, p["sidebar_bg"], field), toward=fg)
    p.setdefault("tooltip_bg", "#ffffe0" if not dark else p["panel"])
    p.setdefault("tooltip_fg", "#1a1a1a" if not dark else p["fg"])
    p.setdefault("timeline_bg", p["field"])
    p.setdefault("timeline_track", mix(p["border"], p["fg"], 0.3))
    p.setdefault("playhead", "#ff3b3b" if dark else "#d81b1b")
    p.setdefault("video_bg", "#000000" if dark else "#1b1b1b")
    for k, v in (_MARKERS_DARK if dark else _MARKERS_LIGHT).items():
        p.setdefault(k, v)
    # info bars / banners (missing ffmpeg, missing packages, update available):
    # a tinted card with a coloured accent strip + icon, normal text colour
    p.setdefault("banner_error_accent", p["error"])
    p.setdefault("banner_warn_accent", p["warn"])
    p.setdefault("banner_info_accent", p["accent"])
    for k in ("error", "warn", "info"):
        p.setdefault(f"banner_{k}_bg", mix(card, p[f"banner_{k}_accent"], 0.14 if dark else 0.1)
                     if not p["hc"] else bg)
        p.setdefault(f"banner_{k}_fg", _readable(fg, (p[f"banner_{k}_bg"],), toward=ink))
    return p


PALETTES = {name: _complete(p) for name, p in PALETTES.items()}
