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
boundary (never loses a detection). Every follow-up step must move the
window outward (a walk that stops at the same spot - e.g. at the file's
own start / end - ends the search), a boundary has a wall-clock budget
(BOUNDARY_BUDGET; a segment's two edges RANGE_BUDGET) and Stop is honoured
between steps and while ffmpeg decodes (RefineAborted - the coarse boundary
is kept, see limits()).

    r = refine_boundary(ep, 92.34, "start", ref=tpl_path, ref_t=0.0, ref_exact=True)
    # -> {"t": 92.426, "src": "visual", "frames": 2, ...} or None
    s, e, notes = refine_range(ep, (s, e), ref, (rs, re), ref_exact=False)

The parts: refine_base.py (constants, budget / Stop, decoding), refine_visual.py
(the picture walk), refine_audio.py (the sound walk), refine_edge.py (one
boundary: refine_boundary, pair_offset), refine_template.py (template edges,
safety margins); this module: both edges of a segment (refine_range /
refine_ranges) and how an edge was placed (edge_kind).
"""
import time

from .refine_base import BOUNDARY_BUDGET, RANGE_BUDGET, LRUCache, RefineAborted, limits
from .refine_edge import pair_offset, refine_boundary
from .refine_template import follow_template_edge, norm_template_mode


# ----------------------------------------------------------------- edge kinds
EDGE_KINDS = ("visual", "fade", "audio", "coarse", "file")


def edge_kind(r):
    """How a refine result placed its boundary: 'visual' (frame-exact picture
    match or a picture cut), 'fade' (a black / flat stretch with no picture
    cut: the first / last non-black frame - approximate), 'audio' (the
    sound placed it - no usable pictures, or inside a black stretch -
    approximate), 'file' (the sound matched up to the file's own start /
    end - exact), 'coarse' (not refined). Anything but 'visual' / 'file'
    deserves a 'check this edge' warning."""
    if not r:
        return "coarse"
    src = r.get("src") or ""
    if src == "visual":
        return "fade" if r.get("fade") else "visual"
    if src == "audio+cut":
        return "visual"
    if src == "file":
        return "file"
    return "audio"


def _edge_note(kind):
    return {"audio": " - audio-placed (approximate)",
            "fade": " - black stretch, no picture cut (approximate)"}.get(kind, "")


def _budget_warn(log, label, edge, t, t0):
    log(f"   [WARN] {label}{edge}: refinement gave up after {time.monotonic() - t0:.1f}s - "
        f"kept the coarse {edge} {t:.3f} (approximate - check this edge)")


def refine_range(path, rng, ref, ref_rng, ref_exact=False, log=None, label="",
                 track=None, ref_track=None, cache=None, video=True, audio=True,
                 ref_cache=None, template_mode="exact", src_out=None, stop_event=None):
    """Refine both edges of `rng` (s, e) in `path` against `ref_rng` in
    `ref`, independently. ref_exact: bool or a (start, end) pair of bools.
    template_mode 'follow': an exact reference edge (a template clip's own
    start / end) is mapped into the episode as is (follow_template_edge -
    the template's margin frames included) instead of walking to the
    content edge; 'exact' / 'exact+margin': the content edge (the caller
    adds the margin). src_out (a list) gets [start kind, end kind]
    (edge_kind). Returns (s, e, (start_refined, end_refined)). Logs one line
    per moved edge ('refined start 92.340 -> 92.426 (visual, +2 frames)').
    Each edge has BOUNDARY_BUDGET seconds (both RANGE_BUDGET): an edge
    that takes longer keeps its coarse value (kind 'coarse', a [WARN]
    line); stop_event set = the remaining edges keep their coarse values."""
    log = log or (lambda m: None)
    s, e = float(rng[0]), float(rng[1])
    exact = tuple(ref_exact) if isinstance(ref_exact, (tuple, list)) else (ref_exact,) * 2
    follow = norm_template_mode(template_mode) == "follow"
    out = [s, e]
    done = [False, False]
    kinds = ["coarse", "coarse"]
    with limits(RANGE_BUDGET, stop_event):
        for n, edge in enumerate(("start", "end")):
            t0 = time.monotonic()
            try:
                with limits(BOUNDARY_BUDGET):
                    r = None
                    if follow and exact[n]:
                        r = follow_template_edge(path, out[n], edge, ref, ref_rng[n],
                                                 track=track, ref_track=ref_track, cache=cache,
                                                 ref_cache=ref_cache, video=video, audio=audio)
                    if r is None:
                        r = refine_boundary(path, out[n], edge, ref, ref_rng[n],
                                            ref_exact=exact[n], track=track,
                                            ref_track=ref_track, cache=cache, video=video,
                                            audio=audio, ref_cache=ref_cache)
            except RefineAborted as exc:
                if exc.reason == "stopped":
                    break
                r = None
                _budget_warn(log, label, edge, out[n], t0)
            except Exception as exc:                  # never break a detection
                r = None
                log(f"   [refine] {label}{edge}: failed ({exc})")
            if r is None:
                continue
            done[n] = True
            kinds[n] = edge_kind(r)
            if abs(r["t"] - out[n]) >= 0.0005 or kinds[n] not in ("visual", "file"):
                log(f"   [refine] {label}refined {edge} {out[n]:.3f} -> {r['t']:.3f} "
                    f"({r['src']}, {r['frames']:+d} frames){_edge_note(kinds[n])}")
            out[n] = r["t"]
    if out[1] <= out[0]:                           # nonsense: keep the coarse range
        if src_out is not None:
            src_out[:] = ["coarse", "coarse"]
        return s, e, (False, False)
    if src_out is not None:
        src_out[:] = kinds
    return out[0], out[1], tuple(done)


def refine_ranges(ranges, log=None, label="", track_for=None, cache=None, video=True,
                  audio=True, known_ref=None, done_out=None, template_mode="exact",
                  src_out=None, stop_event=None):
    """Refine every member of a recurring segment {path: (s, e)} against
    another member (the one whose length is the median); an edge the
    pictures can't settle is also tried against a second member (closest in
    length) and takes the outermost result (pictures preferred).
    known_ref=(template_path, duration): refine against that template clip
    instead (its edges are exact; template_mode as in refine_range).
    track_for(path) -> audio track index or
    None. done_out (dict) gets {path: (start_refined, end_refined)}; src_out
    (dict) gets {path: (start kind, end kind)} (edge_kind). Each attempt
    (and each pair_offset) has BOUNDARY_BUDGET seconds and a member all its
    partners / attempts together RANGE_BUDGET; an edge whose attempt ran out
    of its budget gets no further attempts. stop_event set = return at once
    (the members not refined yet keep their coarse ranges).
    Returns {path: (s, e)} (unrefined edges keep their coarse value)."""
    import os

    def stopped():
        return stop_event is not None and stop_event.is_set()
    log = log or (lambda m: None)
    # bounded: a season's worth of windows would pile up; the partners'
    # windows are reused by every member, so they stay among the recent ones
    cache = LRUCache(48) if cache is None else cache
    tf = track_for or (lambda p: None)
    paths = list(ranges)
    out = dict(ranges)
    if known_ref is not None:
        tpl, tdur = known_ref
        for p in paths:
            if stopped():
                break
            ks = []
            s, e, done = refine_range(p, ranges[p], tpl, (0.0, tdur), ref_exact=True, log=log,
                                      label=f"{label}{os.path.basename(p)}: ", track=tf(p),
                                      cache=cache, video=video, audio=audio,
                                      template_mode=template_mode, src_out=ks,
                                      stop_event=stop_event)
            out[p] = (s, e)
            if src_out is not None:
                src_out[p] = tuple(ks)
            if done_out is not None:
                done_out[p] = done
        return out
    if len(paths) < 2:
        return out
    lens = {p: ranges[p][1] - ranges[p][0] for p in paths}
    order = sorted(paths, key=lambda p: lens[p])
    med = order[len(order) // 2]
    for p in paths:
        if stopped():
            break
        partners = [med] if p != med else []
        others = sorted((q for q in paths if q != p and q not in partners),
                        key=lambda q: abs(lens[q] - lens[p]))
        partners += others[:1]
        new = list(ranges[p])
        done = [False, False]
        kinds = ["coarse", "coarse"]
        found = ([], [])                # per edge: results, one per partner
        gave_up = [False, False]        # per edge: an attempt ran out of its budget
        name = os.path.basename(p)
        try:
            # every partner / attempt of this member together: RANGE_BUDGET
            with limits(RANGE_BUDGET, stop_event):
                for q in partners:
                    off = None         # p - q offset, measured only when needed
                    for n, edge in enumerate(("start", "end")):
                        if gave_up[n] or any(x["src"].startswith("visual") for x in found[n]):
                            continue   # a frame-exact picture result needs no 2nd opinion
                        r = None
                        for attempt in (0, 1):
                            if attempt == 0:
                                ref_t = ranges[q][n]
                            else:
                                # the coarse ranges don't line up (different
                                # sub-runs): measure the real offset between
                                # the two episodes
                                if off is None:
                                    try:
                                        with limits(BOUNDARY_BUDGET, stop_event):
                                            off = pair_offset(p, ranges[p], q, ranges[q], tf(p),
                                                              tf(q), cache)
                                    except RefineAborted as exc:
                                        if exc.reason == "stopped":
                                            raise
                                        off = None     # too slow: no offset for this partner
                                    except Exception:
                                        off = None
                                    off = off if off is not None else False
                                if off is False or abs((ranges[p][n] - off) - ranges[q][n]) < 1.0:
                                    break
                                ref_t = ranges[p][n] - off
                            t0 = time.monotonic()
                            try:
                                with limits(BOUNDARY_BUDGET, stop_event):
                                    r = refine_boundary(p, ranges[p][n], edge, q, ref_t,
                                                        track=tf(p), ref_track=tf(q),
                                                        cache=cache, video=video, audio=audio)
                            except RefineAborted as exc:
                                r = None
                                if exc.reason == "stopped":
                                    raise
                                # out of time: no further attempts at this edge
                                gave_up[n] = True
                                if not found[n]:
                                    _budget_warn(log, f"{label}{name} ", edge, ranges[p][n], t0)
                                break
                            except Exception as exc:
                                r = None
                                log(f"   [refine] {label}{name} {edge}: failed ({exc})")
                            if r is not None:
                                break
                        if r is not None:
                            found[n].append(r)
        except RefineAborted:
            pass                       # Stop / the member's budget: what was found counts
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
            kinds[n] = edge_kind(r)
            if abs(r["t"] - ranges[p][n]) >= 0.0005 or kinds[n] not in ("visual", "file"):
                log(f"   [refine] {label}{os.path.basename(p)}: refined {edge} "
                    f"{ranges[p][n]:.3f} -> {r['t']:.3f} ({r['src']}, {r['frames']:+d} frames)"
                    f"{_edge_note(kinds[n])}")
            new[n] = r["t"]
        if new[1] > new[0]:
            out[p] = (new[0], new[1])
        else:
            done = [False, False]
            kinds = ["coarse", "coarse"]
        if src_out is not None:
            src_out[p] = tuple(kinds)
        if done_out is not None:
            done_out[p] = tuple(done)
    return out
