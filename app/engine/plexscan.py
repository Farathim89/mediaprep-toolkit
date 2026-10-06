"""Template-free "Plex-style" season scan: find each episode's intro and
credits without any template clips. UI-free.

How it works (the same idea Plex uses):
  * Intro  - audio fingerprints of the first N seconds of every episode are
    compared pairwise; audio that recurs across episodes near the start is the
    opening (engine.recurring.detect_recurring_segments - the detector behind
    Templates -> Auto-detect). A season with several openings gives several
    clusters; each episode takes the cluster it belongs to. Needs >= 2 episodes.
    Optionally (intro_mode "visual" / "both") the same is done on VIDEO
    fingerprints: a 64-bit difference hash per frame (4 fps) of the intro
    window, matched pairwise on diagonals, so an opening with the same
    picture but different audio (a dub) is found too - and vice versa.
  * Credits - the same recurring-audio search over the last N seconds (an
    ending song shared by episodes) PLUS a visual detector that looks for
    credits-like pictures near the end: mostly dark frames with bright text
    edges, or text rolling upwards (detect_credits_visual). The two are
    combined per episode; a single file gets visual credits only.
  * Optionally every boundary is snapped to a silence / black edge within
    +-1 s (engine.snap.find_snap).

    res = scan_season(files, {"intro_window": 420, "credits_secs": 420})
    res[path] -> {"intro": (s, e) | None, "credits": (s, e) | None,
                  "intro_src" / "credits_src": "audio" | "visual" |
                  "audio+visual" | None, "intro_conflict": bool (audio and
                  video disagreed), "notes": [English, for the log],
                  "cands": {"intro": [...], "credits": [...]} each
                  {"start", "end", "score", "source"}, chosen first}
"""
import os
import subprocess

from ..config import POPEN_FLAGS
from .probe import _probe_json, audio_track_for_lang, probe_duration, probe_video_duration
from .vfp import MODES, combine, per_episode, recurring_video

DEFAULTS = {
    "intro_window": 420.0,   # search the first N s for the recurring opening
    "credits_secs": 420.0,   # search the last N s for credits (audio + visual)
    "min_intro": 10.0,       # shortest recurring audio that counts as an intro
    "min_credits": 10.0,     # ... as credits
    "thresh": 0.8,           # recurring-audio match threshold (sensitivity)
    "lang": None,            # audio language to fingerprint (None = default track)
    "snap": True,            # snap boundaries to silence / black within +-1 s
    "intro_mode": "both",    # intro by "audio", "visual" (video fingerprint) or "both"
    "credits_mode": "both",  # credits by "audio", "visual" (credits pictures) or "both"
    "intro": True,           # find intros
    "credits": True,         # find credits
}

# visual detector tuning (gray 0-255 at 160 px width)
_DARK = 50            # pixel below this = dark
_BRIGHT = 140         # pixel above this = bright (text)
_EDGE = 30.0          # gradient magnitude that counts as an edge
_MAX_MID = 0.10       # credits frames have few mid-grey pixels (no picture)
_MIN_RUN = 15.0       # shortest credits run (s)
_MAX_GAP = 3.0        # gaps up to this long are bridged (s)


def _frame_height(path, width=160):
    """Even output height for scale=<width>:-2 (storage size of the first
    video stream), or None when the file has no readable video."""
    d = _probe_json(path, ["-select_streams", "v:0", "-show_entries",
                           "stream=width,height:stream_side_data=rotation"])
    try:
        s = d["streams"][0]
        w, h = int(s["width"]), int(s["height"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    rot = 0
    for sd in s.get("side_data_list") or ():
        try:
            rot = abs(int(sd.get("rotation", 0)))
        except (TypeError, ValueError):
            pass
    if rot in (90, 270):             # ffmpeg autorotates: output is portrait
        w, h = h, w
    if w <= 0 or h <= 0:
        return None
    return max(2, int(round(width * h / w / 2.0)) * 2)


def _frame_features(f, np):
    """(dark_ratio, bright_ratio, text_edge_density, mid_ratio, row_profile)
    of one gray frame (2-D float array). Credits text is bright strokes on a
    dark background with few mid tones (only the anti-aliasing)."""
    dark = float((f < _DARK).mean())
    mid = float(((f >= _DARK) & (f <= _BRIGHT)).mean())
    bright_m = f > _BRIGHT
    bright = float(bright_m.mean())
    gy, gx = np.gradient(f)
    edges = np.hypot(gx, gy) > _EDGE
    # edges next to a bright pixel = bright text strokes (3x3 neighbourhood)
    nb = bright_m.copy()
    nb[1:, :] |= bright_m[:-1, :]
    nb[:-1, :] |= bright_m[1:, :]
    nb2 = nb.copy()
    nb2[:, 1:] |= nb[:, :-1]
    nb2[:, :-1] |= nb[:, 1:]
    text = float((edges & nb2).mean())
    return dark, bright, text, mid, f.mean(axis=1)


def _vertical_shift(p0, p1, np, max_shift):
    """Best upward scroll (rows) between two row profiles and its gain over no
    movement: (shift, corr, corr_at_0). shift 0 = no clear scroll."""
    def corr(a, b):
        if a.size < 8:
            return -1.0
        sa, sb = a.std(), b.std()
        if sa < 1.0 or sb < 1.0:
            return -1.0
        return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))
    c0 = corr(p0, p1)
    best, best_c = 0, -1.0
    for s in range(1, max_shift + 1):
        c = corr(p0[s:], p1[:-s])          # content moved UP by s rows
        if c > best_c:
            best, best_c = s, c
    if best_c > 0.85 and best_c > c0 + 0.05:
        return best, best_c, c0
    return 0, best_c, c0


def _runs(mask):
    """[(start, end_exclusive)] of True runs in a bool list."""
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def detect_credits_visual(path, search_secs=420, fps=2, log=None, stop_event=None,
                          duration=None):
    """Find credits-like pictures in the last `search_secs` of `path`: mostly
    dark frames with bright text edges, or text rolling upwards. Returns
    (start, end, score) in file seconds for the longest such run (>= 15 s,
    gaps <= 3 s bridged), or None. Decodes only that tail, at `fps` frames per
    second, 160 px wide, as raw gray frames piped from ffmpeg."""
    import numpy as np
    log = log or (lambda m: None)
    dur = duration or probe_video_duration(path) or probe_duration(path)
    if not dur:
        log(f"   [visual] can't read the duration of {os.path.basename(path)}")
        return None
    h = _frame_height(path)
    if not h:
        log(f"   [visual] no video stream in {os.path.basename(path)}")
        return None
    w = 160
    t0 = max(0.0, float(dur) - float(search_secs))
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t0:.3f}", "-i", path,
           "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", f"fps={fps},scale={w}:{h},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=POPEN_FLAGS)
    except OSError as e:
        log(f"   [visual] ffmpeg could not start: {e}")
        return None
    fsz = w * h
    feats, prev = [], None
    max_shift = max(2, h // 4)
    stopped = False
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            buf = proc.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            f = np.frombuffer(buf, dtype=np.uint8).reshape(h, w).astype(np.float32)
            dark, bright, text, mid, prof = _frame_features(f, np)
            shift = 0
            if prev is not None:
                shift = _vertical_shift(prev, prof, np, max_shift)[0]
            feats.append((dark, bright, text, shift, mid))
            prev = prof
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    if stopped or len(feats) < 4:
        return None

    n = len(feats)
    dark_text = [d >= 0.70 and 0.002 <= b <= 0.25 and t >= 0.004 and m <= _MAX_MID
                 for d, b, t, _s, m in feats]
    # rolling credits: the same upward shift (+-1 row) over several frame pairs
    scroll = [False] * n
    for i in range(n):
        s = feats[i][3]
        if s <= 0 or feats[i][0] < 0.35 or feats[i][4] > 0.3:
            continue
        lo, hi = max(0, i - 2), min(n, i + 3)
        same = sum(1 for j in range(lo, hi) if j != i and feats[j][3] > 0
                   and abs(feats[j][3] - s) <= 1)
        scroll[i] = same >= 2
    raw = [a or b for a, b in zip(dark_text, scroll)]
    # small majority filter (5 frames) against single odd frames
    k = 2
    smooth = []
    for i in range(n):
        win = raw[max(0, i - k):i + k + 1]
        smooth.append(sum(win) * 2 > len(win) or (raw[i] and sum(win) * 2 == len(win)))
    # bridge short gaps (black cards between credit pages)
    gap = int(round(_MAX_GAP * fps))
    runs = _runs(smooth)
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    merged = [(a, b) for a, b in merged if (b - a) / fps >= _MIN_RUN]
    if not merged:
        return None
    a, b = max(merged, key=lambda r: (r[1] - r[0], r[1]))
    score = sum(raw[a:b]) / float(b - a)
    start = t0 + a / fps
    end = t0 + b / fps
    if dur - end <= 1.0 + 1.0 / fps:      # ran to the last frame -> file end
        end = float(dur)
    end = min(end, float(dur))
    log(f"   [visual] credits-like {start:.1f}s -> {end:.1f}s "
        f"({b - a} of {n} frames, score {score:.2f})")
    return round(start, 3), round(end, 3), round(float(score), 3)


def _overlap(a, b):
    return min(a[1], b[1]) - max(a[0], b[0])


def _combine_credits(audio, visual, dur):
    """Pick the credits range from the audio (recurring ending) and visual
    (credits-like pictures) results. Returns ((s, e) | None, source)."""
    if audio and visual:
        if _overlap(audio, visual) > 0:
            return (min(audio[0], visual[0]), max(audio[1], visual[1])), "audio+visual"
        # disjoint: a visual run that ends well before the file end, with a
        # later recurring ending song, is something else (a dark scene)
        if dur and dur - visual[1] > 30 and audio[0] >= visual[1]:
            return audio, "audio"
        # otherwise the one closer to the end of the file
        if dur and (dur - visual[1]) < (dur - audio[1]) - 5:
            return visual, "visual"
        return audio, "audio"
    if audio:
        return audio, "audio"
    if visual:
        return visual, "visual"
    return None, None


def _snap_range(path, rng, track, stop_event=None, kind=None, dur=None):
    """Snap (s, e) to silence / black edges within +-1 s; returns (s, e, note).
    kind='silence' for credits found in the pictures (dark credits are
    'black' themselves, so a black edge there is not the boundary). An end at
    the file end stays there."""
    from .snap import find_snap
    s, e = rng
    bits = []
    if stop_event is not None and stop_event.is_set():
        return s, e, ""
    ns, why = find_snap(path, s, kind=kind, edge="start", radius=1.0,
                        audio_track=track or 0)
    if ns is not None and abs(ns - s) <= 1.0 and ns < e:
        bits.append(f"start {s:.2f}->{ns:.2f} ({why})")
        s = ns
    if dur and e >= dur - 0.05:
        return s, e, "; ".join(bits)
    ne, why = find_snap(path, e, kind=kind, edge="end", radius=1.0,
                        audio_track=track or 0)
    if ne is not None and abs(ne - e) <= 1.0 and ne > s:
        bits.append(f"end {e:.2f}->{ne:.2f} ({why})")
        e = ne
    return s, e, "; ".join(bits)


def scan_season(files, opts=None, progress=None, stop_event=None, log=None):
    """Find intro + credits in every file without templates (see module doc).
    opts: see DEFAULTS. progress(frac, text) / log(msg) are optional; Stop is
    honoured between steps (a stopped scan returns what it has so far)."""
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (opts or {}).items() if v is not None or k == "lang"})
    log = log or (lambda m: None)
    prog = progress or (lambda f, t="": None)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    files = [f for f in dict.fromkeys(files or []) if f and os.path.isfile(f)]
    out = {f: {"intro": None, "credits": None, "intro_src": None, "credits_src": None,
               "intro_conflict": False, "notes": [], "cands": {"intro": [], "credits": []}} for f in files}
    if not files:
        return out
    durs = {f: float(probe_video_duration(f) or probe_duration(f) or 0.0) for f in files}
    good = [f for f in files if durs[f] > 0]
    for f in files:
        if f not in good:
            out[f]["notes"].append("can't read the duration")
    if not good:
        return out
    shortest = min(durs[f] for f in good)
    # keep the start and end searches apart in short files
    half = max(10.0, 0.5 * shortest)
    iw = min(float(o["intro_window"]), half)
    cw = min(float(o["credits_secs"]), half)
    multi = len(good) >= 2
    lang = o.get("lang")

    imode = o["intro_mode"] if o["intro_mode"] in MODES else "both"
    cmode = o["credits_mode"] if o["credits_mode"] in MODES else "both"

    # ---------------- intro: recurring audio near the start ----------------
    intro_a, intro_v = {}, {}
    if o["intro"] and multi and imode in ("audio", "both"):
        from .recurring import detect_recurring_segments
        log(f"[SCAN] intro (audio): fingerprinting the first {iw:.0f}s of {len(good)} file(s)")
        diag = {}
        cl = detect_recurring_segments(
            good, kinds=("intro",), window=iw, min_lens={"intro": o["min_intro"]},
            thresh=o["thresh"], progress=lambda f, t="": prog(0.0 + 0.25 * f, t),
            stop_event=stop_event, diag_out=diag, lang=lang)
        if stopped():
            return out
        cl = [c for c in cl if c["kind"] == "intro"]
        intro_a = per_episode(cl)
        log(f"[SCAN] intro (audio): {len(cl)} recurring opening(s) covering "
            f"{len(intro_a)}/{len(good)} file(s)")
        if not cl:
            bl, bs = diag.get("intro", (0.0, 0.0))
            if bl > 0:
                log(f"[SCAN] intro (audio): best recurring audio was {bl:.1f}s at score "
                    f"{bs:.2f} - try a lower min length or a looser sensitivity")
    # ---------------- intro: recurring pictures near the start ----------------
    if o["intro"] and multi and imode in ("visual", "both"):
        log(f"[SCAN] intro (video): fingerprinting the first {iw:.0f}s of {len(good)} file(s)")
        cl = recurring_video(good, iw, kind="intro", min_len=o["min_intro"],
                             progress=lambda f, t="": prog(0.25 + 0.15 * f, t),
                             stop_event=stop_event, log=log)
        if cl is None or stopped():
            return out
        intro_v = per_episode(cl)
        log(f"[SCAN] intro (video): {len(cl)} recurring opening(s) covering "
            f"{len(intro_v)}/{len(good)} file(s)")
    if o["intro"] and not multi:
        log("[SCAN] intro: needs >= 2 episodes with the same opening - skipped")

    # ---------------- credits: recurring audio near the end ----------------
    cred_by = {}
    if o["credits"] and multi and cmode in ("audio", "both"):
        from .recurring import detect_recurring_segments
        log(f"[SCAN] credits (audio): fingerprinting the last {cw:.0f}s of {len(good)} file(s)")
        cl = detect_recurring_segments(
            good, kinds=("credits",), window=cw, min_lens={"credits": o["min_credits"]},
            thresh=o["thresh"], progress=lambda f, t="": prog(0.4 + 0.2 * f, t),
            stop_event=stop_event, lang=lang)
        if stopped():
            return out
        cl = [c for c in cl if c["kind"] == "credits"]
        cred_by = per_episode(cl)
        log(f"[SCAN] credits (audio): {len(cl)} recurring ending(s) covering "
            f"{len(cred_by)}/{len(good)} file(s)")
    # a single file has nothing to compare with: its credits come from the
    # pictures even when the mode is audio-only
    cvis = o["credits"] and (cmode in ("visual", "both") or not multi)

    # ---------------- per file: combine, visual credits, snap ----------------
    n = len(good)
    for i, f in enumerate(good):
        if stopped():
            return out
        name = os.path.basename(f)
        r = out[f]
        dur = durs[f]
        if o["intro"]:
            a, v = intro_a.get(f), intro_v.get(f)
            for src, c in (("audio", a), ("visual", v)):
                if c:
                    r["cands"]["intro"].append({"start": c[0], "end": c[1], "score": c[2],
                                                "source": src})
            rng, src, conflict = combine(a, v)
            rng = rng[:2] if rng else None
            if rng and src == "audio+visual":
                r["cands"]["intro"].insert(0, {"start": rng[0], "end": rng[1],
                                               "score": max(a[2], v[2]),
                                               "source": "audio+visual"})
            elif rng:
                r["cands"]["intro"].sort(key=lambda c: c["source"] != src)
            r["intro"], r["intro_src"], r["intro_conflict"] = rng, src, conflict
            if conflict:
                r["notes"].append("intro: audio and video found different places - "
                                  f"took the {src} one")
            if not rng:
                r["notes"].append("no intro (no opening shared with another episode)"
                                  if multi else
                                  "no intro (needs >= 2 episodes with the same opening)")
        if o["credits"]:
            audio = cred_by.get(f)
            if audio:
                r["cands"]["credits"].append({"start": audio[0], "end": audio[1],
                                              "score": audio[2], "source": "audio"})
            visual = None
            if cvis:
                prog(0.6 + 0.35 * i / n, f"Looking for credits pictures in {name}")
                vcw = min(float(o["credits_secs"]), max(10.0, 0.5 * dur))
                visual = detect_credits_visual(f, vcw, log=log, stop_event=stop_event,
                                               duration=dur)
                if stopped():
                    return out
                if visual:
                    r["cands"]["credits"].append({"start": visual[0], "end": visual[1],
                                                  "score": visual[2], "source": "visual"})
            rng, src = _combine_credits(audio[:2] if audio else None,
                                        visual[:2] if visual else None, dur)
            if rng and src == "audio+visual":
                sc = max(audio[2], visual[2])
                r["cands"]["credits"].insert(0, {"start": rng[0], "end": rng[1],
                                                 "score": sc, "source": "audio+visual"})
            elif rng:
                # the chosen source first in the candidate list
                r["cands"]["credits"].sort(key=lambda c: c["source"] != src)
            r["credits"], r["credits_src"] = rng, src
            if not rng:
                r["notes"].append("no credits found")
        if o["snap"] and (r["intro"] or r["credits"]):
            prog(0.6 + 0.35 * (i + 0.8) / n, f"Snapping boundaries in {name}")
            track = audio_track_for_lang(f, lang) if lang else 0
            for key in ("intro", "credits"):
                if r[key] and not stopped():
                    vis = "visual" in (r[key + "_src"] or "") and key == "credits"
                    s, e, how = _snap_range(f, r[key], track, stop_event,
                                            "silence" if vis else None, dur)
                    if how:
                        log(f"   [snap] {name} {key}: {how}")
                    r[key] = (s, e)
        for key in ("intro", "credits"):
            if r[key]:
                r[key] = (round(max(0.0, r[key][0]), 3), round(min(dur, r[key][1]), 3))
    prog(1.0, "Scan finished")
    return out
