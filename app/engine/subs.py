"""Subtitle helpers: clip lingering subtitle events to the video length and
carry stream dispositions (forced / default ...) through a concat."""
import os
import re
import json
import subprocess

from ..config import POPEN_FLAGS, TEMP_DIR
from .probe import probe_duration, probe_streams, probe_video_duration
from .process import _run_exempt


_SUB_TEXT_EXT = {"subrip": "srt", "srt": "srt", "ass": "ass", "ssa": "ass",
                 "webvtt": "vtt", "mov_text": "srt"}


def _clip_subtitle_file(fpath, vdur):
    """Clip every subtitle event in a text subtitle file so nothing displays
    past vdur: drop events that start after vdur, and shorten any event whose
    end runs past it. Handles SRT/VTT (HH:MM:SS,mmm --> ...) and ASS/SSA
    (Dialogue: ...,H:MM:SS.cc,H:MM:SS.cc,...). Rewrites the file in place."""
    def s2(t):
        t = t.strip().replace(",", ".")
        parts = t.split(":")
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except (ValueError, IndexError):
            return None

    try:
        txt = open(fpath, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    if fpath.endswith(".ass"):
        def fix_ass(m):
            a, b = s2(m.group(1)), s2(m.group(2))
            if a is None or b is None or a >= vdur:
                return m.group(0)          # leave header-ish / out-of-range as-is
            if b > vdur:
                cs = int((vdur - int(vdur)) * 100)
                b2 = f"{int(vdur)//3600}:{(int(vdur)%3600)//60:02d}:{int(vdur)%60:02d}.{cs:02d}"
                return m.group(0).replace(m.group(2), b2, 1)
            return m.group(0)
        txt = re.sub(r"(?m)^Dialogue:[^,]*,([0-9:.]+),([0-9:.]+),", fix_ass, txt)
    else:  # srt / vtt
        def to_srt(x):
            h = int(x // 3600); mm = int((x % 3600) // 60); s = x - h * 3600 - mm * 60
            return f"{h:02d}:{mm:02d}:{s:06.3f}".replace(".", ",")
        def fix_srt(m):
            a, b = s2(m.group(1)), s2(m.group(2))
            if a is None or b is None or a >= vdur:
                return m.group(0)
            return f"{m.group(1)} --> {to_srt(min(b, vdur))}"
        txt = re.sub(r"([0-9:,.]+)\s*-->\s*([0-9:,.]+)", fix_srt, txt)
    try:
        open(fpath, "w", encoding="utf-8").write(txt)
    except OSError:
        pass


def trim_container_to_video(path):
    """A copied subtitle event lingering past the picture (e.g. an 80-second
    translator-credit) inflates the container duration, freezing the file on
    its last frame for tens of seconds at the end. If the container runs well
    past the video, clip each text subtitle to the video length and remux
    (video/audio/attachments stream-copied). Returns True if it changed the
    file. Best effort - leaves the file untouched on any problem."""
    vdur = probe_video_duration(path)
    cdur = probe_duration(path)
    if not vdur or not cdur or cdur <= vdur + 2.0:
        return False
    info = probe_streams(path)
    subs = info["subtitle"] if info else []
    if not subs:
        return False

    os.makedirs(TEMP_DIR, exist_ok=True)
    base = f"_subclip_{os.getpid()}"
    extra_inputs, maps, meta, sidecars = [], [], [], []
    n_inputs = 1                           # input 0 is the source file
    # every subtitle (text or image) adds exactly one map, in order, so its
    # OUTPUT subtitle index is i - per-stream options below must use that
    for i, s in enumerate(subs):
        ext = _SUB_TEXT_EXT.get(s.get("codec"))
        if not ext:                        # image subtitle: keep as-is (copy)
            maps += ["-map", f"0:s:{i}"]
            continue
        sub_out = os.path.join(TEMP_DIR, f"{base}_{i}.{ext}")
        r = _run_exempt(["ffmpeg", "-y", "-i", path, "-map", f"0:s:{i}", sub_out])
        if r.returncode != 0 or not os.path.exists(sub_out):
            maps += ["-map", f"0:s:{i}"]    # extraction failed: fall back to copy
            continue
        _clip_subtitle_file(sub_out, vdur)
        sidecars.append(sub_out)
        extra_inputs += ["-i", sub_out]
        maps += ["-map", f"{n_inputs}:0"]
        n_inputs += 1
        # a sidecar input carries no tags/flags - restore them from the source
        lang = s.get("lang")
        if lang and lang != "und":
            meta += [f"-metadata:s:s:{i}", f"language={lang}"]
        if s.get("title"):
            meta += [f"-metadata:s:s:{i}", f"title={s['title']}"]
        disp = "+".join(d for d in ("default", "forced") if s.get(d)) or "0"
        meta += [f"-disposition:s:{i}", disp]

    if not sidecars:                        # nothing clippable (all image subs)
        return False

    tmp = os.path.join(os.path.dirname(path) or ".", base + os.path.splitext(path)[1])
    cmd = (["ffmpeg", "-y", "-i", path] + extra_inputs
           + ["-map", "0:v", "-map", "0:a?"] + maps + ["-map", "0:t?"]
           + ["-c", "copy"] + meta + [tmp])
    r = _run_exempt(cmd)
    ok = r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
    if ok:
        try:
            os.replace(tmp, path)
        except OSError:
            ok = False
    for p in sidecars + ([tmp] if not ok else []):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return ok


_KEEP_DISPOSITIONS = ("default", "forced", "hearing_impaired", "visual_impaired",
                      "comment")


def _disposition_args(ref):
    """-disposition args that copy the audio/subtitle flags of `ref` (the first
    cut segment). The concat demuxer drops flags like 'forced', so the stitched
    output would lose them; re-applying them keeps e.g. a forced 'Signs' track
    forced. Returns [] if ref can't be probed."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type:stream_disposition", "-of", "json", ref],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=POPEN_FLAGS, timeout=60)
        streams = json.loads(r.stdout or b"{}").get("streams", [])
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []
    args, idx = [], {"audio": 0, "subtitle": 0}
    for st in streams:
        ct = st.get("codec_type")
        if ct not in idx:
            continue
        disp = st.get("disposition") or {}
        flags = "+".join(d for d in _KEEP_DISPOSITIONS if disp.get(d)) or "0"
        args += [f"-disposition:{ct[0]}:{idx[ct]}", flags]
        idx[ct] += 1
    return args
