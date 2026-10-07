"""Helpers shared by the Cut / Edit sub-tabs (Auto-detect, Manual, Multi cut)."""
import os
from tkinter import ttk

from ..config import CODECS
from ..i18n import N_, tr
from ..ui.widgets import KeyedCombobox, add_tooltip, info_icon
from .common import _KIND_NAMES

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr


# the four sections, in cut order: (key, English label - shown with tr_key)
_SEGS = (("preintro", N_("Pre-intro")), ("intro", N_("Intro")),
         ("credits", N_("Credits")), ("aftercredits", N_("After-credits")))

# where a detection came from (engine 'src' / scan source) -> label
_SRC_NAMES = {"audio": N_("audio"), "visual": N_("visual"),
              "audio+visual": N_("audio + visual")}
# detection source option: key -> combobox label
_MODE_LABELS = {"both": N_("Audio + Visual"), "audio": N_("Audio"), "visual": N_("Visual")}


_MODE_KEYS = ("both", "audio", "visual")


def mode_combobox(parent, var, width=16):
    """'Audio + Visual' / 'Audio' / 'Visual' picker whose variable holds the
    key ('both' / 'audio' / 'visual')."""
    return KeyedCombobox(parent, textvariable=var, values=list(_MODE_KEYS),
                         labels=[tr_key(_MODE_LABELS[k]) for k in _MODE_KEYS],
                         state="readonly", width=width)


# safety margin (engine cfg 'margin_frames', 0-5) and what a template match's
# edges follow (engine cfg 'template_margin')
MARGIN_MAX = 5
MARGIN_DEFAULT = 1
_TPL_MARGIN_LABELS = {"follow": N_("Follow the template's own edges"),
                      "exact": N_("Exact content edges"),
                      "exact+margin": N_("Exact + safety margin")}
_TPL_MARGIN_KEYS = ("follow", "exact", "exact+margin")


def norm_margin(v, default=MARGIN_DEFAULT):
    """Saved / typed safety margin -> int 0..MARGIN_MAX."""
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        v = default
    return max(0, min(MARGIN_MAX, v))


def norm_tpl_margin(v):
    return v if v in _TPL_MARGIN_KEYS else "follow"


def margin_widgets(parent, margin_var, tpl_var=None):
    """'Safety margin: [1] frames ⓘ' (+ 'With templates: [...]' when tpl_var
    is given) packed into a new frame; returns that frame."""
    fr = ttk.Frame(parent)
    r1 = ttk.Frame(fr)
    r1.pack(anchor="w")
    ttk.Label(r1, text=tr("Safety margin:")).pack(side="left")
    sp = ttk.Spinbox(r1, from_=0, to=MARGIN_MAX, increment=1, width=3,
                     textvariable=margin_var, state="readonly")
    sp.pack(side="left", padx=(6, 4))
    ttk.Label(r1, text=tr("frames")).pack(side="left")
    tip = tr("Extra frames cut before and after a detected intro/credits so no single "
             "intro frame flashes at the join")
    add_tooltip(sp, tip)
    info_icon(r1, tip).pack(side="left", padx=(6, 0))
    if tpl_var is not None:
        r2 = ttk.Frame(fr)
        r2.pack(anchor="w", pady=(4, 0))
        ttk.Label(r2, text=tr("With templates:")).pack(side="left")
        cb = KeyedCombobox(r2, textvariable=tpl_var, values=list(_TPL_MARGIN_KEYS),
                           labels=[tr_key(_TPL_MARGIN_LABELS[k]) for k in _TPL_MARGIN_KEYS],
                           state="readonly", width=28)
        cb.pack(side="left", padx=(6, 0))
        add_tooltip(cb, tr(
            "Where a template match starts and ends. Follow the template's own edges = "
            "exactly where the template's first / last frames land in the episode (a "
            "template cut with a frame or two of margin keeps it). Exact content edges = "
            "the intro / credits content itself. Exact + safety margin = the content "
            "edges plus the safety margin above."))
    return fr


def _src_label(src, ui=True):
    """'audio' / 'visual' / 'audio+visual' -> shown text (translated when ui)."""
    if not src:
        return ""
    name = _SRC_NAMES.get(src, src)
    return tr_key(name) if ui else src


# empty-box rules shared by Manual and Multi cut (display, markers and the cut
# itself all go through _resolve_rng so they always agree):
_ZERO_FROM = ("preintro", "intro")       # empty From = start of file
_TO_END = ("credits", "aftercredits")    # empty To = end of file


def _resolve_rng(key, fr, to, dur=None):
    """Apply the empty-box rules to one section. Returns (from, to, err):
    (None, None, None) = nothing set; err = why the section is unusable.
    An empty To on credits/after-credits becomes `dur` (None = end of file).
    `err` is English (for the log); show it with tr_key(err)."""
    if fr is None and to is None:
        return None, None, None
    if fr is None:
        if key not in _ZERO_FROM:
            return None, None, N_("needs a From time")
        fr = 0.0
    if to is None:
        if key not in _TO_END:
            return None, None, N_("needs a To time")
        to = dur
    if to is not None and to <= fr:
        return None, None, N_("To is not after From")
    return fr, to, None


def _is_plex(codec_label):
    """True for the 'Plex-ready HEVC / H.264 (near-lossless, never bigger)'
    codec choices (label or encoder value mentions Plex)."""
    lbl = str(codec_label or "")
    return "plex" in lbl.lower() or "plex" in str(CODECS.get(lbl, "")).lower()


def _mmss(t):
    """Compact time for candidate lists: 01:32.4 (minutes may exceed 59)."""
    t = max(0.0, float(t or 0.0))
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:04.1f}"


def _best_ok(cands):
    """The best candidate with ok=True (the engine lists best first)."""
    for c in cands or ():
        if c.get("ok"):
            return c
    return None


def _cand_label(kind, i, c, ui=False):
    """'Intro #2 'Show_OP2_intro.mkv' 0.71 01:32.4–03:02.4 (weak)'.
    ui=True: translated (menus); else English (log)."""
    tpl = os.path.basename(str(c.get("template") or "?"))
    try:
        score = f"{float(c.get('score') or 0.0):.2f}"
    except (TypeError, ValueError):
        score = "?"
    name = _KIND_NAMES.get(kind, kind)
    txt = (f"{tr_key(name) if ui else name} #{i} '{tpl}' {score} "
           f"{_mmss(c.get('start'))}–{_mmss(c.get('end'))}")
    if c.get("src") and (c["src"] != "audio" or c.get("conflict")):
        txt += " " + _src_label(c["src"], ui)
    if c.get("conflict"):
        txt += " ⚠"
    if c.get("ok"):
        return txt
    return tr("{label} (weak)", label=txt) if ui else txt + " (weak)"


def _detect_notes(res, use, have=None, ui=False):
    """Problems in one detect_segments result, honouring the use_* ticks:
    ['intro weak 0.41', 'credits not found', ...]. `have` = the kinds that
    have templates at all (None = all); the others can't be 'not found'.
    ui=True: translated (the Multi cut Notes column); else English (log)."""
    if not res:
        return [tr("detect failed") if ui else "detect failed"]
    if res.get("error"):
        return [tr("detect failed: {error}", error=res["error"]) if ui
                else f"detect failed: {res['error']}"]
    notes = []
    for key, label in _SEGS:
        if not use.get(key, True) or (have is not None and key not in have):
            continue
        cands = (res or {}).get(key) or []
        seg = tr_key(label) if ui else key
        best = _best_ok(cands)
        if best and best.get("conflict"):
            notes.append(tr("{seg}: audio and pictures disagree", seg=seg) if ui
                         else f"{key}: audio and pictures disagree")
        if best:
            continue
        if cands:
            try:
                score = float(cands[0].get('score') or 0)
                notes.append(tr("{seg} weak {score:.2f}", seg=seg, score=score) if ui
                             else f"{key} weak {score:.2f}")
            except (TypeError, ValueError):
                notes.append(tr("{seg} weak", seg=seg) if ui else f"{key} weak")
        else:
            notes.append(tr("{seg} not found", seg=seg) if ui else f"{key} not found")
    return notes


def _ask_choice(parent, title, message, choices):
    """Small modal dialog with one button per choice; returns the chosen
    key, or None if closed / Escape. choices = [("replace", tr("Replace")),
    ...] (key, button text) pairs; Enter = the first (default) choice.
    See ui.dialogs.ask_choice (themed, centred on the main window, focused,
    grabs the input once it is on screen)."""
    from ..ui.dialogs import ask_choice
    return ask_choice(parent, title, message, choices)
