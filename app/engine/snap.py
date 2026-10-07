"""Snap a cut point to the nearest natural boundary - a hard picture cut (scene
change), a black-frame boundary (blackdetect) or a silence edge
(silencedetect) - within a small window around it. A picture cut wins when
one is in range: segments (intros, credits) start and end on a cut, while
the audio often has a short silent gap AFTER the cut that would put a
silence-based boundary a few hundred ms late. UI-free so any tab can use it:

    new_t, reason = find_snap(path, 90.0, edge="start")
    # -> (90.112, "end of silence")  or  (None, "nothing to snap to within ±1 s")

edge="start" (the START of a segment) prefers where silence/black ENDS - the
segment begins as the sound/picture comes back; edge="end" prefers where
silence/black STARTS. Edges of the other type are only used when no preferred
one is in range. edge=None takes the nearest edge of any type."""
import re
import subprocess

from ..config import limit_cmd, popen_flags

SILENCE_NOISE = "-40dB"
SILENCE_MIN = 0.15
BLACK_MIN = 0.04
BLACK_PIX_TH = 0.10
_COINCIDE = 0.08     # a silence and a black edge this close count as one
_WINDOW_EDGE = 0.02  # edges touching the analysed window's border aren't real

_NUM = r"(-?\d+(?:\.\d+)?)"
_RE_SIL_START = re.compile(r"silence_start:\s*" + _NUM)
_RE_SIL_END = re.compile(r"silence_end:\s*" + _NUM)
_RE_PTS = re.compile(r"pts_time:\s*" + _NUM)
SCENE_TH = 0.22      # ffmpeg scene score for a hard cut (bright-to-bright cuts score ~0.28)
_RE_BLACK = re.compile(r"black_start:\s*" + _NUM + r"\s+black_end:\s*" + _NUM)


def _run(cmd, timeout):
    """ffmpeg stderr text, or None if ffmpeg is missing / timed out / failed
    (unreadable file, no such stream)."""
    try:
        proc = subprocess.run(limit_cmd(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              creationflags=popen_flags(), timeout=timeout)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stderr.decode("utf-8", errors="replace")


def _silence_edges(path, a, length, audio_track, timeout):
    """[(time, 'start'|'end')] of silences inside [a, a+length] (absolute s)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{length:.3f}",
           "-i", path, "-map", f"0:a:{int(audio_track)}?", "-vn", "-sn", "-dn",
           "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN}",
           "-f", "null", "-"]
    err = _run(cmd, timeout)
    if err is None:
        return None
    out = [(a + float(m.group(1)), "start") for m in _RE_SIL_START.finditer(err)]
    out += [(a + float(m.group(1)), "end") for m in _RE_SIL_END.finditer(err)]
    return out


def _black_edges(path, a, length, timeout):
    """[(time, 'start'|'end')] of black stretches inside [a, a+length]."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{length:.3f}",
           "-i", path, "-map", "0:v:0?", "-an", "-sn", "-dn",
           "-vf", f"scale=320:-2,blackdetect=d={BLACK_MIN}:pix_th={BLACK_PIX_TH}",
           "-f", "null", "-"]
    err = _run(cmd, timeout)
    if err is None:
        return None
    out = []
    for m in _RE_BLACK.finditer(err):
        out.append((a + float(m.group(1)), "start"))
        out.append((a + float(m.group(2)), "end"))
    return out


def _cut_edges(path, a, length, timeout):
    """Times of hard picture cuts (first frame of the new shot) inside
    [a, a+length]."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{length:.3f}",
           "-i", path, "-map", "0:v:0?", "-an", "-sn", "-dn",
           "-vf", f"scale=160:-2,select='gt(scene,{SCENE_TH})',showinfo",
           "-f", "null", "-"]
    err = _run(cmd, timeout)
    if err is None:
        return None
    return [a + float(m.group(1)) for m in _RE_PTS.finditer(err)]


def find_snap(path, t, kind=None, radius=1.0, audio_track=0, edge="start",
              timeout=60):
    """Nearest silence edge / black boundary to `t` within ±radius seconds.

    kind: 'silence', 'black', 'cut' or None/'both' (None/'both' = all three;
    a picture cut in range always wins).
    edge: 'start' (segment start: prefer where silence/black ends), 'end'
    (segment end: prefer where it starts) or None (nearest of any).
    Returns (new_t, reason) - e.g. (90.112, 'end of silence + black') - or
    (None, reason) when there is nothing to snap to / ffmpeg failed."""
    try:
        t = float(t)
        radius = max(0.05, float(radius))
    except (TypeError, ValueError):
        return None, "no valid time to snap"
    a = max(0.0, t - radius)
    length = (t + radius) - a
    use_sil = kind in (None, "both", "silence")
    use_black = kind in (None, "both", "black")
    use_cut = kind in (None, "both", "cut")

    edges, ran = [], False
    lo, hi = a, a + length
    cuts = []
    if use_cut:
        c = _cut_edges(path, a, length, timeout)
        if c is not None:
            ran = True
            cuts = c
    if use_sil:
        e = _silence_edges(path, a, length, audio_track, timeout)
        if e is not None:
            ran = True
            edges += [(x, side, "silence") for x, side in e]
    if use_black:
        e = _black_edges(path, a, length, timeout)
        if e is not None:
            ran = True
            edges += [(x, side, "black") for x, side in e]
    if not ran:
        return None, "could not analyse the file (ffmpeg missing, timed out or failed)"

    # an edge on the window's own border is just where the analysis started/
    # stopped mid-silence, not a real boundary (except the true file start)
    edges = [(x, side, src) for x, side, src in edges
             if (x > lo + _WINDOW_EDGE or a == 0.0) and x < hi - _WINDOW_EDGE
             and abs(x - t) <= radius + 1e-6]
    cuts = [x for x in cuts if lo + _WINDOW_EDGE < x < hi - _WINDOW_EDGE
            and abs(x - t) <= radius + 1e-6]
    want = {"start": "end", "end": "start"}.get(edge)
    # picture boundaries first: hard cuts + black edges of the wanted side
    pic = [(x, "cut") for x in cuts]
    pic += [(x, "black") for x, sd, src in edges if src == "black" and (not want or sd == want)]
    if pic:
        x, src = min(pic, key=lambda e: abs(e[0] - t))
        if src == "cut":
            reason = "picture cut"
        else:
            reason = f"{want} of black" if want else "black edge"
        return round(max(0.0, x), 3), reason
    if not edges:
        return None, f"nothing to snap to within ±{radius:g} s"

    pool = [e for e in edges if e[1] == want] if want else edges
    fallback = False
    if not pool:
        pool, fallback = edges, True
    x, side, src = min(pool, key=lambda e: abs(e[0] - t))
    srcs = {src} | {s for xx, sd, s in edges if sd == side and abs(xx - x) <= _COINCIDE}
    what = " + ".join(s for s in ("silence", "black") if s in srcs)
    reason = f"{side} of {what}"
    if fallback:
        reason += " - no " + ("end" if want == "end" else "start") + " edge nearby"
    return round(max(0.0, x), 3), reason
