"""Video fingerprints: a 64-bit difference hash (dHash) per frame, used to
find the same PICTURES in several files - the visual twin of the audio
fingerprints in detect.py / recurring.py. UI-free.

    h = video_hashes(path, start=0, duration=300)     # Hashes or None
    clusters = recurring_video(files, 300, kind="intro")
    start, score = match_template(tpl_hashes, episode_hashes)

Frames are decoded by ffmpeg straight to a raw 64x36 gray pipe at `fps`
(4 by default, so boundaries are accurate to +-0.25 s), cut into 8 x 9
blocks and hashed by comparing neighbouring blocks. Black / flat frames
(low block contrast) are marked invalid and never match anything, so two
black screens don't count as "the same picture"."""
import os
import subprocess

from ..config import POPEN_FLAGS
from .probe import probe_duration, probe_video_duration

FPS = 4               # frames hashed per second
W, H = 64, 36         # decode size
MAX_HAM = 10          # frames this close (of 64 bits) count as the same picture
FLAT_STD = 3.0        # block-grid std below this = black / flat frame
MODES = ("audio", "visual", "both")   # detection source option everywhere


class Hashes:
    """bits [T, 64] bool, valid [T] bool, t0 = file time of frame 0, fps."""
    __slots__ = ("bits", "valid", "t0", "fps")

    def __init__(self, bits, valid, t0, fps):
        self.bits, self.valid, self.t0, self.fps = bits, valid, float(t0), float(fps)

    def __len__(self):
        return len(self.bits)

    def time(self, i):
        return self.t0 + i / self.fps


def norm_mode(mode, default="both"):
    return mode if mode in MODES else default


def video_hashes(path, start=0.0, duration=None, fps=FPS, stop_event=None, log=None):
    """Hashes of [start, start+duration] of `path` (duration None = to the
    end), or None if nothing could be decoded / Stop was pressed."""
    import numpy as np
    log = log or (lambda m: None)
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{float(start):.3f}"]
    if duration is not None:
        cmd += ["-t", f"{float(duration):.3f}"]
    cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"fps={fps},scale={W}:{H},format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=POPEN_FLAGS)
    except OSError as e:
        log(f"   [visual] ffmpeg could not start: {e}")
        return None
    rows = np.linspace(0, H, 9).astype(int)[:-1]     # 8 row bands
    cols = np.linspace(0, W, 10).astype(int)[:-1]    # 9 column bands
    area = np.outer(np.diff(np.append(rows, H)), np.diff(np.append(cols, W))).astype(np.float32)
    bits, valid = [], []
    stopped = False
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            buf = proc.stdout.read(W * H)
            if not buf or len(buf) < W * H:
                break
            f = np.frombuffer(buf, dtype=np.uint8).reshape(H, W).astype(np.float32)
            g = np.add.reduceat(np.add.reduceat(f, rows, axis=0), cols, axis=1) / area
            bits.append((g[:, 1:] > g[:, :-1]).ravel())
            valid.append(float(g.std()) >= FLAT_STD)
    finally:
        _close(proc)
    if stopped or len(bits) < 2:
        return None
    return Hashes(np.array(bits, dtype=bool), np.array(valid, dtype=bool),
                  max(0.0, float(start or 0.0)), fps)


def _close(proc):
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


def _match_matrix(a, b, np):
    """Bool [Ta, Tb]: frame i of a looks like frame j of b (both valid)."""
    A = a.bits.astype(np.float32) * 2.0 - 1.0
    B = b.bits.astype(np.float32) * 2.0 - 1.0
    ham = (64.0 - A @ B.T) / 2.0
    return (ham <= MAX_HAM) & a.valid[:, None] & b.valid[None, :]


def shared_run(a, b, min_frames, max_gap, max_shift=None):
    """Longest run of near-identical frames between two Hashes on one
    diagonal (time shift). Up to `max_gap` mismatched frames in a row are
    bridged (fades / flashes); a run must be >= 60 % matching frames.
    Returns (score, (i0, i1), (j0, j1)) frame indexes, or None."""
    import numpy as np
    M = _match_matrix(a, b, np)
    Ti, Tj = M.shape
    kmin, kmax = -(Ti - 1), Tj - 1
    if max_shift is not None:
        kmin, kmax = max(kmin, -max_shift), min(kmax, max_shift)
    best = None                       # (len, score, i0, j0)
    for k in range(kmin, kmax + 1):
        d = np.diagonal(M, offset=k)
        idx = np.flatnonzero(d)
        if idx.size < max(2, min_frames // 2):
            continue
        splits = np.flatnonzero(np.diff(idx) > (max_gap + 1))
        for g in np.split(idx, splits + 1):
            s, e = int(g[0]), int(g[-1]) + 1
            ln = e - s
            if ln < min_frames:
                continue
            score = g.size / float(ln)
            if score < 0.6:
                continue
            if best is None or (ln, score) > (best[0], best[1]):
                i0, j0 = (s, s + k) if k >= 0 else (s - k, s)
                best = (ln, score, i0, j0)
    if best is None:
        return None
    ln, score, i0, j0 = best
    return score, (i0, i0 + ln), (j0, j0 + ln)


def recurring_video(files, window, kind="intro", min_len=10.0, fps=FPS, progress=None,
                    stop_event=None, log=None):
    """Clusters of files that share the same PICTURES in their first
    (kind intro / preintro) or last (credits / aftercredits) `window`
    seconds - the visual twin of recurring.detect_recurring_segments, same
    cluster format: [{"kind", "members", "ranges": {path: (s, e)}, "score",
    "count", "src": "visual"}]. Several clusters = several variants.
    None if stopped."""
    prog = progress or (lambda f, t="": None)
    from_end = kind in ("credits", "aftercredits")
    hashes = {}
    for i, f in enumerate(files):
        if stop_event is not None and stop_event.is_set():
            return None
        prog(0.6 * i / max(1, len(files)), f"Fingerprinting video of {os.path.basename(f)}")
        start = 0.0
        if from_end:
            dur = probe_video_duration(f) or probe_duration(f)
            if not dur:
                continue
            start = max(0.0, float(dur) - float(window))
        h = video_hashes(f, start, window, fps, stop_event, log)
        if h is not None and len(h) >= 4:
            hashes[f] = h
    if stop_event is not None and stop_event.is_set():
        return None
    paths = list(hashes)
    min_frames = max(4, int(min_len * fps))
    max_gap = max(2, int(1.0 * fps))                 # bridge ~1 s of odd frames
    max_shift = int(max(90.0, 0.75 * window) * fps)
    parent = {p: p for p in paths}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    pair = {}
    total = len(paths) * (len(paths) - 1) // 2
    done = 0
    for ia in range(len(paths)):
        for ib in range(ia + 1, len(paths)):
            if stop_event is not None and stop_event.is_set():
                return None
            done += 1
            prog(0.6 + 0.4 * done / max(1, total), f"Matching episodes ({kind}, video)")
            pa, pb = paths[ia], paths[ib]
            r = shared_run(hashes[pa], hashes[pb], min_frames, max_gap, max_shift)
            if r:
                pair[(pa, pb)] = r
                parent[find(pa)] = find(pb)
    groups = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ranges = {}
        for m in members:
            # the longest run this episode is part of gives its range
            bestr = None
            for (pa, pb), (_sc, si, sj) in pair.items():
                seg = si if pa == m else sj if pb == m else None
                if seg and (bestr is None or seg[1] - seg[0] > bestr[1] - bestr[0]):
                    bestr = seg
            if bestr:
                h = hashes[m]
                ranges[m] = (h.time(bestr[0]), h.time(bestr[1]))
        scores = [r[0] for (pa, pb), r in pair.items() if pa in ranges and pb in ranges]
        if len(ranges) >= 2:
            out.append({"kind": kind, "members": list(ranges), "ranges": ranges,
                        "score": float(sum(scores) / len(scores)) if scores else 0.0,
                        "count": len(ranges), "src": "visual"})
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def per_episode(clusters):
    """{path: (start, end, score)} - the cluster each episode belongs to."""
    out = {}
    for c in clusters:
        for p, (s, e) in c["ranges"].items():
            if p not in out or c["score"] > out[p][2]:
                out[p] = (float(s), float(e), float(c["score"]))
    return out


def combine(audio, visual, tol=0.0):
    """Combine one segment found by audio and by video ((s, e, score) or
    None each) -> ((s, e, score) | None, source, conflict). Overlapping
    results are confirmed ('audio+visual', the overlap, best score); one
    result is taken as is; two that disagree -> the higher score, flagged
    (ties go to audio)."""
    if audio and visual:
        lo, hi = max(audio[0], visual[0]), min(audio[1], visual[1])
        if hi > lo - tol:
            return (lo, max(hi, lo), max(audio[2], visual[2])), "audio+visual", False
        if visual[2] > audio[2]:
            return tuple(visual[:3]), "visual", True
        return tuple(audio[:3]), "audio", True
    if audio:
        return tuple(audio[:3]), "audio", False
    if visual:
        return tuple(visual[:3]), "visual", False
    return None, None, False


def combine_clusters(audio_cl, video_cl, kind):
    """Merge the audio and video clusters of one kind (Templates ->
    Auto-detect, mode 'both'): episodes linked by either source end up in
    one cluster; each member's range comes from combine(). Clusters that
    carry 'known' (covered by an existing template) are passed through."""
    known = [c for c in audio_cl if c.get("known")]
    a_cl = [c for c in audio_cl if not c.get("known")]
    covered = {m for c in known for m in c["ranges"]}
    v_cl = []
    for c in video_cl:
        rng = {m: r for m, r in c["ranges"].items() if m not in covered}
        if len(rng) >= 2:
            v_cl.append(dict(c, ranges=rng, members=list(rng), count=len(rng)))
    a_by, v_by = per_episode(a_cl), per_episode(v_cl)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for c in a_cl + v_cl:
        ms = list(c["ranges"])
        for m in ms[1:]:
            parent[find(m)] = find(ms[0])
    groups = {}
    for m in set(a_by) | set(v_by):
        groups.setdefault(find(m), []).append(m)
    out = list(known)
    for members in groups.values():
        if len(members) < 2:
            continue
        ranges, scores, srcs = {}, [], set()
        for m in members:
            r, src, _conf = combine(a_by.get(m), v_by.get(m))
            if r:
                ranges[m] = (r[0], r[1])
                scores.append(r[2])
                srcs.add(src)
        if len(ranges) < 2:
            continue
        src = ("audio+visual" if "audio+visual" in srcs or len(srcs) > 1
               else next(iter(srcs)))
        out.append({"kind": kind, "members": list(ranges), "ranges": ranges,
                    "score": float(sum(scores) / len(scores)), "count": len(ranges),
                    "src": src})
    return out


def match_template(tpl, ep, min_valid=4):
    """Slide template Hashes `tpl` over episode Hashes `ep`: the offset where
    the largest fraction of the template's valid frames match. Returns
    (start_seconds_in_file, score 0-1) or None (template too flat / longer
    than the search region)."""
    import numpy as np
    n_valid = int(tpl.valid.sum())
    if n_valid < min_valid or len(ep) < 2:
        return None
    M = _match_matrix(tpl, ep, np)          # [Tt, Te]
    Tt, Te = M.shape
    # offset k = episode frame aligned with template frame 0; a template may
    # hang over the region's end by up to a quarter of its length
    sums = np.array([float(np.diagonal(M, offset=k).sum())
                     for k in range(0, max(1, Te - int(Tt * 0.75)))])
    if sums.size == 0:
        return None
    best_k = int(np.argmax(sums))
    best_s = float(sums[best_k])
    score = best_s / n_valid
    # a (nearly) static template fits equally well at many offsets: where it
    # lands is a guess, so the score is cut down
    if best_s > 0 and int((sums >= 0.95 * best_s).sum()) > 4 * tpl.fps:
        score *= 0.4
    return ep.time(best_k), score
