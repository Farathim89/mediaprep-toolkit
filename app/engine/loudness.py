"""Audio levels: volume / peak / loudness measuring, gain change, two-pass
loudnorm, peak normalize and the theme-clip export."""
import os
import re
import math
import json
import subprocess

from ..config import POPEN_FLAGS
from .encode import build_audio_recode_args
from .probe import probe_audio_streams, probe_duration
from .process import _run_capture_stoppable, _run_exempt, run_ffmpeg_with_progress


# ======================= audio tools =======================
# Trim a silent lead-in before measuring with volumedetect (Mean/Peak display),
# so a file that opens with 10-20s of no audio isn't shown quieter than it
# sounds. ONLY for the volumedetect display columns - loudnorm (LUFS + the actual
# normalize) gates silence itself and must NOT be trimmed, or its two-pass gain
# misses the target. Measurement-only: it never touches the audio that's written.
_MEAS_SILENCE_TRIM = "silenceremove=start_periods=1:start_threshold=-50dB"


def probe_volume(input_file, track=0):
    """Mean and max volume (dB) of one audio stream (0:a:track, default the
    first) via ffmpeg volumedetect, ignoring a silent lead-in.
    Returns (mean_db, max_db)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", input_file,
             "-map", f"0:a:{int(track or 0)}?", "-af", f"{_MEAS_SILENCE_TRIM},volumedetect",
             "-vn", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    except OSError:                        # ffmpeg missing
        return None, None
    t = r.stderr.decode(errors="replace")

    def _g(pat):
        m = re.search(pat, t)
        return float(m.group(1)) if m else None

    return _g(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB"), _g(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


def _loudnorm_args(target_i, tp, lra, meas):
    """Second-pass loudnorm filter from a pass-1 report (linear mode). Raises
    the LRA target to the measured LRA so linear mode doesn't silently fall
    back to dynamic (loudnorm's LRA range is 1-50)."""
    lra_t = lra
    try:
        m_lra = float(meas["input_lra"])
        if m_lra > float(lra):
            lra_t = f"{min(50.0, m_lra):.1f}"
    except (TypeError, ValueError, KeyError):
        pass
    return (f"loudnorm=I={target_i}:TP={tp}:LRA={lra_t}"
            f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
            f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
            f":offset={meas['target_offset']}:linear=true")


def export_audio_clip(input_file, output_file, start, end, codec, extra_args,
                      fade_in=0.0, fade_out=0.0, stop_event=None,
                      on_progress=None, on_log=None, track=0,
                      normalize=None, tp="-1.5", lra="11"):
    """Cut [start, end] of the input's audio and encode it (for a Plex theme).
    Optional fade in/out (seconds). track picks the audio stream (0:a:N,
    default the first). MP3/M4A/AAC exports are downmixed to stereo (a 5.1
    source would otherwise fail for MP3 or give a 6-channel theme).
    normalize=<LUFS target> (e.g. -16): the trimmed + faded clip is downmixed
    to STEREO and two-pass loudnorm'ed to that integrated loudness (true-peak
    ceiling tp); the measuring pass sees exactly the audio that is written.
    Pass a stop_event / on_progress to make it interruptible with a live
    progress bar. Returns (returncode, stderr_text); returncode is -1 if
    stopped."""
    dur = max(0.05, end - start)
    af = []
    if fade_in and fade_in > 0:
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out and fade_out > 0:
        af.append(f"afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.3f}")
    head = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", input_file, "-t", f"{dur:.3f}",
            "-map", f"0:a:{int(track or 0)}", "-vn"]
    cmd = list(head)
    stereo = False
    if normalize is not None:
        target = f"{float(normalize):g}"
        af.append("aformat=channel_layouts=stereo")      # downmix BEFORE measuring
        stereo = True
        _rc1, text = _run_capture_stoppable(
            ["ffmpeg", "-hide_banner"] + head[2:]
            + ["-af", ",".join(af + [f"loudnorm=I={target}:TP={tp}:LRA={lra}"
                                     ":print_format=json"]),
               "-f", "null", "-"], stop_event)
        if stop_event is not None and stop_event.is_set():
            return -1, ""
        found = _loudnorm_json(text, "input_i")
        meas = found[-1] if found else None
        if meas:
            if on_log:
                on_log(f"   clip measured {meas['input_i']} LUFS, true peak "
                       f"{meas['input_tp']} dBTP -> normalizing to {target} LUFS (stereo)")
            af.append(_loudnorm_args(target, tp, lra, meas))
        else:
            if on_log:
                on_log("   [WARN] could not measure the clip - single-pass loudnorm")
            af.append(f"loudnorm=I={target}:TP={tp}:LRA={lra}")
        # loudnorm outputs 192 kHz: bring it back to a normal rate
        af.append("aresample=48000")
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:a", codec] + list(extra_args)
    if "-ac" not in extra_args and (stereo or
            codec in ("libmp3lame", "aac")
            or os.path.splitext(output_file)[1].lower() in (".mp3", ".m4a", ".aac")):
        cmd += ["-ac", "2"]
    cmd += [output_file]
    if stop_event is not None or on_progress is not None:
        rc = run_ffmpeg_with_progress(cmd, dur, on_progress=on_progress,
                                      on_log=on_log, stop_event=stop_event)
        return rc, ""      # failures are already reported via on_log
    r = _run_exempt(cmd)
    return r.returncode, r.stderr.decode(errors="replace")


_GAIN_AUDIO_CODEC = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac", "flac": "flac",
                     "wav": "pcm_s16le", "ogg": "libvorbis", "opus": "libopus"}


# libopus (mapping family 1) only takes these layouts; a 5.1(side) source is
# remapped to plain 5.1 (side -> back) instead of failing the encode
_OPUS_LAYOUTS = {3: "3.0", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}
_LOSSY_GAIN_CODECS = ("libmp3lame", "aac", "libvorbis", "libopus")


def _track_encode_args(i, stream, codec, bitrate):
    """Encoder args for output audio track i re-encoded to an EXPLICIT codec,
    plus a filter suffix for that track. Keeps the encode from failing on
    channel layouts the codec can't take: (E-)AC3 is capped at 5.1, MP3 at
    stereo, Opus gets a supported layout + mapping family 1 for >2 channels.
    AAC gets >= 64k per channel for multichannel; Opus is capped at its
    256k-per-channel maximum. Returns (args, filter_suffix)."""
    ch = stream.get("channels") or 2
    args, suffix = [f"-c:a:{i}", codec], ""
    if codec in ("ac3", "eac3") and ch > 6:
        args += [f"-ac:a:{i}", "6"]
        ch = 6
    elif codec == "libmp3lame" and ch > 2:
        args += [f"-ac:a:{i}", "2"]
        ch = 2
    elif codec == "libopus" and ch > 2:
        if ch in _OPUS_LAYOUTS:
            suffix = f",aformat=channel_layouts={_OPUS_LAYOUTS[ch]}"
            args += [f"-mapping_family:a:{i}", "1"]
        else:
            args += [f"-ac:a:{i}", "2"]
            ch = 2
    if bitrate:
        m = re.fullmatch(r"(\d+)k", str(bitrate).strip().lower())
        if m:
            kb = int(m.group(1))
            if codec == "aac" and ch > 2:
                kb = max(kb, 64 * ch)
            elif codec == "libopus":
                kb = min(kb, 256 * ch)
            bitrate = f"{kb}k"
        args += [f"-b:a:{i}", bitrate]
    return args, suffix


def _apply_audio_filters(input_file, output_file, keep_video, audio_out, filters,
                         stop_event=None, on_progress=None, on_log=None,
                         stderr_out=None, tracks=None):
    """Shared encode step of the Audio Gain tools. filters: one filter chain
    per audio track (0:a:0, 0:a:1, ...); a single entry is used for every
    track. keep_video: copy video/subs/data and process EVERY audio track with
    its own filter; else write audio only (track 0, matching what was
    measured). audio_out=None keeps each track's source codec + channels;
    otherwise (codec, bitrate). tracks=[i, ...]: only those audio tracks are
    processed (filters[k] belongs to tracks[k]); with keep_video every other
    track is stream-copied untouched, audio-only output writes tracks[0].
    Returns (returncode, stderr_text)."""
    streams = probe_audio_streams(input_file)
    cmd = ["ffmpeg", "-y", "-i", input_file]
    if tracks is not None:
        tracks = [int(t) for t in tracks] or [0]
    if keep_video and tracks is not None and streams:
        fmap = {t: filters[min(k, len(filters) - 1)] for k, t in enumerate(tracks)}
        cmd += ["-map", "0", "-c:v", "copy", "-c:s", "copy", "-c:d", "copy"]
        for i, s in enumerate(streams):
            if i not in fmap:
                cmd += [f"-c:a:{i}", "copy"]
                continue
            if audio_out is None:
                # the source-matching recode args, re-numbered to this track
                enc = [a[:-len(":a:0")] + f":a:{i}" if a.endswith(":a:0") else a
                       for a in build_audio_recode_args([s])]
                cmd += [f"-filter:a:{i}", fmap[i]] + enc
            else:
                codec, bitrate = audio_out
                enc, suffix = _track_encode_args(i, s, codec, bitrate)
                cmd += [f"-filter:a:{i}", fmap[i] + suffix] + enc
    elif keep_video:
        cmd += ["-map", "0", "-c:v", "copy", "-c:s", "copy", "-c:d", "copy"]
        if not streams:            # couldn't list the tracks: one filter for all
            cmd += ["-filter:a", filters[0]]
            cmd += _audio_encode_args(input_file, output_file, audio_out)
        elif audio_out is None:
            for i in range(len(streams)):
                cmd += [f"-filter:a:{i}", filters[min(i, len(filters) - 1)]]
            cmd += build_audio_recode_args(streams)
        else:
            codec, bitrate = audio_out
            for i, s in enumerate(streams):
                enc, suffix = _track_encode_args(i, s, codec, bitrate)
                cmd += [f"-filter:a:{i}", filters[min(i, len(filters) - 1)] + suffix] + enc
    else:
        if audio_out is not None:
            codec, bitrate = audio_out
        else:
            ext = os.path.splitext(output_file)[1].lower().lstrip(".")
            codec = _GAIN_AUDIO_CODEC.get(ext, "aac")
            bitrate = "320k" if codec in _LOSSY_GAIN_CODECS else None
        ti = tracks[0] if tracks else 0
        if streams and ti >= len(streams):
            ti = 0
        enc, suffix = _track_encode_args(0, streams[ti] if streams else {}, codec, bitrate)
        cmd += ["-map", f"0:a:{ti}", "-vn", "-filter:a:0", filters[0] + suffix] + enc
    cmd += [output_file]
    if stop_event is not None or on_progress is not None:
        dur = probe_duration(input_file) or 0
        rc = run_ffmpeg_with_progress(cmd, dur, on_progress=on_progress,
                                      on_log=on_log, stop_event=stop_event,
                                      stderr_out=stderr_out)
        return rc, ""      # failures already reported via on_log
    r = _run_exempt(cmd)
    text = r.stderr.decode(errors="replace")
    if stderr_out is not None:
        stderr_out.append(text)
    return r.returncode, text


def _audio_encode_args(input_file, output_file, audio_out):
    """Audio-encoder args for filtered output. audio_out=None -> match the source
    (same codec + channels per track, so 5.1/Dolby/DTS survive); otherwise use the
    given (codec, bitrate) for every track."""
    if audio_out is None:
        return build_audio_recode_args(probe_audio_streams(input_file))
    codec, bitrate = audio_out
    args = ["-c:a", codec]
    if bitrate:
        args += ["-b:a", bitrate]
    return args


def change_gain(input_file, output_file, af, keep_video, audio_out=None,
                stop_event=None, on_progress=None, on_log=None, track_filters=None,
                tracks=None):
    """Apply an audio filter (e.g. 'volume=3dB' or a loudnorm string). Video,
    subtitle and data streams are stream-copied; audio must be re-encoded to apply
    the filter. audio_out=None keeps the SOURCE codec + channel layout per track
    (so 5.1 AC3 / DTS / etc. stay intact); otherwise it is a (codec, bitrate).
    track_filters: optional list with a different filter per audio track
    (overrides af). tracks=[i]: change only those audio tracks (track_filters
    then line up with tracks), the rest are stream-copied. Pass a stop_event /
    on_progress to make it interruptible with a progress bar.
    Returns (returncode, stderr_text); rc is -1 if stopped."""
    filters = list(track_filters) if track_filters else [af]
    return _apply_audio_filters(input_file, output_file, keep_video, audio_out, filters,
                                stop_event=stop_event, on_progress=on_progress,
                                on_log=on_log, tracks=tracks)


def _loudnorm_json(text, key):
    """Every loudnorm JSON report in ffmpeg's stderr that contains `key`, in
    the order printed."""
    out = []
    for blk in re.findall(r"\{[^{}]*\}", text):       # loudnorm json is flat
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if key in d:
            out.append(d)
    return out


def normalize_loudness(input_file, output_file, target_i, keep_video, tp="-1.5", lra="11",
                       audio_out=None, stop_event=None, on_progress=None, on_log=None,
                       result=None, tracks=None):
    """Two-pass loudnorm to a target integrated loudness (LUFS). tracks=[i]
    normalizes only those audio tracks (others are stream-copied; audio-only
    output writes tracks[0]); None = all (or track 0 for audio-only). Pass 1 measures
    EACH audio track on its own, pass 2 applies each track's own measurements
    (a quiet commentary track isn't normalized with the main mix's numbers), so
    every file lands at the same loudness - ideal for matching a whole season.
    tp = true-peak ceiling (dBTP), lra = loudness-range target. Video/subtitles/
    chapters are copied; only audio is re-encoded. Returns (returncode,
    stderr_text). Optional result dict is filled with 'measured' (pass-1 report
    per track) and 'modes' (pass-2 normalization_type per track: 'linear' or
    'dynamic'), so the UI can show whether loudnorm stayed linear."""
    # NOTE: do NOT silence-trim here. loudnorm gates silence itself, and its
    # two-pass linear mode needs pass-1's measured values to describe the SAME
    # audio pass 2 processes (the full file). Trimming pass 1 only would feed
    # mismatched measurements and miss the target badly.
    streams = probe_audio_streams(input_file)
    sel = _select_tracks(streams, keep_video, tracks)
    filters, measured = [], []
    for ti in sel:
        if stop_event is not None and stop_event.is_set():
            return -1, ""
        _rc1, text = _run_capture_stoppable(
            ["ffmpeg", "-hide_banner", "-i", input_file, "-map", f"0:a:{ti}?",
             "-af", f"loudnorm=I={target_i}:TP={tp}:LRA={lra}:print_format=json",
             "-vn", "-f", "null", "-"], stop_event)
        if stop_event is not None and stop_event.is_set():
            return -1, ""
        found = _loudnorm_json(text, "input_i")
        meas = found[-1] if found else None
        measured.append(meas)
        label = f"track {ti}" + (f" ({streams[ti].get('lang')})" if ti < len(streams) else "")
        lra_t = lra
        if meas:
            if on_log:
                on_log(f"   {label}: measured {meas['input_i']} LUFS, LRA {meas['input_lra']},"
                       f" true peak {meas['input_tp']} dBTP")
            # linear mode silently falls back to dynamic (compressing) when the
            # measured LRA exceeds the target - so raise the target to fit it
            # (loudnorm's LRA range is 1-50)
            try:
                m_lra = float(meas["input_lra"])
                if m_lra > float(lra):
                    lra_t = f"{min(50.0, m_lra):.1f}"
            except (TypeError, ValueError):
                pass
            # ... and it also goes dynamic if the gain would push peaks over TP
            try:
                peak = float(meas["input_tp"]) + float(target_i) - float(meas["input_i"])
                if on_log and math.isfinite(peak) and peak > float(tp):
                    on_log(f"   note: {label} peaks would reach {peak:+.1f} dBTP (> {tp}) at"
                           " this gain - loudnorm uses dynamic mode for it")
            except (TypeError, ValueError):
                pass
        elif on_log:
            on_log(f"   [WARN] {label}: could not measure - single-pass loudnorm")
        af = f"loudnorm=I={target_i}:TP={tp}:LRA={lra_t}"
        if meas:
            af += (f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
                   f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
                   f":offset={meas['target_offset']}:linear=true")
        # print_format=json: pass 2 reports whether it stayed linear.
        # loudnorm always outputs 192 kHz - resample each track back to its own
        # source rate (fallback 48 kHz)
        rate = (streams[ti].get("sample_rate") if ti < len(streams) else None) or 48000
        filters.append(f"{af}:print_format=json,aresample={rate}")
    err_text = []
    rc, err = _apply_audio_filters(input_file, output_file, keep_video, audio_out, filters,
                                   stop_event=stop_event, on_progress=on_progress,
                                   on_log=on_log, stderr_out=err_text,
                                   tracks=sel if tracks is not None else None)
    modes = [d.get("normalization_type") for d in
             _loudnorm_json("".join(err_text), "normalization_type")]
    if result is not None:
        result["measured"] = measured
        result["modes"] = modes
    if rc == 0 and on_log and modes:
        dyn = sum(1 for m in modes if m != "linear")
        if dyn:
            on_log(f"   [WARN] loudnorm went dynamic (compressing) on {dyn} of"
                   f" {len(modes)} track(s) (true-peak / loudness-range limits)")
        else:
            on_log(f"   loudnorm stayed linear on {len(modes)} track(s)")
    return rc, err


def _select_tracks(streams, keep_video, tracks):
    """Audio tracks (0:a:N numbers) a gain/normalize job processes. tracks=None:
    every track when the video/other streams are kept, else just track 0.
    tracks=[i]: those (valid ones only; audio-only output takes the first)."""
    if tracks is None:
        return list(range(len(streams))) if keep_video and streams else [0]
    sel = [int(t) for t in tracks if not streams or 0 <= int(t) < len(streams)] or [0]
    return sel if keep_video else sel[:1]


def measure_loudness(input_file, track=0, stop_event=None):
    """(integrated LUFS, true peak dBTP) of one audio track (0:a:track) via a
    loudnorm measuring pass - the same as the two-pass normalize's pass 1, so a
    previewed LUFS / Gain equals the actual result. Stoppable. Either value is
    None if it can't be read (no audio / silent / stopped)."""
    # loudnorm gates silence on its own, so measure the file as-is
    _rc, text = _run_capture_stoppable(
        ["ffmpeg", "-hide_banner", "-i", input_file, "-map", f"0:a:{int(track or 0)}?",
         "-af", "loudnorm=print_format=json", "-vn", "-f", "null", "-"], stop_event)
    found = _loudnorm_json(text or "", "input_i")
    if not found:
        return None, None
    d = found[-1]

    def _f(key):
        try:
            v = float(d.get(key))
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v > -99 else None      # -inf = silent
    return _f("input_i"), _f("input_tp")


def measure_loudness_tracks(input_file, stop_event=None):
    """measure_loudness() for EVERY audio track: a list of (lufs, true_peak)
    in track order (empty if the file has no audio)."""
    out = []
    for ti in range(len(probe_audio_streams(input_file))):
        if stop_event is not None and stop_event.is_set():
            break
        out.append(measure_loudness(input_file, ti, stop_event))
    return out


def measure_lufs(input_file, track=0):
    """Integrated loudness (LUFS) of a file's audio via loudnorm pass 1.
    Returns a float or None. (Kept for older callers; see measure_loudness.)"""
    return measure_loudness(input_file, track)[0]


def probe_peaks(input_file, track=0, true_peak=False, stop_event=None):
    """Sample peak (dBFS, volumedetect) and optionally the TRUE (inter-sample)
    peak (dBTP, ebur128 peak=true, same decode pass) of one audio track,
    ignoring a silent lead-in. Returns (sample_peak, true_peak); either may
    be None if it couldn't be measured."""
    af = f"{_MEAS_SILENCE_TRIM},volumedetect"
    if true_peak:
        af += ",ebur128=peak=true"
    _rc, t = _run_capture_stoppable(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", input_file,
         "-map", f"0:a:{track}?", "-af", af, "-vn", "-f", "null", "-"], stop_event)
    m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", t)
    sp = float(m.group(1)) if m else None
    tpk = None
    if true_peak:
        m = re.search(r"True peak:\s*Peak:\s*(-?\d+(?:\.\d+)?)", t)
        tpk = float(m.group(1)) if m else None
    return sp, tpk


def peak_normalize(input_file, output_file, target_peak_db, keep_video, audio_out=None,
                   stop_event=None, on_progress=None, on_log=None, tracks=None):
    """Shift each audio track so its loudest SAMPLE peak sits at target_peak_db
    (dBFS) - i.e. match by peak level rather than perceived loudness. Each
    track is measured and gained on its own. Also logs the true (inter-sample)
    peak, since a sample peak near 0 dBFS can still clip after decoding.
    tracks=[i]: only those audio tracks (others stream-copied).
    Returns (returncode, stderr_text); rc is -1 if stopped."""
    streams = probe_audio_streams(input_file)
    sel = _select_tracks(streams, keep_video, tracks)
    filters = []
    for ti in sel:
        if stop_event is not None and stop_event.is_set():
            return -1, ""
        mx, tpk = probe_peaks(input_file, ti, true_peak=True, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            return -1, ""
        if mx is None:
            return 1, f"could not measure the sample peak of audio track {ti}"
        gain = target_peak_db - mx
        if on_log:
            tp_txt = f", true peak {tpk:.1f} dBTP" if tpk is not None else ""
            on_log(f"   track {ti}: sample peak {mx:.1f} dBFS{tp_txt} -> gain {gain:+.2f} dB")
            if tpk is not None and tpk + gain > 0.0:
                on_log(f"   [WARN] track {ti}: true peak would reach {tpk + gain:+.1f} dBTP"
                       " - may clip on playback; use a lower target")
        filters.append(f"volume={gain:.2f}dB")
    return change_gain(input_file, output_file, filters[0], keep_video, audio_out,
                       stop_event=stop_event, on_progress=on_progress, on_log=on_log,
                       track_filters=filters, tracks=sel if tracks is not None else None)
