"""Helpers shared by the Cut / Edit sub-tabs (Auto-detect, Manual, Multi cut)."""
import os

from ..config import CODECS
from ..i18n import N_, tr
from .common import _KIND_NAMES

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr


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
    if c.get("ok"):
        return txt
    return tr("{label} (weak)", label=txt) if ui else txt + " (weak)"
