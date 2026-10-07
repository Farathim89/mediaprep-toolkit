"""Template-free detection: find the segments (intro, credits, pre-intro,
after-credits) that recur across several episodes."""
import os
import subprocess

from ..config import limit_cmd, popen_flags, TEMP_DIR
from .probe import audio_track_for_lang, probe_duration


# ======================================================================
# Template-free auto-detect: find intro / credits by what recurs across
# the episodes of a season (no reference clip needed). Handles a show
# that uses more than one intro by CLUSTERING the recurring segments -
# each distinct opening becomes its own cluster with its own timing.
# ======================================================================

def _decode_window_wav(src, dst, window, from_end=False, track=None):
    """Decode just the first (or last) `window` seconds of src to mono
    22.05 kHz wav - fast, because ffmpeg only touches that slice. track=None
    uses the default audio; an int picks that audio stream (0:a:N)."""
    if from_end:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-sseof", f"-{window:.3f}", "-i", src]
    else:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", "0", "-t", f"{window:.3f}", "-i", src]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-ar", "22050", "-ac", "1", "-y", dst]
    try:
        subprocess.run(limit_cmd(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=popen_flags())
    except OSError:                      # ffmpeg missing - caller finds no wav
        pass


def _znorm(M, np):
    return (M - M.mean(axis=1, keepdims=True)) / (M.std(axis=1, keepdims=True) + 1e-8)


def _episode_features(src, window, from_end, np, librosa, sr=22050, hop=2048,
                      n_mfcc=13, lang=None):
    """Return (features, feat_per_sec, time_offset). features is a z-normed
    MFCC matrix for the decoded window; time_offset converts a local frame
    time back to an absolute time in the source file (needed for credits,
    which are read from the end). lang (e.g. 'eng') fingerprints that language
    track when present, so a Japanese default track doesn't skew detection."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    dst = os.path.join(TEMP_DIR, f"_detect_{abs(hash(src)) % 10**8}.wav")
    track = audio_track_for_lang(src, lang) if lang else None
    try:
        _decode_window_wav(src, dst, window, from_end, track=track)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return None, 0.0, 0.0
        y, _ = librosa.load(dst, sr=sr, mono=True)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    if y is None or len(y) < sr:               # < 1 s decoded -> unusable
        return None, 0.0, 0.0
    M = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc, hop_length=hop)
    M = _znorm(M, np)
    fps = sr / hop
    if from_end:
        # -sseof starts decoding at (container end - window), so that is local
        # 0 even if the audio stops before the container ends (less is decoded
        # then, and dur - decoded_len would push every time late). A file shorter
        # than the window decodes from 0.
        dur = probe_duration(src)
        offset = max(0.0, dur - window) if dur else 0.0
    else:
        offset = 0.0
    return M, fps, offset


def _shared_segment(Fi, Fj, np, thresh=0.8, smooth=5, max_gap=6, max_shift=None):
    """Find the longest run of near-identical frames between two feature
    matrices - the segment they share. Small dips are bridged (up to max_gap
    frames) so one quiet moment in the intro does not split it. Returns
    (score, (i0,i1), (j0,j1), run_len_frames) for the best run at/above thresh,
    or None if nothing reaches thresh at all. The caller decides if the run is
    long enough. Lower thresh = more forgiving (real re-encoded audio of the
    same intro tends to sit around 0.75-0.9, not a perfect 1.0).

    Seed-and-grow: a run must REACH thresh somewhere (the seed), but is then
    extended outward while similarity stays above a looser grow threshold.
    The same segment in two episodes is rarely aligned to an exact multiple
    of the MFCC hop (~93 ms), and that sub-hop misalignment drags the whole
    run down to ~0.6-0.75 - real, but below the seed threshold. Without the
    grow step that truncated e.g. a 110 s opening to just its first ~14 s
    (the part that happened to be hop-aligned because both files start at 0)."""
    A = Fi / (np.linalg.norm(Fi, axis=0, keepdims=True) + 1e-8)
    B = Fj / (np.linalg.norm(Fj, axis=0, keepdims=True) + 1e-8)
    S = A.T @ B
    Ti, Tj = S.shape
    kmin, kmax = -(Ti - 1), (Tj - 1)
    if max_shift is not None:
        kmin, kmax = max(kmin, -max_shift), min(kmax, max_shift)
    kern = np.ones(smooth) / smooth if smooth > 1 else None
    grow = max(0.5, thresh - 0.2)

    def _grow_run(ds, a, b):
        gap = 0
        i = a - 1
        while i >= 0:
            if ds[i] >= grow:
                a, gap = i, 0
            else:
                gap += 1
                if gap > max_gap:
                    break
            i -= 1
        gap = 0
        j = b
        while j < ds.size:
            if ds[j] >= grow:
                b, gap = j + 1, 0
            else:
                gap += 1
                if gap > max_gap:
                    break
            j += 1
        return a, b

    best = None                       # (rlen, score, segi, segj)
    for k in range(kmin, kmax + 1):
        diag = np.diagonal(S, offset=k)
        if diag.size < 2:
            continue
        ds = np.convolve(diag, kern, mode="same") if kern is not None else diag
        idx = np.flatnonzero(ds >= thresh)
        if idx.size == 0:
            continue
        # split into groups, allowing gaps of up to max_gap frames within a run
        splits = np.flatnonzero(np.diff(idx) > (max_gap + 1))
        for g in np.split(idx, splits + 1):
            a, b = _grow_run(ds, int(g[0]), int(g[-1]) + 1)
            rlen = b - a
            score = float(diag[a:b].mean())
            if best is None or (rlen, score) > (best[0], best[1]):
                i0, j0 = (a, a + k) if k >= 0 else (a - k, a)
                best = (rlen, score, (i0, i0 + rlen), (j0, j0 + rlen))
    if best is None:
        return None
    return best[1], best[2], best[3], best[0]


def _pairwise_clusters(feats, fps_ref, kind, min_len, max_shift, thresh, np,
                       progress=None, stop_event=None, diag_out=None,
                       prog_lo=0.6, prog_hi=0.98):
    """Match every pair of episodes in `feats` ({path: (M, fps, offset)}) and
    union-find them into clusters that share a recurring segment. Returns a
    list of cluster dicts (see detect_recurring_segments) or None if stopped.
    diag_out[kind] = (best_len_sec, best_score) of the best near-miss."""
    paths = list(feats.keys())
    min_frames = max(3, int(min_len * fps_ref))
    max_frames = int(max_shift * fps_ref)
    max_gap = max(3, int(0.5 * fps_ref))          # bridge dips up to ~0.5 s
    parent = {p: p for p in paths}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    best_seen = (0.0, 0.0)                          # (len_sec, score) - diagnostics
    pair = {}
    total = len(paths) * (len(paths) - 1) // 2
    done = 0
    for a in range(len(paths)):
        for b in range(a + 1, len(paths)):
            if stop_event is not None and stop_event.is_set():
                return None
            done += 1
            if progress and total:
                progress(prog_lo + (prog_hi - prog_lo) * done / total,
                         f"Matching episodes ({kind})")
            pa, pb = paths[a], paths[b]
            r = _shared_segment(feats[pa][0], feats[pb][0], np, thresh=thresh,
                                max_gap=max_gap, max_shift=max_frames)
            if not r:
                continue
            score, segi, segj, rlen = r
            seg_sec = rlen / fps_ref
            if seg_sec > best_seen[0]:
                best_seen = (seg_sec, score)
            if rlen >= min_frames:
                pair[(pa, pb)] = (score, segi, segj)
                parent[find(pa)] = find(pb)

    if diag_out is not None:
        diag_out[kind] = best_seen

    groups = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ranges, scores = {}, []
        for m in members:
            seg = None
            for (pa, pb), r in pair.items():
                if pa == m:
                    seg = r[1]
                    break
                if pb == m:
                    seg = r[2]
                    break
            if seg is None:
                continue
            M, fps, off = feats[m]
            ranges[m] = (off + seg[0] / fps, off + seg[1] / fps)
        for (pa, pb), r in pair.items():
            if pa in ranges and pb in ranges:
                scores.append(r[0])
        if not ranges:
            continue
        clusters.append({
            "kind": kind,
            "members": [m for m in members if m in ranges],
            "ranges": ranges,
            "score": float(sum(scores) / len(scores)) if scores else 0.0,
            "count": len(ranges),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def _match_known_templates(feats, tpl_paths, kind, fps_ref, thresh, np, librosa,
                           hop, progress=None, stop_event=None,
                           prog_lo=0.0, prog_hi=0.0, min_cover=0.8, lang=None):
    """Match episodes against EXISTING template clips (the ones already cut
    into the input folders). An episode 'covered' by a template joins that
    template's cluster (marked with 'known' = template filename) and is
    excluded from new-variant clustering - so a full-season scan stays fast
    and the result cleanly separates 'already have' from 'new variant'.
    Returns (clusters, covered_paths) or (None, None) if stopped."""
    if not tpl_paths:
        return [], set()
    max_gap = max(3, int(0.5 * fps_ref))
    tplf = {}
    for p in tpl_paths:
        M, fps, _off = _episode_features(p, 600.0, False, np, librosa, hop=hop, lang=lang)
        if M is not None and M.shape[1] >= 4:
            tplf[p] = M
    if not tplf:
        return [], set()
    best_for = {}                 # episode -> (score, tpl_path, (start, end))
    n = len(feats)
    for i, (ep, (Me, fps, off)) in enumerate(feats.items()):
        if stop_event is not None and stop_event.is_set():
            return None, None
        if progress:
            progress(prog_lo + (prog_hi - prog_lo) * i / max(1, n),
                     f"Matching known {kind} templates")
        for tp, Mt in tplf.items():
            r = _shared_segment(Mt, Me, np, thresh=thresh, max_gap=max_gap,
                                max_shift=None)
            if not r:
                continue
            score, segi, segj, rlen = r
            if rlen < min_cover * Mt.shape[1]:   # needs to cover most of the template
                continue
            if ep not in best_for or score > best_for[ep][0]:
                tpl_dur = Mt.shape[1] / fps
                s = max(0.0, off + (segj[0] - segi[0]) / fps)
                best_for[ep] = (score, tp, (s, s + tpl_dur))
    clusters = []
    for tp in tplf:
        mem = {ep: b for ep, b in best_for.items() if b[1] == tp}
        if not mem:
            continue
        clusters.append({
            "kind": kind,
            "known": os.path.basename(tp),
            "known_path": tp,
            "members": list(mem),
            "ranges": {ep: b[2] for ep, b in mem.items()},
            "score": float(sum(b[0] for b in mem.values()) / len(mem)),
            "count": len(mem),
        })
    return clusters, set(best_for)


def detect_recurring_segments(files, kinds=("intro", "credits"), window=240.0,
                              min_lens=None, max_shift=None, thresh=0.8, hop=2048,
                              progress=None, stop_event=None, diag_out=None,
                              known=None, lang=None):
    """Find the segment(s) that recur across `files`. kinds may include:
        'intro'        - near the start of each episode
        'preintro'     - a recurring bit BEFORE each episode's intro
                         (recap jingle / studio logo)
        'credits'      - near the end
        'aftercredits' - a recurring bit AFTER the credits (teaser/preview)
    Returns a list of clusters, each:
        {"kind", "members":[path...], "ranges":{path:(start,end)},
         "score", "count"}
    A show with several different openings yields several clusters. Times are
    absolute seconds in each file. min_lens is a {kind: seconds} dict.
    Pre-intro needs the intro's position to bound its search region (and
    after-credits the credits'), so those are detected internally even when
    not requested. diag_out (a dict) gets diag_out[kind] = (best_len,
    best_score) of the best near-miss, plus '<kind>_files' = how many episodes
    had a usable search region for preintro/aftercredits.

    known: optional {kind: [template file paths]} of templates the user has
    already cut. Episodes matching one are grouped under that template
    (cluster gets 'known' = its filename) and skipped by the new-variant
    clustering - see _match_known_templates."""
    import numpy as np
    import librosa

    min_lens = min_lens or {}
    known = known or {}
    # episodes' recap lengths can differ by minutes, so the allowed start-time
    # shift between two episodes' shared segment must scale with the window
    # (the old fixed 90 s rejected real matches in shows with long recaps)
    if max_shift is None:
        max_shift = max(90.0, 0.75 * window)
    kinds = [k for k in ("preintro", "intro", "credits", "aftercredits") if k in kinds]
    want_start = any(k in ("preintro", "intro") for k in kinds)
    want_end = any(k in ("credits", "aftercredits") for k in kinds)
    if not (want_start or want_end):
        return []
    MARGIN = 0.5     # seconds kept clear of the intro/credits boundary

    # progress bands per side; decoding is the slower part of each
    bands, lo = {}, 0.02
    span = 0.96 / ((1 if want_start else 0) + (1 if want_end else 0))
    if want_start:
        bands["start"] = (lo, lo + span * 0.55, lo + span)
        lo += span
    if want_end:
        bands["end"] = (lo, lo + span * 0.55, lo + span)

    def _decode(from_end, b0, b1):
        feats, fps_ref = {}, None
        for idx, f in enumerate(files):
            if stop_event is not None and stop_event.is_set():
                return None, None
            if progress:
                progress(b0 + (b1 - b0) * idx / max(1, len(files)),
                         f"Analysing {os.path.basename(f)}")
            M, fps, off = _episode_features(f, window, from_end, np, librosa, hop=hop, lang=lang)
            if M is not None and M.shape[1] >= 4:
                feats[f] = (M, fps, off)
                fps_ref = fps
        return feats, fps_ref

    out = []

    if want_start:
        b0, bm, b1 = bands["start"]
        feats, fps_ref = _decode(False, b0, bm)
        if feats is None:
            return []
        if len(feats) >= 2 and fps_ref:
            km = bm + (b1 - bm) * 0.15
            kn_intro, cov = _match_known_templates(
                feats, known.get("intro", []), "intro", fps_ref, thresh, np,
                librosa, hop, progress, stop_event, bm, km, lang=lang)
            if kn_intro is None:
                return []
            fresh = {p: f for p, f in feats.items() if p not in cov}
            new_intro = []
            if len(fresh) >= 2:
                new_intro = _pairwise_clusters(fresh, fps_ref, "intro",
                                               min_lens.get("intro", 10.0), max_shift,
                                               thresh, np, progress, stop_event,
                                               diag_out, km, b1)
                if new_intro is None:
                    return []
            intro_cl = kn_intro + new_intro
            if "intro" in kinds:
                out += intro_cl
            if "preintro" in kinds:
                min_pre = min_lens.get("preintro", 4.0)
                kn_pre, cov_p = _match_known_templates(
                    feats, known.get("preintro", []), "preintro", fps_ref,
                    thresh, np, librosa, hop, progress, stop_event, b1, b1, lang=lang)
                if kn_pre is None:
                    return []
                out += kn_pre
                starts = {}      # each episode's earliest detected intro start
                for c in intro_cl:
                    for m, (s, _e) in c["ranges"].items():
                        starts[m] = min(starts.get(m, s), s)
                sub = {}
                for m, bnd in starts.items():
                    if m in cov_p:
                        continue
                    M, fps, off = feats[m]
                    cut = int((bnd - off - MARGIN) * fps)
                    if cut >= max(3, int(min_pre * fps)):
                        sub[m] = (M[:, :cut], fps, off)
                if diag_out is not None:
                    diag_out["preintro_files"] = len(sub) + len(cov_p)
                if len(sub) >= 2:
                    pre = _pairwise_clusters(sub, fps_ref, "preintro", min_pre,
                                             max_shift, thresh, np, progress,
                                             stop_event, diag_out, b1 - 0.01, b1)
                    if pre is None:
                        return []
                    out += pre

    if want_end:
        b0, bm, b1 = bands["end"]
        feats, fps_ref = _decode(True, b0, bm)
        if feats is None:
            return []
        if len(feats) >= 2 and fps_ref:
            km = bm + (b1 - bm) * 0.15
            kn_cred, cov = _match_known_templates(
                feats, known.get("credits", []), "credits", fps_ref, thresh, np,
                librosa, hop, progress, stop_event, bm, km, lang=lang)
            if kn_cred is None:
                return []
            fresh = {p: f for p, f in feats.items() if p not in cov}
            new_cred = []
            if len(fresh) >= 2:
                new_cred = _pairwise_clusters(fresh, fps_ref, "credits",
                                              min_lens.get("credits", 10.0), max_shift,
                                              thresh, np, progress, stop_event,
                                              diag_out, km, b1)
                if new_cred is None:
                    return []
            cred_cl = kn_cred + new_cred
            if "credits" in kinds:
                out += cred_cl
            if "aftercredits" in kinds:
                min_ac = min_lens.get("aftercredits", 4.0)
                kn_ac, cov_a = _match_known_templates(
                    feats, known.get("aftercredits", []), "aftercredits", fps_ref,
                    thresh, np, librosa, hop, progress, stop_event, b1, b1, lang=lang)
                if kn_ac is None:
                    return []
                out += kn_ac
                ends = {}        # each episode's latest detected credits end
                for c in cred_cl:
                    for m, (_s, e) in c["ranges"].items():
                        ends[m] = max(ends.get(m, e), e)
                sub = {}
                for m, bnd in ends.items():
                    if m in cov_a:
                        continue
                    M, fps, off = feats[m]
                    start_f = int((bnd - off + MARGIN) * fps)
                    if M.shape[1] - start_f >= max(3, int(min_ac * fps)):
                        sub[m] = (M[:, start_f:], fps, off + start_f / fps)
                if diag_out is not None:
                    diag_out["aftercredits_files"] = len(sub) + len(cov_a)
                if len(sub) >= 2:
                    ac = _pairwise_clusters(sub, fps_ref, "aftercredits", min_ac,
                                            max_shift, thresh, np, progress,
                                            stop_event, diag_out, b1 - 0.01, b1)
                    if ac is None:
                        return []
                    out += ac

    return out


def representative_member(cluster):
    """Pick the cluster member whose detected length is the median - the most
    'typical' episode to cut a template from or preview."""
    members = cluster["members"]
    lens = {m: cluster["ranges"][m][1] - cluster["ranges"][m][0] for m in members}
    ordered = sorted(members, key=lambda m: lens[m])
    return ordered[len(ordered) // 2]


def refine_clusters(clusters, lang=None, log=None, progress=None, stop_event=None,
                    template_mode="follow"):
    """Frame-exact edges for every cluster (engine.refine): each member's
    start and end against another member of its cluster - or, for a cluster
    covered by an existing template ('known_path'), against that clip. The
    ranges are replaced in place; each cluster gets 'refined' = how many
    edges were refined and 'edge_src' = {path: {"start", "end"}} how each
    edge was placed (engine.refine.edge_kind: 'visual' exact; 'fade' /
    'audio' approximate - worth a "check this edge" warning). template_mode:
    how a known template's edges map (engine.refine.refine_range)."""
    from .refine import refine_ranges
    from .probe import probe_video_duration
    tracks = {}

    def track_for(p):
        if not lang:
            return None
        if p not in tracks:
            tracks[p] = audio_track_for_lang(p, lang)
        return tracks[p]
    n = len(clusters)
    for i, c in enumerate(clusters):
        if stop_event is not None and stop_event.is_set():
            return
        if progress:
            progress(i / max(1, n), f"Refining {c['kind']} boundaries")
        known = None
        if c.get("known_path"):
            tdur = probe_video_duration(c["known_path"]) or probe_duration(c["known_path"])
            if not tdur:
                continue
            known = (c["known_path"], float(tdur))
        done, kinds = {}, {}
        c["ranges"] = refine_ranges(c["ranges"], log=log, label=f"{c['kind']} ",
                                    track_for=track_for, known_ref=known, done_out=done,
                                    template_mode=template_mode, src_out=kinds)
        c["edge_src"] = {p: {"start": k[0], "end": k[1]} for p, k in kinds.items()}
        c["refined"] = sum(int(a) + int(b) for a, b in done.values())


def detect_recurring(files, mode="audio", kinds=("intro", "credits"), window=240.0,
                     min_lens=None, progress=None, stop_event=None, diag_out=None,
                     log=None, refine=True, margin_frames=1, template_margin="follow", **kw):
    """detect_recurring_segments with a source choice. mode 'audio' = the
    classic audio fingerprints; 'visual' = recurring PICTURES (engine.vfp,
    intro and credits only - pre-intro / after-credits stay audio); 'both' =
    both, merged per episode (vfp.combine_clusters: overlapping results are
    confirmed, else the better one). Every cluster gets 'src'. refine (on by
    default) snaps every boundary to the exact frame (refine_clusters).
    margin_frames (0-5, default 1): after the refinement every segment is
    widened by that many frames each side (real frame times, clamped to the
    file and to the episode's other segments) so no intro / credits frame
    survives at a join; template_margin ('follow' default / 'exact' /
    'exact+margin'): clusters matched to an existing template ('known')
    follow the template's own edges, or take the content edge (+ margin)."""
    from .refine import norm_margin, norm_template_mode
    margin = norm_margin(margin_frames)
    tmode = norm_template_mode(template_margin)
    from .vfp import combine_clusters, norm_mode, recurring_video
    mode = norm_mode(mode, "audio")
    min_lens = min_lens or {}
    prog = progress or (lambda f, t="": None)
    vis_kinds = [k for k in ("intro", "credits") if k in kinds] if mode != "audio" else []
    a_kinds = list(kinds) if mode != "visual" else [k for k in kinds
                                                    if k in ("preintro", "aftercredits")]
    a_span = 0.6 if vis_kinds and a_kinds else 1.0
    prog0 = prog
    if refine:
        # the last 10 % of the progress bar is the frame-exact refinement
        def prog(f, t=""):
            prog0(0.9 * f, t)
    out = []
    if a_kinds:
        out = detect_recurring_segments(
            files, kinds=a_kinds, window=window, min_lens=min_lens,
            progress=lambda f, t="": prog(a_span * f, t), stop_event=stop_event,
            diag_out=diag_out, **kw)
        for c in out:
            c.setdefault("src", "audio")
        if stop_event is not None and stop_event.is_set():
            return out
    lo = a_span if a_kinds else 0.0
    step = (1.0 - lo) / max(1, len(vis_kinds)) if a_kinds else 1.0 / max(1, len(vis_kinds))
    for i, kind in enumerate(vis_kinds):
        b0 = lo + step * i
        vc = recurring_video(files, window, kind=kind, min_len=min_lens.get(kind, 10.0),
                             progress=lambda f, t="", b0=b0: prog(b0 + step * f, t),
                             stop_event=stop_event, log=log)
        if vc is None:
            return out
        if mode == "both":
            ac = [c for c in out if c["kind"] == kind]
            out = [c for c in out if c["kind"] != kind] + combine_clusters(ac, vc, kind)
        else:
            out += vc
    if refine and out and not (stop_event is not None and stop_event.is_set()):
        refine_clusters(out, lang=kw.get("lang"), log=log,
                        progress=lambda f, t="": prog0(0.9 + 0.1 * f, t),
                        stop_event=stop_event, template_mode=tmode)
    if out and margin and not (stop_event is not None and stop_event.is_set()):
        _margin_clusters(out, margin, tmode, log)
    return out


def _margin_clusters(clusters, n, tmode, log=None):
    """Widen every member range by n frames each side (engine.refine
    .margin_segments: real frame times, never into another segment of the
    same episode). Clusters that follow a known template ('follow') keep
    its edges as they are."""
    from .refine import margin_segments
    from .probe import probe_video_duration
    per = {}
    for i, c in enumerate(clusters):
        if c.get("known_path") and tmode == "follow":
            continue
        for p, rng in c["ranges"].items():
            per.setdefault(p, {})[i] = tuple(rng[:2])
    # the episode's other (template-followed) segments bound the margin too
    for i, c in enumerate(clusters):
        if c.get("known_path") and tmode == "follow":
            for p, rng in c["ranges"].items():
                if p in per:
                    per[p][("fixed", i)] = tuple(rng[:2])
    durs = {}
    for p in per:
        try:
            durs[p] = float(probe_video_duration(p) or probe_duration(p) or 0.0) or None
        except Exception:
            durs[p] = None
    cache = {}
    margin_segments(per, n, durs, cache, fixed=lambda k: not isinstance(k, int))
    for p, segs in per.items():
        for i, rng in segs.items():
            if isinstance(i, int):
                clusters[i]["ranges"][p] = rng
    if log:
        log(f"   [margin] {n} frame(s) added around {sum(len(s) for s in per.values())} "
            "detected segment(s)")
