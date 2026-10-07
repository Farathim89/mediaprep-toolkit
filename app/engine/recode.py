"""Convert engine (HandBrake-style re-encode). No Tk.

A JOB is a plain dict (JSON-able, so a queued job keeps its settings):

    {"input": path, "info": probe_job(path), "settings": {...},
     "audio": [per source audio track], "subs": [per source subtitle track],
     "detect": {"crop": ..., "interlace": ...}}     # filled on demand

    settings  default_settings() - video codec / quality / filters / output
    audio[i]  {"src", "keep", "codec", "bitrate", "mix", "default", "title"}
    subs[i]   {"src", "keep", "default", "forced", "burn"}

    probe_job(path)            streams, duration, HDR, source video bitrate
    detect_crop(path)          cropdetect at ~10 points, majority vote
    detect_interlace(path)     idet at 3 points
    is_hdr(info_or_path)       HDR10 (smpte2084) / HLG (arib-std-b67)
    build_recode_cmd(job, out) ffmpeg argv + a plan (expected result)
    encode(job, out, ...)      .part output, commit, verify, size line
    make_preview(job, ...)     10 s sample + source segment + estimates
"""
import os
import re
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ..config import (DATA_DIR, MEDIA_DIR, NVENC_PRESET_MAP, POPEN_FLAGS, SVT_PRESET_MAP,
                      TEMP_DIR)
from .encode import NVENC_CPU_FALLBACK, nvenc_available
from .files import _commit_part, _part_path
from .formatting import format_seconds, format_size
from .probe import _probe_json, probe_source_video_bitrate, probe_video_duration
from .process import _run_capture_stoppable, run_ffmpeg_with_progress

# folders (created on demand by the Convert page / encode)
CONVERT_DIR = os.path.join(MEDIA_DIR, "convert")
CONVERT_INPUT = os.path.join(CONVERT_DIR, "input")
CONVERT_OUTPUT = os.path.join(CONVERT_DIR, "output")
PRESETS_DIR = os.path.join(DATA_DIR, "presets", "convert")
PREVIEW_DIR = os.path.join(TEMP_DIR, "convert_preview")

VIDEO_CODECS = ("libx264", "libx265", "libsvtav1", "h264_nvenc", "hevc_nvenc",
                "av1_nvenc", "copy")
NVENC = ("h264_nvenc", "hevc_nvenc", "av1_nvenc")
_CPU_FALLBACK = dict(NVENC_CPU_FALLBACK, av1_nvenc="libsvtav1")
# encoders that can write 10-bit (h264_nvenc can't; x264 can but "auto"
# keeps H.264 at 8-bit - Hi10P H.264 barely plays anywhere)
_TEN_BIT = ("libx265", "libsvtav1", "hevc_nvenc", "av1_nvenc", "libx264")
_TEN_BIT_AUTO = ("libx265", "libsvtav1", "hevc_nvenc", "av1_nvenc")
CODEC_TAG = {"libx264": "h264", "h264_nvenc": "h264", "libx265": "hevc",
             "hevc_nvenc": "hevc", "libsvtav1": "av1", "av1_nvenc": "av1", "copy": "copy"}

AUDIO_CODECS = ("copy", "aac", "ac3", "eac3", "libopus", "flac", "libmp3lame")
AUDIO_BITRATES = ("96k", "128k", "160k", "192k", "224k", "256k", "320k", "384k",
                  "448k", "640k")
MIXDOWNS = ("keep", "stereo", "5.1")
FPS_CHOICES = ("source", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
_FPS_EXACT = {"23.976": "24000/1001", "29.97": "30000/1001", "59.94": "60000/1001"}
HEIGHTS = (0, 2160, 1440, 1080, 720, 576, 480)
CONTAINERS = ("mkv", "mp4")
DEINTERLACE = ("off", "auto", "bwdif", "yadif")
DENOISE = ("off", "light", "medium", "strong")
DENOISE_METHODS = ("hqdn3d", "nlmeans")
_HQDN3D = {"light": "hqdn3d=2:1:2:3", "medium": "hqdn3d=3:2:2:3", "strong": "hqdn3d=7:7:5:5"}
_NLMEANS = {"light": "nlmeans=s=1.0:p=7:r=9", "medium": "nlmeans=s=2.0:p=7:r=11",
            "strong": "nlmeans=s=3.5:p=7:r=15"}

TEXT_SUBS = ("ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text")
BITMAP_SUBS = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub")
# what MP4 can't hold as a stream copy -> E-AC3 (lossy) instead
_MP4_AUDIO_BAD = ("truehd", "mlp", "vorbis", "wmav2", "wmapro", "alac_x")
_HDR_TRC = ("smpte2084", "arib-std-b67")


def default_settings():
    """A fresh Settings dict (H.265 CRF 22, MKV, every track copied)."""
    return {
        # video
        "vcodec": "libx265", "rate": "crf", "crf": 22, "bitrate": 4000,
        "preset": "medium", "depth": "auto", "cap": False,
        "fps": "source", "fps_mode": "cfr",
        "height": 0, "no_upscale": True,
        "crop": "off", "crop_l": 0, "crop_r": 0, "crop_t": 0, "crop_b": 0,
        "deint": "off", "denoise": "off", "denoise_method": "hqdn3d",
        "deblock": False, "tonemap": True,
        # output
        "container": "mkv", "out_dir": "", "pattern": "{name}",
        "subs_srt": False, "attachments": True, "chapters": True, "metadata": True,
        # rules for the per-track tables of newly added files
        "audio_codec": "copy", "audio_bitrate": "", "audio_mix": "keep",
        "audio_langs": "", "sub_langs": "",
    }


def normalize_settings(d):
    """A complete, type-checked settings dict from a saved / preset dict."""
    out = default_settings()
    for k, dflt in out.items():
        if k not in (d or {}):
            continue
        v = d[k]
        try:
            if isinstance(dflt, bool):
                v = bool(v)
            elif isinstance(dflt, int):
                v = int(float(v))
            elif isinstance(dflt, str):
                v = str(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    if out["vcodec"] not in VIDEO_CODECS + ("plex_hevc", "plex_h264"):
        out["vcodec"] = "libx265"
    out["crf"] = max(0, min(63, out["crf"]))
    out["bitrate"] = max(100, min(200000, out["bitrate"]))
    for k in ("crop_l", "crop_r", "crop_t", "crop_b"):
        out[k] = max(0, out[k])
    if out["container"] not in CONTAINERS:
        out["container"] = "mkv"
    return out


def _langs(text):
    """'eng, jpn' -> {'eng', 'jpn'} (empty = all)."""
    return {x.strip().lower() for x in re.split(r"[,; ]+", text or "") if x.strip()}


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
                  "bps": bps},
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


_AV1_NV = {}
_AV1_LOCK = threading.Lock()


def encoder_usable(enc):
    """True if this PC can run `enc` (NVENC needs an NVIDIA GPU + driver)."""
    if enc in NVENC_CPU_FALLBACK:
        return nvenc_available(enc)
    if enc != "av1_nvenc":
        return True
    with _AV1_LOCK:
        if "ok" not in _AV1_NV:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                     "-i", "color=s=256x256:d=0.1", "-frames:v", "1", "-c:v", "av1_nvenc",
                     "-f", "null", "-"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=POPEN_FLAGS, timeout=30)
                _AV1_NV["ok"] = r.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                _AV1_NV["ok"] = False
        return _AV1_NV["ok"]


def _has_filter(name, _cache={}):
    if name not in _cache:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=POPEN_FLAGS, timeout=30)
            names = {ln.split()[1] for ln in r.stdout.decode(errors="replace").splitlines()
                     if len(ln.split()) > 2 and ln.startswith(" ")}
        except (subprocess.TimeoutExpired, OSError, IndexError):
            names = set()
        for n in ("zscale", "libplacebo", "tonemap"):
            _cache[n] = n in names
    return _cache.get(name, False)


# ======================= per-track tables =======================
def _plex_audio(codec):
    return codec in ("aac", "ac3", "eac3", "mp3")


def default_bitrate(codec, channels):
    if codec in ("copy", "flac"):
        return ""
    ch = channels or 2
    if codec in ("ac3", "eac3"):
        return "640k" if ch > 2 else "224k"
    if codec == "libmp3lame":
        return "256k"
    return ("384k" if codec == "aac" else "320k") if ch > 2 else "192k" if codec == "aac" else "160k"


def tracks_from_rules(info, settings):
    """Fresh per-track tables for a file from the settings' rules (default
    codec / bitrate / mixdown, languages to keep). Returns (audio, subs)."""
    s = settings
    alangs, slangs = _langs(s.get("audio_langs")), _langs(s.get("sub_langs"))
    audio = []
    for n, a in enumerate(info.get("audio") or []):
        codec = s.get("audio_codec") or "copy"
        if codec == "plex":           # Plex direct-play: copy those, the rest -> E-AC3
            codec = "copy" if _plex_audio(a["codec"]) else "eac3"
        br = s.get("audio_bitrate") or default_bitrate(codec, a["channels"])
        if codec == "eac3" and (s.get("audio_codec") == "plex"):
            br = "640k"
        audio.append({"src": n, "keep": not alangs or a["lang"] in alangs, "codec": codec,
                      "bitrate": br, "mix": s.get("audio_mix") or "keep",
                      "default": False, "title": a["title"]})
    if audio and not any(t["keep"] for t in audio):
        audio[0]["keep"] = True           # never silently drop every track
    srcdef = [i for i, a in enumerate(info.get("audio") or []) if a["default"]]
    kept = [t for t in audio if t["keep"]]
    if kept:
        pick = next((t for t in kept if t["src"] in srcdef), kept[0])
        pick["default"] = True
    subs = []
    for n, x in enumerate(info.get("subs") or []):
        subs.append({"src": n, "keep": not slangs or x["lang"] in slangs,
                     "default": x["default"], "forced": x["forced"], "burn": False})
    return audio, subs


def new_job(path, settings, info=None):
    """A queue job for `path` (probed now unless `info` is given), or None."""
    info = info or probe_job(path)
    if not info:
        return None
    s = normalize_settings(settings)
    audio, subs = tracks_from_rules(info, s)
    return {"input": path, "info": info, "settings": s, "audio": audio, "subs": subs,
            "detect": {}}


# ======================= output naming =======================
def output_name(job):
    """File name (with extension) from the naming pattern: {name}, {codec},
    {res} (output height, e.g. 720p) - characters Windows forbids become _."""
    s = job["settings"]
    info = job["info"]
    name = os.path.splitext(os.path.basename(job["input"]))[0]
    h = _out_dims(job)[1] or info["video"]["h"]
    enc = s["vcodec"]
    codec = {"plex_hevc": "hevc", "plex_h264": "h264"}.get(enc, CODEC_TAG.get(enc, enc))
    if codec == "copy":
        codec = info["video"]["codec"]
    pat = (s.get("pattern") or "{name}").strip() or "{name}"
    try:
        stem = pat.format(name=name, codec=codec, res=f"{h}p")
    except (KeyError, IndexError, ValueError):
        stem = name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip().strip(".") or name
    return f"{stem}.{s['container']}"


def output_path(job, out_dir=None):
    d = out_dir or job["settings"].get("out_dir") or CONVERT_OUTPUT
    return os.path.join(d, output_name(job))


def keep_both_path(path):
    """'name (2).ext', 'name (3).ext' ... - the first that doesn't exist."""
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


# ======================= command builder =======================
def _ff_path(path):
    """A file path as a filter option value inside a filtergraph (two
    escaping levels: option value, then the graph)."""
    p = path.replace("\\", "/")
    for ch in "\\':":
        p = p.replace(ch, "\\" + ch)
    out = ""
    for ch in p:
        out += "\\" + ch if ch in "\\'[],;" else ch
    return out


def resolve_encoder(enc, log=None):
    """The encoder that really runs: NVENC -> CPU fallback without a GPU."""
    if enc in NVENC and not encoder_usable(enc):
        cpu = _CPU_FALLBACK[enc]
        if log:
            log(f"   [WARN] {enc} is not usable here (no NVIDIA GPU/driver?) - "
                f"using {cpu} (CPU) instead")
        return cpu
    return enc


def _crop_of(job):
    """(l, r, t, b) to remove, or None."""
    s = job["settings"]
    if s["crop"] == "manual":
        c = (s["crop_l"], s["crop_r"], s["crop_t"], s["crop_b"])
    elif s["crop"] == "auto":
        d = job.get("detect", {}).get("crop")
        if not d:
            return None
        c = (d["l"], d["r"], d["t"], d["b"])
    else:
        return None
    c = tuple(int(v) - int(v) % 2 for v in c)      # 4:2:0 needs even edges
    return c if any(c) else None


def _out_dims(job):
    """(w, h) of the output picture after crop + resize."""
    v = job["info"]["video"]
    s = job["settings"]
    if s["vcodec"] == "copy":
        return v["w"], v["h"]
    w, h = v["w"], v["h"]
    c = _crop_of(job)
    if c:
        w, h = w - c[0] - c[1], h - c[2] - c[3]
    th = int(s.get("height") or 0)
    if th and h and th != h and not (s.get("no_upscale") and th > h):
        w = int(round(w * th / h / 2.0)) * 2
        h = th
    return w, h


def _out_fps(job):
    s = job["settings"]
    if s["fps"] != "source":
        num = _FPS_EXACT.get(s["fps"])
        if num:
            a, b = num.split("/")
            return float(a) / float(b)
        return float(s["fps"])
    return job["info"]["video"]["fps"]


def _tonemap_chain(v, depth):
    trc = v["transfer"] if v["transfer"] in _HDR_TRC else "smpte2084"
    fmt = "yuv420p10le" if depth >= 10 else "yuv420p"
    if _has_filter("zscale") and _has_filter("tonemap"):
        prim = v["primaries"] or "bt2020"
        mat = v["matrix"] if v["matrix"] in ("bt2020nc", "bt2020c") else "bt2020nc"
        return (f"zscale=tin={trc}:pin={prim}:min={mat}:t=linear:npl=100,format=gbrpf32le,"
                "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv"), "zscale + tonemap (hable)"
    if _has_filter("libplacebo"):
        return (f"libplacebo=tonemapping=hable:colorspace=bt709:color_primaries=bt709:"
                f"color_trc=bt709:range=tv:format={fmt}"), "libplacebo (hable)"
    return None, "no tone-mapping filter in this ffmpeg"


def build_recode_cmd(job, output, log=None, sample=None):
    """ffmpeg argv for `job` writing `output` (container from its extension).
    sample=(start, length): only that part (Preview). Returns (cmd, plan);
    plan = {"encoder", "fps", "duration", "audio": n, "subs": n, "w", "h",
    "tonemap": bool, "notes": [log lines]}."""
    log = log or (lambda m: None)
    info, s = job["info"], job["settings"]
    v = info["video"]
    mp4 = output.lower().endswith(".mp4")
    notes = []
    plan = {"tonemap": False, "notes": notes}
    enc = s["vcodec"]
    plex = enc in ("plex_hevc", "plex_h264")
    if plex:
        enc = "hevc_nvenc" if enc == "plex_hevc" else "h264_nvenc"
    enc = resolve_encoder(enc, log)
    plan["encoder"] = enc
    copy_v = enc == "copy"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    # a sample: fast seek to 10 s before, then an exact output -ss - an input
    # -ss alone starts COPIED audio at the earlier keyframe (out of sync)
    coarse = max(0.0, sample[0] - 10.0) if sample else 0.0
    if sample and coarse:
        cmd += ["-ss", f"{coarse:.3f}"]
    cmd += ["-i", job["input"]]
    if sample:
        cmd += ["-ss", f"{sample[0] - coarse:.3f}", "-t", f"{sample[1]:.3f}"]

    # ---------------- video ----------------
    depth = 8
    if not copy_v:
        want = s["depth"]
        if want == "auto":
            depth = 10 if v["bit_depth"] >= 10 and enc in _TEN_BIT_AUTO else 8
        else:
            depth = 10 if want == "10" else 8
        if depth == 10 and enc not in _TEN_BIT:
            notes.append(f"{enc} is 8-bit only - 10-bit not possible, using 8-bit")
            depth = 8
    plan["depth"] = depth
    pre, post = [], []          # filters before / after a PGS overlay
    burn = [t for t in job["subs"] if t.get("burn")]
    burn = burn[0] if burn else None
    bsrc = info["subs"][burn["src"]] if burn and burn["src"] < len(info["subs"]) else None
    if not copy_v:
        de = s["deint"]
        if de == "auto":
            det = job.get("detect", {}).get("interlace")
            de = "bwdif" if det and det.get("interlaced") else "off"
            notes.append("deinterlace auto: " + ("interlaced -> bwdif" if de == "bwdif"
                                                 else "progressive - not needed"))
        if de == "bwdif":
            pre.append("bwdif=mode=send_frame:parity=auto:deint=all")
        elif de == "yadif":
            pre.append("yadif=mode=send_frame:parity=auto:deint=all")
        c = _crop_of(job)
        if c:
            w, h = v["w"] - c[0] - c[1], v["h"] - c[2] - c[3]
            post.append(f"crop={w}:{h}:{c[0]}:{c[2]}")
        if s["tonemap"] and v["transfer"] in _HDR_TRC:
            chain, how = _tonemap_chain(v, depth)
            if chain:
                post.append(chain)
                plan["tonemap"] = True
                notes.append(f"HDR -> SDR ({v['transfer']} -> bt709): {how}")
            else:
                notes.append(f"[WARN] HDR source but {how} - kept as HDR")
        if s["denoise"] in _HQDN3D:
            post.append((_NLMEANS if s["denoise_method"] == "nlmeans" else _HQDN3D)[s["denoise"]])
        if s["deblock"]:
            post.append("deblock=filter=weak:block=4")
        ow, oh = _out_dims(job)
        cw, ch = (v["w"] - c[0] - c[1], v["h"] - c[2] - c[3]) if c else (v["w"], v["h"])
        if oh != ch:
            post.append(f"scale={ow}:{oh}:flags=lanczos")
        elif s.get("height") and int(s["height"]) > ch and s.get("no_upscale"):
            notes.append(f"resize to {s['height']}p skipped (never upscale; source is {ch}p)")
        if bsrc and bsrc["text"]:
            sub_f = (f"subtitles=filename={_ff_path(job['input'])}:si={burn['src']}")
            if coarse:      # the filter reads the file from 0 - shift into its timeline
                sub_f = f"setpts=PTS+{coarse:.3f}/TB,{sub_f},setpts=PTS-{coarse:.3f}/TB"
            post.append(sub_f)
            notes.append(f"burn-in: subtitle #{burn['src'] + 1} ({bsrc['codec']})")
        fps = s["fps"]
        if fps != "source" and s["fps_mode"] == "cfr":
            post.append(f"fps={_FPS_EXACT.get(fps, fps)}")
        pix = ("p010le" if depth >= 10 else "yuv420p") if enc in NVENC else (
            "yuv420p10le" if depth >= 10 else "yuv420p")
        post.append(f"format={pix}")
        plan["w"], plan["h"] = ow, oh
        vin = f"[0:{v['idx']}]"
        if bsrc and not bsrc["text"]:
            graph = (f"{vin}{','.join(pre) or 'null'}[pv];"
                     f"[pv][0:{bsrc['idx']}]overlay=eof_action=pass[ov];"
                     f"[ov]{','.join(post)}[vout]")
            notes.append(f"burn-in: subtitle #{burn['src'] + 1} ({bsrc['codec']}, overlay)")
        else:
            graph = f"{vin}{','.join(pre + post)}[vout]"
        cmd += ["-filter_complex", graph, "-map", "[vout]"]
    else:
        cmd += ["-map", f"0:{v['idx']}"]
        plan["w"], plan["h"] = v["w"], v["h"]
        if burn or _crop_of(job) or s.get("height") or s["deint"] != "off" or \
                s["denoise"] != "off" or s["deblock"]:
            notes.append("[WARN] video is copied - crop / resize / filters / burn-in are ignored")

    # encoder args
    q = int(s["crf"])
    cap = None
    if not copy_v and s["cap"] and v.get("bps"):
        cap = int(v["bps"] * 0.95)
    if copy_v:
        cmd += ["-c:v", "copy"]
    elif enc in NVENC:
        cmd += ["-c:v", enc, "-preset", NVENC_PRESET_MAP.get(s["preset"], "p5"),
                "-tune", "hq", "-rc", "vbr"]
        if plex:
            cmd += ["-multipass", "fullres"]
        if s["rate"] == "bitrate":
            cmd += ["-b:v", f"{int(s['bitrate'])}k"]
        else:
            cmd += ["-cq", str(q), "-b:v", "0"]
        if enc == "h264_nvenc":
            cmd += ["-profile:v", "high"]
    elif enc == "libsvtav1":
        cmd += ["-c:v", enc, "-preset", SVT_PRESET_MAP.get(s["preset"], "8")]
        cmd += (["-b:v", f"{int(s['bitrate'])}k"] if s["rate"] == "bitrate"
                else ["-crf", str(q)])
    else:
        cmd += ["-c:v", enc, "-preset", s["preset"]]
        cmd += (["-b:v", f"{int(s['bitrate'])}k"] if s["rate"] == "bitrate"
                else ["-crf", str(q)])
        if enc == "libx265":
            cmd += ["-x265-params", "log-level=error"]
    if cap and not copy_v:
        if s["rate"] == "bitrate" and int(s["bitrate"]) * 1000 > cap:
            cmd[cmd.index("-b:v") + 1] = str(cap)
        cmd += ["-maxrate:v", str(cap), "-bufsize:v", str(2 * cap)]
        notes.append(f"never bigger: video capped at {cap / 1e6:.1f} Mb/s "
                     f"(source {v['bps'] / 1e6:.1f})")
    elif s["cap"] and not copy_v:
        notes.append("[WARN] never bigger: source video bitrate unknown - no cap")
    if plan["tonemap"]:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if not copy_v and mp4 and enc in ("libx265", "hevc_nvenc"):
        cmd += ["-tag:v", "hvc1"]
    out_fps = _out_fps(job)
    if s["fps"] != "source" and s["fps_mode"] == "vfr" and not copy_v:
        cmd += ["-fpsmax", _FPS_EXACT.get(s["fps"], s["fps"]), "-fps_mode", "vfr"]
        out_fps = None
    elif not copy_v:
        cmd += ["-fps_mode", "vfr" if s["fps_mode"] == "vfr" else "cfr"]
        if s["fps_mode"] == "vfr":
            out_fps = None
    plan["fps"] = out_fps

    # ---------------- audio ----------------
    na = 0
    for t in job["audio"]:
        if not t.get("keep") or t["src"] >= len(info["audio"]):
            continue
        a = info["audio"][t["src"]]
        codec, br, mix = t.get("codec") or "copy", t.get("bitrate") or "", t.get("mix") or "keep"
        ch = a["channels"] or 2
        want_ch = {"stereo": 2, "5.1": 6}.get(mix)
        if want_ch and want_ch >= ch:
            want_ch = None             # never up-mix
        if codec == "copy" and want_ch:
            codec = "aac"
            br = br or default_bitrate("aac", want_ch)
            notes.append(f"audio #{t['src'] + 1}: mixdown needs a re-encode -> AAC")
        if codec == "copy" and mp4 and (a["codec"] in _MP4_AUDIO_BAD
                                        or a["codec"].startswith("pcm_")):
            codec, br = "eac3", "640k"
            notes.append(f"audio #{t['src'] + 1}: MP4 can't hold {a['codec']} -> E-AC3 640k")
        if codec in ("ac3", "eac3") and (want_ch or ch) > 6:
            want_ch = 6
        cmd += ["-map", f"0:{a['idx']}", f"-c:a:{na}", codec]
        if codec not in ("copy", "flac") and br:
            cmd += [f"-b:a:{na}", br]
        if want_ch:
            cmd += [f"-ac:a:{na}", str(want_ch)]
        if codec == "libopus" and (want_ch or ch) == 6:
            cmd += [f"-filter:a:{na}", "aformat=channel_layouts=5.1"]
        cmd += [f"-metadata:s:a:{na}", f"language={a['lang']}",
                f"-metadata:s:a:{na}", f"title={t.get('title') or ''}",
                f"-disposition:a:{na}", "default" if t.get("default") else "0"]
        na += 1
    plan["audio"] = na

    # ---------------- subtitles ----------------
    ns = 0
    dropped = []
    for t in job["subs"]:
        if not t.get("keep") or t.get("burn") or t["src"] >= len(info["subs"]):
            continue
        if sample:          # a preview is about the picture / sound (copied
            continue        # subtitles that began before -ss shift its start)
        x = info["subs"][t["src"]]
        if mp4:
            if not x["text"]:
                dropped.append(f"#{t['src'] + 1} {x['codec']}")
                continue
            codec = "mov_text"
            if x["codec"] != "mov_text":
                notes.append(f"subtitle #{t['src'] + 1}: {x['codec']} -> mov_text (MP4)")
        elif x["codec"] == "mov_text" or (s["subs_srt"] and x["text"] and x["codec"] != "subrip"):
            codec = "srt"
            notes.append(f"subtitle #{t['src'] + 1}: {x['codec']} -> SRT")
        else:
            codec = "copy"
        flags = "+".join(f for f, on in (("default", t.get("default")),
                                         ("forced", t.get("forced"))) if on) or "0"
        cmd += ["-map", f"0:{x['idx']}", f"-c:s:{ns}", codec,
                f"-metadata:s:s:{ns}", f"language={x['lang']}",
                f"-disposition:s:{ns}", flags]
        if x["title"]:
            cmd += [f"-metadata:s:s:{ns}", f"title={x['title']}"]
        ns += 1
    if dropped:
        notes.append(f"[WARN] MP4 can't hold bitmap subtitles - dropped {', '.join(dropped)}"
                     " (use MKV or burn one in)")
    plan["subs"] = ns

    # ---------------- attachments / chapters / metadata ----------------
    if info["attachments"] and not sample:
        if mp4:
            if s["attachments"]:
                notes.append(f"[WARN] MP4 can't hold attachments - {info['attachments']} "
                             "font(s) / attachment(s) dropped")
        elif s["attachments"]:
            cmd += ["-map", "0:t?"]      # last - after the subtitle streams
    if sample:
        cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += ["-map_chapters", "0" if s["chapters"] and not sample else "-1",
            "-map_metadata", "0" if s["metadata"] else "-1"]
    if mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "4096", output]
    plan["duration"] = (sample[1] if sample else info.get("vduration") or info["duration"]) or 0
    return cmd, plan


def summary_line(job):
    """One-line summary of the settings (log)."""
    s = job["settings"]
    q = f"{s['bitrate']} kb/s" if s["rate"] == "bitrate" else f"q {s['crf']}"
    ka = sum(1 for t in job["audio"] if t["keep"])
    ks = sum(1 for t in job["subs"] if t["keep"] and not t.get("burn"))
    return (f"{s['vcodec']} {q} {s['preset']}, {s['container'].upper()}, "
            f"audio {ka}/{len(job['audio'])}, subtitles {ks}/{len(job['subs'])}")


# ======================= run =======================
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


def verify_recode(job, out, plan):
    """Check the written file: track counts, resolution, duration, colour.
    Returns a list of warning strings (empty = OK)."""
    o = probe_job(out)
    if not o:
        return ["could not probe the output"]
    warns = []
    if len(o["audio"]) != plan["audio"]:
        warns.append(f"audio tracks: expected {plan['audio']}, got {len(o['audio'])}")
    if len(o["subs"]) != plan["subs"]:
        warns.append(f"subtitle tracks: expected {plan['subs']}, got {len(o['subs'])}")
    if (o["video"]["w"], o["video"]["h"]) != (plan["w"], plan["h"]):
        warns.append(f"resolution: expected {plan['w']}x{plan['h']}, got "
                     f"{o['video']['w']}x{o['video']['h']}")
    want = plan["duration"]
    got = probe_video_duration(out) or o["duration"]
    if want and got and abs(got - want) > 1.0:
        warns.append(f"duration: expected {want:.2f}s, got {got:.2f}s")
    if plan.get("tonemap") and o["video"]["transfer"] not in ("bt709", ""):
        warns.append(f"colour: expected bt709, got {o['video']['transfer']}")
    return warns


def encode(job, output, progress=None, stop_event=None, log=None):
    """Convert one job into `output` (written as a .part sibling first, put in
    place only on success; Stop / failure removes the partial file).
    progress(frac, text). Returns {"ok", "stopped", "output", "size_in",
    "size_out", "warnings", "elapsed", "error"}."""
    log = log or (lambda m: None)
    stop_event = stop_event or threading.Event()
    t0 = time.time()
    res = {"ok": False, "stopped": False, "output": output, "size_in": job["info"]["size"],
           "size_out": 0, "warnings": [], "elapsed": 0.0, "error": ""}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    ensure_detect(job, log, stop_event)
    if stop_event.is_set():
        res["stopped"] = True
        return res
    part = _part_path(output)
    cmd, plan = build_recode_cmd(job, part, log)
    for n in plan["notes"]:
        log(f"   {n}")
    log(f"   encoder {plan['encoder']}, {plan['w']}x{plan['h']}, "
        f"{plan['audio']} audio / {plan['subs']} subtitle track(s)")
    rc = run_ffmpeg_with_progress(cmd, plan["duration"] or 1.0, fps=plan["fps"],
                                  on_progress=progress, on_log=log, stop_event=stop_event)
    ok = _commit_part(rc, part, output, log)
    res["elapsed"] = time.time() - t0
    if rc == -1 or stop_event.is_set():
        res["stopped"] = True
        log("   [STOPPED] partial output removed")
        return res
    if not ok:
        res["error"] = f"ffmpeg failed (code {rc})"
        log(f"   [FAIL] {res['error']}")
        return res
    res["ok"] = True
    res["size_out"] = os.path.getsize(output)
    warns = verify_recode(job, output, plan)
    res["warnings"] = warns
    if warns:
        for w in warns:
            log(f"   [WARN] verify: {w}")
    else:
        log("   [OK] verify: tracks, resolution and duration as expected")
    si, so = res["size_in"], res["size_out"]
    pct = f" ({so / si * 100:.0f}%)" if si else ""
    log(f"   Size: {format_size(si)} -> {format_size(so)}{pct} - "
        f"{format_seconds(res['elapsed'])}")
    if job["settings"]["cap"] and si and so > si:
        log(f"   [WARN] output is {(so / si - 1) * 100:.1f}% larger than the source")
    return res


# ======================= preview + estimate =======================
def _ssim(sample, src, start, length, plan, job, stop_event=None):
    """SSIM of the sample vs the same source segment (None when the
    pictures can't be lined up: crop / fps / deinterlace changes)."""
    s = job["settings"]
    if (_crop_of(job) or s["fps"] != "source" or s["deint"] != "off"
            or plan.get("tonemap") or any(t.get("burn") for t in job["subs"])):
        return None
    w, h = plan["w"], plan["h"]
    graph = (f"[0:v]format=yuv420p,setpts=PTS-STARTPTS[b];"
             f"[1:{job['info']['video']['idx']}]scale={w}:{h}:flags=bicubic,format=yuv420p,"
             f"setpts=PTS-STARTPTS[a];[b][a]ssim")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", sample, "-ss", f"{start:.3f}",
           "-t", f"{length:.3f}", "-i", src, "-lavfi", graph, "-f", "null", "-"]
    text = _ffmpeg_stderr(cmd, stop_event) or ""
    m = re.findall(r"All:([0-9.]+)", text)
    return float(m[-1]) if m else None


def _scaled(progress, lo, hi):
    if not progress:
        return None
    return lambda f, t: progress(lo + f * (hi - lo), t)


def make_preview(job, start=None, length=10.0, progress=None, stop_event=None, log=None,
                 out_dir=None, ssim=True, est_points=6, est_len=4.0):
    """Encode `length` s from `start` (default: the middle) with the job's
    settings into Data\temp\convert_preview, plus the same source segment
    (near-lossless H.264, for the Dual Player's A side). The size / time
    estimate also encodes `est_points` short pieces spread over the file -
    one scene says little about a whole episode (a quiet middle vs. an
    action-heavy opening differ 5x in bitrate at the same CRF).
    Returns {"ok", "sample", "source", "start", "est_size", "est_time",
    "sample_size", "ssim", "speed", "stopped"}."""
    log = log or (lambda m: None)
    stop_event = stop_event or threading.Event()
    info = job["info"]
    dur = info.get("vduration") or info["duration"] or 0
    length = min(length, dur) if dur else length
    if start is None:
        start = max(0.0, dur / 2 - length / 2)
    start = max(0.0, min(start, max(0.0, dur - length)))
    out_dir = out_dir or PREVIEW_DIR
    os.makedirs(out_dir, exist_ok=True)
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(os.path.basename(job["input"]))[0])[:60]
    ext = job["settings"]["container"]
    sample = os.path.join(out_dir, f"{stem}_sample.{ext}")
    source = os.path.join(out_dir, f"{stem}_source.mkv")
    res = {"ok": False, "stopped": False, "sample": sample, "source": source, "start": start}
    ensure_detect(job, log, stop_event)

    def piece(path, t, n, lo, hi, quiet=False):
        cmd, plan = build_recode_cmd(job, path, None if quiet else log, sample=(t, n))
        if not quiet:
            for line in plan["notes"]:
                log(f"   {line}")
        t0 = time.time()
        rc = run_ffmpeg_with_progress(cmd, n, fps=plan["fps"],
                                      on_progress=_scaled(progress, lo, hi),
                                      on_log=log, stop_event=stop_event)
        return rc, time.time() - t0, plan

    rc, wall, plan = piece(sample, start, length, 0.0, 0.4)
    if rc != 0 or stop_event.is_set():
        res["stopped"] = rc == -1 or stop_event.is_set()
        return res
    sizes, secs, walls = [os.path.getsize(sample)], [length], [wall]
    res["sample_size"] = sizes[0]
    # the estimate: + short pieces spread over the whole file
    if dur > 3 * length and est_points:
        tmp = os.path.join(out_dir, f"{stem}_est.{ext}")
        for k in range(est_points):
            t = dur * (k + 0.5) / est_points - est_len / 2
            lo = 0.4 + 0.4 * k / est_points
            rc, w, _p = piece(tmp, max(0.0, t), est_len, lo, lo + 0.4 / est_points, quiet=True)
            if rc != 0 or stop_event.is_set():
                res["stopped"] = rc == -1 or stop_event.is_set()
                return res
            sizes.append(os.path.getsize(tmp))
            secs.append(est_len)
            walls.append(w)
        try:
            os.remove(tmp)
        except OSError:
            pass
    res["est_size"] = int(sum(sizes) / sum(secs) * dur) if dur else None
    # every run pays a fixed start-up (probe, encoder / GPU init): the 10 s
    # sample vs the short pieces separate it from the per-second cost
    if len(walls) > 1 and length > est_len:
        short = sum(walls[1:]) / (len(walls) - 1)
        per_s = (walls[0] - short) / (length - est_len)
        if per_s <= 0:
            per_s = walls[0] / length
        overhead = max(0.0, short - per_s * est_len)
    else:
        per_s, overhead = max(0.01, walls[0] - 0.6) / length, 0.6
    res["speed"] = 1.0 / max(1e-3, per_s)
    res["est_time"] = overhead + per_s * dur if dur else None
    seg = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-ss", f"{start:.3f}",
           "-i", job["input"], "-t", f"{length:.3f}", "-map", f"0:{info['video']['idx']}",
           "-map", "0:a:0?", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-avoid_negative_ts", "make_zero", source]
    rc = run_ffmpeg_with_progress(seg, length, fps=info["video"]["fps"],
                                  on_progress=_scaled(progress, 0.8, 0.95),
                                  on_log=log, stop_event=stop_event)
    if rc != 0:
        res["stopped"] = rc == -1
        return res
    res["ssim"] = (_ssim(sample, job["input"], start, length, plan, job, stop_event)
                   if ssim else None)
    if progress:
        progress(1.0, "")
    res["ok"] = True
    return res
