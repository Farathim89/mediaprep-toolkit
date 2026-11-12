"""Chapter markers: build the chapter timeline and write it (stream copy)."""
import os

from ..config import TEMP_DIR
from .encode import build_stream_maps
from .files import _same_path
from .probe import probe_duration
from .process import _run_exempt, run_ffmpeg_with_progress, temp_tag


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
           + ["-map_metadata", "0", "-map_chapters", "1", "-c", "copy", output_file])
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
