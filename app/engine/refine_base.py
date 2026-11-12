"""Frame-accurate boundary refinement (engine/refine.py): the tuning
constants, the time budget / Stop (limits, RefineAborted) and the decoding
of the short picture / sound windows the edges are searched in. UI-free."""
import contextlib
import re
import subprocess
import threading
import time

from ..config import limit_cmd, popen_flags

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
MIN_STEP = 0.5           # a follow-up window must move the boundary outward at least this far (s)
MAX_STEPS = 24           # ... and at most this many follow-up windows per boundary
CUT_MIN = 18.0           # mean abs frame change (0-255) that counts as a picture cut
BOUNDARY_BUDGET = 8.0    # wall-clock seconds one boundary may take (then: coarse, [WARN])
RANGE_BUDGET = 16.0      # ... both edges of one segment together
MARGIN_BUDGET = 8.0      # ... the safety margin of one segment (add_margin)
FILE_SNAP = 2.0          # follow mode: a template edge mapped onto the file start / end
                         # takes the file edge when the content edge is this close (s)
END_PROBE = 1.0          # a picture window is decoded this far past its end: frames there
                         # = the file goes on (at_end, exact on variable frame rates too)
# audio: sample 0 of a decode is the window's start even when the track starts
# later (a delayed / gapped track is padded with silence, not shifted)
AUDIO_ALIGN = "aresample=async=1:first_pts=0"
MARGIN_MAX = 5               # cfg['margin_frames'] range 0..MARGIN_MAX

_RE_PTS = re.compile(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?[0-9.]+(?:e[-+]?\d+)?)")


# ----------------------------------------------------------------- budget / Stop
class RefineAborted(Exception):
    """Stop was pressed (reason 'stopped') or the time budget ran out
    ('budget') - the caller keeps the coarse boundary."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


_TLS = threading.local()


@contextlib.contextmanager
def limits(seconds=None, stop_event=None):
    """Within the block every refinement step of THIS thread checks a
    wall-clock deadline (seconds from now; nested blocks keep the earlier
    one) and stop_event, and raises RefineAborted - running ffmpeg decodes
    are killed."""
    prev = getattr(_TLS, "lim", None)
    dl = time.monotonic() + float(seconds) if seconds else None
    stop = stop_event
    if prev is not None:
        if prev[0] is not None:
            dl = prev[0] if dl is None else min(dl, prev[0])
        stop = stop if stop is not None else prev[1]
    _TLS.lim = (dl, stop)
    try:
        yield
    finally:
        _TLS.lim = prev


def _check():
    """Raise RefineAborted when Stop was pressed / the budget is used up."""
    lim = getattr(_TLS, "lim", None)
    if lim is None:
        return
    dl, stop = lim
    if stop is not None and stop.is_set():
        raise RefineAborted("stopped")
    if dl is not None and time.monotonic() > dl:
        raise RefineAborted("budget")


# ----------------------------------------------------------------- decoding
class _Frames:
    """Gray frames of [t0, t1] of a file: times (app timeline, s), raw
    [N, D] float32, normalized X, flat mask, mean brightness, frame duration."""
    __slots__ = ("t", "raw", "X", "flat", "mean", "std", "fdur", "t0", "t1", "at_start",
                 "at_end")


def _run(cmd, timeout=120):
    """Run ffmpeg, capture stdout / stderr. None on failure / timeout;
    RefineAborted (process killed) on Stop / an exhausted budget."""
    _check()
    try:
        p = subprocess.Popen(limit_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, creationflags=popen_flags())
    except OSError:
        return None
    t_end = time.monotonic() + timeout
    while True:
        try:
            out, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        try:
            _check()
            late = time.monotonic() > t_end
        except RefineAborted:
            p.kill()
            p.communicate()
            raise
        if late:
            p.kill()
            p.communicate()
            return None
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


_VEND = {}                   # path -> video stream duration (or None), probed once


def _video_end(path):
    """Duration of the video stream of `path` (cached; None if unknown)."""
    if path not in _VEND:
        v = None
        try:
            from .probe import probe_video_duration
            v = probe_video_duration(path)
        except Exception:
            v = None
        if len(_VEND) > 512:
            _VEND.clear()
        _VEND[path] = float(v) if v else None
    return _VEND[path]


def decode_frames(path, t0, t1, w=VW, h=VH, fps=None):
    """Every frame of [t0, t1] (no frame-rate conversion; fps=N samples N
    frames per second instead - coarse alignment only), scaled to w x h
    gray, with its real timestamp. None if nothing could be decoded."""
    import numpy as np
    t0 = max(0.0, float(t0))
    dur = max(0.05, float(t1) - t0)
    lim = float(f"{dur:.3f}")            # frames at / after this are not in the window
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info"]
    if t0 > 0:
        base += ["-ss", f"{t0:.3f}"]
    # END_PROBE more than the window: frames there tell that the file goes on
    base += ["-i", path, "-t", f"{dur + END_PROBE:.3f}", "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", (f"fps={fps}," if fps else "") + f"scale={w}:{h}:flags=area,format=gray,showinfo"]
    tail = ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = _run(base + ["-fps_mode", "passthrough"] + tail)
    if p is not None and p.returncode != 0 and b"fps_mode" in (p.stderr or b""):
        p = _run(base + ["-vsync", "passthrough"] + tail)       # ffmpeg < 5.1
    if p is None or p.returncode != 0:
        return None
    times = [float(m.group(1)) for m in _RE_PTS.finditer(p.stderr.decode("utf-8", "replace"))]
    n_all = min(len(times), len(p.stdout) // (w * h))
    n = 0
    while n < n_all and times[n] < lim:
        n += 1
    beyond = n < n_all                   # a frame after the window came out
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
    # the window reached the end of the file when no frame came out after it
    # (frame timestamps, not the frame rate: exact on variable frame rates) -
    # unless the video stream says it lasts well past the probe (a long
    # still-frame gap of a VFR file)
    f.at_end = not beyond
    if f.at_end:
        vend = _video_end(path)
        if vend and t0 + lim + END_PROBE < vend - 0.5:
            f.at_end = False
    return f


def decode_audio(path, t0, t1, track=None):
    """Mono SR-Hz float32 samples of [t0, t1] (sample 0 = time t0 on the
    app timeline - an input seek trims to the exact time; a track that
    starts later is padded with silence), or None."""
    import numpy as np
    t0 = max(0.0, float(t0))
    dur = max(0.05, float(t1) - t0)
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if t0 > 0:
        cmd += ["-ss", f"{t0:.3f}"]
    cmd += ["-i", path, "-t", f"{dur:.3f}", "-map", f"0:a:{int(track or 0)}", "-vn", "-sn",
            "-dn", "-af", AUDIO_ALIGN, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = _run(cmd)
    if p is None or p.returncode != 0 or len(p.stdout) < SR // 2:
        return None
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


class LRUCache(dict):
    """A dict that keeps at most `maxsize` entries, dropping the least
    recently used one (for window caches that live across a batch, e.g. a
    template's decoded windows)."""

    def __init__(self, maxsize=16):
        super().__init__()
        self.maxsize = max(1, int(maxsize))
        self._lock = threading.Lock()

    def __getitem__(self, key):
        with self._lock:
            v = super().pop(key)
            super().__setitem__(key, v)          # most recent last
            return v

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                super().pop(key)
            super().__setitem__(key, value)
            while len(self) > self.maxsize:
                super().pop(next(iter(self)))


def _cached(cache, key, fn):
    if cache is None:
        return fn()
    if key in cache:
        try:
            return cache[key]
        except KeyError:                         # dropped meanwhile (LRU, other thread)
            pass
    v = fn()
    cache[key] = v
    return v


def _window(t, radius):
    return round(max(0.0, t - radius - PAD), 3), round(t + radius + PAD, 3)
