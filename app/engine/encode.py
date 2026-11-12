"""Encoder / codec resolution (NVENC check, Auto CPU/GPU), video and audio
codec arguments, the Plex-ready plan + size check and the ffmpeg cut /
keyframe-inject command builders."""
import os
import subprocess
import threading

from ..config import (AUDIO_RECODE_MAP, AUTO_CODEC_MAP, AUTO_GPU_CODEC_MAP,
                      DEFAULT_AUDIO_RECODE, limit_cmd, NVENC_PRESET_MAP, popen_flags,
                      SVT_PRESET_MAP, TEMP_DIR)
from .formatting import format_ffmpeg_timestamp, format_size
from .probe import (_probe_json, count_frames_between, probe_first_frame_at,
                    probe_source_video_bitrate, probe_start_time, probe_streams,
                    probe_video_info, stats_tag_args)
from .process import _run_exempt, run_ffmpeg_with_progress, temp_tag


# NVENC encoder -> CPU encoder of the same family, used when there's no GPU
NVENC_CPU_FALLBACK = {"h264_nvenc": "libx264", "hevc_nvenc": "libx265"}
# Plex-ready presets -> (NVENC encoder, CPU fallback)
PLEX_PRESETS = {"plex_hevc": ("hevc_nvenc", "libx265"),
                "plex_h264": ("h264_nvenc", "libx264")}
_nvenc_cache = {}
_nvenc_lock = threading.Lock()


def nvenc_available(codec="h264_nvenc"):
    """True if `codec` (h264_nvenc / hevc_nvenc) can really encode on this PC.
    ffmpeg builds list NVENC even without an NVIDIA GPU/driver, and then fail
    every file - so run a tiny one-frame test encode, once per codec per
    process (cached). Non-NVENC codecs always return True."""
    if codec not in NVENC_CPU_FALLBACK:
        return True
    with _nvenc_lock:
        if codec not in _nvenc_cache:
            try:
                # limit_cmd + popen_flags: the "Limit CPU use" caps apply here too
                r = subprocess.run(
                    limit_cmd(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                               "-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-frames:v", "1",
                               "-c:v", codec, "-f", "null", "-"]),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=popen_flags(), timeout=30)
                _nvenc_cache[codec] = r.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                _nvenc_cache[codec] = False
        return _nvenc_cache[codec]


def resolve_encoder(encoder, src_codec, log=None):
    """'auto' -> the CPU encoder of the source's codec family (AUTO_CODEC_MAP;
    unknown/H.264 -> libx264). 'auto_gpu' -> the NVENC hardware encoder of the
    same family (AUTO_GPU_CODEC_MAP; unknown/H.264 -> h264_nvenc). Explicit
    choices pass through unchanged - except that any NVENC encoder this PC
    can't run (no NVIDIA GPU/driver) falls back to the CPU encoder of the same
    family, with a [WARN] line, instead of failing every file.
    'plex_hevc' / 'plex_h264' -> hevc_nvenc / h264_nvenc when this PC can
    run them, else libx265 / libx264 (never AV1)."""
    if encoder in PLEX_PRESETS:
        nv, cpu = PLEX_PRESETS[encoder]
        chosen = nv if nvenc_available(nv) else cpu
        if log:
            log(f"   Plex-ready codec: {chosen}"
                + ("" if chosen == nv else f" ({nv} not usable here)"))
        return chosen
    if encoder == "auto":
        chosen = AUTO_CODEC_MAP.get(src_codec, "libx264")
        if log:
            log(f"   Auto codec (CPU): source is {src_codec or 'unknown'} -> {chosen}")
        return chosen
    if encoder == "auto_gpu":
        chosen = AUTO_GPU_CODEC_MAP.get(src_codec, "h264_nvenc")
        if log:
            log(f"   Auto codec (GPU): source is {src_codec or 'unknown'} -> {chosen}")
    else:
        chosen = encoder
    if chosen in NVENC_CPU_FALLBACK and not nvenc_available(chosen):
        cpu = NVENC_CPU_FALLBACK[chosen]
        if log:
            log(f"   [WARN] {chosen} is not usable here (no NVIDIA GPU/driver?)"
                f" - using {cpu} (CPU) instead")
        return cpu
    return chosen


def crf_for_encoder(encoder, cfg):
    """The right quality number for the resolved encoder: H.265/AV1 reach the
    same visual quality as H.264 at a higher CRF, so they get their own
    setting (cfg['crf_h265'], falling back to cfg['crf'])."""
    if encoder in ("libx265", "hevc_nvenc", "libsvtav1"):
        return cfg.get("crf_h265", cfg["crf"])
    return cfg["crf"]


def build_video_codec_args(encoder, crf, preset, output_file="", bit_depth=8):
    """Encoder-correct quality/preset/pixel-format arguments. crf is reused
    as CQ for NVENC (same 0-51 idea, controlled quality)."""
    if encoder in ("h264_nvenc", "hevc_nvenc"):
        args = ["-c:v", encoder, "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
                "-preset", NVENC_PRESET_MAP.get(preset, "p5")]
        if bit_depth >= 10 and encoder == "hevc_nvenc":
            args += ["-pix_fmt", "p010le"]
        else:
            args += ["-pix_fmt", "yuv420p"]  # h264_nvenc is 8-bit only
    else:
        if encoder == "libsvtav1":
            args = ["-c:v", encoder, "-crf", str(crf),
                    "-preset", SVT_PRESET_MAP.get(preset, "8")]
        else:
            args = ["-c:v", encoder, "-crf", str(crf), "-preset", preset]
        args += ["-pix_fmt", "yuv420p10le" if bit_depth >= 10 else "yuv420p"]
    # HEVC in .mp4/.mov: tag so Apple/QuickTime players recognize it
    if encoder in ("libx265", "hevc_nvenc") and \
            os.path.splitext(output_file)[1].lower() in (".mp4", ".mov"):
        args += ["-tag:v", "hvc1"]
    return args


# ======================= Plex-ready presets =======================
# Plex direct-plays these audio codecs almost everywhere -> stream copy them;
# the "transcode" set (DTS, TrueHD, FLAC, Opus, PCM) often forces a server
# transcode, so it becomes E-AC3 640k (max 5.1). Anything else is copied.
_PLEX_AUDIO_COPY = ("aac", "ac3", "eac3", "mp3")
_PLEX_AUDIO_TO_EAC3 = ("dts", "truehd", "flac", "opus", "mlp")


def plex_plan(preset_key, input_file, ui_preset="medium", log=None):
    """Everything the cut/inject builders need for a Plex-ready preset
    ('plex_hevc' / 'plex_h264') on this source: encoder, output bit depth,
    full video args (near-lossless + 'never bigger' VBV cap at 95% of the
    source video bitrate), per-track audio args and the subtitle codecs.
    Logs one summary line."""
    log = log or (lambda m: None)
    hevc = preset_key == "plex_hevc"
    data = _probe_json(input_file, ["-show_streams", "-show_format"])
    vinfo = probe_video_info(input_file)
    encoder = resolve_encoder(preset_key, vinfo["codec"])
    nv = encoder.endswith("_nvenc")
    if hevc:
        depth = 10 if vinfo["bit_depth"] >= 10 else 8
    else:
        depth = 8
        if vinfo["bit_depth"] >= 10:
            log(f"   Plex-ready H.264: {vinfo['bit_depth']}-bit source reduced to"
                " 8-bit yuv420p (Plex/TVs can't direct-play Hi10P H.264)")
    q = (19 if hevc else 18) if nv else (18 if hevc else 16)
    args = ["-c:v", encoder]
    if nv:
        args += ["-preset", "p6", "-tune", "hq", "-multipass", "fullres",
                 "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                 "-pix_fmt", "p010le" if depth >= 10 else "yuv420p"]
        if not hevc:
            args += ["-profile:v", "high"]
    else:
        args += ["-crf", str(q), "-preset", ui_preset or "medium",
                 "-pix_fmt", "yuv420p10le" if depth >= 10 else "yuv420p"]
    src_bps, how = probe_source_video_bitrate(input_file, data)
    if src_bps:
        maxrate = int(src_bps * 0.95)
        args += ["-maxrate:v", str(maxrate), "-bufsize:v", str(2 * maxrate)]
        cap = (f"cap {maxrate / 1e6:.1f} Mb/s (source {src_bps / 1e6:.1f}"
               + (", estimated)" if how == "estimated" else ")"))
    else:
        maxrate = None
        cap = "no cap"
        log(f"   [WARN] Plex-ready: source video bitrate unknown ({how})"
            " - encoding without the never-bigger cap")
    audio_args, audio_codecs, parts = [], [], []
    for i, s in enumerate(s for s in data.get("streams") or []
                          if s.get("codec_type") == "audio"):
        c = (s.get("codec_name") or "?").lower()
        ch = int(s.get("channels") or 2)
        if c in _PLEX_AUDIO_TO_EAC3 or c.startswith("pcm_"):
            audio_args += [f"-c:a:{i}", "eac3", f"-b:a:{i}", "640k",
                           f"-ac:a:{i}", str(min(ch, 6))]
            audio_codecs.append(("eac3", "640k", min(ch, 6)))
            label = f"{c.split('_')[0]}→eac3" + (" (7.1→5.1)" if ch > 6 else "")
        else:
            audio_args += [f"-c:a:{i}", "copy"]
            audio_codecs.append(None)
            label = f"{c} copy"
        if label not in parts:
            parts.append(label)
    subs = [{"codec": (s.get("codec_name") or "").lower(),
             "lang": ((s.get("tags") or {}).get("language") or "und").lower()}
            for s in data.get("streams") or [] if s.get("codec_type") == "subtitle"]
    log(f"   Plex-ready: {encoder} {'cq' if nv else 'crf'}{q}, {cap}, "
        f"{depth}-bit, audio: {', '.join(parts) or 'none'}")
    return {"encoder": encoder, "bit_depth": depth, "crf": q, "video_args": args,
            "audio_args": audio_args, "audio_codecs": audio_codecs, "subs": subs, "src_video_bps": src_bps,
            "maxrate": maxrate}


def _plex_sub_args(plan, subs_lang, shift=None):
    """MP4 text subtitles (mov_text) can't be copied into MKV -> SRT.
    shift: the cut segment's start (s). ffmpeg doesn't apply
    -output_ts_offset to RE-ENCODED subtitles (only to copied streams), so
    a converted track is rebased with the setts bitstream filter instead."""
    kept = [s for s in plan.get("subs", [])
            if not subs_lang or s["lang"] in subs_lang]
    args = []
    for i, s in enumerate(kept):
        if s["codec"] == "mov_text":
            args += [f"-c:s:{i}", "srt"]
            if shift:
                args += [f"-bsf:s:{i}", f"setts=ts=TS-{shift:.6f}/TB"]
        else:
            args += [f"-c:s:{i}", "copy"]
    return args or ["-c:s", "copy"]


def plex_size_check(final_output, src_size, kept_sec, total_sec, log):
    """Never-bigger check: compare the output with the source's share for the
    kept duration (src_size x kept/total). Only warns - no re-encode."""
    if not (os.path.exists(final_output) and total_sec and kept_sec and src_size):
        return
    share = src_size * min(1.0, kept_sec / total_sec)
    out = os.path.getsize(final_output)
    if out > share:
        log(f"   [WARN] output is {(out / share - 1) * 100:.1f}% larger than the"
            f" source share ({format_size(out)} vs {format_size(int(share))})")
    else:
        log(f"   Plex-ready size check OK: {format_size(out)} <= source share"
            f" {format_size(int(share))}")


def audio_track_codecs(audio_streams, plex=None):
    """Per source audio track, what a cut encodes it to: (codec, bitrate or
    None, channels), or None if the track is stream-copied (Plex-ready
    direct-play codecs). audio_streams: probe_audio_streams() of the source."""
    if plex:
        return list(plex.get("audio_codecs") or [])
    out = []
    for s in audio_streams:
        name = s["codec_name"]
        out_codec, bitrate = (AUDIO_RECODE_MAP.get(name) or _AUDIO_RECODE_EXTRA.get(name)
                              or DEFAULT_AUDIO_RECODE)
        ch = s["channels"]
        # ffmpeg's (E-)AC3 encoders top out at 5.1 - fold 7.1 down instead of failing
        if out_codec in ("ac3", "eac3") and ch and ch > 6:
            ch = 6
        # MPEG-1 Layer II is mono/stereo only (384k max stereo, 192k mono)
        if out_codec == "mp2":
            ch = min(ch or 2, 2)
            bitrate = "384k" if ch >= 2 else "192k"
        # libopus refuses more than 256k per channel (a mono 320k fails)
        if out_codec == "libopus" and bitrate and str(bitrate).endswith("k"):
            try:
                bitrate = f"{min(int(bitrate[:-1]), 256 * (ch or 2))}k"
            except ValueError:
                pass
        out.append((out_codec, bitrate, ch))
    return out


# source codecs the shared AUDIO_RECODE_MAP (config) doesn't list: MP2 (DVB
# TS recordings, .mpg/.vob) stays MP2 - every container that held it can
# again, while AAC is refused by MPEG-PS
_AUDIO_RECODE_EXTRA = {"mp2": ("mp2", "384k")}

# what a re-encoding cut / inject may keep the SOURCE container for: a WebM
# only holds VP8/VP9/AV1 + Opus/Vorbis, MPEG-PS (.mpg/.vob) is for MPEG-1/2
# video (our encoders are H.264/H.265/AV1), FLV for H.264 + AAC/MP3
_WEBM_VIDEO = ("libvpx-vp9", "libvpx", "libsvtav1", "libaom-av1", "av1_nvenc")
_WEBM_AUDIO = ("libopus", "libvorbis")
_MPEG_PS_EXTS = (".mpg", ".mpeg", ".vob", ".m2p", ".mpe")


def fit_container(ext, encoder, audio_codecs):
    """Output extension for a cut / inject that re-encodes the video with
    `encoder` and the audio as audio_codecs (audio_track_codecs() items, None
    = stream copy): the source's own `ext` when that container can hold
    them, else '.mkv' (e.g. a VP9 .webm cut with libx265, an .mpg)."""
    e = (ext or "").lower()
    out = [c[0] for c in audio_codecs or () if c]
    if e == ".webm":
        ok = encoder in _WEBM_VIDEO and all(a in _WEBM_AUDIO for a in out)
    elif e in _MPEG_PS_EXTS:
        ok = False
    elif e == ".flv":
        ok = (encoder in ("libx264", "h264_nvenc")
              and all(a in ("aac", "libmp3lame") for a in out))
    else:
        ok = True
    return ext if ok else ".mkv"


def _audio_codec_args(i, codec):
    """-c/-b/-ac args for output AUDIO stream i. -ac:a:N = Nth AUDIO stream; a
    bare -ac:N would hit output stream N (with -map 0 that is the video /
    another track, downmixing 5.1)."""
    out_codec, bitrate, ch = codec
    args = [f"-c:a:{i}", out_codec]
    if bitrate:
        args += [f"-b:a:{i}", bitrate]
    if ch:
        args += [f"-ac:a:{i}", str(ch)]
    return args


def build_audio_recode_args(audio_streams):
    args = []
    for i, codec in enumerate(audio_track_codecs(audio_streams)):
        args += _audio_codec_args(i, codec)
    return args


def build_audio_pass(input_file, output_file, ranges, tracks, **cb):
    """Seamless audio for a multi-piece cut: ONE ffmpeg run over the source
    that trims every kept range of each audio track (sample-accurate atrim)
    and joins them with the concat FILTER, so the pieces meet without the
    encoder-priming silence that per-piece audio encodes leave at each join.
    ranges: [(start, end)] counted from the file start (format.start_time =
    0, the timeline build_ffmpeg_cut uses; -copyts raw timestamps get that
    start time added here). tracks:
    [(source audio index, (codec, bitrate, channels))] - output audio track
    j = tracks[j]. Language, title and dispositions are carried over from
    the source track.
    Each piece is rebased on ITS range start (PTS - s0), not on its first
    sample: an audio track that starts later than the video (or has a gap)
    keeps that offset - aresample first_pts=0 fills it with silence - and
    apad makes every piece exactly its range long, so a short track can't
    pull the following pieces early (both broke A/V sync before)."""
    data = _probe_json(input_file, ["-show_streams", "-select_streams", "a"])
    src = data.get("streams") or []
    base = probe_start_time(input_file)
    n = len(ranges)
    graph, maps, args = [], [], []
    for j, (k, codec) in enumerate(tracks):
        outs = "".join(f"[s{j}_{i}]" for i in range(n))
        chain = [f"[0:a:{k}]asplit={n}{outs}" if n > 1 else f"[0:a:{k}]anull[s{j}_0]"]
        for i, (s0, s1) in enumerate(ranges):
            r0, r1 = s0 + base, s1 + base
            chain.append(f"[s{j}_{i}]atrim=start={r0:.6f}:end={r1:.6f},"
                         f"asetpts=PTS{-r0:+.6f}/TB,aresample=async=1:first_pts=0,"
                         f"apad=whole_dur={max(0.0, s1 - s0):.6f}[p{j}_{i}]")
        chain.append("".join(f"[p{j}_{i}]" for i in range(n))
                     + f"concat=n={n}:v=0:a=1[a{j}]")
        graph.append(";".join(chain))
        maps += ["-map", f"[a{j}]"]
        args += _audio_codec_args(j, codec)
        st = src[k] if k < len(src) else {}
        tags = st.get("tags") or {}
        lang = tags.get("language") or tags.get("LANGUAGE")
        if lang:
            args += [f"-metadata:s:a:{j}", f"language={lang}"]
        if tags.get("title"):
            args += [f"-metadata:s:a:{j}", f"title={tags['title']}"]
    total = sum(e - s for s, e in ranges)
    cmd = (["ffmpeg", "-y", "-copyts", "-i", input_file,
            "-filter_complex", ";".join(graph)] + maps + args
           + ["-map_metadata", "-1", "-map_chapters", "-1"]
           + stats_tag_args(input_file) + [output_file])
    return run_ffmpeg_with_progress(cmd, total, **cb)


def build_stream_maps(input_file, subs_lang=None, data=False):
    """ffmpeg -map args: the REAL video (0:V - never attached cover art,
    which -map 0 / 0:v would encode as a 1-frame video track or, in MKV,
    leave as an empty file; restore_covers() puts covers back afterwards),
    every audio track, the subtitles, attachments (fonts) and - data=True,
    for a stream copy into the source's own container - data streams (an
    MKV can't hold those). subs_lang=None (or empty) keeps every subtitle;
    otherwise only the tracks whose language is in subs_lang (a set like
    {"eng"}) - this keeps English 'forced' tracks too, since they're tagged
    eng. Every subtitle is kept if the languages can't be probed."""
    maps = ["-map", "0:V?", "-map", "0:a?"]
    info = probe_streams(input_file) if subs_lang else None
    if info:
        for i, s in enumerate(info["subtitle"]):
            if (s.get("lang") or "und").lower() in subs_lang:
                maps += ["-map", f"0:s:{i}"]
    else:
        maps += ["-map", "0:s?"]
    if data:
        maps += ["-map", "0:d?"]
    # attachments (fonts) LAST - mapping them before the subtitle streams makes
    # the muxer reject packets ("Invalid argument") under -copyts, the same
    # order -map 0 uses
    maps += ["-map", "0:t?"]
    return maps


# ======================= cover art (attached pictures) =======================
_COVER_IMG = {"mjpeg": (".jpg", "image/jpeg"), "png": (".png", "image/png"),
              "bmp": (".bmp", "image/bmp"), "gif": (".gif", "image/gif"),
              "webp": (".webp", "image/webp")}


def cover_streams(path):
    """The attached pictures (cover art) of a file: [{"index", "codec",
    "filename", "mimetype"}] - an MKV's image attachment shows up as one."""
    out = []
    for s in _probe_json(path, ["-show_streams"]).get("streams") or []:
        if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic"):
            tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
            out.append({"index": s.get("index"), "codec": (s.get("codec_name") or "").lower(),
                        "filename": tags.get("filename") or "",
                        "mimetype": tags.get("mimetype") or ""})
    return out


def restore_covers(src, out, log=None, covers=None):
    """Put the source's cover art back into a finished output (stream copy
    of `out` + the pictures; encodes map 0:V and leave covers out). MP4 /
    M4V / MOV get attached-picture streams; MKV gets image ATTACHMENTS (an
    attached_pic stream copied into MKV would become a 1-frame video track).
    Other containers: nothing. Best effort - on any problem `out` stays as
    it is (without the covers) and a [WARN] is logged. True if changed."""
    covers = cover_streams(src) if covers is None else covers
    ext = os.path.splitext(out)[1].lower()
    if not covers or ext not in (".mkv", ".mp4", ".m4v", ".mov") or not os.path.isfile(out):
        return False
    log = log or (lambda m: None)
    have = _probe_json(out, ["-show_streams"]).get("streams") or []
    if any((s.get("disposition") or {}).get("attached_pic") for s in have):
        return False                                   # already there
    n_v = sum(1 for s in have if s.get("codec_type") == "video")
    n_t = sum(1 for s in have if s.get("codec_type") == "attachment")
    tag = temp_tag()
    tmp = f"{os.path.splitext(out)[0]}.part{ext}"
    imgs, cmd = [], ["ffmpeg", "-y", "-i", out]
    if ext == ".mkv":
        extra = []
        for k, c in enumerate(covers):
            img_ext, mime = _COVER_IMG.get(c["codec"], (None, None))
            if not img_ext:
                continue
            img = os.path.join(TEMP_DIR, f"cover_{tag}_{k}{img_ext}")
            os.makedirs(TEMP_DIR, exist_ok=True)
            r = _run_exempt(["ffmpeg", "-y", "-i", src, "-map", f"0:{c['index']}", "-c", "copy",
                             "-frames:v", "1", "-f", "image2", "-update", "1", img])
            if r.returncode != 0 or not os.path.isfile(img) or not os.path.getsize(img):
                continue
            imgs.append(img)
            j = n_t + len(imgs) - 1
            extra += ["-attach", img,
                      f"-metadata:s:t:{j}", f"mimetype={c['mimetype'] or mime}",
                      f"-metadata:s:t:{j}", f"filename={c['filename'] or 'cover' + img_ext}"]
        if not imgs:
            log("   [WARN] could not copy the cover art into the output")
            return False
        cmd += ["-map", "0", "-c", "copy"] + extra
    else:
        cmd += ["-i", src, "-map", "0"]
        for c in covers:
            cmd += ["-map", f"1:{c['index']}"]
        cmd += ["-c", "copy"]
        for k in range(len(covers)):
            cmd += [f"-disposition:v:{n_v + k}", "attached_pic"]
        cmd += ["-movflags", "+faststart"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", tmp]
    r = _run_exempt(cmd)
    ok = r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
    if ok:
        try:
            os.replace(tmp, out)
        except OSError:
            ok = False
    for p in imgs + ([] if ok else [tmp]):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    log("   Cover art copied into the output" if ok else
        "   [WARN] could not copy the cover art into the output (kept without it)")
    return ok


def _even_size_vf(input_file):
    """['-vf', crop] when the source picture has an odd width / height:
    4:2:0 x264 / x265 / NVENC refuse e.g. 853x480, so the last column / row
    is cropped off (no resampling). [] otherwise."""
    v = probe_video_info(input_file)
    if v["w"] and v["h"] and (v["w"] % 2 or v["h"] % 2):
        return ["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2:0:0"]
    return []


# cut points are frame times rounded to the millisecond (detectors, the UI),
# while a container's frame timestamps may be rational (mp4 1/24000: 4.170833
# shows as 4.171). -ss / -t keep frames with start <= pts < end, so a point
# rounded UP would drop the segment's first frame / keep the frame at the
# end. Both points are moved this much earlier: well inside the gap before
# the intended frame (half a frame is >= 8 ms), far below a sync error.
CUT_EPS = 0.0006


def build_ffmpeg_cut(input_file, output_file, start_sec, end_sec, audio_streams,
                     crf, preset, kf_interval, seg_name=None, fps=None,
                     encoder="libx264", bit_depth=8, subs_lang=None, plex=None,
                     audio=True, **cb):
    """
    Frame-accurate segment cut (v18 approach):
      input -ss (start-10s) fast keyframe seek, -copyts to keep original
      timestamps, output -ss for the exact frame, -output_ts_offset -start
      to rebase the segment to 0 so segments stand alone and stitch cleanly.
      start_sec / end_sec count from the file start (format.start_time = 0,
      like the detectors, the player and the frame-count probes); a TS that
      starts at 1.48 s or 1001 s gets that start added for the raw -ss.
    subs_lang: keep only these subtitle languages (e.g. {"eng"}); None = all.
    plex: a plex_plan() dict -> its video/audio/subtitle args replace the
    encoder/crf/bit_depth ones (Plex-direct-play audio is stream-copied,
    packet-accurate, the rest -> E-AC3).
    audio=False (non-Plex only): leave the audio out - a multi-piece cut
    encodes it in one seamless pass instead (build_audio_pass).
    """
    base = probe_start_time(input_file)
    # start ON the first video frame the cut keeps, not at the requested
    # point between two frames: -t is checked on the encoder's 1/fps grid,
    # and a first frame more than half a frame after the start (a detector
    # time, or a TS whose audio starts before the video) rounds the LAST
    # frame onto the end point - it was dropped. Audio / subtitles then
    # start with the picture (< 1 frame later than asked).
    try:
        first = probe_first_frame_at(input_file, start_sec, base=base)
    except Exception:
        first = None
    if first is not None and start_sec - CUT_EPS <= first < start_sec + 0.25:
        start_sec = max(start_sec, first)
    if start_sec > CUT_EPS:
        start_sec -= CUT_EPS
    end_sec -= CUT_EPS
    seg_duration = end_sec - start_sec
    seek1 = max(0.0, start_sec - 10.0)       # input -ss counts from the file start
    # -copyts keeps the RAW timestamps (what ffprobe shows), so the output -ss
    # is start + format.start_time. (-start_at_zero is no substitute: its zero
    # is the earliest of the streams USED - a video-only piece of a TS whose
    # audio starts first would be cut one frame late.)
    raw_start = start_sec + base
    maps = build_stream_maps(input_file, subs_lang)
    # no input seek at all near the file start: an '-ss 0' seek on a TS whose
    # first video frame comes after the audio lost that first frame
    cmd = (["ffmpeg", "-y"] + (["-ss", f"{seek1:.6f}"] if seek1 > 0 else [])
           + ["-copyts", "-i", input_file,
              "-ss", f"{raw_start:.6f}", "-t", f"{seg_duration:.6f}",
              "-output_ts_offset", f"{-raw_start:.6f}"] + maps)
    # -t is checked on the ENCODER's frame-grid timestamps, which can let the
    # frame at the end point slip in: the video is limited by its real frame
    # count (start <= pts < end) as well
    try:
        n_frames = count_frames_between(input_file, start_sec, end_sec, base=base)
    except Exception:
        n_frames = None
    if n_frames:
        cmd += ["-frames:v", str(n_frames)]
    cmd += _even_size_vf(input_file)
    cmd += (list(plex["video_args"]) if plex else
            build_video_codec_args(encoder, crf, preset, output_file, bit_depth))
    # dense keyframes only for SHORT cold-open / post-credits pieces (so they
    # seek well); never across a long piece, where it just bloats the file
    if (kf_interval and kf_interval > 0 and seg_name in ("Cold Open", "Post Credits")
            and seg_duration <= _SHORT_PIECE):
        cmd += ["-force_key_frames:v", f"expr:gte(t,n_forced*{kf_interval})"]
    # NOTE: no force_key_frames otherwise. The first frame of every encoded
    # segment is automatically an IDR keyframe, which is all the cut needs.
    # The old "expr:gte(t,0)" (inherited from v15) evaluated TRUE for EVERY
    # frame, silently producing all-intra video 3-4x the normal size - this
    # was the real cause of 800 MB files ballooning to 2-3 GB.
    if plex:
        cmd += list(plex["audio_args"]) + _plex_sub_args(plex, subs_lang, raw_start)
    else:
        cmd += ((build_audio_recode_args(audio_streams) if audio else ["-an"])
                + ["-c:s", "copy", "-c:d", "copy"])
    cmd += (["-map_metadata", "0"] + stats_tag_args(input_file)
            + ["-avoid_negative_ts", "make_non_negative", output_file])
    return run_ffmpeg_with_progress(cmd, seg_duration, fps=fps,
                                    expected_offset=start_sec, **cb)


# a kept piece at most this long (s) at the very start / end can be a cold
# open / post-credits scene; anything longer is the episode itself
_SHORT_PIECE = 300.0


def build_ffmpeg_inject(input_file, output_file, kf_times, total_duration,
                        crf, preset, fps=None, encoder="libx264", bit_depth=8,
                        subs_lang=None, plex=None, **cb):
    """Re-encode the whole file with forced keyframes at the given times.
    plex: a plex_plan() dict (see build_ffmpeg_cut)."""
    # (no -copyts: the output counts from the file start, like kf_times)
    cmd = (["ffmpeg", "-y", "-i", input_file] + build_stream_maps(input_file, subs_lang)
           + _even_size_vf(input_file))
    if plex:
        cmd += list(plex["video_args"])
    else:
        cmd += build_video_codec_args(encoder, crf, preset, output_file, bit_depth)
    if kf_times:
        cmd += ["-force_key_frames", ",".join(format_ffmpeg_timestamp(t) for t in kf_times)]
    if plex:
        cmd += list(plex["audio_args"]) + _plex_sub_args(plex, subs_lang)
    else:
        cmd += ["-c:a", "copy", "-c:s", "copy", "-c:d", "copy"]
    cmd += ["-map_metadata", "0"] + stats_tag_args(input_file) + [output_file]
    return run_ffmpeg_with_progress(cmd, total_duration, fps=fps, **cb)
