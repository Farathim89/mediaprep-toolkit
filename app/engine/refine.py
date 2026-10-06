"""Frame-accurate boundary refinement - the shared last stage of every
detector (template matching, Templates -> Auto-detect, Plex-style scan).
UI-free.

The detectors find a segment to within a fraction of a second up to a few
seconds (MFCC hops of 23-93 ms, 4 fps / 2 fps picture hashes, run smoothing,
silence/black snapping). This module takes such a coarse boundary and a
REFERENCE showing the same boundary - the template clip (whose edges are
exact by definition) or the same segment in another episode - and finds the
exact frame, each edge on its own (intros / credits can differ by a frame or
two in length):

  * visual: every frame (native fps, 48x27 gray, real timestamps from
    ffmpeg showinfo) of a short window around the boundary in both files;
    the frame offset between them (best diagonal), then a walk from inside
    the segment outward while the episode's frames keep matching the
    reference's (+-2 frames for held animation frames, 3-frame hysteresis).
    Black / flat frames only match flat frames of similar brightness; a long
    flat run at the edge (fade through black) is ambiguous and is settled by
    the audio, else the first / last non-flat frame wins.
  * audio: 16 kHz mono of the same windows; sample-exact offset by FFT
    cross-correlation (needs a clear peak), then the same outward walk on
    20 ms blocks (waveform or magnitude-spectrum match, 10-block
    hysteresis). Quiet on both sides is neutral; when the edge falls in a
    quiet gap / fade, a picture cut inside the gap (largest frame change)
    decides, else the gap goes with the segment (never cuts into the
    episode's own sound).
  * when the pictures / the sound still match at the window's edge (the
    coarse boundary was further off), the walk follows them with the next
    window (up to MAX_TRAVEL); when two episodes' coarse ranges came from
    different sub-runs, their real offset is measured first (pair_offset).
  * the result is snapped to a real frame time of the episode: start = the
    first frame of the segment, end = the first frame AFTER it, so a cut
    [start, end) removes exactly the segment's frames.

Everything is decoded from short windows only (~10 s per edge), so a
boundary costs well under a second or two. Any failure keeps the coarse
boundary (never loses a detection).

    r = refine_boundary(ep, 92.34, "start", ref=tpl_path, ref_t=0.0, ref_exact=True)
    # -> {"t": 92.426, "src": "visual", "frames": 2, ...} or None
    s, e, notes = refine_range(ep, (s, e), ref, (rs, re), ref_exact=False)
"""
import re
import subprocess

from ..config import POPEN_FLAGS

VW, VH = 48, 27          # decode size of the visual windows
SEARCH = 3.0             # the coarse boundary may be this far off (further: widened / followed)
PAD = 2.5                # extra context beyond the search radius (s)
SIM_OK = 0.85            # frame correlation that counts as the same picture
FLAT_STD = 6.0           # gray std below this = black / flat frame
FLAT_MEAN_TOL = 8.0      # two flat frames match when their brightness is this close
TOL = 2                  # held frames: compare with ref frames +-TOL around the aligned one
MISS = 3                 # this many mismatches in a row end a run (hysteresis)
MIN_ANCHOR = 12          # matched non-flat frame pairs needed on the best diagonal
MAX_FLAT_EDGE = 6        # flat frames at an edge beyond this = ambiguous (fade)
SR = 16000               # audio refinement sample rate
A_BLOCK = 0.02           # audio walk block (s)
A_SIM_OK = 0.45          # aligned audio block correlation that counts as the same
A_MISS = 10              # audio: this many non-matching blocks in a row end a run
A_SPEC_OK = 0.92         # ... or magnitude-spectrum similarity
A_SILENT = 10 ** (-55 / 20.0)   # block RMS below this (-55 dBFS) = silence
A_QUIET = 10 ** (-50 / 20.0)    # both blocks below this = a quiet gap / fade (neutral)
A_QUIET2 = 10 ** (-40 / 20.0)   # one side silent, the other below this = still a fade
A_MIN_PEAK = 0.35        # normalized cross-correlation peak needed for an audio offset
MAX_TRAVEL = 120.0       # follow still-matching pictures at most this far (s)
CUT_MIN = 18.0           # mean abs frame change (0-255) that counts as a picture cut

_RE_PTS = re.compile(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?[0-9.]+(?:e[-+]?\d+)?)")


# ----------------------------------------------------------------- decoding
class _Frames:
    """Gray frames of [t0, t1] of a file: times (app timeline, s), raw
    [N, D] float32, normalized X, flat mask, mean brightness, frame duration."""
    __slots__ = ("t", "raw", "X", "flat", "mean", "std", "fdur", "t0", "t1", "at_start",
                 "at_end")


def _run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, creationflags=POPEN_FLAGS,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p


def decode_frames(path, t0, t1, w=VW, h=VH, fps=None):
    """Every frame of [t0, t1] (no frame-rate conversion; fps=N samples N
    frames per second instead - coarse alignment only), scaled to w x h
    gray, with its real timestamp. None if nothing could be decoded."""
    import numpy as np
    t0 = max(0.0, float(t0))
    dur = max(0.05, float(t1) - t0)
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info"]
    if t0 > 0:
        base += ["-ss", f"{t0:.3f}"]
    base += ["-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", (f"fps={fps}," if fps else "") + f"scale={w}:{h}:flags=area,format=gray,showinfo"]
    tail = ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = _run(base + ["-fps_mode", "passthrough"] + tail)
    if p is not None and p.returncode != 0 and b"fps_mode" in (p.stderr or b""):
        p = _run(base + ["-vsync", "passthrough"] + tail)       # ffmpeg < 5.1
    if p is None or p.returncode != 0:
        return None
    times = [float(m.group(1)) for m in _RE_PTS.finditer(p.stderr.decode("utf-8", "replace"))]
    n = min(len(times), len(p.stdout) // (w * h))
    if n < 2:
        return None
    raw = np.frombuffer(p.stdout[:n * w * h], dtype=np.uint8).reshape(n, w * h)
    g = raw.astype(np.float32)
    f = _Frames()
    f.t = np.array(times[:n], dtype=np.float64) + t0
    f.raw = raw.copy()                       # uint8: small enough to cache
    f.mean = g.mean(axis=1)
    f.std = g.std(axis=1)
    f.flat = f.std < FLAT_STD
    f.X = (g - f.mean[:, None]) / (f.std[:, None] + 1e-6) / np.sqrt(g.shape[1])
    d = np.diff(f.t)
    f.fdur = float(np.median(d)) if d.size else 1.0 / 24.0
    f.t0, f.t1 = t0, t0 + dur
    f.at_start = t0 <= 1e-6
    # the window reached the end of the file when fewer frames came out than
    # the window could hold
    f.at_end = f.t[-1] + 1.5 * f.fdur < t0 + dur
    return f


def decode_audio(path, t0, t1, track=None):
    """Mono SR-Hz float32 samples of [t0, t1] (sample 0 = time t0 on the
    app timeline - an input seek trims to the exact time), or None."""
    import numpy as np
    t0 = max(0.0, float(t0))
    dur = max(0.05, float(t1) - t0)
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if t0 > 0:
        cmd += ["-ss", f"{t0:.3f}"]
    cmd += ["-i", path, "-t", f"{dur:.3f}", "-map", f"0:a:{int(track or 0)}", "-vn", "-sn",
            "-dn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = _run(cmd)
    if p is None or p.returncode != 0 or len(p.stdout) < SR // 2:
        return None
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _cached(cache, key, fn):
    if cache is None:
        return fn()
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _window(t, radius):
    return round(max(0.0, t - radius - PAD), 3), round(t + radius + PAD, 3)


# ----------------------------------------------------------------- visual
def _match_vec(E, i, R, j, np):
    """Does episode frame i look like reference frame j (or one within
    +-TOL - held animation frames)? Returns True / False."""
    lo, hi = max(0, j - TOL), min(len(R.t), j + TOL + 1)
    if lo >= hi:
        return False
    for jj in range(lo, hi):
        if E.flat[i] or R.flat[jj]:
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


def _visual_edge(E, R, te, tr, edge, kmax, inside_t, np):
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
    return idx, {"offset": k, "pairs": n, "stop": stop, "span": span,
                 "dt": float(E.t[anchor] - R.t[anchor - k])}


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


# ----------------------------------------------------------------- audio
def _xcorr_norm(x, y, np):
    """Normalized cross-correlation of chunk y slid over x: array c[lag]
    (y placed at x[lag:lag+len(y)])."""
    n = len(x) + len(y)
    N = 1 << (n - 1).bit_length()
    y0 = y - y.mean()
    X = np.fft.rfft(x, N)
    Y = np.fft.rfft(y0[::-1], N)
    c = np.fft.irfft(X * Y, N)[len(y) - 1:len(x)]
    # local energy of x under the chunk
    cs = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    cm = np.concatenate([[0.0], np.cumsum(x.astype(np.float64))])
    L = len(y)
    e = cs[L:] - cs[:-L] - (cm[L:] - cm[:-L]) ** 2 / L
    den = np.sqrt(np.maximum(e, 1e-12) * float((y0 ** 2).sum()) + 1e-20)
    return c[:len(den)] / den[:len(c)]


def _audio_edge(xe, ae0, xr, ar0, te, tr, edge, ref_exact, radius, np, cont=False):
    """Audio boundary: sample-exact offset from an inside chunk of the
    reference, then an outward walk on aligned 20 ms blocks. Returns
    (t_episode, info) or (None, reason). info['span'] = (a, b) when the edge
    sits in a silent gap (the exact spot inside it is unknown); reason is a
    dict {"beyond", "te", "tr"} when the sound still matches at the window's
    edge. cont: te / tr are a matched pair inside the segment (a follow-up
    window) - the chunk starts right there."""
    if cont:
        c0, c1 = (tr, tr + 2.5) if edge == "start" else (tr - 2.5, tr)
    elif ref_exact:
        c0, c1 = (tr + 0.6, tr + 3.6) if edge == "start" else (tr - 3.6, tr - 0.6)
    else:
        c0, c1 = ((tr + radius, tr + radius + 2.5) if edge == "start"
                  else (tr - radius - 2.5, tr - radius))
    i0, i1 = int(round((c0 - ar0) * SR)), int(round((c1 - ar0) * SR))
    i0, i1 = max(0, i0), min(len(xr), i1)
    if i1 - i0 < SR:
        return None, "reference chunk too short"
    chunk = xr[i0:i1]
    if float(np.sqrt((chunk ** 2).mean())) < A_SILENT:
        return None, "reference chunk is silent"
    c = _xcorr_norm(xe, chunk, np)
    if c.size < 3:
        return None, "episode window too short"
    # only lags that keep the coarse offset within +-2*radius
    want = (c0 - (tr - te)) - ae0               # expected chunk position in xe (s)
    lo = max(0, int((want - 2 * radius) * SR))
    hi = min(c.size, int((want + 2 * radius) * SR) + 1)
    if hi - lo < 3:
        return None, "no audio overlap"
    seg = c[lo:hi]
    p = int(np.argmax(seg))
    peak = float(seg[p])
    if peak < A_MIN_PEAK:
        return None, f"no clear audio match ({peak:.2f})"
    # clear peak: the best value 50 ms+ away must be clearly lower
    g = int(0.05 * SR)
    rest = np.concatenate([seg[:max(0, p - g)], seg[p + g:]])
    if rest.size and float(rest.max()) > 0.9 * peak:
        return None, "ambiguous audio match"
    lag = lo + p
    # episode time = reference time + off
    off = (ae0 + lag / SR) - c0
    B = int(A_BLOCK * SR)

    def blk(x, x0, t):
        a = int(round((t - x0) * SR))
        if a < 0 or a + B > len(x):
            return None
        return x[a:a + B]

    hann = np.hanning(B)

    def rms(v):
        return float(np.sqrt((v ** 2).mean()))
    # walk outward from the chunk's outer edge, one block at a time; `last`
    # = outer edge of the matched run, `last_sound` = outer edge of its last
    # non-silent matched block (episode timeline)
    last = (c0 if edge == "start" else c1) + off
    last_sound = last
    miss = 0
    stop = None
    pos = last
    for _ in range(int((2 * radius + PAD + 3) / A_BLOCK)):
        a = pos - A_BLOCK if edge == "start" else pos      # block [a, a+B) in the episode
        pos = a if edge == "start" else a + A_BLOCK
        xe_b = blk(xe, ae0, a)
        xr_b = blk(xr, ar0, a - off)
        if xe_b is None or xr_b is None:
            if xr_b is None and ref_exact:
                stop = "reference edge"
            break
        se, sr_ = rms(xe_b), rms(xr_b)
        if max(se, sr_) < A_QUIET or min(se, sr_) < A_SILENT and max(se, sr_) < A_QUIET2:
            # (near) silence on both sides - fades and gaps carry no
            # fingerprint: neutral, they neither extend nor break the run
            if miss == 0:
                last = pos
            continue
        if min(se, sr_) < A_SILENT:
            same = False
        else:
            u, v = xe_b - xe_b.mean(), xr_b - xr_b.mean()
            wav = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-12))
            # magnitude spectra too: the same sound a few samples apart (the
            # tracks drift slightly) loses its waveform match but not this
            fu, fv = np.abs(np.fft.rfft(u * hann)), np.abs(np.fft.rfft(v * hann))
            spec = float(np.dot(fu, fv) / (np.linalg.norm(fu) * np.linalg.norm(fv) + 1e-12))
            same = wav >= A_SIM_OK or spec >= A_SPEC_OK
        if same:
            last, last_sound, miss = pos, pos, 0
        else:
            miss += 1
            if miss >= A_MISS:
                stop = "audio differs"
                break
    if stop is None:
        return None, {"beyond": True, "te": last, "tr": last - off, "peak": peak}
    span = None
    if stop != "reference edge" and abs(last - last_sound) >= 2 * A_BLOCK:
        # the matched run ends in silence shared by both: the boundary is
        # somewhere in that gap
        span = (min(last, last_sound), max(last, last_sound))
    return last, {"offset": off, "peak": peak, "stop": stop, "span": span}


# ----------------------------------------------------------------- public
def refine_boundary(path, t, edge, ref, ref_t, ref_exact=False, radius=SEARCH,
                    track=None, ref_track=None, cache=None, video=True, audio=True,
                    ref_cache=None):
    """Refine one boundary of `path` near time `t` against the same boundary
    in `ref` at `ref_t`. edge 'start' (t = first frame of the segment) or
    'end' (t = first frame after it). ref_exact: the reference's segment
    starts at its file start / ends at its file end (a template clip), so
    the reference's own edge is a valid stop. Returns {"t", "src" ('visual'
    / 'audio' / 'audio+cut'), "frames" (shift in frames), "fdur", "why"} or
    None (keep the coarse value). cache / ref_cache: dicts that keep decoded
    windows of the episode / the reference for the next call (a template's
    windows can be reused for a whole batch)."""
    import numpy as np
    t, ref_t = float(t), float(ref_t)
    if ref_cache is None:
        ref_cache = cache
    E = R = None
    cur_t, cur_r = t, ref_t
    inside = t + (radius if edge == "start" else -radius)
    rad = radius
    widened = False
    while video:
        ea, eb = _window(cur_t, rad)
        ra, rb = _window(cur_r, rad)
        if ref_exact:
            # the clip's edge lies inside the window: decode to/from it
            if edge == "start":
                ra = max(0.0, min(ra, ref_t))
            else:
                rb = max(rb, ref_t + 1.0)
        E = _cached(cache, ("v", path, ea, eb), lambda: decode_frames(path, ea, eb))
        R = _cached(ref_cache, ("v", ref, ra, rb), lambda: decode_frames(ref, ra, rb))
        if E is None or R is None:
            break
        if ref_exact and ra <= ref_t <= rb:
            if edge == "start":
                R.at_start = R.at_start or ra <= 1e-6
            else:
                R.at_end = True
        kmax = int(round((rad if ref_exact or cur_t != t else 2 * rad) / max(E.fdur, 1e-3)))
        idx, info = _visual_edge(E, R, cur_t, cur_r, edge, kmax, inside, np)
        if idx is not None:
            span = info["span"]
            res = {"t": _frame_time(E, idx), "src": "visual", "fdur": E.fdur,
                   "why": info["stop"]}
            if span is None:
                return _finish(res, t)
            # fade through black: let the audio pick inside the flat span
            lo_t, hi_t = _frame_time(E, span[0]), _frame_time(E, span[1])
            at = _audio_refine(path, res["t"], edge, ref, res["t"] - info["dt"], ref_exact,
                               radius, track, ref_track, cache, ref_cache, np) if audio else None
            if (at is not None and not at[1].get("span")
                    and lo_t - E.fdur <= at[0] <= hi_t + E.fdur):
                res["t"] = _frame_time(E, _snap_index(E, at[0], np))
                res["src"] = "visual+audio"
            else:
                # first non-black frame (start) / first black frame (end)
                res["t"] = hi_t if edge == "start" else lo_t
                res["why"] += ", fade: non-black edge"
            return _finish(res, t)
        if isinstance(info, dict):
            # still the same pictures at the window's edge: follow them (the
            # coarse boundary was further off than the search radius)
            if abs(info["te"] - t) > MAX_TRAVEL:
                break
            cur_t, cur_r = info["te"], info["tr"]
            inside = cur_t
            rad = radius
            continue
        if widened or cur_t != t:
            break
        rad, widened = 2.5 * radius, True        # maybe further off: one wider look
    if not audio:
        return None
    at = _audio_refine(path, t, edge, ref, ref_t, ref_exact, radius, track, ref_track,
                       cache, ref_cache, np)
    if at is None:
        return None
    ta, ainfo = at
    span = ainfo.get("span")
    if span:
        # the run ends in a quiet gap / fade shared by both sides: the gap
        # goes with the segment (never cuts into the episode's own sound) -
        # unless a picture cut inside it says exactly where the cut is
        ta = span[0] if edge == "start" else span[1]
    lo_t, hi_t = span if span else (ta, ta)
    if video and (E is None or not (E.t[0] <= lo_t - 0.1 and hi_t + 0.1 <= E.t[-1])):
        # frames around the audio boundary (it may lie outside the windows
        # looked at so far - the sound was followed further than the pictures)
        wa, wb = round(max(0.0, lo_t - 1.0), 3), round(hi_t + 1.0, 3)
        E = _cached(cache, ("v", path, wa, wb), lambda: decode_frames(path, wa, wb))
    res = {"t": ta, "src": "audio", "fdur": E.fdur if E is not None else None,
           "why": ainfo["stop"]}
    if E is not None:
        fd = E.fdur
        lo_c, hi_c = (lo_t - 0.5 * fd, hi_t + 0.5 * fd) if span else (ta - 2 * fd, ta + 2 * fd)
        ci = _cut_near(E, lo_c, hi_c, np)
        if ci is not None:
            res["t"] = float(E.t[ci])
            res["src"] = "audio+cut"
        else:
            res["t"] = _frame_time(E, _snap_index(E, ta, np))
    return _finish(res, t)


def _audio_refine(path, t, edge, ref, ref_t, ref_exact, radius, track, ref_track, cache,
                  ref_cache, np):
    """Best audio boundary over the episode's candidate tracks, following the
    sound past the window when it still matches there (up to MAX_TRAVEL)."""
    tracks = track if isinstance(track, (list, tuple)) else [track]
    best = None
    for tk in tracks:
        cur_t, cur_r, cont = t, ref_t, False
        while True:
            span = 2 * radius + PAD + 4.0
            ea, eb = max(0.0, cur_t - span), cur_t + span
            if ref_exact and not cont:
                ra, rb = ((0.0, cur_r + span) if edge == "start"
                          else (max(0.0, cur_r - span), cur_r + 0.5))
            else:
                ra, rb = max(0.0, cur_r - span), cur_r + span
            xr = _cached(ref_cache, ("a", ref, ref_track, round(ra, 3), round(rb, 3)),
                         lambda: decode_audio(ref, ra, rb, ref_track))
            xe = _cached(cache, ("a", path, tk, round(ea, 3), round(eb, 3)),
                         lambda: decode_audio(path, ea, eb, tk))
            if xr is None or xe is None:
                break
            ta, info = _audio_edge(xe, ea, xr, ra, cur_t, cur_r, edge, ref_exact, radius, np,
                                   cont=cont)
            if ta is not None:
                if best is None or info["peak"] > best[1]["peak"]:
                    best = (ta, info)
                break
            if not isinstance(info, dict) or abs(info["te"] - t) > MAX_TRAVEL:
                break
            cur_t, cur_r, cont = info["te"], info["tr"], True
    return best


def _finish(res, t0):
    fd = res.get("fdur") or (1001 / 24000.0)
    res["t"] = round(float(res["t"]), 3)
    res["frames"] = int(round((res["t"] - t0) / fd))
    return res


def pair_offset(p, rng_p, q, rng_q, track_p=None, track_q=None, cache=None, margin=30.0):
    """Coarse time offset (p_time - q_time, s) of the segment shared by p
    (coarse range rng_p) and q (rng_q), for when their coarse ranges don't
    line up (each came from a different pair / sub-run): a few seconds from
    inside p's range are searched in q's range +-margin - by audio
    (sample-exact cross-correlation, needs a clear peak), else by pictures
    (4 fps). Returns the offset or None."""
    import numpy as np
    sp, ep_ = float(rng_p[0]), float(rng_p[1])
    sq, eq = max(0.0, float(rng_q[0]) - margin), float(rng_q[1]) + margin
    if ep_ - sp < 4.0:
        return None
    probes = [sp + f * (ep_ - sp) for f in (0.5, 0.3, 0.7)]
    xq = _cached(cache, ("a", q, track_q, round(sq, 3), round(eq, 3)),
                 lambda: decode_audio(q, sq, eq, track_q))
    if xq is not None:
        for c in probes:
            xp = decode_audio(p, c - 2.0, c + 2.0, track_p)
            if xp is None or float(np.sqrt((xp ** 2).mean())) < A_QUIET2:
                continue
            cc = _xcorr_norm(xq, xp, np)
            if cc.size < 3:
                continue
            j = int(np.argmax(cc))
            peak = float(cc[j])
            g = int(0.1 * SR)
            rest = np.concatenate([cc[:max(0, j - g)], cc[j + g:]])
            if peak >= 0.5 and (not rest.size or float(rest.max()) < 0.85 * peak):
                return (max(0.0, c - 2.0)) - (sq + j / SR)
    # pictures: 8 s of p at 4 fps slid over q's range at 4 fps
    Q = _cached(cache, ("v4", q, round(sq, 3), round(eq, 3)),
                lambda: decode_frames(q, sq, eq, fps=4))
    if Q is None:
        return None
    for c in probes:
        P = decode_frames(p, c - 4.0, c + 4.0, fps=4)
        if P is None or int((~P.flat).sum()) < 8:
            continue
        S = (P.X @ Q.X.T >= SIM_OK) & (~P.flat)[:, None] & (~Q.flat)[None, :]
        best, second, bk = 0, 0, None
        for k in range(-(len(P.t) - 1), len(Q.t)):
            n = int(np.diagonal(S, offset=k).sum())
            if n > best:
                best, second, bk = n, best, k
            elif n > second:
                second = n
        if bk is not None and best >= 8 and best >= 0.5 * int((~P.flat).sum())                 and second < 0.8 * best:
            # P frame i <-> Q frame i + bk
            i = max(0, -bk)
            return float(P.t[i] - Q.t[i + bk])
    return None


def refine_range(path, rng, ref, ref_rng, ref_exact=False, log=None, label="",
                 track=None, ref_track=None, cache=None, video=True, audio=True,
                 ref_cache=None):
    """Refine both edges of `rng` (s, e) in `path` against `ref_rng` in
    `ref`, independently. ref_exact: bool or a (start, end) pair of bools.
    Returns (s, e, (start_refined, end_refined)). Logs one line per moved
    edge ('refined start 92.340 -> 92.426 (visual, +2 frames)')."""
    log = log or (lambda m: None)
    s, e = float(rng[0]), float(rng[1])
    exact = tuple(ref_exact) if isinstance(ref_exact, (tuple, list)) else (ref_exact,) * 2
    out = [s, e]
    done = [False, False]
    for n, edge in enumerate(("start", "end")):
        try:
            r = refine_boundary(path, out[n], edge, ref, ref_rng[n], ref_exact=exact[n],
                                track=track, ref_track=ref_track, cache=cache,
                                video=video, audio=audio, ref_cache=ref_cache)
        except Exception as exc:                  # never break a detection
            r = None
            log(f"   [refine] {label}{edge}: failed ({exc})")
        if r is None:
            continue
        done[n] = True
        if abs(r["t"] - out[n]) >= 0.0005:
            log(f"   [refine] {label}refined {edge} {out[n]:.3f} -> {r['t']:.3f} "
                f"({r['src']}, {r['frames']:+d} frames)")
        out[n] = r["t"]
    if out[1] <= out[0]:                           # nonsense: keep the coarse range
        return s, e, (False, False)
    return out[0], out[1], tuple(done)


def refine_ranges(ranges, log=None, label="", track_for=None, cache=None, video=True,
                  audio=True, known_ref=None, done_out=None):
    """Refine every member of a recurring segment {path: (s, e)} against
    another member (the one whose length is the median); an edge the
    pictures can't settle is also tried against a second member (closest in
    length) and takes the outermost result (pictures preferred).
    known_ref=(template_path, duration): refine against that template clip
    instead (its edges are exact). track_for(path) -> audio track index or
    None. done_out (dict) gets {path: (start_refined, end_refined)}.
    Returns {path: (s, e)} (unrefined edges keep their coarse value)."""
    import os
    log = log or (lambda m: None)
    cache = {} if cache is None else cache
    tf = track_for or (lambda p: None)
    paths = list(ranges)
    out = dict(ranges)
    if known_ref is not None:
        tpl, tdur = known_ref
        for p in paths:
            s, e, done = refine_range(p, ranges[p], tpl, (0.0, tdur), ref_exact=True, log=log,
                                      label=f"{label}{os.path.basename(p)}: ", track=tf(p),
                                      cache=cache, video=video, audio=audio)
            out[p] = (s, e)
            if done_out is not None:
                done_out[p] = done
        return out
    if len(paths) < 2:
        return out
    lens = {p: ranges[p][1] - ranges[p][0] for p in paths}
    order = sorted(paths, key=lambda p: lens[p])
    med = order[len(order) // 2]
    for p in paths:
        partners = [med] if p != med else []
        others = sorted((q for q in paths if q != p and q not in partners),
                        key=lambda q: abs(lens[q] - lens[p]))
        partners += others[:1]
        new = list(ranges[p])
        done = [False, False]
        found = ([], [])                # per edge: results, one per partner
        for q in partners:
            off = None                 # p - q offset, measured only when needed
            for n, edge in enumerate(("start", "end")):
                if any(x["src"].startswith("visual") for x in found[n]):
                    continue           # a frame-exact picture result needs no 2nd opinion
                r = None
                for attempt in (0, 1):
                    if attempt == 0:
                        ref_t = ranges[q][n]
                    else:
                        # the coarse ranges don't line up (different sub-runs):
                        # measure the real offset between the two episodes
                        if off is None:
                            try:
                                off = pair_offset(p, ranges[p], q, ranges[q], tf(p), tf(q),
                                                  cache)
                            except Exception:
                                off = None
                            off = off if off is not None else False
                        if off is False or abs((ranges[p][n] - off) - ranges[q][n]) < 1.0:
                            break
                        ref_t = ranges[p][n] - off
                    try:
                        r = refine_boundary(p, ranges[p][n], edge, q, ref_t,
                                            track=tf(p), ref_track=tf(q),
                                            cache=cache, video=video, audio=audio)
                    except Exception as exc:
                        r = None
                        log(f"   [refine] {label}{os.path.basename(p)} {edge}: "
                            f"failed ({exc})")
                    if r is not None:
                        break
                if r is not None:
                    found[n].append(r)
        for n, edge in enumerate(("start", "end")):
            rs = found[n]
            if not rs:
                continue
            # the segment is what p shares with ANY partner (one partner may
            # differ in a part - an episode-specific sound effect, a shot):
            # the outermost result, pictures preferred over sound
            vis = [r for r in rs if r["src"].startswith("visual")]
            pool = vis or rs
            r = (min(pool, key=lambda x: x["t"]) if edge == "start"
                 else max(pool, key=lambda x: x["t"]))
            done[n] = True
            if abs(r["t"] - ranges[p][n]) >= 0.0005:
                log(f"   [refine] {label}{os.path.basename(p)}: refined {edge} "
                    f"{ranges[p][n]:.3f} -> {r['t']:.3f} ({r['src']}, {r['frames']:+d} frames)")
            new[n] = r["t"]
        if new[1] > new[0]:
            out[p] = (new[0], new[1])
        else:
            done = [False, False]
        if done_out is not None:
            done_out[p] = tuple(done)
    return out
