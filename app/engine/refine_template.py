"""Frame-accurate boundary refinement (engine/refine.py): template edges
(follow_template_edge - a template's edge mapped onto an episode) and the
safety margins (norm_margin, frame_step, add_margin, margin_segments).
UI-free."""
from .refine_audio import _xcorr_norm
from .refine_base import (A_MIN_PEAK, A_SILENT, MARGIN_BUDGET, MARGIN_MAX, PAD, SEARCH, SR,
                          _cached, decode_audio, decode_frames, limits)
from .refine_edge import _finish, refine_boundary
from .refine_visual import _frame_time, _snap_index


# ----------------------------------------------------------------- template edges / margins
TEMPLATE_MODES = ("follow", "exact", "exact+margin")


def norm_margin(n, default=1):
    """cfg['margin_frames'] -> int in [0, MARGIN_MAX]."""
    try:
        n = int(n if n is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(0, min(MARGIN_MAX, n))


def norm_template_mode(m):
    """cfg['template_margin'] -> 'follow' (default) / 'exact' / 'exact+margin'."""
    m = str(m or "follow").strip().lower()
    return m if m in TEMPLATE_MODES else "follow"


def _audio_offset(path, t, edge, ref, ref_t, radius, track, ref_track, cache, ref_cache, np):
    """Sample-exact offset (episode time - reference time) of a 3 s chunk of
    the reference just inside its edge ref_t, searched around t. None when
    there is no clear peak."""
    c0, c1 = (ref_t + 0.6, ref_t + 3.6) if edge == "start" else (ref_t - 3.6, ref_t - 0.6)
    if c0 < 0:
        c0, c1 = 0.0, 3.0
    span = 2 * radius + PAD + 4.0
    ea = max(0.0, t - span)
    eb = t + span
    xr = _cached(ref_cache, ("a", ref, ref_track, round(c0, 3), round(c1, 3)),
                 lambda: decode_audio(ref, c0, c1, ref_track))
    xe = _cached(cache, ("a", path, track, round(ea, 3), round(eb, 3)),
                 lambda: decode_audio(path, ea, eb, track))
    if xr is None or xe is None or len(xr) < SR:
        return None
    if float(np.sqrt((xr ** 2).mean())) < A_SILENT:
        return None
    c = _xcorr_norm(xe, xr, np)
    if c.size < 3:
        return None
    want = (c0 + (t - ref_t)) - ea
    lo = max(0, int((want - 2 * radius) * SR))
    hi = min(c.size, int((want + 2 * radius) * SR) + 1)
    if hi - lo < 3:
        return None
    seg = c[lo:hi]
    p = int(np.argmax(seg))
    peak = float(seg[p])
    g = int(0.05 * SR)
    rest = np.concatenate([seg[:max(0, p - g)], seg[p + g:]])
    if peak < A_MIN_PEAK or (rest.size and float(rest.max()) > 0.9 * peak):
        return None
    return (ea + (lo + p) / SR) - c0


def follow_template_edge(path, t, edge, ref, ref_t, radius=SEARCH, track=None, ref_track=None,
                         cache=None, ref_cache=None, video=True, audio=True):
    """Where the template's own edge lands in the episode - the template is
    followed exactly, including the safety-margin frames its maker left
    around the intro / credits (they show source-episode content that
    matches nothing elsewhere; the alignment comes from the content next to
    them). edge 'start': ref_t = 0 (the template's first frame); 'end':
    ref_t = the template's end (-> the frame after its last one). Pictures:
    the template's margin (the frames outside the first hard cut at its
    edge) is set aside, the content-edge walk runs near THAT edge (the two
    edges' offsets can differ by a frame between episodes; a picture this
    episode holds a frame longer still counts) and the margin's frame count
    is added beyond the content edge. Without pictures:
    the template edge mapped by the sound's sample-exact offset, snapped to
    the nearest frame. Returns the refine_boundary result dict or None."""
    import numpy as np
    t, ref_t = float(t), float(ref_t)
    if video:
        r = refine_boundary(path, t, edge, ref, ref_t, ref_exact=True, radius=radius,
                            track=track, ref_track=ref_track, cache=cache, video=True,
                            audio=audio, ref_cache=ref_cache, follow=True)
        if r is not None and (r["src"].startswith("visual") or r["src"] in ("audio+cut", "file")):
            return r
    if not audio:
        return None
    best = None
    for tk in (track if isinstance(track, (list, tuple)) else [track]):
        off = _audio_offset(path, t, edge, ref, ref_t, radius, tk, ref_track, cache,
                            ref_cache, np)
        if off is not None:
            best = off
            break
    if best is None:
        return None
    ta = ref_t + best
    res = {"t": ta, "src": "audio", "fdur": None, "why": "template edge (audio offset)"}
    if video:
        wa, wb = round(max(0.0, ta - 1.0), 3), round(ta + 1.0, 3)
        E = _cached(cache, ("v", path, wa, wb), lambda: decode_frames(path, wa, wb))
        if E is not None:
            res["t"] = _frame_time(E, _snap_index(E, ta, np))
            res["fdur"] = E.fdur
    return _finish(res, t)


def frame_step(path, t, n, cache=None):
    """The real frame time n frames away from the frame at t (n < 0 =
    earlier), stepped on the file's own frame timestamps. Clamped to the
    file start. None if nothing could be decoded."""
    import numpy as np
    t = float(t)
    if n == 0:
        return t
    pad = 0.5 + abs(n) * 0.25
    a, b = round(max(0.0, t - pad), 3), round(t + pad, 3)
    E = _cached(cache, ("m", path, a, b), lambda: decode_frames(path, a, b, w=16, h=9))
    if E is None:
        return None
    i = _snap_index(E, t, np) + int(n)
    if i < 0:
        return 0.0 if E.at_start else float(E.t[0])
    if i >= len(E.t):
        return float(E.t[-1] + E.fdur * (i - len(E.t) + 1))
    return float(E.t[i])


def add_margin(path, rng, n, dur=None, lo=0.0, hi=None, cache=None, edges=(True, True),
               stop_event=None):
    """(s, e) widened by n frames each side (start - n frames, end(excl) + n
    frames) on the file's real frame times, clamped to [lo, hi] (the file /
    the neighbouring segments). An end at the file end stays there. edges:
    which side(s) to widen. MARGIN_BUDGET seconds at most; stop_event set
    (or the budget used up) = the edges not widened yet stay as they are.
    Returns (s, e)."""
    s, e = float(rng[0]), float(rng[1])
    n = int(n or 0)
    if n <= 0:
        return s, e
    if dur:
        hi = dur if hi is None else min(hi, dur)
    lo = max(0.0, lo or 0.0)
    try:
        with limits(MARGIN_BUDGET, stop_event):
            if edges[0] and s > lo + 1e-3:
                ns = frame_step(path, s, -n, cache)
                if ns is not None:
                    s = max(lo, min(s, ns))
            if edges[1] and not (dur and e >= dur - 1e-3) and (hi is None or e < hi - 1e-3):
                ne = frame_step(path, e, n, cache)
                if ne is not None:
                    e = max(e, ne)
                    if hi is not None:
                        e = min(e, hi)
    except Exception:
        pass                                  # never break a detection (Stop / budget too)
    return round(s, 3), round(e, 3)


def margin_segments(per_file, n, durs=None, cache=None, fixed=None, stop_event=None):
    """add_margin for every segment of every file without overlapping a
    neighbouring segment of the same file. per_file: {path: {key: (s, e) |
    None}} (changed in place); durs: {path: duration}; fixed(key) -> True =
    that segment only bounds the others (kept as is). stop_event set = the
    segments not done yet stay as they are. Returns per_file."""
    if int(n or 0) <= 0:
        return per_file
    for p, segs in per_file.items():
        dur = (durs or {}).get(p)
        orig = {k: tuple(v[:2]) for k, v in segs.items() if v}
        for k, (s, e) in orig.items():
            if stop_event is not None and stop_event.is_set():
                return per_file
            if fixed is not None and fixed(k):
                continue
            lo = max([v[1] for kk, v in orig.items() if kk != k and v[1] <= s + 1e-3] or [0.0])
            his = [v[0] for kk, v in orig.items() if kk != k and v[0] >= e - 1e-3]
            hi = min(his) if his else dur
            segs[k] = add_margin(p, (s, e), n, dur, lo, hi, cache, stop_event=stop_event)
    return per_file
