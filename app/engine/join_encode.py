"""Join engine (engine/join.py): the re-encode way - every file decoded,
brought to ONE format and joined by ffmpeg's concat filter. No Tk.

    video   the first file's display size (others scaled with the aspect
            kept, letter- / pillarboxed), square pixels, the first file's
            frame rate, Convert's encoder settings (codec, CRF / CQ,
            preset, bit depth, NVENC with CPU fallback, Plex-ready)
    audio   output track i = track i of each file, or silence for a file
            without it - so tracks stay aligned; 48 kHz, the channel layout
            of the first file that has the track; codec from the settings'
            audio rule
    subs    text subtitle track i, re-timed and merged, when EVERY file has
            it (else dropped, with a log line); picture subtitles (PGS /
            VobSub / DVB) stream-copied piece by piece, a file without the
            track has none there (MKV only - MP4 can't hold them)

Each part is exactly as long as its video: audio is cut / padded to it, so
the sound stays in sync at every join."""
import os
import shutil
import threading

from .join import (_finish, _noop, _tmp_dir, default_layout, global_tags, join_tolerance,
                   merge_chapters, offsets_of, part_progress, probe_join, stream_meta_args,
                   sub_disposition, unique_attachments, verify)
from .chapters import write_ffmetadata
from .formatting import format_size
from .probe import stats_tag_args
from .process import run_ffmpeg_with_progress
from .files import _part_path
from .join_subs import merge_subtitle_tracks
from .recode import _HDR_TRC, default_bitrate, normalize_settings
from .recode_cmd import (_tonemap_chain, encoder_pix_fmt, hvc1_tag, output_depth,
                         resolve_video_encoder, video_encoder_args)

_INTERLACED = ("tt", "bb", "tb", "bt")
# a source codec -> the encoder that makes the same kind ("copy" audio rule)
_SAME_KIND = {"aac": "aac", "ac3": "ac3", "eac3": "eac3", "opus": "libopus", "flac": "flac",
              "mp3": "libmp3lame"}


# ======================= video =======================
def display_size(v):
    """(w, h) of a video with square pixels (anamorphic -> its display
    width), even numbers - exactly what _video_chain's
    scale=trunc(iw*sar/2)*2 makes (a rounded width could be 2 px off and
    the concat filter refuses parts of different sizes)."""
    sar = v["sar_f"] or 1.0
    if abs(sar - 1.0) > 0.001:
        w = int(v["width"] * sar / 2) * 2
    else:
        w = v["width"] - v["width"] % 2
    return w, v["height"] - v["height"] % 2


def _video_chain(fi, W, H, rate_str, pix, s, depth, notes):
    v = fi["video"]
    # (ffmpeg has moved each input to start at 0 - as the times of probe_file)
    chain = []
    if v["field_order"] in _INTERLACED:
        chain.append("bwdif=mode=send_frame:parity=auto:deint=all")
        notes.append(f"{os.path.basename(fi['path'])}: interlaced -> deinterlaced (bwdif)")
    trc = v["color"]["transfer"]
    if s["tonemap"] and trc in _HDR_TRC:
        tm, how = _tonemap_chain({"transfer": trc, "primaries": v["color"]["primaries"],
                                  "matrix": v["color"]["matrix"]}, depth)
        if tm:
            chain.append(tm)
            notes.append(f"{os.path.basename(fi['path'])}: HDR -> SDR ({how})")
    if abs((v["sar_f"] or 1.0) - 1.0) > 0.001:
        chain.append("scale=trunc(iw*sar/2)*2:ih,setsar=1")
    if display_size(v) != (W, H):
        chain.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:"
                     "force_divisible_by=2:flags=lanczos")
        chain.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    chain += ["setsar=1", f"fps={rate_str}:start_time=0", f"format={pix}"]
    return ",".join(chain)


# ======================= audio =======================
def audio_plan(s, ref, mp4):
    """(encoder, bitrate, channel layout) of an output audio track whose
    first source is `ref`, from the settings' audio rule (audio_codec /
    audio_bitrate / audio_mix). Re-encoding is unavoidable here, so 'copy'
    means: the same kind of codec as the source."""
    rule = s.get("audio_codec") or "copy"
    src, ch = ref["codec"], ref["channels"] or 2
    if rule == "copy":
        codec = _SAME_KIND.get(src)
        if codec is None:
            lossless = src.startswith("pcm_") or src in ("truehd", "mlp", "alac")
            codec = "flac" if lossless and not mp4 else "eac3" if ch > 2 else "aac"
    elif rule == "plex":
        codec = src if src in ("aac", "ac3", "eac3") else "eac3"
    else:
        codec = rule
    want = {"stereo": 2, "5.1": 6}.get(s.get("audio_mix") or "keep")
    out_ch = want if want and want < ch else ch
    if codec in ("ac3", "eac3") and out_ch > 6:
        out_ch = 6
    if codec == "libmp3lame" and out_ch > 2:
        out_ch = 2
    br = s.get("audio_bitrate") or default_bitrate(codec, out_ch)
    if rule == "plex" and codec == "eac3":
        br = "640k"
    if out_ch == ch and ref["layout"] and codec != "libopus":
        layout = ref["layout"]
    else:
        layout = default_layout(out_ch)
    return codec, br, layout


def _audio_chain(fi, a, layout, span):
    """One part of an output audio track: the file's track `a`, or silence
    when a is None - always exactly `span` seconds."""
    fmt = f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts={layout}"
    if a is None:
        return f"anullsrc=r=48000:cl={layout},{fmt},atrim=end={span:.6f}"
    return (f"[{{inp}}:{a['idx']}]aresample=48000:async=1:first_pts=0,{fmt},apad,"
            f"atrim=end={span:.6f}")


# ======================= the re-encode =======================
def join_reencode(files, out, settings, info=None, progress=None, stop_event=None, log=None,
                  on_part=None):
    """Re-encode every file to one format and join them into `out`.
    settings = Convert settings (recode.default_settings keys). Returns
    (ok, output path or None)."""
    log = log or _noop
    stop_event = stop_event or threading.Event()
    mp4 = out.lower().endswith(".mp4")
    info = info or probe_join(files, "mp4" if mp4 else "mkv")
    infos = info["files"]
    n = len(infos)
    ref = infos[0]
    s = normalize_settings(settings)
    if s["vcodec"] == "copy":
        log("   [WARN] the preset copies the video - joining different files needs a "
            "re-encode: using H.265 (x265)")
        s["vcodec"] = "libx265"
    notes = []
    enc, plex = resolve_video_encoder(s["vcodec"], log)
    depth = output_depth(s, enc, ref["video"]["bit_depth"], notes)
    pix = encoder_pix_fmt(enc, depth)
    W, H = display_size(ref["video"])
    fps, rate_str = ref["video"]["fps"], ref["video"]["fps_str"]
    if not fps:
        fps, rate_str = 25.0, "25/1"
        notes.append("[WARN] the first file's frame rate is unknown - using 25 fps")
    spans = [fi["vduration"] or fi["duration"] for fi in infos]
    offsets = offsets_of(spans)
    total = offsets[-1] + spans[-1]
    tmp = _tmp_dir()

    # ---- filter graph: normalize every part, then concat ----
    n_a = max(len(fi["audio"]) for fi in infos)
    plans = []
    for i in range(n_a):
        first = next(fi["audio"][i] for fi in infos if i < len(fi["audio"]))
        plans.append((first,) + audio_plan(s, first, mp4))
    graph, pads = [], []
    for j, fi in enumerate(infos):
        graph.append(f"[{j}:{fi['video']['idx']}]"
                     f"{_video_chain(fi, W, H, rate_str, pix, s, depth, notes)}[v{j}]")
        pads.append(f"[v{j}]")
        for i, (first, _codec, _br, layout) in enumerate(plans):
            a = fi["audio"][i] if i < len(fi["audio"]) else None
            if a is None:
                notes.append(f"{os.path.basename(fi['path'])}: no audio track {i + 1} - "
                             "silence there")
            graph.append(_audio_chain(fi, a, layout, spans[j]).replace("{inp}", str(j))
                         + f"[a{j}_{i}]")
            pads.append(f"[a{j}_{i}]")
    graph.append("".join(pads) + f"concat=n={n}:v=1:a={n_a}[vout]"
                 + "".join(f"[aout{i}]" for i in range(n_a)))

    subs = merge_subtitle_tracks(infos, offsets, spans, tmp, log, stop_event, mp4)
    chapters = merge_chapters(infos, offsets, spans)
    meta = os.path.join(tmp, "meta.txt")
    write_ffmetadata(meta, chapters, global_tags(ref))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    for fi in infos:
        cmd += ["-i", fi["path"]]
    for sub in subs:
        # (a picture track: ffmpeg moves the input to start at 0 - shift back)
        if sub["itsoffset"] is not None:
            cmd += ["-itsoffset", f"{sub['itsoffset']:.6f}"]
        cmd += ["-i", sub["path"]]
    m_idx = n + len(subs)
    cmd += ["-f", "ffmetadata", "-i", meta]
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    cmd += video_encoder_args(s, enc, plex, max((fi["video"]["bps"] or 0 for fi in infos),
                                                default=0) or None, notes)
    cmd += hvc1_tag(enc, mp4)
    if any("HDR -> SDR" in x for x in notes):
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    cmd += stream_meta_args("v", 0, ref["video"]["lang"], ref["video"]["title"])
    cmd += ["-fps_mode", "cfr"]
    for i, (first, codec, br, layout) in enumerate(plans):
        cmd += ["-map", f"[aout{i}]", f"-c:a:{i}", codec]
        if codec != "flac" and br:
            cmd += [f"-b:a:{i}", br]
        cmd += stream_meta_args("a", i, first["lang"], first["title"])
        cmd += [f"-disposition:a:{i}", "default" if first["default"] else "0"]
        notes.append(f"audio {i + 1}: {codec} {br or ''} {layout}".rstrip())
    for k, sub in enumerate(subs):
        t = sub["track"]
        cmd += ["-map", f"{n + k}:0", f"-c:s:{k}", sub["codec"]]
        cmd += stream_meta_args("s", k, t["lang"], t["title"])
        cmd += [f"-disposition:s:{k}", sub_disposition(t)]
    n_att = len({a["filename"].lower() for fi in infos for a in fi["attachments"]})
    if n_att and mp4:
        log(f"   [WARN] MP4 can't hold attachments - {n_att} font(s) / attachment(s) dropped")
    elif n_att:
        atts = unique_attachments(infos, log)
        for fidx, a in atts:
            cmd += ["-map", f"{fidx}:{a['idx']}"]
        log(f"   attachments: {len(atts)} (fonts of every part, each name once)")
    cmd += ["-map_metadata", str(m_idx), "-map_chapters", str(m_idx)]
    cmd += stats_tag_args(*[fi["path"] for fi in infos])
    if mp4:
        cmd += ["-movflags", "+faststart"]
    part = _part_path(out)
    cmd += ["-max_muxing_queue_size", "4096", part]
    for x in notes:
        log(f"   {x}")
    log(f"   encoder {enc}, {W}x{H} @ {fps:.3f} fps, {depth}-bit, {n_a} audio / "
        f"{len(subs)} subtitle track(s), {len(chapters)} chapter(s), {total:.2f} s")
    if stop_event.is_set():
        shutil.rmtree(tmp, ignore_errors=True)
        log("   [STOPPED] partial output removed")
        return False, None
    rc = run_ffmpeg_with_progress(cmd, total, fps=fps, on_progress=part_progress(
        progress, on_part, offsets, total), on_log=log, stop_event=stop_event)
    ok, placed = _finish(rc, part, out, n, total, log, stop_event, tmp)
    if not ok:
        return False, None
    verify(placed, infos, n_a, len(subs), (W, H), log)
    log(f"   Size: {format_size(os.path.getsize(placed))} (allowed length difference "
        f"±{join_tolerance(n):.1f} s)")
    return True, placed
