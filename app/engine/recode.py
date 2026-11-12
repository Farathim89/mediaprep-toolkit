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

This module: the folders, the settings (choices, default_settings,
normalize_settings), the per-track tables and the output names. The rest:
recode_analyze.py (probe_job, new_job, detect_crop / detect_interlace),
recode_cmd.py (build_recode_cmd, encoders), recode_run.py (encode,
make_preview).
"""
import os
import re

from ..config import DATA_DIR, MEDIA_DIR, TEMP_DIR
from .encode import NVENC_CPU_FALLBACK

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


def keep_both_path(path, reserved=None):
    """'name (2).ext', 'name (3).ext' ... - the first that doesn't exist (and,
    with `reserved` from reserve_output_path, isn't claimed by another job of
    the same batch)."""
    root, ext = os.path.splitext(path)
    n = 2
    while (os.path.exists(f"{root} ({n}){ext}")
           or (reserved is not None and _name_key(f"{root} ({n}){ext}") in reserved)):
        n += 1
    return f"{root} ({n}){ext}"


def _name_key(path):
    return os.path.normcase(os.path.abspath(path))


def reserve_output_path(path, reserved, avoid_existing=False):
    """Claim an output path for ONE job of a batch so two jobs never write
    the same file (two sources named alike, or ep.mp4 + ep.mkv both -> ep.mkv).
    reserved: a set the caller keeps for the whole batch (start with set();
    it holds case-insensitive keys). If `path` is already claimed - or, with
    avoid_existing, already on disk - 'name (2).ext', 'name (3).ext' ... is
    used instead. The returned path is added to `reserved`."""
    if _name_key(path) in reserved or (avoid_existing and os.path.exists(path)):
        path = keep_both_path(path, reserved)
    reserved.add(_name_key(path))
    return path


# ======================= output picture size =======================
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
    # 4:2:0 encoders need even sizes: an odd 853x480 is cropped to 852x480
    return w - w % 2, h - h % 2


def summary_line(job):
    """One-line summary of the settings (log)."""
    s = job["settings"]
    q = f"{s['bitrate']} kb/s" if s["rate"] == "bitrate" else f"q {s['crf']}"
    ka = sum(1 for t in job["audio"] if t["keep"])
    ks = sum(1 for t in job["subs"] if t["keep"] and not t.get("burn"))
    return (f"{s['vcodec']} {q} {s['preset']}, {s['container'].upper()}, "
            f"audio {ka}/{len(job['audio'])}, subtitles {ks}/{len(job['subs'])}")
