"""Frame-accurate boundary refinement (engine/refine.py): the visual edge -
every frame of a short window in both files, the frame offset between them
and the outward walk while the pictures keep matching. UI-free."""
from .refine_base import (CUT_MIN, FLAT_MEAN_TOL, MARGIN_MAX, MAX_FLAT_EDGE, MIN_ANCHOR, MISS,
                          SIM_OK, TOL, _Frames)


# ----------------------------------------------------------------- visual
def _match_vec(E, i, R, j, np):
    """Does episode frame i look like reference frame j (or one within
    +-TOL - held animation frames)? Black / flat frames only match the
    aligned frame itself (a black frame next to a black frame would let
    the walk run into an episode's own fade / black gap). Returns True /
    False."""
    lo, hi = max(0, j - TOL), min(len(R.t), j + TOL + 1)
    if lo >= hi:
        return False
    for jj in range(lo, hi):
        if E.flat[i] or R.flat[jj]:
            if jj != j:
                continue
            if (abs(E.mean[i] - R.mean[jj]) < FLAT_MEAN_TOL
                    and abs(E.std[i] - R.std[jj]) < 4.0):
                return True
            continue
        if float(E.X[i] @ R.X[jj]) >= SIM_OK:
            return True
    return False


def _best_offset(E, R, k0, kmax, np):
    """Frame offset k (episode index i <-> reference index i - k) near k0
    with the most matching non-flat frame pairs. Returns (k, count) or
    (None, 0)."""
    S = E.X @ R.X.T                                   # [Ne, Nr]
    ok = (S >= SIM_OK) & (~E.flat)[:, None] & (~R.flat)[None, :]
    best = (None, 0, -1.0)
    for k in range(k0 - kmax, k0 + kmax + 1):
        # diagonal i - j = k  ->  numpy offset (j - i) = -k
        d = np.diagonal(ok, offset=-k)
        n = int(d.sum())
        if n == 0:
            continue
        s = float(np.diagonal(S, offset=-k)[d].sum())
        if (n, s) > (best[1], best[2]):
            best = (k, n, s)
    return best[0], best[1]


def _visual_edge(E, R, te, tr, edge, kmax, inside_t, np, hold=False):
    """Exact boundary frame in E for the boundary at te (episode) / tr
    (reference); inside_t = an episode time inside the segment (where the
    walk starts). Returns (index, info) - index of the first frame of the
    segment (start) or the first frame after it (end) - or (None, reason);
    reason is a dict {"beyond", "te", "tr"} when the pictures still match at
    the window's edge (the boundary is further out)."""
    ie = int(np.argmin(np.abs(E.t - te)))
    ir = int(np.argmin(np.abs(R.t - tr)))
    k, n = _best_offset(E, R, ie - ir, kmax, np)
    if k is None or n < MIN_ANCHOR:
        return None, "no matching pictures"
    cand = [i for i in range(len(E.t)) if 0 <= i - k < len(R.t) and not E.flat[i]
            and not R.flat[i - k] and float(E.X[i] @ R.X[i - k]) >= SIM_OK]
    if not cand:
        return None, "no anchor"
    anchor = min(cand, key=lambda i: abs(E.t[i] - inside_t))
    step = -1 if edge == "start" else 1
    last, miss, i = anchor, 0, anchor
    stop = None
    while True:
        i += step
        if not 0 <= i < len(E.t):
            # ran off the episode window: the true file edge is a valid stop,
            # otherwise the boundary lies beyond the window
            if (step < 0 and E.at_start) or (step > 0 and E.at_end):
                stop = "file edge"
            break
        j = i - k
        if not 0 <= j < len(R.t):
            if (step < 0 and R.at_start and j < 0) or (step > 0 and R.at_end and j >= len(R.t)):
                stop = "reference edge"        # template start / end reached
            break
        if _match_vec(E, i, R, j, np):
            last, miss = i, 0
        else:
            miss += 1
            if miss >= MISS:
                stop = "content differs"
                break
    if stop is None:
        return None, {"beyond": True, "te": float(E.t[last]), "tr": float(R.t[last - k])}
    if hold and stop == "reference edge":
        # the template ends here, but this episode may hold its edge picture
        # a frame or two longer (its segment is a frame longer): those
        # frames are the segment too (never a black / flat frame)
        je = 0 if step < 0 else len(R.t) - 1
        i = last + step
        for _ in range(2):
            if not 0 <= i < len(E.t) or E.flat[i] or R.flat[je] or not _match_vec(E, i, R, je, np):
                break
            last = i
            i += step
    # flat run at the edge (fade through black): ambiguous span
    span = None
    if edge == "start":
        a = last
        while a < len(E.t) and E.flat[a]:
            a += 1
        if a - last > MAX_FLAT_EDGE:
            span = (last, a)                       # first frame .. first non-flat
        idx = last
    else:
        b = last
        while b >= 0 and E.flat[b]:
            b -= 1
        if last - b > MAX_FLAT_EDGE:
            span = (b + 1, last + 1)               # first flat .. first after
        idx = last + 1
    # the edge between two black / flat frames of the same brightness: no
    # picture cut shows where it is (the alignment placed it) - approximate
    a, b = (idx - 1, idx)
    black = (0 <= a and b < len(E.t) and E.flat[a] and E.flat[b]
             and abs(E.mean[a] - E.mean[b]) < FLAT_MEAN_TOL)
    return idx, {"offset": k, "pairs": n, "stop": stop, "span": span, "last": last,
                 "black": bool(black), "dt": float(E.t[anchor] - R.t[anchor - k])}


def _template_margin(R, edge, np):
    """Follow mode: the template maker's margin at a template clip's edge -
    the outermost frames before the first hard picture cut within
    MARGIN_MAX + 1 frames of the edge (a frame or two of the source episode
    kept around the intro / credits). 0 when there is no cut there (the
    template starts / ends on the segment itself) or the clip's edge isn't
    in the window."""
    n = len(R.t)
    if n < MARGIN_MAX + 3:
        return 0
    if edge == "start":
        if not R.at_start:
            return 0
        for j in range(1, MARGIN_MAX + 1):
            if float(np.abs(R.raw[j].astype(np.int16) - R.raw[j - 1]).mean()) >= CUT_MIN:
                return j
        return 0
    if not R.at_end:
        return 0
    for m in range(1, MARGIN_MAX + 1):
        j = n - m                          # cut between frame j-1 and j -> m frames margin
        if float(np.abs(R.raw[j].astype(np.int16) - R.raw[j - 1]).mean()) >= CUT_MIN:
            return m
    return 0


def _trim(R, edge, m):
    """R without its m edge frames (the template's margin); the cut-off
    edge counts as the reference's edge."""
    f = _Frames()
    sl = slice(m, None) if edge == "start" else slice(0, len(R.t) - m)
    for a in ("t", "raw", "X", "flat", "mean", "std"):
        setattr(f, a, getattr(R, a)[sl])
    f.fdur, f.t0, f.t1 = R.fdur, R.t0, R.t1
    f.at_start = R.at_start or edge == "start"
    f.at_end = R.at_end or edge == "end"
    return f

def _frame_time(E, idx):
    """Time of frame idx of E; one past the last frame = its end."""
    if idx < len(E.t):
        return float(E.t[idx])
    return float(E.t[-1] + E.fdur)


def _cut_near(E, lo, hi, np):
    """Index of the strongest picture cut (frame idx differs from idx-1)
    with time in [lo, hi], or None when no clear cut is there."""
    best, bi = CUT_MIN, None
    for i in range(1, len(E.t)):
        if lo - 1e-6 <= E.t[i] <= hi + 1e-6:
            d = float(np.abs(E.raw[i].astype(np.int16) - E.raw[i - 1]).mean())
            if d > best:
                best, bi = d, i
    return bi


def _snap_index(E, t, np):
    """Frame whose start time is nearest t."""
    return int(np.argmin(np.abs(E.t - t)))
