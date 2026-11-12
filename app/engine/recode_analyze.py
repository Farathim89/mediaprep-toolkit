"""Convert engine (engine/recode.py): what a source holds and needs -
probe_job (streams, duration, HDR, bitrate), new_job, and the automatic crop
/ interlace detection (detect_crop, detect_interlace, ensure_detect). No Tk."""
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .probe import _probe_json, probe_source_video_bitrate, probe_video_duration
from .process import _run_capture_stoppable
from .recode import TEXT_SUBS, _HDR_TRC, normalize_settings, tracks_from_rules


# ======================= probes =======================
def _fps_of(s):
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = str(s.get(key) or "")
        if "/" in v:
            n, _, d = v.partition("/")
            try:
                n, d = float(n), float(d)
                if n > 0 and d > 0:
                    return n / d
            except ValueError:
                pass
    return None


def probe_job(path):
    """Everything the Convert page shows / the builder needs about a source.
    Returns a dict, or None when the file can't be read / has no video."""
    data = _probe_json(path, ["-show_streams", "-show_format", "-show_chapters"])
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        return None
    v = vids[0]
    pix = v.get("pix_fmt") or ""
    bits = str(v.get("bits_per_raw_sample") or "")
    depth = int(bits) if bits.isdigit() and int(bits) > 0 else (
        12 if "12" in pix else 10 if "10" in pix else 8)
    try:
        dur = float(fmt.get("duration") or 0) or None
    except (TypeError, ValueError):
        dur = None
    try:
        size = int(fmt.get("size") or os.path.getsize(path))
    except (TypeError, ValueError, OSError):
        size = 0
    bps, _how = probe_source_video_bitrate(path, data)

    def tags(s):
        return {k.lower(): val for k, val in (s.get("tags") or {}).items()}

    audio, subs, atts = [], [], 0
    for s in streams:
        ct = s.get("codec_type")
        t = tags(s)
        disp = s.get("disposition") or {}
        if ct == "audio":
            audio.append({"idx": s.get("index"), "codec": (s.get("codec_name") or "?").lower(),
                          "channels": int(s.get("channels") or 0),
                          "layout": s.get("channel_layout") or "",
                          "lang": (t.get("language") or "und").lower(),
                          "title": (t.get("title") or "").strip(),
                          "default": bool(disp.get("default"))})
        elif ct == "subtitle":
            c = (s.get("codec_name") or "?").lower()
            subs.append({"idx": s.get("index"), "codec": c,
                         "lang": (t.get("language") or "und").lower(),
                         "title": (t.get("title") or "").strip(),
                         "default": bool(disp.get("default")),
                         "forced": bool(disp.get("forced")),
                         "text": c in TEXT_SUBS})
        elif ct == "attachment":
            atts += 1
    trc = (v.get("color_transfer") or "").lower()
    vt = tags(v)
    vdur = probe_video_duration(path)
    return {
        "path": path, "duration": dur, "vduration": vdur or dur, "size": size, "chapters": len(data.get("chapters") or []),
        "attachments": atts, "format": (fmt.get("format_name") or ""),
        "video": {"idx": v.get("index"), "codec": (v.get("codec_name") or "?").lower(),
                  "w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
                  "fps": _fps_of(v), "pix_fmt": pix, "bit_depth": depth,
                  "transfer": trc, "primaries": (v.get("color_primaries") or "").lower(),
                  "matrix": (v.get("color_space") or "").lower(),
                  "field_order": (v.get("field_order") or "").lower(),
                  "bps": bps,
                  # tags a filtered (re-encoded) stream loses: set again
                  "lang": (vt.get("language") or "").strip(),
                  "title": (vt.get("title") or "").strip()},
        "audio": audio, "subs": subs,
    }


def is_hdr(info_or_path):
    """True for an HDR10 (PQ / smpte2084) or HLG (arib-std-b67) source."""
    info = probe_job(info_or_path) if isinstance(info_or_path, str) else info_or_path
    return bool(info) and info["video"]["transfer"] in _HDR_TRC


def _sample_points(duration, n):
    """n times spread over the file (skipping the first / last 5 %)."""
    if not duration or duration <= 0:
        return [0.0]
    lo, hi = duration * 0.05, duration * 0.95
    if n <= 1:
        return [duration / 2]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _ffmpeg_stderr(cmd, stop_event=None):
    rc, text = _run_capture_stoppable(cmd, stop_event)
    return text if rc != -1 else None


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def detect_crop(path, info=None, points=10, stop_event=None):
    """Black-border detection: cropdetect on a few frames at ~10 points
    across the file, majority vote. Returns {"w","h","x","y","l","r","t","b",
    "votes","samples"} (l/r/t/b = 0 when nothing to crop), or None when it
    could not be measured."""
    info = info or probe_job(path)
    if not info:
        return None
    W, H = info["video"]["w"], info["video"]["h"]
    vidx = info["video"]["idx"]

    def one(t):
        if stop_event is not None and stop_event.is_set():
            return None
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{t:.3f}", "-i", path,
               "-map", f"0:{vidx}", "-vf", "cropdetect=limit=0.094:round=2:reset=0",
               "-frames:v", "8", "-an", "-sn", "-dn", "-f", "null", "-"]
        text = _ffmpeg_stderr(cmd, stop_event) or ""
        found = _CROP_RE.findall(text)
        if not found:
            return None
        w, h, x, y = (int(v) for v in found[-1])
        # an (almost) black frame crops to nothing - it can't vote
        if w < W // 3 or h < H // 3:
            return None
        return (w, h, x, y)

    with ThreadPoolExecutor(max_workers=4) as ex:
        votes = [r for r in ex.map(one, _sample_points(info["duration"], points)) if r]
    if not votes:
        return None
    (w, h, x, y), n = Counter(votes).most_common(1)[0]
    # an odd-sized remainder goes to the bottom / right edge
    return {"w": w, "h": h, "x": x, "y": y, "l": x, "t": y,
            "r": max(0, W - w - x), "b": max(0, H - h - y),
            "votes": n, "samples": len(votes)}


def crop_label(crop, W=None, H=None):
    """'letterbox' / 'pillarbox' / 'borders' / '' (nothing removed)."""
    if not crop:
        return ""
    tb = crop["t"] + crop["b"] > 0
    lr = crop["l"] + crop["r"] > 0
    return "borders" if tb and lr else "letterbox" if tb else "pillarbox" if lr else ""


_IDET_RE = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
                      r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")


def detect_interlace(path, info=None, points=3, frames=200, stop_event=None):
    """idet on `frames` frames at a few points. Returns {"interlaced": bool,
    "tff", "bff", "progressive", "undetermined"}, or None if it failed."""
    info = info or probe_job(path)
    if not info:
        return None
    tot = [0, 0, 0, 0]
    ok = False
    for t in _sample_points(info["duration"], points):
        if stop_event is not None and stop_event.is_set():
            return None
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{t:.3f}", "-i", path,
               "-map", f"0:{info['video']['idx']}", "-vf", "idet",
               "-frames:v", str(frames), "-an", "-sn", "-dn", "-f", "null", "-"]
        text = _ffmpeg_stderr(cmd, stop_event) or ""
        rows = [tuple(int(v) for v in m) for m in _IDET_RE.findall(text)]
        if rows:
            best = max(rows, key=sum)        # ffmpeg may print an empty instance too
            tot = [a + b for a, b in zip(tot, best)]
            ok = True
    if not ok:
        return None
    inter = tot[0] + tot[1]
    return {"interlaced": inter > 10 and inter > 0.5 * (inter + tot[2]),
            "tff": tot[0], "bff": tot[1], "progressive": tot[2], "undetermined": tot[3]}


def new_job(path, settings, info=None):
    """A queue job for `path` (probed now unless `info` is given), or None."""
    info = info or probe_job(path)
    if not info:
        return None
    s = normalize_settings(settings)
    audio, subs = tracks_from_rules(info, s)
    return {"input": path, "info": info, "settings": s, "audio": audio, "subs": subs,
            "detect": {}}


def ensure_detect(job, log=None, stop_event=None):
    """Run the auto crop / interlace detection the settings ask for (once
    per job - the result is cached in job['detect'])."""
    log = log or (lambda m: None)
    s = job["settings"]
    det = job.setdefault("detect", {})
    if s["vcodec"] == "copy":
        return
    if s["crop"] == "auto" and "crop" not in det:
        c = detect_crop(job["input"], job["info"], stop_event=stop_event)
        det["crop"] = c
        if c and crop_label(c):
            log(f"   auto crop: {c['w']}x{c['h']} ({crop_label(c)} removed: "
                f"L{c['l']} R{c['r']} T{c['t']} B{c['b']}, {c['votes']}/{c['samples']} samples)")
        else:
            log("   auto crop: no black borders found")
    if s["deint"] == "auto" and "interlace" not in det:
        d = detect_interlace(job["input"], job["info"], stop_event=stop_event)
        det["interlace"] = d
        if d:
            log(f"   interlace detection: TFF {d['tff']}, BFF {d['bff']}, progressive "
                f"{d['progressive']} -> {'interlaced' if d['interlaced'] else 'progressive'}")
