"""Frame-accurate boundary refinement (engine/refine.py): the audio edge -
a sample-exact offset by FFT cross-correlation, then the outward walk on
20 ms blocks; _audio_refine follows it over further windows. UI-free."""
from .refine_base import (A_BLOCK, A_MIN_PEAK, A_MISS, A_QUIET, A_QUIET2, A_SILENT, A_SIM_OK,
                          A_SPEC_OK, MAX_STEPS, MAX_TRAVEL, MIN_STEP, PAD, SR, _cached, _check,
                          decode_audio)


# ----------------------------------------------------------------- audio
def _xcorr_norm(x, y, np):
    """Normalized cross-correlation of chunk y slid over x: array c[lag]
    (y placed at x[lag:lag+len(y)])."""
    _check()
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


def _audio_edge(xe, ae0, xr, ar0, te, tr, edge, ref_exact, radius, np, cont=False,
                e_edges=(False, False), r_edges=(False, False)):
    """Audio boundary: sample-exact offset from an inside chunk of the
    reference, then an outward walk on aligned 20 ms blocks. Returns
    (t_episode, info) or (None, reason). info['span'] = (a, b) when the edge
    sits in a silent gap (the exact spot inside it is unknown); reason is a
    dict {"beyond", "te", "tr"} when the sound still matches at the window's
    edge. cont: te / tr are a matched pair inside the segment (a follow-up
    window) - the chunk starts right there. e_edges / r_edges: (window starts
    at the file start, window reaches the file end) of the episode / the
    reference - the walk running into a file's own edge stops there
    ('file edge' / 'reference edge') instead of asking for the next window."""
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
    # the chunk clamped to the reference window (a short template, a segment
    # ending a few seconds into its file): its real times, or every position
    # below would be off by the clamped part
    c0, c1 = ar0 + i0 / SR, ar0 + i1 / SR
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
    side = 0 if edge == "start" else 1
    for _ in range(int((2 * radius + PAD + 3) / A_BLOCK)):
        a = pos - A_BLOCK if edge == "start" else pos      # block [a, a+B) in the episode
        pos = a if edge == "start" else a + A_BLOCK
        xe_b = blk(xe, ae0, a)
        xr_b = blk(xr, ar0, a - off)
        if xe_b is None or xr_b is None:
            if xr_b is None and (ref_exact or r_edges[side]):
                stop = "reference edge"
            elif xe_b is None and e_edges[side]:
                # the episode's own start / end: the segment runs to it
                stop = "file edge"
                if miss == 0:
                    last = last_sound = ae0 if edge == "start" else ae0 + len(xe) / SR
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


def _audio_refine(path, t, edge, ref, ref_t, ref_exact, radius, track, ref_track, cache,
                  ref_cache, np):
    """Best audio boundary over the episode's candidate tracks, following the
    sound past the window when it still matches there (up to MAX_TRAVEL)."""
    tracks = track if isinstance(track, (list, tuple)) else [track]
    best = None

    def edges(x, a, b):
        # (window starts at the file start, the file ended inside the window)
        return a <= 1e-6, len(x) < (b - a - 0.1) * SR
    for tk in tracks:
        cur_t, cur_r, cont = t, ref_t, False
        for _step in range(MAX_STEPS + 1):
            _check()
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
                                   cont=cont, e_edges=edges(xe, ea, eb),
                                   r_edges=edges(xr, ra, rb))
            if ta is not None:
                if best is None or info["peak"] > best[1]["peak"]:
                    best = (ta, info)
                break
            if not isinstance(info, dict) or abs(info["te"] - t) > MAX_TRAVEL:
                break
            # the next window must lie further out - a walk that ends where
            # this one started (a window / file edge it can't pass) would
            # otherwise repeat the same window forever
            moved = cur_t - info["te"] if edge == "start" else info["te"] - cur_t
            if moved < MIN_STEP:
                break
            cur_t, cur_r, cont = info["te"], info["tr"], True
    return best
