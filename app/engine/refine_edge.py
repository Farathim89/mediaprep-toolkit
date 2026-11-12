"""Frame-accurate boundary refinement (engine/refine.py): one boundary
(refine_boundary - the pictures, settled by the sound where they can't
tell) and the coarse offset between two episodes' ranges (pair_offset).
UI-free."""
from .refine_audio import _audio_refine, _xcorr_norm
from .refine_base import (A_QUIET2, FILE_SNAP, MAX_STEPS, MAX_TRAVEL, MIN_STEP, SEARCH, SIM_OK,
                          SR, _cached, _check, _window, decode_audio, decode_frames)
from .refine_visual import (_cut_near, _frame_time, _snap_index, _template_margin, _trim,
                            _visual_edge)


# ----------------------------------------------------------------- public
def refine_boundary(path, t, edge, ref, ref_t, ref_exact=False, radius=SEARCH,
                    track=None, ref_track=None, cache=None, video=True, audio=True,
                    ref_cache=None, follow=False):
    """Refine one boundary of `path` near time `t` against the same boundary
    in `ref` at `ref_t`. edge 'start' (t = first frame of the segment) or
    'end' (t = first frame after it). ref_exact: the reference's segment
    starts at its file start / ends at its file end (a template clip), so
    the reference's own edge is a valid stop. Returns {"t", "src" ('visual'
    / 'audio' / 'audio+cut'), "frames" (shift in frames), "fdur", "why"} or
    None (keep the coarse value). cache / ref_cache: dicts that keep decoded
    windows of the episode / the reference for the next call (a template's
    windows can be reused for a whole batch). follow (with ref_exact): a
    template's margin frames (_template_margin) are cut off for the walk
    and added back beyond the content edge (res['margin'] = how many)."""
    import numpy as np
    t, ref_t = float(t), float(ref_t)
    if ref_cache is None:
        ref_cache = cache
    E = R = None
    cur_t, cur_r = t, ref_t
    inside = t + (radius if edge == "start" else -radius)
    rad = radius
    widened = hopped = False
    steps = 0
    while video:
        _check()
        steps += 1
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
        tm = _template_margin(R, edge, np) if follow and ref_exact else 0
        Rw = _trim(R, edge, tm) if tm else R
        idx, info = _visual_edge(E, Rw, cur_t, cur_r, edge, kmax, inside, np,
                                 hold=follow and ref_exact)
        if idx is not None:
            span = info["span"]
            if info["stop"] == "file edge":
                # the pictures match right up to the episode's own first /
                # last frame: the segment runs to the file's start / end -
                # a black stretch there needs no sound to place it
                span = None
            res = {"t": _frame_time(E, idx), "src": "visual", "fdur": E.fdur,
                   "why": info["stop"]}
            if info.get("black") and span is None:
                res["fade"] = True
                res["why"] += ", black on both sides (approximate)"
            if tm and span is None:
                # the template's margin frames beyond the content edge
                fi = idx - tm if edge == "start" else idx + tm
                if 0 <= fi <= len(E.t):
                    res["t"] = _frame_time(E, fi)
                    res["why"] += f", template margin {tm} frame(s)"
                    res["margin"] = tm
            if follow and ref_exact and (E.at_start if edge == "start" else E.at_end):
                # the template's own first / last frame lands on (or beyond)
                # the episode's file start / end: the segment runs to it, even
                # when the last pictures differ a little (a copyright line,
                # the next-episode tail) - no stub of a few frames is left
                fe = _frame_time(E, 0 if edge == "start" else len(E.t))
                mapped = ref_t + info["dt"]
                beyond = (mapped <= fe + 0.5 * E.fdur if edge == "start"
                          else mapped >= fe - 0.5 * E.fdur)
                if beyond and 1e-6 < abs(fe - res["t"]) <= FILE_SNAP:
                    res["t"] = fe
                    res["why"] += ", template runs to the file " + (
                        "start" if edge == "start" else "end")
                    res.pop("fade", None)
                    span = None
            if span is None:
                return _finish(res, t)
            # fade through black: let the audio pick inside the flat span
            lo_t, hi_t = _frame_time(E, span[0]), _frame_time(E, span[1])
            at = _audio_refine(path, res["t"], edge, ref, res["t"] - info["dt"], ref_exact,
                               radius, track, ref_track, cache, ref_cache, np) if audio else None
            nb = hi_t if edge == "start" else lo_t     # first non-black / first black frame
            if (at is not None and not at[1].get("span")
                    and lo_t - E.fdur <= at[0] <= hi_t + E.fdur):
                ta = _frame_time(E, _snap_index(E, at[0], np))
                if abs(ta - nb) <= 1.01 * E.fdur:
                    # the sound starts / stops right at the non-black edge:
                    # the picture's edge, confirmed
                    res["t"] = nb
                    res["why"] += ", fade: non-black edge (sound agrees)"
                else:
                    # the picture can't tell where in the black the cut is:
                    # the sound placed it (approximate - edge_kind 'audio')
                    res["t"] = ta
                    res["src"] = "visual+audio"
            else:
                # first non-black frame (start) / first black frame (end) -
                # approximate (edge_kind 'fade')
                res["t"] = nb
                res["why"] += ", fade: non-black edge"
                res["fade"] = True
            return _finish(res, t)
        if isinstance(info, dict):
            # still the same pictures at the window's edge: follow them (the
            # coarse boundary was further off than the search radius) - only
            # outward: a follow-up whose walk ends where its window began
            # would repeat that window forever
            moved = cur_t - info["te"] if edge == "start" else info["te"] - cur_t
            if (abs(info["te"] - t) > MAX_TRAVEL or steps > MAX_STEPS
                    or (hopped and moved < MIN_STEP)):
                break
            hopped = True
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
    if ainfo.get("stop") == "file edge":
        # the sound matches right up to the episode's own start / end: the
        # segment runs to the file's first frame / past its last one
        if E is not None and (E.at_start if edge == "start" else E.at_end):
            res["t"] = _frame_time(E, 0 if edge == "start" else len(E.t))
        res["src"] = "file"
        return _finish(res, t)
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
            _check()
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
        _check()
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
