#!/usr/bin/env python3
"""
intro_credits_toolkit.py  -  one GUI, two tabs

Tab 1: Template Cutter
    Pick a video, set From/To for intro and/or credits (checkboxes),
    frame-exact cuts land in .\\intro and .\\credits with audio copied
    untouched (the Remover's MFCC matching listens to the audio).

Tab 2: Batch Remover  (engine from batch_trim_intro_credits_v18)
    Detects intro/credits in every video in the videos folder using the
    templates, then cuts them out (keeping cold open + post-credits) or
    just injects keyframes at the boundaries. Live progress bar driven
    by ffmpeg's frame counter, step-by-step log, Stop button.

Requires: ffmpeg/ffprobe on PATH. Tab 2 additionally needs
    pip install librosa numpy scipy
Tkinter ships with Python - nothing else to install.

Times accept HH:MM:SS(.ms), MM:SS, or plain seconds.
"""

import os
import re
import glob
import json
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ======================= shared settings =======================
INTRO_DIR = "intro"
CREDITS_DIR = "credits"
VIDEO_DIR = "videos"
OUTPUT_DIR = "output"
TEMP_DIR = "temp"

INTRO_SEARCH_WINDOW = 600
CREDITS_SEARCH_WINDOW = 600

VALID_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]

# ---- video codec selection ----
CODECS = {
    "H.264 (libx264) - most compatible": "libx264",
    "H.265 / HEVC (libx265) - smaller files": "libx265",
    "H.264 NVENC (NVIDIA GPU, fast)": "h264_nvenc",
    "H.265 NVENC (NVIDIA GPU, fast)": "hevc_nvenc",
    "AV1 (libsvtav1) - smallest, slow": "libsvtav1",
}
DEFAULT_CODEC_LABEL = "H.264 (libx264) - most compatible"

# NVENC uses p1-p7 presets and -cq instead of -crf
NVENC_PRESET_MAP = {"ultrafast": "p2", "superfast": "p2", "veryfast": "p3",
                    "faster": "p4", "fast": "p4", "medium": "p5",
                    "slow": "p6", "slower": "p7", "veryslow": "p7"}
# SVT-AV1 uses numeric presets 0(slowest)-13(fastest)
SVT_PRESET_MAP = {"ultrafast": "12", "superfast": "11", "veryfast": "10",
                  "faster": "9", "fast": "9", "medium": "8",
                  "slow": "6", "slower": "5", "veryslow": "4"}


BIT_DEPTHS = {"Auto (match source)": "auto", "8-bit": "8", "10-bit": "10"}


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


def probe_video_info(input_file):
    """Codec, pixel format, bit depth and container bitrate of the first
    video stream - used for the source log line and 'Auto' bit depth."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,bits_per_raw_sample",
         "-show_entries", "format=bit_rate",
         "-print_format", "json", input_file],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    info = {"codec": "?", "pix_fmt": "?", "bit_depth": 8, "bitrate_mbps": None}
    if result.returncode != 0:
        return info
    try:
        data = json.loads(result.stdout)
        s = (data.get("streams") or [{}])[0]
        info["codec"] = s.get("codec_name", "?")
        info["pix_fmt"] = s.get("pix_fmt") or "?"
        bits = str(s.get("bits_per_raw_sample") or "")
        if bits.isdigit() and int(bits) > 0:
            info["bit_depth"] = int(bits)
        elif "12" in info["pix_fmt"]:
            info["bit_depth"] = 12
        elif "10" in info["pix_fmt"]:
            info["bit_depth"] = 10
        br = str((data.get("format") or {}).get("bit_rate") or "")
        if br.isdigit():
            info["bitrate_mbps"] = int(br) / 1e6
    except (ValueError, KeyError, IndexError):
        pass
    return info


POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

AUDIO_RECODE_MAP = {
    "ac3": ("ac3", "640k"),
    "eac3": ("eac3", "640k"),
    "dts": ("dca", "1536k"),
    "truehd": ("flac", None),
    "flac": ("flac", None),
    "aac": ("aac", "320k"),
    "mp3": ("libmp3lame", "320k"),
    "opus": ("libopus", "320k"),
    "vorbis": ("libvorbis", "320k"),
    "pcm_s16le": ("pcm_s16le", None),
    "pcm_s24le": ("pcm_s24le", None),
}
DEFAULT_AUDIO_RECODE = ("aac", "320k")


# ======================= small helpers =======================
def parse_time(text):
    """'HH:MM:SS(.ms)', 'MM:SS' or plain seconds -> float seconds, else None."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]?\d(?:\.\d+)?)", text)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def format_seconds(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(num_bytes):
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=POPEN_FLAGS)
    try:
        return float(r.stdout.decode().strip())
    except ValueError:
        return None


def duration_check_line(final_output, expected_sec):
    """Verifies no content was lost: output length must match the sum of
    the kept segments (0.5 s tolerance for container/audio-priming padding)."""
    got = probe_duration(final_output)
    if got is None:
        return "   Duration check: could not probe output\n"
    diff = got - expected_sec
    tag = "[OK]" if abs(diff) < 0.5 else "[WARN]"
    return (f"   {tag} Duration check: expected {expected_sec:.2f}s,"
            f" got {got:.2f}s ({diff:+.2f}s)\n")


def saved_size_line(final_output, src_size, wall_start):
    """Log line with before -> after size comparison for a finished video."""
    try:
        out_size = os.path.getsize(final_output)
    except OSError:
        out_size = 0
    pct = f" ({out_size / src_size * 100:.0f}% of original)" if src_size and out_size else ""
    return (f"   [OK] Saved: {final_output}\n"
            f"   Size: {format_size(src_size)} -> {format_size(out_size)}{pct}"
            f" - total {format_seconds(time.time() - wall_start)}\n")


def fmt_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def format_ffmpeg_timestamp(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:09.6f}"


def probe_video_fps(input_file):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate,r_frame_rate",
         "-of", "csv=p=0", input_file],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    if result.returncode != 0:
        return None
    for token in result.stdout.decode(errors="replace").strip().split(","):
        token = token.strip()
        if "/" in token:
            num, _, den = token.partition("/")
            try:
                num, den = float(num), float(den)
                if num > 0 and den > 0:
                    return num / den
            except ValueError:
                pass
        elif token:
            try:
                v = float(token)
                if v > 0:
                    return v
            except ValueError:
                pass
    return None


def probe_audio_streams(input_file):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a", input_file],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    return [{"index": s.get("index", 0),
             "codec_name": s.get("codec_name", "aac"),
             "channels": s.get("channels", 2)} for s in data.get("streams", [])]


def build_audio_recode_args(audio_streams):
    args = []
    for i, s in enumerate(audio_streams):
        out_codec, bitrate = AUDIO_RECODE_MAP.get(s["codec_name"], DEFAULT_AUDIO_RECODE)
        args += [f"-c:a:{i}", out_codec]
        if bitrate:
            args += [f"-b:a:{i}", bitrate]
        args += [f"-ac:{i}", str(s["channels"])]
    return args


# ======================= ffmpeg runner (v18 progress engine) =======================
_progress_file_counter = 0

def run_ffmpeg_with_progress(cmd, duration_sec, fps=None, expected_offset=0.0,
                             on_progress=None, on_log=None, stop_event=None):
    """
    Runs ffmpeg, polling the -progress FILE it writes (avoids Windows pipe
    buffering). Progress derivation, verified against real ffmpeg output:
      * encoding steps (fps known): frame= counter / fps - exact and
        independent of -copyts/-ss/-output_ts_offset and ffmpeg version
      * copy steps (no fps): out_time, with an absolute-timestamp safety net
    on_progress(frac, text) is called on updates; stop_event terminates ffmpeg.
    Returns ffmpeg's return code (-1 if stopped).
    """
    global _progress_file_counter
    _progress_file_counter += 1
    tag = f"{os.getpid()}_{_progress_file_counter}"
    progress_file = os.path.join(TEMP_DIR, f"progress_{tag}.tmp")
    stderr_file = os.path.join(TEMP_DIR, f"stderr_{tag}.log")
    os.makedirs(TEMP_DIR, exist_ok=True)
    if os.path.exists(progress_file):
        try:
            os.remove(progress_file)
        except OSError:
            pass

    if "-progress" not in cmd:
        cmd = [cmd[0], "-progress", progress_file, "-stats_period", "0.5"] + cmd[1:]

    start_wall = time.time()
    stderr_fh = open(stderr_file, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_fh,
                            creationflags=POPEN_FLAGS)

    state = {"t": 0.0, "frame": 0, "speed": "", "ended": False}
    done_flag = threading.Event()

    def current_position():
        if state["ended"]:
            return duration_sec
        if fps and state["frame"] > 0:
            return state["frame"] / fps
        t = state["t"]
        if expected_offset > 0 and t > duration_sec * 1.02 + 0.5 and t - expected_offset >= 0:
            t -= expected_offset
        return max(0.0, min(t, duration_sec))

    def emit():
        if not on_progress:
            return
        cur = current_position()
        frac = min(max(cur / duration_sec, 0.0), 1.0) if duration_sec > 0 else 0.0
        elapsed = time.time() - start_wall
        if 0.0 < frac < 1.0:
            eta = format_seconds((elapsed / frac) * (1.0 - frac))
        elif frac >= 1.0:
            eta = "0:00"
        else:
            eta = "--:--"
        speed = f"  {state['speed']}" if state["speed"] else ""
        on_progress(frac, f"{frac*100:5.1f}%  ETA {eta}{speed}")

    def poll():
        pos = 0
        pending = ""
        while not done_flag.is_set():
            time.sleep(0.2)
            if stop_event is not None and stop_event.is_set():
                try:
                    proc.terminate()
                except OSError:
                    pass
                return
            try:
                with open(progress_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                continue
            if not chunk:
                continue
            pending += chunk
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                line = line.strip()
                if line.startswith("frame="):
                    val = line.split("=", 1)[1].strip()
                    if val.isdigit():
                        state["frame"] = int(val)
                elif line.startswith("out_time_us="):
                    val = line.split("=", 1)[1]
                    if val.lstrip("-").isdigit():
                        state["t"] = int(val) / 1_000_000.0
                elif line.startswith("speed="):
                    state["speed"] = line.split("=", 1)[1].strip()
                elif line == "progress=end":
                    state["ended"] = True
            emit()

    emit()
    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    proc.wait()
    done_flag.set()
    poller.join(timeout=1.0)
    stderr_fh.close()

    stopped = stop_event is not None and stop_event.is_set()
    if proc.returncode != 0 and not stopped and on_log:
        try:
            with open(stderr_file, "r", encoding="utf-8", errors="replace") as f:
                for ln in [l.rstrip() for l in f.readlines() if l.strip()][-5:]:
                    on_log(f"      | {ln}")
        except OSError:
            pass

    for p in (progress_file, stderr_file):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return -1 if stopped else proc.returncode


def build_ffmpeg_cut(input_file, output_file, start_sec, end_sec, audio_streams,
                     crf, preset, kf_interval, seg_name=None, fps=None,
                     encoder="libx264", bit_depth=8, **cb):
    """
    Frame-accurate segment cut (v18 approach):
      input -ss (start-10s) fast keyframe seek, -copyts to keep original
      timestamps, output -ss for the exact frame, -output_ts_offset -start
      to rebase the segment to 0 so segments stand alone and stitch cleanly.
    """
    seg_duration = end_sec - start_sec
    seek1 = max(0.0, start_sec - 10.0)
    cmd = ["ffmpeg", "-y", "-ss", f"{seek1:.6f}", "-copyts", "-i", input_file,
           "-ss", f"{start_sec:.6f}", "-t", f"{seg_duration:.6f}",
           "-output_ts_offset", f"{-start_sec:.6f}", "-map", "0"]
    cmd += build_video_codec_args(encoder, crf, preset, output_file, bit_depth)
    if kf_interval and kf_interval > 0 and seg_name in ("Cold Open", "Post Credits"):
        cmd += ["-force_key_frames:v", f"expr:gte(t,n_forced*{kf_interval})"]
    # NOTE: no force_key_frames otherwise. The first frame of every encoded
    # segment is automatically an IDR keyframe, which is all the cut needs.
    # The old "expr:gte(t,0)" (inherited from v15) evaluated TRUE for EVERY
    # frame, silently producing all-intra video 3-4x the normal size - this
    # was the real cause of 800 MB files ballooning to 2-3 GB.
    cmd += build_audio_recode_args(audio_streams)
    cmd += ["-c:s", "copy", "-c:d", "copy", "-map_metadata", "0",
            "-avoid_negative_ts", "make_non_negative", output_file]
    return run_ffmpeg_with_progress(cmd, seg_duration, fps=fps,
                                    expected_offset=start_sec, **cb)


def build_ffmpeg_inject(input_file, output_file, kf_times, total_duration,
                        crf, preset, fps=None, encoder="libx264", bit_depth=8, **cb):
    cmd = ["ffmpeg", "-y", "-i", input_file, "-map", "0"]
    cmd += build_video_codec_args(encoder, crf, preset, output_file, bit_depth)
    if kf_times:
        cmd += ["-force_key_frames", ",".join(format_ffmpeg_timestamp(t) for t in kf_times)]
    cmd += ["-c:a", "copy", "-c:s", "copy", "-c:d", "copy", "-map_metadata", "0", output_file]
    return run_ffmpeg_with_progress(cmd, total_duration, fps=fps, **cb)


def build_forced_keyframe_times(valid_intro, intro_start, intro_end,
                                valid_credits, credits_start, credits_end,
                                keyframe_interval, total_duration):
    times = set()
    for valid, a, b in ((valid_intro, intro_start, intro_end),
                        (valid_credits, credits_start, credits_end)):
        if valid:
            times.add(a)
            times.add(b)
            if keyframe_interval and keyframe_interval > 0:
                t = a
                while t < b:
                    times.add(t)
                    t += keyframe_interval
    return sorted(t for t in times if 0.0 < t < total_duration)


# ======================= detection (needs librosa) =======================
def get_mfcc_match(y_main, y_temp, sr, librosa, np, fftconvolve):
    if len(y_main) < len(y_temp):
        return 0.0, 0.0
    mfcc_main = librosa.feature.mfcc(y=y_main, sr=sr, n_mfcc=13)
    mfcc_temp = librosa.feature.mfcc(y=y_temp, sr=sr, n_mfcc=13)
    mfcc_main = (mfcc_main - np.mean(mfcc_main, axis=1, keepdims=True)) / (np.std(mfcc_main, axis=1, keepdims=True) + 1e-8)
    mfcc_temp = (mfcc_temp - np.mean(mfcc_temp, axis=1, keepdims=True)) / (np.std(mfcc_temp, axis=1, keepdims=True) + 1e-8)
    if mfcc_main.shape[1] < mfcc_temp.shape[1]:
        return 0.0, 0.0
    scores = np.zeros(mfcc_main.shape[1] - mfcc_temp.shape[1] + 1)
    for i in range(13):
        scores += fftconvolve(mfcc_main[i], mfcc_temp[i, ::-1], mode='valid')
    scores = scores / (13 * mfcc_temp.shape[1])
    best_frame = int(np.argmax(scores))
    return librosa.frames_to_time(best_frame, sr=sr), float(scores[best_frame])


def extract_wav(src, dst):
    ret = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
                          "-ar", "22050", "-ac", "1", dst],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=POPEN_FLAGS)
    return ret.returncode == 0


# ======================= batch remover engine =======================
def run_batch(cfg, ui, stop_event):
    """cfg: dict of settings. ui: object with .log(msg), .status(text),
    .progress(frac, text). stop_event: threading.Event."""
    try:
        import numpy as np
        import librosa
        from scipy.signal import fftconvolve
    except ImportError as e:
        ui.log(f"[FAIL] Missing package: {e}")
        ui.log("Install with:  pip install librosa numpy scipy")
        return

    os.makedirs(cfg["output_dir"], exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    cb = {"on_progress": ui.progress, "on_log": ui.log, "stop_event": stop_event}
    temp_wavs = []

    def load_templates(folder, kind):
        data = []
        for i, file in enumerate(sorted(glob.glob(os.path.join(folder, "*.*")))):
            if file.lower().endswith((".txt", ".json", ".ini", ".md")):
                continue  # notes/config files, not templates
            name = os.path.basename(file)
            ui.log(f"Loading {kind}: {name}")
            tmp = os.path.join(TEMP_DIR, f"{kind.lower()}_{i}.wav")
            if not extract_wav(file, tmp):
                ui.log(f"   [WARN] could not convert template {name}")
                continue
            temp_wavs.append(tmp)
            y, sr = librosa.load(tmp, sr=None)
            data.append({'name': name, 'y': y, 'duration': len(y) / sr})
        return data

    try:
        intros_data = load_templates(cfg["intro_dir"], "Intro")
        credits_data = load_templates(cfg["credits_dir"], "Credits")
        if not intros_data and not credits_data:
            ui.log("[FAIL] No usable templates found - nothing to detect with.")
            return

        video_files = []
        for pat in ('*.mp4', '*.mkv', '*.mov', '*.avi', '*.webm'):
            video_files.extend(glob.glob(os.path.join(cfg["video_dir"], pat)))
        video_files.sort()
        ui.log(f"\nFound {len(video_files)} video(s)\n")
        if not video_files:
            return

        for vi, video in enumerate(video_files, 1):
            if stop_event.is_set():
                break
            filename = os.path.basename(video)
            name, ext = os.path.splitext(filename)
            final_output = os.path.join(cfg["output_dir"], f"{name}_clean{ext}")
            temp_segs = []
            video_wall = time.time()

            ui.status(f"Video {vi}/{len(video_files)}: {filename}")
            src_size = os.path.getsize(video)
            ui.log(f"[{vi}/{len(video_files)}] {filename}  ({format_size(src_size)})")

            temp_audio = os.path.join(TEMP_DIR, f"{name}_audio.wav")
            if not extract_wav(video, temp_audio):
                ui.log("   [FAIL] audio extract failed - skipping\n")
                continue
            y_main, sr = librosa.load(temp_audio, sr=None)
            total_duration = len(y_main) / sr
            ui.log(f"   Duration: {total_duration:.1f}s")

            # --- detect intro ---
            intro_start = intro_end = 0.0
            valid_intro = False
            best = 0.0
            if intros_data:
                y_start = y_main[:min(int(INTRO_SEARCH_WINDOW * sr), len(y_main))]
                for tpl in intros_data:
                    s, score = get_mfcc_match(y_start, tpl['y'], sr, librosa, np, fftconvolve)
                    ui.log(f"     intro {tpl['name']:30} -> {score:.3f}")
                    if score > best:
                        best, intro_start, intro_end = score, s, s + tpl['duration']
                valid_intro = best >= cfg["confidence"]
                ui.log(f"   {'[OK] INTRO at %.2fs' % intro_start if valid_intro else '[--] weak intro match (%.3f)' % best}")

            # --- detect credits ---
            credits_start = credits_end = total_duration
            valid_credits = False
            best = 0.0
            if credits_data:
                win = min(int(CREDITS_SEARCH_WINDOW * sr), len(y_main))
                offset = max(0, len(y_main) - win) / sr
                y_end = y_main[-win:]
                for tpl in credits_data:
                    s, score = get_mfcc_match(y_end, tpl['y'], sr, librosa, np, fftconvolve)
                    ui.log(f"     credits {tpl['name']:28} -> {score:.3f}")
                    if score > best:
                        best = score
                        credits_start = s + offset
                        credits_end = credits_start + tpl['duration']
                valid_credits = best >= cfg["confidence"] and credits_start > intro_end + 10
                ui.log(f"   {'[OK] CREDITS at %.2fs' % credits_start if valid_credits else '[--] weak credits match (%.3f)' % best}")

            if not (valid_intro or valid_credits):
                ui.log("   [SKIP] no matches\n")
                _cleanup(temp_audio, temp_segs)
                continue
            if stop_event.is_set():
                _cleanup(temp_audio, temp_segs)
                break

            fps = probe_video_fps(video)

            # --- source info + output bit depth + size warning ---
            vinfo = probe_video_info(video)
            br = f", ~{vinfo['bitrate_mbps']:.1f} Mbit/s" if vinfo["bitrate_mbps"] else ""
            ui.log(f"   Source video: {vinfo['codec']} {vinfo['bit_depth']}-bit"
                   f" ({vinfo['pix_fmt']}){br}")
            if cfg["bit_depth"] == "auto":
                out_depth = 10 if vinfo["bit_depth"] >= 10 else 8
            else:
                out_depth = int(cfg["bit_depth"])
            if cfg["encoder"] == "h264_nvenc" and out_depth >= 10:
                ui.log("   [WARN] H.264 NVENC cannot encode 10-bit - using 8-bit.")
                out_depth = 8
            src_eff = 2 if vinfo["codec"] in ("hevc", "av1", "vp9") else 1
            tgt_eff = 2 if cfg["encoder"] in ("libx265", "hevc_nvenc", "libsvtav1") else 1
            if tgt_eff < src_eff or (vinfo["bit_depth"] >= 10 and out_depth == 8):
                ui.log("   [WARN] Source codec/bit depth is more efficient than the"
                       " chosen output - the file may get MUCH bigger."
                       " Consider H.265, 10-bit, CRF 22-24, preset slow.")
            ui.log(f"   Output: {cfg['encoder']} {out_depth}-bit,"
                   f" CRF/CQ {cfg['crf']}, preset {cfg['preset']}")

            if cfg["mode"] == "cut":
                keep = []
                if valid_intro and intro_start > 0.5:
                    keep.append(("Cold Open", 0.0, intro_start))
                mid_s = intro_end if valid_intro else 0.0
                mid_e = credits_start if valid_credits else total_duration
                if mid_e > mid_s + 0.5:
                    keep.append(("Main Content", mid_s, mid_e))
                if valid_credits and total_duration - credits_end > 0.5:
                    keep.append(("Post Credits", credits_end, total_duration))

                steps = [n for n, _, _ in keep] + (["Stitching"] if len(keep) > 1 else [])
                n_steps = len(steps)
                ui.log(f"   Steps: {' -> '.join(steps)}")
                audio_streams = probe_audio_streams(video)

                ok = True
                for i, (seg_name, s0, s1) in enumerate(keep, 1):
                    if stop_event.is_set():
                        ok = False
                        break
                    ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  [{i}/{n_steps}] {seg_name}")
                    seg_out = os.path.join(TEMP_DIR, f"seg_{vi}_{i}{ext}")
                    temp_segs.append(seg_out)
                    t0 = time.time()
                    rc = build_ffmpeg_cut(video, seg_out, s0, s1, audio_streams,
                                          cfg["crf"], cfg["preset"], cfg["kf_interval"],
                                          seg_name=seg_name, fps=fps,
                                          encoder=cfg["encoder"],
                                          bit_depth=out_depth, **cb)
                    if rc != 0:
                        if rc != -1:  # -1 = stopped by user, already logged
                            ui.log(f"   [FAIL] {seg_name} failed")
                        ok = False
                        break
                    ui.log(f"   [{i}/{n_steps}] {seg_name}: done in {format_seconds(time.time()-t0)}")

                if ok and len(temp_segs) == 1:
                    if os.path.exists(final_output):
                        os.remove(final_output)
                    shutil.move(temp_segs[0], final_output)
                    ui.log(duration_check_line(
                        final_output, sum(e - s for _, s, e in keep)).rstrip("\n"))
                    ui.log(saved_size_line(final_output, src_size, video_wall))
                elif ok and len(temp_segs) > 1:
                    ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  [{n_steps}/{n_steps}] Stitching")
                    concat_list = os.path.join(TEMP_DIR, f"concat_{vi}.txt")
                    with open(concat_list, "w", encoding="utf-8") as f:
                        for t in temp_segs:
                            f.write(f"file '{os.path.basename(t)}'\n")
                    rc = run_ffmpeg_with_progress(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                         "-c", "copy", "-map", "0", final_output],
                        sum(e - s for _, s, e in keep), **cb)
                    if rc == 0 and os.path.exists(final_output):
                        ui.log(duration_check_line(
                            final_output, sum(e - s for _, s, e in keep)).rstrip("\n"))
                        ui.log(saved_size_line(final_output, src_size, video_wall))
                    elif rc != -1:
                        ui.log("   [FAIL] merge failed\n")
                    try:
                        os.remove(concat_list)
                    except OSError:
                        pass
                elif not stop_event.is_set():
                    ui.log("   [FAIL] skipping this video\n")
            else:
                kf_times = build_forced_keyframe_times(
                    valid_intro, intro_start, intro_end,
                    valid_credits, credits_start, credits_end,
                    cfg["kf_interval"], total_duration)
                ui.log(f"   Injecting {len(kf_times)} forced keyframe(s), no cutting")
                ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  Inject Keyframes")
                rc = build_ffmpeg_inject(video, final_output, kf_times, total_duration,
                                         cfg["crf"], cfg["preset"], fps=fps,
                                         encoder=cfg["encoder"],
                                         bit_depth=out_depth, **cb)
                if rc == 0 and os.path.exists(final_output):
                    ui.log(duration_check_line(
                        final_output, total_duration).rstrip("\n"))
                    ui.log(saved_size_line(final_output, src_size, video_wall))
                elif rc != -1:
                    ui.log("   [FAIL] keyframe injection failed\n")

            _cleanup(temp_audio, temp_segs)

        ui.log("STOPPED by user." if stop_event.is_set() else "FINISHED!")
    finally:
        for w in temp_wavs:
            try:
                if os.path.exists(w):
                    os.remove(w)
            except OSError:
                pass


def _cleanup(temp_audio, temp_segs):
    for p in [temp_audio] + list(temp_segs):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ======================= settings persistence =======================
SETTINGS_FILE = "toolkit_settings.json"

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


# ======================= themes =======================
THEMES = {
    "Light": dict(bg="#f0f0f0", fg="#1a1a1a", field="#ffffff", border="#b0b0b0",
                  tab="#dcdcdc", btn="#e1e1e1", btn_active="#d0d0d0",
                  accent="#2f6fd6", text_bg="#ffffff", text_fg="#1a1a1a",
                  hint="#666666"),
    "Dark": dict(bg="#2b2b2b", fg="#e6e6e6", field="#3c3f41", border="#555555",
                 tab="#3c3f41", btn="#444444", btn_active="#555555",
                 accent="#4a88ff", text_bg="#1e1e1e", text_fg="#dcdcdc",
                 hint="#9a9a9a"),
    "High Contrast": dict(bg="#000000", fg="#ffffff", field="#000000", border="#ffffff",
                          tab="#000000", btn="#000000", btn_active="#333333",
                          accent="#ffff00", text_bg="#000000", text_fg="#ffff00",
                          hint="#00ffff"),
}

def apply_theme(root, style, name):
    t = THEMES.get(name, THEMES["Light"])
    style.theme_use("clam")
    style.configure(".", background=t["bg"], foreground=t["fg"],
                    fieldbackground=t["field"], bordercolor=t["border"],
                    lightcolor=t["bg"], darkcolor=t["bg"])
    style.configure("TFrame", background=t["bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["fg"])
    style.configure("Hint.TLabel", background=t["bg"], foreground=t["hint"])
    style.configure("TNotebook", background=t["bg"])
    style.configure("TNotebook.Tab", background=t["tab"], foreground=t["fg"])
    style.map("TNotebook.Tab", background=[("selected", t["bg"])])
    style.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"],
                    insertcolor=t["fg"])
    style.configure("TCombobox", fieldbackground=t["field"], foreground=t["fg"],
                    arrowcolor=t["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", t["field"])],
              foreground=[("readonly", t["fg"])])
    style.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"],
                    arrowcolor=t["fg"], insertcolor=t["fg"])
    style.configure("TButton", background=t["btn"], foreground=t["fg"])
    style.map("TButton", background=[("active", t["btn_active"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, background=t["bg"], foreground=t["fg"])
        style.map(w, background=[("active", t["bg"])])
    style.configure("Horizontal.TProgressbar", background=t["accent"],
                    troughcolor=t["field"])
    style.configure("TLabelframe", background=t["bg"], bordercolor=t["border"],
                    lightcolor=t["bg"], darkcolor=t["bg"])
    style.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"])
    root.configure(bg=t["bg"])
    # dropdown lists of comboboxes
    root.option_add("*TCombobox*Listbox.background", t["field"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    # plain-tk widgets (Text logs, Canvas) aren't themed by ttk - walk and recolor
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Text):
                c.configure(bg=t["text_bg"], fg=t["text_fg"],
                            insertbackground=t["fg"],
                            selectbackground=t["accent"])
            elif isinstance(c, tk.Canvas):
                c.configure(bg=t["bg"])
            walk(c)
    walk(root)


# ======================= GUI: HH:MM:SS time entry widget =======================
class TimeEntry(ttk.Frame):
    """Three boxes (HH : MM : SS) so times are easy to type.
    Empty box counts as 00; seconds may be decimal (30.5).
    All boxes empty = no time given (used for 'to end of file')."""

    def __init__(self, master):
        super().__init__(master)
        self.h, self.m, self.s = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self._entries = []
        for i, (var, w) in enumerate(((self.h, 3), (self.m, 3), (self.s, 5))):
            e = ttk.Entry(self, textvariable=var, width=w, justify="center")
            e.grid(row=0, column=i * 2)
            self._entries.append(e)
            if i < 2:
                ttk.Label(self, text=":").grid(row=0, column=i * 2 + 1)
        # auto-jump to the next box once two digits are typed
        for i in range(2):
            self._entries[i].bind(
                "<KeyRelease>",
                lambda ev, i=i: self._advance(ev, i))

    def _advance(self, ev, i):
        if ev.char.isdigit() and len(self._entries[i].get()) >= 2:
            self._entries[i + 1].focus_set()
            self._entries[i + 1].icursor("end")

    def get_seconds(self):
        """Returns (seconds_or_None, ok). None+ok=True means 'left empty'."""
        h = self.h.get().strip()
        m = self.m.get().strip()
        s = self.s.get().strip()
        if not (h or m or s):
            return None, True
        try:
            h = int(h) if h else 0
            m = int(m) if m else 0
            s = float(s) if s else 0.0
            if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
                raise ValueError
            return h * 3600 + m * 60 + s, True
        except ValueError:
            return None, False


# ======================= GUI: Tab 1 - Template Cutter =======================
class TemplateTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        pad = {"padx": 6, "pady": 4}

        ttk.Label(self, text="Video file:").grid(row=0, column=0, sticky="w", **pad)
        self.file_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.file_var, width=52).grid(
            row=0, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(self, text="Browse...", command=self.browse).grid(row=0, column=4, **pad)

        # --- Intro section: centered checkbox above its From/To row ---
        self.intro_on = tk.BooleanVar(value=True)
        intro_sec = ttk.Frame(self)
        intro_sec.grid(row=1, column=0, columnspan=5, sticky="we", pady=(10, 2))
        ttk.Checkbutton(intro_sec, text="Intro", variable=self.intro_on).pack(anchor="center")
        irow = ttk.Frame(intro_sec)
        irow.pack(anchor="center", pady=(6, 2))
        ttk.Label(irow, text="From:").pack(side="left", padx=(0, 8))
        self.intro_from = TimeEntry(irow)
        self.intro_from.pack(side="left")
        ttk.Label(irow, text="To:").pack(side="left", padx=(30, 8))
        self.intro_to = TimeEntry(irow)
        self.intro_to.pack(side="left")

        # --- Credits section: same centered layout ---
        self.credits_on = tk.BooleanVar(value=True)
        credits_sec = ttk.Frame(self)
        credits_sec.grid(row=2, column=0, columnspan=5, sticky="we", pady=(12, 2))
        ttk.Checkbutton(credits_sec, text="Credits", variable=self.credits_on).pack(anchor="center")
        crow = ttk.Frame(credits_sec)
        crow.pack(anchor="center", pady=(6, 2))
        ttk.Label(crow, text="From:").pack(side="left", padx=(0, 8))
        self.credits_from = TimeEntry(crow)
        self.credits_from.pack(side="left")
        ttk.Label(crow, text="To:").pack(side="left", padx=(30, 8))
        self.credits_to = TimeEntry(crow)
        self.credits_to.pack(side="left")

        ttk.Label(self, text="Boxes are HH : MM : SS - empty box = 00, seconds may be decimal (30.5).\n"
                             "Credits 'To' fully empty = end of file. Cut the WHOLE intro/credits -\n"
                             "the Remover uses the template length as the cut length.",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=5, sticky="w", **pad)

        self.cut_btn = ttk.Button(self, text="Cut template(s)", command=self.start_cut)
        self.cut_btn.grid(row=4, column=0, columnspan=5, sticky="we", **pad)
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.grid(row=5, column=0, columnspan=5, sticky="we", **pad)
        self.logbox = tk.Text(self, height=8, width=76, state="disabled", font=("Consolas", 9))
        self.logbox.grid(row=6, column=0, columnspan=5, sticky="nsew", **pad)
        # let the log grow when the window is resized
        self.rowconfigure(6, weight=1)
        self.columnconfigure(2, weight=1)

    def browse(self):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")])
        if path:
            self.file_var.set(path)

    def log(self, msg):
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def start_cut(self):
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror("Error", "Please select a valid video file.")
            return
        jobs = []
        if self.intro_on.get():
            s, s_ok = self.intro_from.get_seconds()
            e, e_ok = self.intro_to.get_seconds()
            if not s_ok or not e_ok or s is None or e is None or e <= s:
                messagebox.showerror("Error", "Intro needs valid From and To times, with To after From.")
                return
            jobs.append(("intro", s, e))
        if self.credits_on.get():
            s, s_ok = self.credits_from.get_seconds()
            e, e_ok = self.credits_to.get_seconds()
            if not s_ok or not e_ok or s is None or (e is not None and e <= s):
                messagebox.showerror("Error", "Credits needs a valid From time; To (optional) must be after From.")
                return
            jobs.append(("credits", s, e))
        if not jobs:
            messagebox.showerror("Error", "Enable Intro and/or Credits first.")
            return
        self.cut_btn.configure(state="disabled")
        self.bar.start(12)
        threading.Thread(target=self.worker, args=(video, jobs), daemon=True).start()

    def worker(self, video, jobs):
        name, ext = os.path.splitext(os.path.basename(video))
        for kind, s, e in jobs:
            outdir = INTRO_DIR if kind == "intro" else CREDITS_DIR
            os.makedirs(outdir, exist_ok=True)
            out = os.path.join(outdir, f"{name}_{kind}{ext}")
            self.log(f"[{kind.upper()}] {fmt_time(s)} -> {fmt_time(e) if e is not None else 'end'}  ->  {out}")
            cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", video, "-ss", "0"]
            if e is not None:
                cmd += ["-t", f"{e - s:.3f}"]
            cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "superfast",
                    "-c:a", "copy", out]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=POPEN_FLAGS)
            if proc.returncode == 0:
                self.log("  [OK] saved")
            else:
                for ln in proc.stderr.decode(errors="replace").strip().splitlines()[-4:]:
                    self.log(f"    | {ln}")
                self.log(f"  [FAIL] {kind} template failed")
        self.log("Done.")

        def _f():
            self.bar.stop()
            self.cut_btn.configure(state="normal")
        self.after(0, _f)


# ======================= GUI: Tab 2 - Batch Remover =======================
class RemoverTab(ttk.Frame):
    def __init__(self, master, saved=None):
        super().__init__(master, padding=10)
        pad = {"padx": 6, "pady": 3}
        self.stop_event = threading.Event()
        saved = saved or {}
        self.save_hook = None  # set by main(); called on Start to persist settings

        # folder rows
        self.dirs = {}
        for r, (label, default) in enumerate([
                ("Videos folder:", saved.get("video_dir", VIDEO_DIR)),
                ("Intro templates:", saved.get("intro_dir", INTRO_DIR)),
                ("Credits templates:", saved.get("credits_dir", CREDITS_DIR)),
                ("Output folder:", saved.get("output_dir", OUTPUT_DIR))]):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", **pad)
            var = tk.StringVar(value=default)
            self.dirs[label] = var
            ttk.Entry(self, textvariable=var, width=44).grid(row=r, column=1, columnspan=3, sticky="we", **pad)
            ttk.Button(self, text="...", width=3,
                       command=lambda v=var: self._pick_dir(v)).grid(row=r, column=4, **pad)

        # codec + bit depth row
        ttk.Label(self, text="Video codec:").grid(row=4, column=0, sticky="w", **pad)
        codec_saved = saved.get("codec", DEFAULT_CODEC_LABEL)
        self.codec_var = tk.StringVar(
            value=codec_saved if codec_saved in CODECS else DEFAULT_CODEC_LABEL)
        ttk.Combobox(self, textvariable=self.codec_var, values=list(CODECS.keys()),
                     state="readonly", width=38).grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(self, text="Bit depth:").grid(row=4, column=3, sticky="e", **pad)
        depth_saved = saved.get("bit_depth", "Auto (match source)")
        self.depth_var = tk.StringVar(
            value=depth_saved if depth_saved in BIT_DEPTHS else "Auto (match source)")
        ttk.Combobox(self, textvariable=self.depth_var, values=list(BIT_DEPTHS.keys()),
                     state="readonly", width=16).grid(row=4, column=4, sticky="w", **pad)

        # settings row
        ttk.Label(self, text="CRF / CQ:").grid(row=5, column=0, sticky="w", **pad)
        # CRF 18 + slow: closest to the original quality (near-transparent)
        self.crf_var = tk.StringVar(value=str(saved.get("crf", "18")))
        ttk.Spinbox(self, from_=0, to=51, textvariable=self.crf_var, width=5).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(self, text="Preset:").grid(row=5, column=2, sticky="e", **pad)
        preset_saved = saved.get("preset", "slow")
        self.preset_var = tk.StringVar(
            value=preset_saved if preset_saved in VALID_PRESETS else "slow")
        ttk.Combobox(self, textvariable=self.preset_var, values=VALID_PRESETS,
                     state="readonly", width=10).grid(row=5, column=3, sticky="w", **pad)

        ttk.Label(self, text="Keyframe every N s (0 = cut points only):").grid(row=6, column=0, columnspan=2, sticky="w", **pad)
        self.kf_var = tk.StringVar(value=str(saved.get("kf_interval", "0")))
        ttk.Entry(self, textvariable=self.kf_var, width=6).grid(row=6, column=2, sticky="w", **pad)
        ttk.Label(self, text="Min confidence:").grid(row=6, column=3, sticky="e", **pad)
        self.conf_var = tk.StringVar(value=str(saved.get("confidence", "0.32")))
        ttk.Entry(self, textvariable=self.conf_var, width=6).grid(row=6, column=4, sticky="w", **pad)

        self.mode_var = tk.StringVar(
            value=saved.get("mode", "cut") if saved.get("mode") in ("cut", "inject") else "cut")
        ttk.Radiobutton(self, text="Cut intro/credits out", variable=self.mode_var,
                        value="cut").grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(self, text="Keep video, inject keyframes only", variable=self.mode_var,
                        value="inject").grid(row=7, column=2, columnspan=3, sticky="w", **pad)

        # run controls
        self.start_btn = ttk.Button(self, text="Start", command=self.start)
        self.start_btn.grid(row=8, column=0, columnspan=4, sticky="we", **pad)
        self.stop_btn = ttk.Button(self, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=8, column=4, sticky="we", **pad)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status_var).grid(row=9, column=0, columnspan=5, sticky="w", **pad)
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self.bar.grid(row=10, column=0, columnspan=4, sticky="we", **pad)
        self.pct_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.pct_var, width=24).grid(row=10, column=4, sticky="w", **pad)

        self.logbox = tk.Text(self, height=10, width=76, state="disabled", font=("Consolas", 9))
        self.logbox.grid(row=11, column=0, columnspan=5, sticky="nsew", **pad)
        # let the log grow when the window is resized
        self.rowconfigure(11, weight=1)
        self.columnconfigure(2, weight=1)

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="Select folder")
        if path:
            var.set(path)

    # --- ui callbacks (thread-safe) ---
    def log(self, msg):
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def progress(self, frac, text):
        def _a():
            self.bar["value"] = int(frac * 1000)
            self.pct_var.set(text)
        self.after(0, _a)

    # --- run control ---
    def start(self):
        try:
            crf = int(self.crf_var.get())
            assert 0 <= crf <= 51
            kf = float(self.kf_var.get())
            assert kf >= 0
            conf = float(self.conf_var.get())
        except (ValueError, AssertionError):
            messagebox.showerror("Error", "Check CRF (0-51), keyframe interval (>=0) and confidence.")
            return
        cfg = {
            "video_dir": self.dirs["Videos folder:"].get(),
            "intro_dir": self.dirs["Intro templates:"].get(),
            "credits_dir": self.dirs["Credits templates:"].get(),
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": crf, "preset": self.preset_var.get(),
            "encoder": CODECS.get(self.codec_var.get(), "libx264"),
            "bit_depth": BIT_DEPTHS.get(self.depth_var.get(), "auto"),
            "kf_interval": kf, "confidence": conf, "mode": self.mode_var.get(),
        }
        if self.save_hook:
            self.save_hook()  # remember settings for next launch
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg):
        try:
            run_batch(cfg, self, self.stop_event)
        except Exception as e:
            self.log(f"[FAIL] Unexpected error: {e!r}")
        def _f():
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.status_var.set("Idle")
        self.after(0, _f)

    def stop(self):
        self.stop_event.set()
        self.log("Stopping after current ffmpeg call...")

    def snapshot(self):
        """Current settings as a dict for persistence."""
        return {
            "video_dir": self.dirs["Videos folder:"].get(),
            "intro_dir": self.dirs["Intro templates:"].get(),
            "credits_dir": self.dirs["Credits templates:"].get(),
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": self.crf_var.get(),
            "preset": self.preset_var.get(),
            "codec": self.codec_var.get(),
            "bit_depth": self.depth_var.get(),
            "kf_interval": self.kf_var.get(),
            "confidence": self.conf_var.get(),
            "mode": self.mode_var.get(),
        }


# ======================= GUI: Info + Recommended tabs =======================
INFO_SECTIONS = [
    ("Video codec",
     "H.264 (libx264) - plays on everything (TVs, phones, old devices). "
     "Baseline choice. Biggest files of the modern codecs.\n\n"
     "H.265 (libx265) - roughly 30-50% smaller files than H.264 at the same "
     "visual quality. Needs a reasonably modern player/TV. Encoding is 2-4x "
     "slower than H.264.\n\n"
     "H.264 / H.265 NVENC - uses your NVIDIA GPU. 5-15x faster encoding, but "
     "at the same file size the quality is a bit lower than the CPU (libx26x) "
     "encoders. Great for bulk jobs where time matters more than megabytes.\n\n"
     "AV1 (libsvtav1) - smallest files of all (~20-30% below H.265), but slow "
     "to encode and only newer devices play it. Best for archiving on a "
     "machine you can leave running."),
    ("Bit depth - 8 vs 10",
     "10-bit stores brightness/color in finer steps. Even for 8-bit sources, "
     "encoding in 10-bit usually compresses ~5% BETTER (less rounding noise "
     "inside the encoder) and strongly reduces banding in smooth gradients - "
     "especially anime.\n\n"
     "Catches: 10-bit H.264 (Hi10P) has almost no hardware decoding support, "
     "so pair 10-bit with H.265 instead - modern devices decode HEVC main10 "
     "in hardware. H.264 NVENC cannot do 10-bit at all (the tool falls back "
     "to 8-bit and says so in the log).\n\n"
     "Auto (recommended) matches the source: 10-bit source -> 10-bit output. "
     "The log shows each source's codec, bit depth and bitrate before "
     "encoding, and warns when your settings will likely inflate the file."),
    ("CRF / CQ - quality knob (0-51, LOWER = better quality = bigger file)",
     "Logarithmic scale: +6 roughly halves the file size.\n\n"
     "H.264: 18 = visually lossless, 20-23 = great, 26+ = visible loss.\n"
     "H.265: 20 = visually lossless, 22-26 = great (x265 numbers sit ~2-4 "
     "higher than x264 for the same look).\n"
     "NVENC: CQ works the same idea; use ~2 lower than you would for CPU.\n"
     "AV1: 26-32 is the typical sweet spot."),
    ("Preset - speed knob (does NOT change quality, changes file size)",
     "With CRF, quality is fixed; the preset decides how hard the encoder "
     "works to COMPRESS that quality. Slower preset = same look, smaller "
     "file.\n\n"
     "ultrafast = biggest files, instant.  medium = the balanced default.  "
     "slow = ~5-10% smaller than medium at ~2x encode time (sweet spot).  "
     "veryslow = diminishing returns, mostly not worth it."),
    ("Keyframe interval (Batch Remover)",
     "0 (default): a clean keyframe is forced exactly at every cut point - "
     "that is all a normal player needs. Set 2-5 s if your media server "
     "scrubs/skips inside the Cold Open / Post Credits and you want every "
     "seek frame-perfect. Smaller interval = slightly bigger file (only "
     "affects those short segments)."),
    ("Min confidence (Batch Remover)",
     "How strong the audio match must be to accept an intro/credits "
     "detection.\n\n"
     "0.32 (default) works for most shows. Raise to 0.40+ if it cuts things "
     "it should not (false matches). Lower to ~0.25 if it misses intros it "
     "should find - check the per-template scores in the log to see how "
     "close it was."),
    ("Modes",
     "Cut: removes intro/credits, keeps cold open + post-credits, stitches "
     "the rest back together.\n\n"
     "Inject: keeps the whole video, only inserts keyframes at the detected "
     "boundaries (for media servers with their own skip buttons)."),
    ("Templates (Template Cutter tab)",
     "Only the AUDIO of a template matters (matching is done on sound), so "
     "the template tab always uses fast x264 - no need to configure it.\n\n"
     "IMPORTANT: the Remover uses the template LENGTH as the cut length, so "
     "cut the WHOLE intro/credits, not just a recognizable part."),
]

RECOMMENDED_SECTIONS = [
    ("Closest to original quality  (the tool's default)",
     "H.264,  CRF 18,  preset slow,  bit depth Auto\n\n"
     "Visually indistinguishable from the source. The file lands near the "
     "original's size - sometimes a bit over, which is the price of "
     "re-encoding an already-compressed file. Use CRF 17 if you ever spot "
     "a difference in very dark scenes."),
    ("Best balance  (recommended)",
     "H.265,  CRF 22,  preset slow\n\n"
     "High quality + small files. Use preset 'medium' if encodes feel too slow."),
    ("Anime / 10-bit sources (Hi10P)",
     "H.265,  10-bit,  CRF 22-23,  preset slow\n\n"
     "Matches the efficiency of typical fansub Hi10P H.264 encodes. Keeping "
     "8-bit H.264 output for such sources can easily triple the file size."),
    ("Max compatibility",
     "H.264,  CRF 20,  preset medium\n\n"
     "For old TVs/devices, or when unsure what will play the files."),
    ("Fast bulk job  (NVIDIA GPU)",
     "H.265 NVENC,  CQ 24,  preset slow\n\n"
     "Encodes many episodes quickly with good (not maximal) efficiency."),
    ("Smallest archive",
     "AV1,  CRF 28,  preset slow\n\n"
     "Leave it running overnight; check that your players support AV1."),
    ("Note on audio & subtitles",
     "Audio is always re-encoded to the same codec/channels at high bitrate "
     "and subtitles are copied - none of the settings above touch them."),
]


class SectionList(ttk.Frame):
    """Scrollable list of titled boxes (LabelFrames), reflows on resize."""
    def __init__(self, master, sections, height=430):
        super().__init__(master, padding=(10, 8))
        canvas = tk.Canvas(self, height=height, width=660,
                           highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._labels = []
        for title, body in sections:
            box = ttk.LabelFrame(inner, text=f" {title} ", padding=(10, 6))
            box.pack(fill="x", expand=True, padx=4, pady=5)
            lbl = ttk.Label(box, text=body, wraplength=580, justify="left")
            lbl.pack(anchor="w", fill="x")
            self._labels.append(lbl)

        def on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", on_inner)

        def on_canvas(e):
            canvas.itemconfigure(inner_id, width=e.width)
            for lbl in self._labels:
                lbl.configure(wraplength=max(300, e.width - 60))
        canvas.bind("<Configure>", on_canvas)

        def on_wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))


class InfoTab(SectionList):
    def __init__(self, master):
        super().__init__(master, INFO_SECTIONS)


class RecommendedTab(SectionList):
    def __init__(self, master):
        super().__init__(master, RECOMMENDED_SECTIONS)


# ======================= main =======================
def main():
    # premade working folders next to the script
    for d in (INTRO_DIR, CREDITS_DIR, VIDEO_DIR, OUTPUT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    saved = load_settings()

    root = tk.Tk()
    root.title("Intro & Credits Toolkit")
    root.resizable(True, True)
    root.minsize(720, 460)
    saved_geo = saved.get("geometry", "900x560")
    if re.fullmatch(r"\d{3,4}x\d{3,4}(\+-?\d+\+-?\d+)?", str(saved_geo)):
        root.geometry(saved_geo)
    else:
        root.geometry("900x560")
    style = ttk.Style(root)

    # top bar with theme selector
    top = ttk.Frame(root, padding=(10, 6, 10, 0))
    top.pack(fill="x")
    ttk.Label(top, text="Theme:").pack(side="left")
    theme_saved = saved.get("theme", "Light")
    theme_var = tk.StringVar(value=theme_saved if theme_saved in THEMES else "Light")
    theme_box = ttk.Combobox(top, textvariable=theme_var, values=list(THEMES.keys()),
                             state="readonly", width=14)
    theme_box.pack(side="left", padx=6)

    nb = ttk.Notebook(root)
    template_tab = TemplateTab(nb)
    remover_tab = RemoverTab(nb, saved=saved)
    nb.add(template_tab, text="  Template Cutter  ")
    nb.add(remover_tab, text="  Batch Remover  ")
    nb.add(InfoTab(nb), text="  Info / Settings  ")
    nb.add(RecommendedTab(nb), text="  Recommended  ")
    nb.pack(fill="both", expand=True)

    def persist():
        d = remover_tab.snapshot()
        d["theme"] = theme_var.get()
        d["geometry"] = root.geometry()  # remember window size/position too
        save_settings(d)

    def on_theme_change(_evt=None):
        apply_theme(root, style, theme_var.get())
        persist()

    theme_box.bind("<<ComboboxSelected>>", on_theme_change)
    apply_theme(root, style, theme_var.get())

    remover_tab.save_hook = persist  # also persist whenever Start is pressed

    def on_close():
        persist()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
