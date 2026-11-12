"""Chapter markers: build the chapter timeline and write it (stream copy),
and carry a source's own chapters over to a cut (remap_chapters)."""
import os
import re

from ..config import TEMP_DIR
from .encode import build_stream_maps
from .files import _same_path
from .probe import _probe_json, is_stats_tag, probe_duration, stats_tag_args
from .process import _run_exempt, run_ffmpeg_with_progress, temp_tag

_EPS = 0.0005


def probe_chapters(path):
    """The chapters of `path` as ffprobe lists them (raw times), [] if none."""
    return _probe_json(path, ["-show_chapters"]).get("chapters") or []


def remap_chapters(chapters, base, ranges):
    """Source chapters (ffprobe's, raw times) clipped to the kept ranges
    [(F, E, O)] - the source span F..E (seconds from the file start; base =
    the file's start time) that plays from output time O - and moved onto
    the output timeline as [(start, end, title)]. A chapter running across
    a join stays one chapter; slivers (< 1 s) left by a cut point are
    dropped."""
    out = []
    for c in chapters:
        try:
            cs, ce = float(c["start_time"]) - base, float(c["end_time"]) - base
        except (KeyError, TypeError, ValueError):
            continue
        title = (c.get("tags") or {}).get("title") or ""
        for F, E, O in ranges:
            a, b = max(cs, F), min(ce, E)
            if b - a <= _EPS:
                continue
            oa, ob = O + a - F, O + b - F
            if out and out[-1][2] == title and abs(out[-1][1] - oa) < 0.002:
                out[-1] = (out[-1][0], ob, title)
            else:
                out.append((oa, ob, title))
    big = [c for c in out if c[1] - c[0] >= 1.0]
    return big or out


def ffmeta_escape(s):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(s))


def write_ffmetadata(path, chapters, fmt_tags=None):
    """An FFMETADATA file: the global tags fmt_tags (minus encoder /
    statistics / DURATION) and the chapters [(start, end, title)]."""
    lines = [";FFMETADATA1"]
    for k, v in (fmt_tags or {}).items():
        if k.lower() != "encoder" and not is_stats_tag(k) and k.upper() != "DURATION":
            lines.append(f"{ffmeta_escape(k)}={ffmeta_escape(v)}")
    for s, e, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(s * 1000))}",
                  f"END={int(round(e * 1000))}", f"title={ffmeta_escape(title)}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_chapters(total_duration, drops, lead_title=None):
    """drops: [(start, end, title), ...] of the detected segments. Returns a full
    timeline partition [(start, end, title), ...], gaps titled 'Content'.
    lead_title: optional title for the gap before the first segment (e.g.
    'Cold Open'), instead of 'Content'."""
    clean = sorted((max(0.0, s), min(total_duration, e), t) for s, e, t in drops if e > s)
    segs, cur = [], 0.0
    for s, e, t in clean:
        if e <= cur:            # wholly inside an earlier drop - no chapter
            continue
        if s > cur + 0.1:
            segs.append((cur, s, (lead_title if not segs and lead_title else "Content")))
        segs.append((max(s, cur), e, t))
        cur = max(cur, e)
    if total_duration > cur + 0.1:
        segs.append((cur, total_duration, "Content"))
    return segs


def add_chapters(input_file, output_file, chapters, maps=None, stop_event=None,
                 on_progress=None, on_log=None):
    """Write chapter markers into a copy of the file (stream copy, no re-encode)
    so players can offer Skip Intro / Skip Credits. maps: optional -map args
    (e.g. build_stream_maps() to drop subtitle languages; default: every
    stream except cover art, which -map 0 would turn into a 1-frame video
    track in MKV - encode.restore_covers() puts covers back).
    Pass stop_event / on_progress for a stoppable run with a progress bar.
    Returns (rc, stderr_text); rc is -1 if stopped."""
    if _same_path(input_file, output_file):
        return 1, "output path is the input file - refusing to overwrite it"
    if not maps:
        maps = build_stream_maps(input_file, data=os.path.splitext(output_file)[1].lower()
                                 not in (".mkv", ".webm"))
    os.makedirs(TEMP_DIR, exist_ok=True)
    meta = os.path.join(TEMP_DIR, f"chapters_{temp_tag()}.txt")
    lines = [";FFMETADATA1"]
    for s, e, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(s * 1000))}", f"END={int(round(e * 1000))}",
                  f"title={title}"]
    with open(meta, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    cmd = (["ffmpeg", "-y", "-i", input_file, "-i", meta]
           + list(maps)
           + ["-map_metadata", "0", "-map_chapters", "1", "-c", "copy"]
           + stats_tag_args(input_file) + [output_file])
    if stop_event is not None or on_progress is not None:
        rc = run_ffmpeg_with_progress(cmd, probe_duration(input_file) or 0,
                                      on_progress=on_progress, on_log=on_log,
                                      stop_event=stop_event)
        err = ""           # failures already reported via on_log
    else:
        r = _run_exempt(cmd)
        rc, err = r.returncode, r.stderr.decode(errors="replace")
    try:
        os.remove(meta)
    except OSError:
        pass
    return rc, err
