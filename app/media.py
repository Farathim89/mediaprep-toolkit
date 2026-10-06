"""ffmpeg/ffprobe helpers, time formatting, segment maths, and the detection
and cutting engines (batch auto-detect + single-video manual cut)."""
import os
import re
import errno
import glob
import math
import json
import shutil
import subprocess
import threading
import time

from .config import (POPEN_FLAGS, TEMP_DIR, NVENC_PRESET_MAP, SVT_PRESET_MAP,
                     AUDIO_RECODE_MAP, DEFAULT_AUDIO_RECODE, AUTO_CODEC_MAP,
                     AUTO_GPU_CODEC_MAP, INTRO_SEARCH_WINDOW, CREDITS_SEARCH_WINDOW)


def resolve_encoder(encoder, src_codec, log=None):
    """'auto' -> the CPU encoder of the source's codec family (AUTO_CODEC_MAP;
    unknown/H.264 -> libx264). 'auto_gpu' -> the NVENC hardware encoder of the
    same family (AUTO_GPU_CODEC_MAP; unknown/H.264 -> h264_nvenc). Explicit
    choices pass through unchanged."""
    if encoder == "auto":
        chosen = AUTO_CODEC_MAP.get(src_codec, "libx264")
        if log:
            log(f"   Auto codec (CPU): source is {src_codec or 'unknown'} -> {chosen}")
        return chosen
    if encoder == "auto_gpu":
        chosen = AUTO_GPU_CODEC_MAP.get(src_codec, "h264_nvenc")
        if log:
            log(f"   Auto codec (GPU): source is {src_codec or 'unknown'} -> {chosen}")
        return chosen
    return encoder


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


# ======================= small helpers =======================
def parse_time(text):
    """'HH:MM:SS(.ms)', 'HH:MM:SS:mmm', 'MM:SS' or plain seconds -> float
    seconds, else None."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]?\d)[:.](\d{1,3})", text)
    if m:
        return (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000.0)
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
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=POPEN_FLAGS, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    try:
        return float(r.stdout.decode(errors="replace").strip())
    except ValueError:
        return None


def probe_video_duration(path):
    """Duration of the VIDEO stream itself, independent of the container. A
    lingering copied subtitle can push the container duration well past the
    video, so the duration check uses this to avoid false alarms. Uses the
    per-stream duration/DURATION tag (instant); returns None if unavailable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration:stream_tags=DURATION",
             "-of", "default=nw=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=POPEN_FLAGS, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    val = None
    for line in r.stdout.decode(errors="replace").splitlines():
        v = line.split("=", 1)[-1].strip()
        if not v or v == "N/A":
            continue
        if ":" in v:                                  # HH:MM:SS.mmm tag form
            try:
                h, m, s = v.split(":")
                val = int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                continue
        else:                                         # plain seconds
            try:
                val = float(v)
            except ValueError:
                continue
    return val


def duration_check_line(final_output, expected_sec):
    """Verifies no VIDEO content was lost: the video length must match the sum
    of the kept segments. Measures the video stream (not the container), so a
    subtitle event lingering past the picture doesn't trip a false alarm. Falls
    back to container duration if the frame count can't be read."""
    got = probe_video_duration(final_output)
    basis = "video"
    if got is None:
        got = probe_duration(final_output)
        basis = "file"
    if got is None:
        return "   Duration check: could not probe output\n"
    diff = got - expected_sec
    tag = "[OK]" if abs(diff) < 1.0 else "[WARN]"
    return (f"   {tag} Duration check ({basis}): expected {expected_sec:.2f}s,"
            f" got {got:.2f}s ({diff:+.2f}s)\n")


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
    """Display format HH:MM:SS:mmm (milliseconds as a 4th colon group)."""
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}:{ms:03d}"


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
             "channels": s.get("channels", 2),
             "sample_rate": int(s["sample_rate"]) if str(s.get("sample_rate", "")).isdigit() else None,
             "lang": ((s.get("tags") or {}).get("language") or "und").lower()}
            for s in data.get("streams", [])]


def probe_sample_rate(input_file):
    """Sample rate (Hz) of the first audio stream, or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", input_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=POPEN_FLAGS, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    v = r.stdout.decode(errors="replace").strip().split(",")[0].strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


def audio_track_for_lang(input_file, lang):
    """Index (within the audio streams, i.e. 0:a:N) of the first audio track
    whose language matches `lang`; None if there isn't one. Used so detection
    can fingerprint a chosen language track instead of the default."""
    if not lang:
        return None
    for i, s in enumerate(probe_audio_streams(input_file)):
        if s.get("lang") == lang.lower():
            return i
    return None


def build_audio_recode_args(audio_streams):
    args = []
    for i, s in enumerate(audio_streams):
        out_codec, bitrate = AUDIO_RECODE_MAP.get(s["codec_name"], DEFAULT_AUDIO_RECODE)
        args += [f"-c:a:{i}", out_codec]
        if bitrate:
            args += [f"-b:a:{i}", bitrate]
        ch = s["channels"]
        # ffmpeg's (E-)AC3 encoders top out at 5.1 - fold 7.1 down instead of failing
        if out_codec in ("ac3", "eac3") and ch and ch > 6:
            ch = 6
        # -ac:a:N = Nth AUDIO stream; a bare -ac:N would hit output stream N
        # (with -map 0 that is the video/another track, downmixing 5.1)
        args += [f"-ac:a:{i}", str(ch)]
    return args


def _set_no_power_throttle(handle):
    """Opt one process (by OS handle) out of Windows 11 power throttling
    (EcoQoS) so it always runs at full execution speed. Best effort."""
    if os.name != "nt":
        return
    try:
        import ctypes

        class _PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong),
                        ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]

        # Version=1, control EXECUTION_SPEED (0x1), state 0 = throttling OFF
        state = _PowerThrottlingState(1, 1, 0)
        ctypes.windll.kernel32.SetProcessInformation(
            handle, 4,  # 4 = ProcessPowerThrottling
            ctypes.byref(state), ctypes.sizeof(state))
    except Exception:
        pass                       # best effort - older Windows lacks the API


def _exempt_from_power_throttling(proc):
    """Mark an ffmpeg child as 'never throttle execution speed' so it keeps
    full speed when the app window is minimized / in the background. On hybrid
    CPUs throttling otherwise shoves the work onto the slow E-cores."""
    if os.name != "nt":
        return
    try:
        _set_no_power_throttle(int(proc._handle))
    except Exception:
        pass


def prevent_power_throttling():
    """Opt the CURRENT process out of power throttling. Child processes inherit
    this, so every ffmpeg/ffprobe the app spawns (encodes, audio normalize,
    probes) keeps full speed even while the window is minimized. Call once at
    startup. Belt-and-suspenders alongside the per-child exemption."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        _set_no_power_throttle(k32.GetCurrentProcess())
    except Exception:
        pass


def _run_exempt(cmd, **kw):
    """subprocess.run replacement that opts the child out of power throttling,
    so blocking ffmpeg calls (audio normalize / gain) keep full speed when the
    window is minimized. Returns a CompletedProcess like subprocess.run."""
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    kw.setdefault("creationflags", POPEN_FLAGS)
    proc = subprocess.Popen(cmd, **kw)
    _exempt_from_power_throttling(proc)
    out, err = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _run_capture_stoppable(cmd, stop_event=None):
    """Like _run_exempt but polls stop_event and terminates ffmpeg if set.
    Returns (returncode, stderr_text); rc is -1 if stopped. Used for the short
    measurement pass so Stop is responsive even before the long encode.

    stderr goes to a temp FILE, not a pipe: ffmpeg emits a lot of stderr on a
    long file, and an unread pipe fills its OS buffer and deadlocks ffmpeg (the
    'starts but nothing happens' hang). A file never blocks the writer."""
    global _progress_file_counter
    _progress_file_counter += 1
    os.makedirs(TEMP_DIR, exist_ok=True)
    errpath = os.path.join(TEMP_DIR, f"meas_{os.getpid()}_{_progress_file_counter}.log")
    text, rc = "", -1
    with open(errpath, "w", encoding="utf-8", errors="replace") as errfh:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errfh,
                                creationflags=POPEN_FLAGS)
        _exempt_from_power_throttling(proc)
        stopped = False
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                try:
                    proc.terminate()
                except OSError:
                    pass
                proc.wait()
                stopped = True
                break
            time.sleep(0.1)
        rc = -1 if stopped else proc.returncode
    if not stopped:
        try:
            with open(errpath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
    try:
        os.remove(errpath)
    except OSError:
        pass
    return rc, text


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
    _exempt_from_power_throttling(proc)

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


def build_stream_maps(input_file, subs_lang=None):
    """ffmpeg -map args. subs_lang=None (or empty) keeps EVERYTHING (-map 0).
    Otherwise keeps video, all audio, all attachments (fonts), and only the
    subtitle tracks whose language is in subs_lang (a set like {"eng"}) - this
    keeps English 'forced' tracks too, since they're tagged eng. Falls back to
    -map 0 if the subtitle languages can't be probed."""
    if not subs_lang:
        return ["-map", "0"]
    info = probe_streams(input_file)
    if not info:
        return ["-map", "0"]
    maps = ["-map", "0:v", "-map", "0:a?"]
    for i, s in enumerate(info["subtitle"]):
        if (s.get("lang") or "und").lower() in subs_lang:
            maps += ["-map", f"0:s:{i}"]
    # attachments (fonts) LAST - mapping them before the subtitle streams makes
    # the muxer reject packets ("Invalid argument") under -copyts, the same
    # order -map 0 uses
    maps += ["-map", "0:t?"]
    return maps


def build_ffmpeg_cut(input_file, output_file, start_sec, end_sec, audio_streams,
                     crf, preset, kf_interval, seg_name=None, fps=None,
                     encoder="libx264", bit_depth=8, subs_lang=None, **cb):
    """
    Frame-accurate segment cut (v18 approach):
      input -ss (start-10s) fast keyframe seek, -copyts to keep original
      timestamps, output -ss for the exact frame, -output_ts_offset -start
      to rebase the segment to 0 so segments stand alone and stitch cleanly.
    subs_lang: keep only these subtitle languages (e.g. {"eng"}); None = all.
    """
    seg_duration = end_sec - start_sec
    seek1 = max(0.0, start_sec - 10.0)
    cmd = ["ffmpeg", "-y", "-ss", f"{seek1:.6f}", "-copyts", "-i", input_file,
           "-ss", f"{start_sec:.6f}", "-t", f"{seg_duration:.6f}",
           "-output_ts_offset", f"{-start_sec:.6f}"] + build_stream_maps(input_file, subs_lang)
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
                        crf, preset, fps=None, encoder="libx264", bit_depth=8,
                        subs_lang=None, **cb):
    cmd = ["ffmpeg", "-y", "-i", input_file] + build_stream_maps(input_file, subs_lang)
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


def compute_keep_segments(total_duration, pre_intro=None, intro=None,
                          credits=None, after_credits=None, min_seg=0.5):
    """Turn boundary points into the list of segments to KEEP.

    pre_intro:     keep starts here (drop everything before). None -> 0.
    after_credits: keep ends here (drop everything after).   None -> end.
    intro/credits: (start, end) ranges to DROP, or None.

    Returns [(start, end), ...] left-to-right, tiny slivers (< min_seg) removed.
    Overlapping drops are merged, so it is safe if a manual intro range bleeds
    into the credits range or past the trim points.
    """
    start = 0.0 if pre_intro is None else max(0.0, pre_intro)
    end = total_duration if after_credits is None else min(total_duration, after_credits)
    if end <= start:
        return []
    drops = []
    for rng in (intro, credits):
        if rng and rng[1] > rng[0]:
            ds, de = max(rng[0], start), min(rng[1], end)
            if de > ds:
                drops.append((ds, de))
    drops.sort()
    keep, cur = [], start
    for ds, de in drops:
        if ds > cur:
            keep.append((cur, ds))
        cur = max(cur, de)
    if end > cur:
        keep.append((cur, end))
    return [(s, e) for s, e in keep if e - s > min_seg]


def keep_from_drops(total_duration, drops, min_seg=0.5):
    """Complement of a set of DROP ranges within [0, total]: returns the KEEP
    segments [(s, e), ...] with overlaps merged and slivers (< min_seg) removed."""
    clean = []
    for rng in drops:
        if rng and rng[1] > rng[0]:
            a, b = max(0.0, rng[0]), min(total_duration, rng[1])
            if b > a:
                clean.append((a, b))
    clean.sort()
    keep, cur = [], 0.0
    for ds, de in clean:
        if ds > cur:
            keep.append((cur, ds))
        cur = max(cur, de)
    if total_duration > cur:
        keep.append((cur, total_duration))
    return [(s, e) for s, e in keep if e - s > min_seg]


def keyframe_times_from_ranges(ranges, keyframe_interval, total_duration):
    """Forced-keyframe times from any number of (valid, start, end) ranges."""
    times = set()
    for valid, a, b in ranges:
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
    """Find where `y_temp` best lines up inside `y_main` and how well it fits.
    Returns (time_sec, score).

    The score's SCALE is deliberately compressed: even a near-perfect match
    reads ~0.7 (not 1.0), because the whole search window is z-normalized
    globally. That global normalization is exactly what makes this a strong
    *discriminator* - it crushes wrong matches down to ~0.05-0.15, leaving a
    huge gap between a real match (~0.5-0.7) and a coincidence. A locally-
    normalized cross-correlation reads a prettier ~1.0 on a perfect match but
    inflates wrong matches to ~0.6 (taking the max over a long window), which
    squeezes that gap and causes mistakes - measured and rejected, see history.
    So the number looks low but the detection is reliable; keep the confidence
    threshold around 0.3-0.4."""
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


def audio_fingerprint(path, sr=22050, hop=512, n_mfcc=13):
    """Mono-audio MFCC (z-normed) for alignment, plus feature-frames-per-second.
    Finer hop than detection (512 -> ~23 ms) for frame-accurate alignment.
    Returns (M, fps) or (None, 0.0) if the audio can't be read."""
    import librosa
    os.makedirs(TEMP_DIR, exist_ok=True)
    dst = os.path.join(TEMP_DIR, f"_align_{abs(hash(path)) % 10**8}.wav")
    try:
        if not extract_wav(path, dst):
            return None, 0.0
        y, _ = librosa.load(dst, sr=sr, mono=True)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    if y is None or len(y) < sr:
        return None, 0.0
    M = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc, hop_length=hop)
    M = (M - M.mean(axis=1, keepdims=True)) / (M.std(axis=1, keepdims=True) + 1e-8)
    return M, sr / hop


def align_window(M_ref, fps, M_new, new_pos, length):
    """Find where the new file's audio at [new_pos, new_pos+length] best lines
    up inside the reference (cross-correlating the precomputed MFCCs). Returns
    (ref_pos_sec, score); score ~1 = confident, low = no good match."""
    import numpy as np
    from scipy.signal import fftconvolve
    a = max(0, int(new_pos * fps))
    b = int((new_pos + length) * fps) if length else M_new.shape[1]
    needle = M_new[:, a:b]
    if needle.shape[1] < 4 or M_ref.shape[1] < needle.shape[1]:
        return None, 0.0
    scores = np.zeros(M_ref.shape[1] - needle.shape[1] + 1)
    for i in range(needle.shape[0]):
        scores += fftconvolve(M_ref[i], needle[i, ::-1], mode="valid")
    scores /= needle.shape[0] * needle.shape[1]
    j = int(np.argmax(scores))
    # parabolic interpolation around the peak for sub-frame (sub-hop) accuracy,
    # so the aligned position lands within a fraction of a video frame
    delta = 0.0
    if 0 < j < len(scores) - 1:
        a, b, c = scores[j - 1], scores[j], scores[j + 1]
        denom = a - 2 * b + c
        if denom != 0:
            delta = max(-0.5, min(0.5, 0.5 * (a - c) / denom))
    return (j + delta) / fps, float(scores[j])


def extract_wav(src, dst, track=None):
    """Decode audio to mono 22.05 kHz WAV. track=None uses the default audio
    stream; an int picks that audio stream (0:a:N) so each language track can
    be fingerprinted separately."""
    cmd = ["ffmpeg", "-y", "-i", src]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-vn", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1", dst]
    ret = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=POPEN_FLAGS)
    return ret.returncode == 0


def best_match_over(slices, tpl_y, sr, librosa, np, fftconvolve):
    """Match a template against several audio-track slices (same timeline) and
    return the best (time, score). Lets a template match whichever language
    track it was cut from, so the episode's default track no longer matters."""
    best_t, best_s = 0.0, 0.0
    for y in slices:
        t, s = get_mfcc_match(y, tpl_y, sr, librosa, np, fftconvolve)
        if s > best_s:
            best_t, best_s = t, s
    return best_t, best_s


def anchor_match(slices, tpl_y, sr, anchor_secs, librosa, np, fftconvolve):
    """Locate a segment by matching only the FIRST and LAST `anchor_secs` of the
    template, independently - so the cut spans the real boundaries in each
    episode even if the segment's length varies. Returns (start, end, score)
    relative to the slice, or None if the template is too short to split or the
    ends don't line up sensibly. score is the weaker of the two ends (both must
    be found)."""
    N = int(anchor_secs * sr)
    if len(tpl_y) < 2 * N + int(1.0 * sr):     # too short to take two ends
        return None
    hs, h_score = best_match_over(slices, tpl_y[:N], sr, librosa, np, fftconvolve)
    ts, t_score = best_match_over(slices, tpl_y[-N:], sr, librosa, np, fftconvolve)
    start, end = hs, ts + anchor_secs
    tpl_len = len(tpl_y) / sr
    # the found length must be in a sane range of the template (guards against
    # one end mis-locating); otherwise fall back to the full-template method
    if not (0.5 * tpl_len <= (end - start) <= 1.6 * tpl_len):
        return None
    # report the AVERAGE of the two ends (comparable to a full-template score),
    # not the min - the intro's start is often the less distinctive end, and
    # taking the min made a solid match look weak and fall under the threshold.
    # The length check above already ensures both ends landed sensibly.
    return start, end, (h_score + t_score) / 2


def matched_seconds(tracks, start_sec, tpl_y, sr, librosa, np,
                    hop=2048, thresh=0.5, max_gap_s=0.8):
    """How many seconds from `start_sec` the template audio actually keeps
    matching the episode - so a fixed-length template doesn't overcut an
    episode whose segment is genuinely shorter. Compares template vs episode
    frame-by-frame (best of all audio tracks) and finds where the run stops,
    bridging brief quiet dips. Returns the matched length in seconds, or the
    full template length if it can't tell (never trims on a bad read)."""
    def feats(y):
        m = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
        # z-norm each coefficient (kills the coeff-0/energy bias that would make
        # even unrelated audio look similar), then L2-normalize each frame so the
        # per-frame dot product is a clean cosine similarity
        m = (m - m.mean(axis=1, keepdims=True)) / (m.std(axis=1, keepdims=True) + 1e-8)
        return m / (np.linalg.norm(m, axis=0, keepdims=True) + 1e-8)

    tpl_len = len(tpl_y) / sr
    Mt = feats(tpl_y)
    fps = sr / hop
    max_gap = int(max_gap_s * fps)
    a = int(start_sec * sr)
    best_len = 0.0
    for y in tracks:
        reg = y[a:a + len(tpl_y) + int(2 * sr)]
        if len(reg) < sr:
            continue
        Mr = feats(reg)
        L = min(Mt.shape[1], Mr.shape[1])
        if L < 3:
            continue
        sim = np.sum(Mt[:, :L] * Mr[:, :L], axis=0)
        if L >= 5:
            sim = np.convolve(sim, np.ones(5) / 5, mode="same")
        last_good, gap = -1, 0
        for i in range(L):
            if sim[i] >= thresh:
                last_good, gap = i, 0
            else:
                gap += 1
                if gap > max_gap and last_good >= 0:
                    break
        if last_good >= 0:
            best_len = max(best_len, (last_good + 1) / fps)
    # never trim below half the template (guards against a false early cut);
    # if nothing matched at all, keep the full length
    if best_len < 0.5 * tpl_len:
        return tpl_len
    return min(best_len, tpl_len)


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
            # A silent/quiet lead-in has no fingerprint, so the matcher would
            # lock onto the first audible note and the cut would start late.
            # Trim the leading silence for MATCHING and remember how much, so
            # the cut can be backed up to the true (silent) start.
            lead = 0.0
            y_match = y
            try:
                _, idx = librosa.effects.trim(y, top_db=45)
                cut = int(idx[0])
                if 0.3 * sr < cut < 0.5 * len(y):     # real lead-in, not the whole clip
                    lead = cut / sr
                    y_match = y[cut:]
            except Exception:
                pass
            data.append({'name': name, 'y': y, 'y_match': y_match,
                         'lead': lead, 'duration': len(y) / sr})
        return data

    subs_lang = {s.lower() for s in cfg["subs_langs"]} if cfg.get("subs_langs") else None
    anchor_cut = bool(cfg.get("anchor_cut"))
    anchor_secs = float(cfg.get("anchor_secs", 15.0))
    try:
        use = cfg.get("use", {})   # segment-type checkboxes; missing key = on

        def _want(kind):
            return use.get(kind, True)

        intros_data = load_templates(cfg["intro_dir"], "Intro") if _want("intro") else []
        credits_data = load_templates(cfg["credits_dir"], "Credits") if _want("credits") else []
        preintro_data = (load_templates(cfg["preintro_dir"], "Pre-intro")
                         if cfg.get("preintro_dir") and _want("preintro") else [])
        aftercredits_data = (load_templates(cfg["aftercredits_dir"], "After-credits")
                             if cfg.get("aftercredits_dir") and _want("aftercredits") else [])
        skipped = [k for k in ("preintro", "intro", "credits", "aftercredits") if not _want(k)]
        if skipped:
            ui.log(f"Segment type(s) disabled for this run: {', '.join(skipped)}")
        if not (intros_data or credits_data or preintro_data or aftercredits_data):
            ui.log("[FAIL] No usable templates found - nothing to detect with.")
            return

        video_files = []
        for pat in ('*.mp4', '*.mkv', '*.mov', '*.avi', '*.webm'):
            video_files.extend(glob.glob(os.path.join(cfg["video_dir"], pat)))
        video_files.sort()
        ui.log(f"\nFound {len(video_files)} video(s)\n")
        if not video_files:
            return

        n_videos = len(video_files)
        batch_t0 = time.time()

        def batch_progress(base, span, vb):
            """The BAR shows this video's progress (continuous across its
            segments); the TEXT adds the whole batch's percentage + estimated
            time left. base/span are the ffmpeg call's slice in batch units."""
            def _cb(frac, text):
                f = min(1.0, base + frac * span)              # batch fraction
                vf = min(1.0, max(0.0, (f - vb) * n_videos))  # this video's
                elapsed = time.time() - batch_t0
                left = (format_seconds(elapsed * (1 - f) / f)
                        if f > 0.003 and elapsed > 5 else "--:--")
                ui.progress(vf, f"{text} | all {f * 100:.0f}% ~{left}")
            return _cb

        for vi, video in enumerate(video_files, 1):
            if stop_event.is_set():
                break
            filename = os.path.basename(video)
            name, ext = os.path.splitext(filename)
            final_output = os.path.join(cfg["output_dir"], f"{name}{ext}")
            temp_segs = []
            produced = False        # True only if THIS run wrote the output ok
            video_wall = time.time()
            vb, vspan = (vi - 1) / n_videos, 1.0 / n_videos
            if _same_path(final_output, video):
                ui.log(f"[{vi}/{len(video_files)}] {filename}\n   [FAIL] output folder is"
                       " the source folder - would overwrite the source, skipped\n")
                continue

            ui.status(f"Video {vi}/{len(video_files)}: {filename}")
            ui.progress(0.0, f"analysing video {vi}/{n_videos}")
            src_size = os.path.getsize(video)
            ui.log(f"[{vi}/{len(video_files)}] {filename}  ({format_size(src_size)})")

            # fingerprint EVERY audio track (not just the default), so a
            # template matches whichever language track it was cut from -
            # otherwise an English template scores low against a Japanese
            # default track and vice versa. If match_lang is set, use only that
            # language's track (falling back to all if the file lacks it).
            n_audio = max(1, len(probe_audio_streams(video)))
            match_lang = cfg.get("match_lang")
            if match_lang:
                mi = audio_track_for_lang(video, match_lang)
                track_indices = [mi] if mi is not None else list(range(n_audio))
            else:
                track_indices = list(range(n_audio))
            temp_audio = os.path.join(TEMP_DIR, f"{name}_audio.wav")
            tracks, sr = [], None
            for ai in track_indices:
                if not extract_wav(video, temp_audio, track=ai):
                    continue
                y, sr = librosa.load(temp_audio, sr=None)
                tracks.append(y)
            if not tracks:
                ui.log("   [FAIL] audio extract failed - skipping\n")
                continue
            y_main = tracks[0]
            # the timeline to cut is the VIDEO's, not the first audio track's
            # (audio often ends a little early or runs a little long)
            total_duration = (probe_video_duration(video) or probe_duration(video)
                              or len(y_main) / sr)
            # tail search: slice every track from ONE common absolute start, so
            # a single offset maps a match in any track back to file time
            # (tracks can differ in length; their own -win tails wouldn't align)
            longest = max(len(y) for y in tracks)
            ui.log(f"   Duration: {total_duration:.1f}s"
                   + (f"  ({len(tracks)} audio tracks)" if len(tracks) > 1 else ""))

            # --- detect intro ---
            intro_start = intro_end = 0.0
            valid_intro = False
            best = 0.0
            if intros_data:
                win = min(int(INTRO_SEARCH_WINDOW * sr), len(y_main))
                starts = [y[:win] for y in tracks]
                best_tpl = None
                for tpl in intros_data:
                    am = anchor_match(starts, tpl['y_match'], sr, anchor_secs, librosa, np, fftconvolve) if anchor_cut else None
                    if am is not None:
                        st, en, score = am
                        st -= tpl['lead']         # back up over the silent lead-in
                        ui.log(f"     intro {tpl['name']:30} -> {score:.3f} (anchored {en - st:.0f}s)")
                    else:
                        s, score = best_match_over(starts, tpl['y_match'], sr, librosa, np, fftconvolve)
                        st, en = s - tpl['lead'], s - tpl['lead'] + tpl['duration']
                        ui.log(f"     intro {tpl['name']:30} -> {score:.3f}")
                    if score > best:
                        best, intro_start, intro_end, best_tpl = score, st, en, tpl
                valid_intro = best >= cfg["confidence"]
                if valid_intro and not anchor_cut and cfg.get("trim_to_match") and best_tpl:
                    ml = matched_seconds(tracks, intro_start, best_tpl['y'], sr, librosa, np)
                    if ml < best_tpl['duration'] - 1.0:
                        ui.log(f"   intro shorter here - trimmed cut {best_tpl['duration']:.0f}s -> {ml:.0f}s")
                        intro_end = intro_start + ml
                ui.log(f"   {'[OK] INTRO at %.2fs' % intro_start if valid_intro else '[--] weak intro match (%.3f)' % best}")

            # --- detect credits ---
            credits_start = credits_end = total_duration
            valid_credits = False
            best = 0.0
            if credits_data:
                a0 = max(0, longest - int(CREDITS_SEARCH_WINDOW * sr))
                offset = a0 / sr
                ends = [y[a0:] for y in tracks]
                best_tpl = None
                for tpl in credits_data:
                    am = anchor_match(ends, tpl['y_match'], sr, anchor_secs, librosa, np, fftconvolve) if anchor_cut else None
                    if am is not None:
                        st, en, score = am
                        st -= tpl['lead']
                        ui.log(f"     credits {tpl['name']:28} -> {score:.3f} (anchored {en - st:.0f}s)")
                    else:
                        s, score = best_match_over(ends, tpl['y_match'], sr, librosa, np, fftconvolve)
                        st, en = s - tpl['lead'], s - tpl['lead'] + tpl['duration']
                        ui.log(f"     credits {tpl['name']:28} -> {score:.3f}")
                    if score > best:
                        best = score
                        credits_start = st + offset
                        credits_end = en + offset
                        best_tpl = tpl
                # only a VALID intro may veto credits (a weak match's end is noise)
                valid_credits = (best >= cfg["confidence"]
                                 and credits_start > (intro_end if valid_intro else 0.0) + 10)
                if valid_credits and not anchor_cut and cfg.get("trim_to_match") and best_tpl:
                    ml = matched_seconds(tracks, credits_start, best_tpl['y'], sr, librosa, np)
                    if ml < best_tpl['duration'] - 1.0:
                        ui.log(f"   credits shorter here - trimmed cut {best_tpl['duration']:.0f}s -> {ml:.0f}s")
                        credits_end = credits_start + ml
                ui.log(f"   {'[OK] CREDITS at %.2fs' % credits_start if valid_credits else '[--] weak credits match (%.3f)' % best}")

            # --- detect pre-intro (before the intro, near the start) ---
            preintro_start = preintro_end = 0.0
            valid_preintro = False
            best = 0.0
            if preintro_data:
                win = min(int(INTRO_SEARCH_WINDOW * sr), len(y_main))
                starts = [y[:win] for y in tracks]
                best_tpl = None
                for tpl in preintro_data:
                    am = anchor_match(starts, tpl['y_match'], sr, anchor_secs, librosa, np, fftconvolve) if anchor_cut else None
                    if am is not None:
                        st, en, score = am
                        st -= tpl['lead']
                        ui.log(f"     pre-intro {tpl['name']:26} -> {score:.3f} (anchored {en - st:.0f}s)")
                    else:
                        s, score = best_match_over(starts, tpl['y_match'], sr, librosa, np, fftconvolve)
                        st, en = s - tpl['lead'], s - tpl['lead'] + tpl['duration']
                        ui.log(f"     pre-intro {tpl['name']:26} -> {score:.3f}")
                    if score > best:
                        best, preintro_start, preintro_end, best_tpl = score, st, en, tpl
                valid_preintro = best >= cfg["confidence"]
                if valid_preintro and not anchor_cut and cfg.get("trim_to_match") and best_tpl:
                    ml = matched_seconds(tracks, preintro_start, best_tpl['y'], sr, librosa, np)
                    if ml < best_tpl['duration'] - 1.0:
                        ui.log(f"   pre-intro shorter here - trimmed cut {best_tpl['duration']:.0f}s -> {ml:.0f}s")
                        preintro_end = preintro_start + ml
                ui.log(f"   {'[OK] PRE-INTRO at %.2fs' % preintro_start if valid_preintro else '[--] weak pre-intro match (%.3f)' % best}")

            # --- detect after-credits (after the credits, near the end) ---
            aftercredits_start = aftercredits_end = total_duration
            valid_aftercredits = False
            best = 0.0
            if aftercredits_data:
                a0 = max(0, longest - int(CREDITS_SEARCH_WINDOW * sr))
                offset = a0 / sr
                ends = [y[a0:] for y in tracks]
                best_tpl = None
                for tpl in aftercredits_data:
                    am = anchor_match(ends, tpl['y_match'], sr, anchor_secs, librosa, np, fftconvolve) if anchor_cut else None
                    if am is not None:
                        st, en, score = am
                        st -= tpl['lead']
                        ui.log(f"     after-credits {tpl['name']:22} -> {score:.3f} (anchored {en - st:.0f}s)")
                    else:
                        s, score = best_match_over(ends, tpl['y_match'], sr, librosa, np, fftconvolve)
                        st, en = s - tpl['lead'], s - tpl['lead'] + tpl['duration']
                        ui.log(f"     after-credits {tpl['name']:22} -> {score:.3f}")
                    if score > best:
                        best = score
                        aftercredits_start = st + offset
                        aftercredits_end = en + offset
                        best_tpl = tpl
                valid_aftercredits = best >= cfg["confidence"]
                if valid_aftercredits and not anchor_cut and cfg.get("trim_to_match") and best_tpl:
                    ml = matched_seconds(tracks, aftercredits_start, best_tpl['y'], sr, librosa, np)
                    if ml < best_tpl['duration'] - 1.0:
                        ui.log(f"   after-credits shorter here - trimmed cut {best_tpl['duration']:.0f}s -> {ml:.0f}s")
                        aftercredits_end = aftercredits_start + ml
                ui.log(f"   {'[OK] AFTER-CREDITS at %.2fs' % aftercredits_start if valid_aftercredits else '[--] weak after-credits match (%.3f)' % best}")

            # optional: extend the intro cut back to the START of the file (also
            # removes any recap/cold-open before the intro), and the credits cut
            # forward to the END of the file (removes credits + preview + anything
            # after). Only when that segment was actually found.
            if valid_intro and cfg.get("intro_from_start") and intro_start > 0.05:
                ui.log(f"   extending intro cut to file start (was {intro_start:.1f}s)")
                intro_start = 0.0
            if valid_credits and cfg.get("credits_to_end") and credits_end < total_duration - 0.05:
                ui.log(f"   extending credits cut to file end (was {credits_end:.1f}s)")
                credits_end = total_duration

            if not (valid_intro or valid_credits or valid_preintro or valid_aftercredits):
                ui.log("   [SKIP] no matches\n")
                _cleanup(temp_audio, temp_segs)
                continue
            # optional: if a segment the user enabled (and has templates for) was
            # NOT found above the confidence, skip the WHOLE episode instead of
            # producing a partial cut - so weak-credits episodes are left intact
            # for you to handle, not output with only the intro removed
            if cfg.get("skip_incomplete"):
                missing = []
                for kind, have_tpl, is_valid in (
                        ("pre-intro", preintro_data, valid_preintro),
                        ("intro", intros_data, valid_intro),
                        ("credits", credits_data, valid_credits),
                        ("after-credits", aftercredits_data, valid_aftercredits)):
                    if have_tpl and not is_valid:
                        missing.append(kind)
                if missing:
                    ui.log(f"   [SKIP] enabled segment(s) not found: {', '.join(missing)}"
                           " - leaving this episode untouched for review\n")
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
            encoder = resolve_encoder(cfg["encoder"], vinfo["codec"], ui.log)
            crf = crf_for_encoder(encoder, cfg)
            if cfg["bit_depth"] == "auto":
                out_depth = 10 if vinfo["bit_depth"] >= 10 else 8
            else:
                out_depth = int(cfg["bit_depth"])
            if encoder == "h264_nvenc" and out_depth >= 10:
                ui.log("   [WARN] H.264 NVENC cannot encode 10-bit - using 8-bit.")
                out_depth = 8
            src_eff = 2 if vinfo["codec"] in ("hevc", "av1", "vp9") else 1
            tgt_eff = 2 if encoder in ("libx265", "hevc_nvenc", "libsvtav1") else 1
            if tgt_eff < src_eff or (vinfo["bit_depth"] >= 10 and out_depth == 8):
                ui.log("   [WARN] Source codec/bit depth is more efficient than the"
                       " chosen output - the file may get MUCH bigger."
                       " Consider H.265, 10-bit, CRF 22-24, preset slow.")
            ui.log(f"   Output: {encoder} {out_depth}-bit,"
                   f" CRF/CQ {crf}, preset {cfg['preset']}")

            if cfg["mode"] == "cut":
                drops = []
                if valid_preintro:
                    drops.append((preintro_start, preintro_end))
                if valid_intro:
                    drops.append((intro_start, intro_end))
                if valid_credits:
                    drops.append((credits_start, credits_end))
                if valid_aftercredits:
                    drops.append((aftercredits_start, aftercredits_end))
                keep = _seg_names(keep_from_drops(total_duration, drops))

                steps = [n for n, _, _ in keep] + (["Stitching"] if len(keep) > 1 else [])
                n_steps = len(steps)
                ui.log(f"   Steps: {' -> '.join(steps)}")
                audio_streams = probe_audio_streams(video)

                # weight each segment's share of the batch bar by its duration
                total_d = sum(e - s for _, s, e in keep) or 1.0
                enc_span = vspan * (0.95 if len(keep) > 1 else 1.0)
                done_d = 0.0

                ok = True
                for i, (seg_name, s0, s1) in enumerate(keep, 1):
                    if stop_event.is_set():
                        ok = False
                        break
                    ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  [{i}/{n_steps}] {seg_name}")
                    seg_out = os.path.join(TEMP_DIR, f"seg_{vi}_{i}{ext}")
                    temp_segs.append(seg_out)
                    t0 = time.time()
                    seg_cb = dict(cb, on_progress=batch_progress(
                        vb + done_d / total_d * enc_span,
                        (s1 - s0) / total_d * enc_span, vb))
                    rc = build_ffmpeg_cut(video, seg_out, s0, s1, audio_streams,
                                          crf, cfg["preset"], cfg["kf_interval"],
                                          seg_name=seg_name, fps=fps,
                                          encoder=encoder, subs_lang=subs_lang,
                                          bit_depth=out_depth, **seg_cb)
                    if rc != 0:
                        if rc != -1:  # -1 = stopped by user, already logged
                            ui.log(f"   [FAIL] {seg_name} failed")
                        ok = False
                        break
                    done_d += s1 - s0
                    ui.log(f"   [{i}/{n_steps}] {seg_name}: done in {format_seconds(time.time()-t0)}")

                if ok and len(temp_segs) == 1:
                    if _place_output(temp_segs[0], final_output, ui.log):
                        produced = True
                        ui.log(duration_check_line(
                            final_output, sum(e - s for _, s, e in keep)).rstrip("\n"))
                        ui.log(saved_size_line(final_output, src_size, video_wall))
                elif ok and len(temp_segs) > 1:
                    ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  [{n_steps}/{n_steps}] Stitching")
                    concat_list = os.path.join(TEMP_DIR, f"concat_{vi}.txt")
                    # a copied subtitle event can linger past a segment's video,
                    # inflating its container duration; the concat demuxer would
                    # then offset the next segment by that inflated length and
                    # leave a frozen GAP. Pin each segment to its ACTUAL video
                    # duration (not the requested length) so the seam is tight
                    # and no frames are dropped or held.
                    with open(concat_list, "w", encoding="utf-8") as f:
                        for t, (_n, s0, s1) in zip(temp_segs, keep):
                            vd = probe_video_duration(t) or (s1 - s0)
                            f.write(f"file '{os.path.basename(t)}'\n")
                            f.write(f"duration {vd:.6f}\n")
                    part = _part_path(final_output)
                    rc = run_ffmpeg_with_progress(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                         "-c", "copy",
                         # map only real media types so a stray DATA/unknown
                         # stream can't abort the merge (see run_manual note)
                         "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
                         "-ignore_unknown", part],
                        sum(e - s for _, s, e in keep),
                        **dict(cb, on_progress=batch_progress(vb + enc_span,
                                                              vspan - enc_span, vb)))
                    placed = _commit_part(rc, part, final_output, ui.log)
                    if placed:
                        produced = True
                        ui.log(duration_check_line(
                            final_output, sum(e - s for _, s, e in keep)).rstrip("\n"))
                        ui.log(saved_size_line(final_output, src_size, video_wall))
                    elif rc not in (0, -1):    # rc 0 = placing failed, logged already
                        ui.log("   [FAIL] merge failed\n")
                    try:
                        os.remove(concat_list)
                    except OSError:
                        pass
                elif not stop_event.is_set():
                    ui.log("   [FAIL] skipping this video\n")
            elif cfg["mode"] == "chapters":
                ch_drops = []
                if valid_preintro:
                    ch_drops.append((preintro_start, preintro_end, "Pre-intro"))
                if valid_intro:
                    ch_drops.append((intro_start, intro_end, "Intro"))
                if valid_credits:
                    ch_drops.append((credits_start, credits_end, "Credits"))
                if valid_aftercredits:
                    ch_drops.append((aftercredits_start, aftercredits_end, "After-credits"))
                chapters = build_chapters(total_duration, ch_drops)
                # A pure stream copy keeps the original GOP structure, so a
                # player's "skip intro/credits" seek (which can only land on a
                # keyframe) would jump to whatever keyframe happens to precede
                # the chapter boundary - often several seconds off. Force a
                # real keyframe at each boundary first, same as "inject" mode.
                kf_times = keyframe_times_from_ranges(
                    [(valid_preintro, preintro_start, preintro_end),
                     (valid_intro, intro_start, intro_end),
                     (valid_credits, credits_start, credits_end),
                     (valid_aftercredits, aftercredits_start, aftercredits_end)],
                    0, total_duration)
                ui.log(f"   Adding {len(chapters)} chapter marker(s), forcing keyframes at boundaries")
                ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  Add Chapters")
                kf_out = os.path.join(TEMP_DIR, f"kf_{vi}{ext}")
                temp_segs.append(kf_out)
                rc = build_ffmpeg_inject(video, kf_out, kf_times, total_duration,
                                         crf, cfg["preset"], fps=fps,
                                         encoder=encoder, bit_depth=out_depth, subs_lang=subs_lang,
                                         **dict(cb, on_progress=batch_progress(vb, vspan, vb)))
                if rc == 0 and os.path.exists(kf_out):
                    part = _part_path(final_output)
                    rc, err = add_chapters(kf_out, part, chapters)
                    if _commit_part(rc, part, final_output, ui.log):
                        produced = True
                        ui.log(saved_size_line(final_output, src_size, video_wall))
                    elif rc != 0:
                        for ln in err.strip().splitlines()[-4:]:
                            ui.log(f"      | {ln}")
                        ui.log("   [FAIL] adding chapters failed\n")
                elif rc != -1:
                    ui.log("   [FAIL] keyframe injection for chapters failed\n")
            else:
                kf_times = keyframe_times_from_ranges(
                    [(valid_preintro, preintro_start, preintro_end),
                     (valid_intro, intro_start, intro_end),
                     (valid_credits, credits_start, credits_end),
                     (valid_aftercredits, aftercredits_start, aftercredits_end)],
                    cfg["kf_interval"], total_duration)
                ui.log(f"   Injecting {len(kf_times)} forced keyframe(s), no cutting")
                ui.status(f"Video {vi}/{len(video_files)}: {filename}  -  Inject Keyframes")
                part = _part_path(final_output)
                rc = build_ffmpeg_inject(video, part, kf_times, total_duration,
                                         crf, cfg["preset"], fps=fps,
                                         encoder=encoder, bit_depth=out_depth, subs_lang=subs_lang,
                                         **dict(cb, on_progress=batch_progress(vb, vspan, vb)))
                if _commit_part(rc, part, final_output, ui.log):
                    produced = True
                    ui.log(duration_check_line(
                        final_output, total_duration).rstrip("\n"))
                    ui.log(saved_size_line(final_output, src_size, video_wall))
                elif rc not in (0, -1):
                    ui.log("   [FAIL] keyframe injection failed\n")

            # cut mode can leave a lingering subtitle past the picture; clip it
            if produced and cfg["mode"] == "cut":
                if trim_container_to_video(final_output):
                    ui.log("   Trimmed a lingering subtitle tail to the video length")

            if os.path.exists(final_output):
                warns = verify_output(video, final_output)
                if warns:
                    ui.log("   [VERIFY] WARNING: " + "; ".join(warns))
                else:
                    ui.log("   [VERIFY] OK - tracks & resolution preserved")

            # move the SOURCE of a finished video into videos/done so it's easy
            # to see what's left. Skipped/failed videos stay put on purpose.
            if produced and cfg.get("move_done") and not stop_event.is_set():
                done_dir = os.path.join(os.path.dirname(video) or ".", "done")
                try:
                    os.makedirs(done_dir, exist_ok=True)
                    dst = os.path.join(done_dir, filename)
                    if not _same_path(dst, video):
                        _move_to_done(video, dst, ui.log)
                except OSError as e:
                    ui.log(f"   [WARN] could not move source to done: {e}")

            _cleanup(temp_audio, temp_segs)

        if not stop_event.is_set():
            ui.progress(1.0, "done")
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


def _same_path(a, b):
    """True if a and b name the same file (Windows paths are case-insensitive)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _part_path(final):
    """Temp sibling '<name>.part<ext>' to write an output into. Keeps the real
    extension so ffmpeg still picks the right muxer."""
    root, ext = os.path.splitext(final)
    return f"{root}.part{ext}"


def _commit_part(rc, part, final, log):
    """ffmpeg wrote `part` with return code rc: on success swap it into place
    (only now replacing any previous output), else delete the partial file so
    a failed/stopped run never leaves a truncated file under the final name.
    Returns True if `final` now holds the new output."""
    if rc == 0 and os.path.exists(part):
        try:
            os.replace(part, final)
            return True
        except OSError as e:
            log(f"   [FAIL] could not write output (file open elsewhere?): {e}\n")
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError:
        pass
    return False


def _place_output(src, final, log):
    """Move a finished temp segment to `final` via a .part sibling (the temp
    folder may be on another drive, so the move itself isn't atomic). Logs
    [FAIL] instead of raising if the existing output is locked."""
    part = _part_path(final)
    try:
        shutil.move(src, part)
    except OSError as e:
        log(f"   [FAIL] could not write output: {e}\n")
        return _commit_part(1, part, final, log)
    return _commit_part(0, part, final, log)


def _move_to_done(video, dst, log, tries=6, delay=0.5):
    """Move a finished source into done/. A player/indexer/AV scanner holding
    the file briefly (WinError 32) is retried a few times before giving up.
    os.replace overwrites an old done/ copy in one step (never deleting it
    before the move can succeed); shutil.move only if it's another drive."""
    err = None
    for attempt in range(tries):
        try:
            try:
                os.replace(video, dst)
            except OSError as e:
                if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:
                    raise
                shutil.move(video, dst)            # different drive
            log(f"   [DONE] moved source -> {os.path.join('done', os.path.basename(dst))}")
            return True
        except PermissionError as e:               # in use - wait and retry
            err = e
            if attempt < tries - 1:
                time.sleep(delay)
        except OSError as e:
            err = e
            break
    log(f"   [WARN] could not move source to done: {err}")
    return False


def _seg_names(keep):
    """Label kept segments: first=Cold Open, last=Post Credits, else Main Content."""
    out = []
    n = len(keep)
    for i, (s0, s1) in enumerate(keep):
        if n == 1:
            nm = "Main Content"
        elif i == 0:
            nm = "Cold Open"
        elif i == n - 1:
            nm = "Post Credits"
        else:
            nm = "Main Content"
        out.append((nm, s0, s1))
    return out


def run_manual(cfg, keep, ui, stop_event):
    """Cut ONE video into the given keep-segments and stitch them, using the
    same encoder settings and ffmpeg helpers as the batch engine. `keep` is a
    list of (start, end) seconds from compute_keep_segments()."""
    video = cfg["video"]
    if not os.path.isfile(video):
        ui.log("[FAIL] video not found")
        return
    os.makedirs(cfg["output_dir"], exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    cb = {"on_progress": ui.progress, "on_log": ui.log, "stop_event": stop_event}
    name, ext = os.path.splitext(os.path.basename(video))
    # optional "[15/38] " prefix so a multi-file run shows which file this is
    prefix = cfg.get("job_label", "")
    prefix = f"{prefix} " if prefix else ""
    final_output = os.path.join(cfg["output_dir"], f"{name}{ext}")
    video_wall = time.time()
    src_size = os.path.getsize(video)
    ui.log(f"{prefix}[MANUAL] {name}{ext}  ({format_size(src_size)})")
    if _same_path(final_output, video):
        ui.log("   [FAIL] output folder is the source folder - would overwrite"
               " the source, skipped\n")
        return

    named = _seg_names(keep)
    if not named:
        ui.log("   [SKIP] those points leave nothing to keep\n")
        return

    fps = probe_video_fps(video)
    vinfo = probe_video_info(video)
    encoder = resolve_encoder(cfg["encoder"], vinfo["codec"], ui.log)
    crf = crf_for_encoder(encoder, cfg)
    if cfg["bit_depth"] == "auto":
        out_depth = 10 if vinfo["bit_depth"] >= 10 else 8
    else:
        out_depth = int(cfg["bit_depth"])
    if encoder == "h264_nvenc" and out_depth >= 10:
        ui.log("   [WARN] H.264 NVENC cannot encode 10-bit - using 8-bit.")
        out_depth = 8
    audio_streams = probe_audio_streams(video)
    subs_lang = {s.lower() for s in cfg["subs_langs"]} if cfg.get("subs_langs") else None
    ui.log(f"   Keep: {' -> '.join(f'{n} [{fmt_time(s)}-{fmt_time(e)}]' for n, s, e in named)}")

    def overall_progress(base, span):
        """One 0-1 bar for the whole job instead of resetting per segment."""
        def _cb(frac, text):
            f = min(1.0, base + frac * span)
            elapsed = time.time() - video_wall
            left = (format_seconds(elapsed * (1 - f) / f)
                    if f > 0.003 and elapsed > 5 else "--:--")
            ui.progress(f, f"{text} | all {f * 100:.0f}% ~{left}")
        return _cb

    total_d = sum(e - s for _, s, e in named) or 1.0
    enc_span = 0.95 if len(named) > 1 else 1.0
    done_d = 0.0

    temp_segs, ok = [], True
    for i, (seg_name, s0, s1) in enumerate(named, 1):
        if stop_event.is_set():
            ok = False
            break
        ui.status(f"{prefix}[MANUAL] {name}{ext}  -  [{i}/{len(named)}] {seg_name}")
        seg_out = os.path.join(TEMP_DIR, f"manual_{i}{ext}")
        temp_segs.append(seg_out)
        seg_cb = dict(cb, on_progress=overall_progress(
            done_d / total_d * enc_span, (s1 - s0) / total_d * enc_span))
        rc = build_ffmpeg_cut(video, seg_out, s0, s1, audio_streams,
                              crf, cfg["preset"], cfg["kf_interval"],
                              seg_name=seg_name, fps=fps, subs_lang=subs_lang,
                              encoder=encoder, bit_depth=out_depth, **seg_cb)
        if rc != 0:
            if rc != -1:
                ui.log(f"   [FAIL] {seg_name} failed")
            ok = False
            break
        done_d += s1 - s0

    expected = sum(e - s for _, s, e in named)
    if ok and len(temp_segs) == 1:
        ok = _place_output(temp_segs[0], final_output, ui.log)
        if ok:
            ui.log(duration_check_line(final_output, expected).rstrip("\n"))
            ui.log(saved_size_line(final_output, src_size, video_wall))
    elif ok and len(temp_segs) > 1:
        ui.status(f"{prefix}[MANUAL] {name}{ext}  -  Stitching")
        concat_list = os.path.join(TEMP_DIR, "concat_manual.txt")
        # pin each segment to its ACTUAL video length so a lingering copied
        # subtitle can't inflate the container and open a gap, and no frames
        # are dropped or held at the seam
        with open(concat_list, "w", encoding="utf-8") as f:
            for t, (_n, s0, s1) in zip(temp_segs, named):
                vd = probe_video_duration(t) or (s1 - s0)
                f.write(f"file '{os.path.basename(t)}'\n")
                f.write(f"duration {vd:.6f}\n")
        part = _part_path(final_output)
        rc = run_ffmpeg_with_progress(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
             "-c", "copy",
             # map only real media types (video/audio/subs/fonts). A stray
             # DATA/unknown stream some sources carry (e.g. #0:13) can't be
             # copied into matroska and would abort the whole merge, so we
             # never select it. -ignore_unknown is a belt-and-braces backup.
             "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
             "-ignore_unknown", part], expected,
            **dict(cb, on_progress=overall_progress(enc_span, 1.0 - enc_span)))
        # a failed/stopped merge must not count as produced (no trim, no move)
        ok = _commit_part(rc, part, final_output, ui.log)
        if ok:
            ui.log(duration_check_line(final_output, expected).rstrip("\n"))
            ui.log(saved_size_line(final_output, src_size, video_wall))
        elif rc not in (0, -1):    # rc 0 = placing failed, logged already
            ui.log("   [FAIL] merge failed\n")
        try:
            os.remove(concat_list)
        except OSError:
            pass
    elif not stop_event.is_set():
        ui.log("   [FAIL] could not produce output\n")

    if os.path.exists(final_output) and ok:
        if trim_container_to_video(final_output):
            ui.log("   Trimmed a lingering subtitle tail to the video length")

    produced = os.path.exists(final_output) and ok
    if os.path.exists(final_output):
        warns = verify_output(video, final_output)
        if warns:
            ui.log("   [VERIFY] WARNING: " + "; ".join(warns))
        else:
            ui.log("   [VERIFY] OK - tracks & resolution preserved")

    # move the SOURCE of a finished video into <source>/done, exactly like the
    # auto (batch) tool. Only when the cut actually produced an output and we
    # weren't stopped; a same-path output (in-place) is left alone.
    if produced and cfg.get("move_done") and not stop_event.is_set():
        done_dir = os.path.join(os.path.dirname(video) or ".", "done")
        try:
            os.makedirs(done_dir, exist_ok=True)
            dst = os.path.join(done_dir, f"{name}{ext}")
            if not _same_path(dst, video) and not _same_path(final_output, video):
                _move_to_done(video, dst, ui.log)
        except OSError as e:
            ui.log(f"   [WARN] could not move source to done: {e}")

    _cleanup("", temp_segs)
    ui.log("STOPPED by user." if stop_event.is_set() else "FINISHED!")


# ======================= audio tools =======================
# Trim a silent lead-in before measuring with volumedetect (Mean/Peak display),
# so a file that opens with 10-20s of no audio isn't shown quieter than it
# sounds. ONLY for the volumedetect display columns - loudnorm (LUFS + the actual
# normalize) gates silence itself and must NOT be trimmed, or its two-pass gain
# misses the target. Measurement-only: it never touches the audio that's written.
_MEAS_SILENCE_TRIM = "silenceremove=start_periods=1:start_threshold=-50dB"


def probe_volume(input_file):
    """Mean and max volume (dB) of the first audio stream via ffmpeg
    volumedetect, ignoring a silent lead-in. Returns (mean_db, max_db)."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", input_file,
         "-map", "0:a:0?", "-af", f"{_MEAS_SILENCE_TRIM},volumedetect",
         "-vn", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    t = r.stderr.decode(errors="replace")

    def _g(pat):
        m = re.search(pat, t)
        return float(m.group(1)) if m else None

    return _g(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB"), _g(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


def has_video_stream(input_file):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_file],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    return b"video" in r.stdout


def export_audio_clip(input_file, output_file, start, end, codec, extra_args,
                      fade_in=0.0, fade_out=0.0, stop_event=None,
                      on_progress=None, on_log=None):
    """Cut [start, end] of the input's audio and encode it (for a Plex theme).
    Optional fade in/out (seconds). Pass a stop_event / on_progress to make it
    interruptible with a live progress bar. Returns (returncode, stderr_text);
    returncode is -1 if stopped."""
    dur = max(0.05, end - start)
    af = []
    if fade_in and fade_in > 0:
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out and fade_out > 0:
        af.append(f"afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.3f}")
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", input_file, "-t", f"{dur:.3f}", "-vn"]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:a", codec] + list(extra_args) + [output_file]
    if stop_event is not None or on_progress is not None:
        rc = run_ffmpeg_with_progress(cmd, dur, on_progress=on_progress,
                                      on_log=on_log, stop_event=stop_event)
        return rc, ""      # failures are already reported via on_log
    r = _run_exempt(cmd)
    return r.returncode, r.stderr.decode(errors="replace")


_GAIN_AUDIO_CODEC = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac", "flac": "flac",
                     "wav": "pcm_s16le", "ogg": "libvorbis", "opus": "libopus"}


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
                stop_event=None, on_progress=None, on_log=None):
    """Apply an audio filter (e.g. 'volume=3dB' or a loudnorm string). Video,
    subtitle and data streams are stream-copied; audio must be re-encoded to apply
    the filter. audio_out=None keeps the SOURCE codec + channel layout per track
    (so 5.1 AC3 / DTS / etc. stay intact); otherwise it is a (codec, bitrate).
    Pass a stop_event / on_progress to make it interruptible with a progress bar.
    Returns (returncode, stderr_text); rc is -1 if stopped."""
    cmd = ["ffmpeg", "-y", "-i", input_file]
    if keep_video:
        cmd += ["-map", "0", "-c:v", "copy", "-c:s", "copy", "-c:d", "copy", "-filter:a", af]
        cmd += _audio_encode_args(input_file, output_file, audio_out)
    elif audio_out is not None:
        codec, bitrate = audio_out
        cmd += ["-vn", "-af", af, "-c:a", codec]
        if bitrate:
            cmd += ["-b:a", bitrate]
    else:
        ext = os.path.splitext(output_file)[1].lower().lstrip(".")
        codec = _GAIN_AUDIO_CODEC.get(ext, "aac")
        cmd += ["-vn", "-af", af, "-c:a", codec]
        if codec in ("libmp3lame", "aac", "libvorbis", "libopus"):
            cmd += ["-b:a", "320k"]
    cmd += [output_file]
    if stop_event is not None or on_progress is not None:
        dur = probe_duration(input_file) or 0
        rc = run_ffmpeg_with_progress(cmd, dur, on_progress=on_progress,
                                      on_log=on_log, stop_event=stop_event)
        return rc, ""      # failures already reported via on_log
    r = _run_exempt(cmd)
    return r.returncode, r.stderr.decode(errors="replace")


def normalize_loudness(input_file, output_file, target_i, keep_video, tp="-1.5", lra="11",
                       audio_out=None, stop_event=None, on_progress=None, on_log=None):
    """Two-pass loudnorm to a target integrated loudness (LUFS). Pass 1 measures
    the file, pass 2 applies with those measurements so every file lands at the
    same loudness - ideal for matching a whole season. Video/subtitles/chapters
    are copied; only audio is re-encoded. Returns (returncode, stderr_text)."""
    # NOTE: do NOT silence-trim here. loudnorm gates silence itself, and its
    # two-pass linear mode needs pass-1's measured values to describe the SAME
    # audio pass 2 processes (the full file). Trimming pass 1 only would feed
    # mismatched measurements and miss the target badly.
    _rc1, text = _run_capture_stoppable(
        ["ffmpeg", "-hide_banner", "-i", input_file, "-map", "0:a:0?",
         "-af", f"loudnorm=I={target_i}:TP={tp}:LRA={lra}:print_format=json",
         "-vn", "-f", "null", "-"], stop_event)
    if stop_event is not None and stop_event.is_set():
        return -1, ""
    meas = None
    for blk in reversed(re.findall(r"\{[^{}]*\}", text)):   # loudnorm json is flat
        try:
            d = json.loads(blk)
            if "input_i" in d:
                meas = d
                break
        except ValueError:
            continue
    lra_t = lra
    if meas:
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
                on_log(f"   note: peaks would reach {peak:+.1f} dBTP (> {tp}) at this"
                       " gain - loudnorm uses dynamic mode for this file")
        except (TypeError, ValueError):
            pass
    af = f"loudnorm=I={target_i}:TP={tp}:LRA={lra_t}"
    if meas:
        af += (f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
               f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
               f":offset={meas['target_offset']}:linear=true")
    # loudnorm always outputs 192 kHz - resample each track back to its own
    # source rate (fallback 48 kHz)
    cmd = ["ffmpeg", "-y", "-i", input_file]
    if keep_video:
        cmd += ["-map", "0", "-c:v", "copy", "-c:s", "copy", "-c:d", "copy"]
        for i, s in enumerate(probe_audio_streams(input_file)):
            cmd += [f"-filter:a:{i}", f"{af},aresample={s.get('sample_rate') or 48000}"]
        cmd += _audio_encode_args(input_file, output_file, audio_out)
    elif audio_out is not None:
        af += f",aresample={probe_sample_rate(input_file) or 48000}"
        codec, bitrate = audio_out
        cmd += ["-vn", "-af", af, "-c:a", codec]
        if bitrate:
            cmd += ["-b:a", bitrate]
    else:
        af += f",aresample={probe_sample_rate(input_file) or 48000}"
        ext = os.path.splitext(output_file)[1].lower().lstrip(".")
        codec = _GAIN_AUDIO_CODEC.get(ext, "aac")
        cmd += ["-vn", "-af", af, "-c:a", codec]
        if codec in ("libmp3lame", "aac", "libvorbis", "libopus"):
            cmd += ["-b:a", "320k"]
    cmd += [output_file]
    if stop_event is not None or on_progress is not None:
        dur = probe_duration(input_file) or 0
        rc = run_ffmpeg_with_progress(cmd, dur, on_progress=on_progress,
                                      on_log=on_log, stop_event=stop_event)
        return rc, ""      # failures already reported via on_log
    r = _run_exempt(cmd)
    return r.returncode, r.stderr.decode(errors="replace")


def measure_lufs(input_file):
    """Integrated loudness (LUFS) of a file's audio via loudnorm pass 1.
    Returns a float or None."""
    # loudnorm gates silence on its own, so measure the file as-is (matching the
    # two-pass normalize, so the previewed LUFS/Gain equals the actual result)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", input_file, "-map", "0:a:0?",
         "-af", "loudnorm=print_format=json", "-vn", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
    t = r.stderr.decode(errors="replace")
    for blk in reversed(re.findall(r"\{[^{}]*\}", t)):
        try:
            d = json.loads(blk)
            if "input_i" in d:
                return float(d["input_i"])
        except ValueError:
            continue
    return None


def peak_normalize(input_file, output_file, target_peak_db, keep_video, audio_out=None,
                   stop_event=None, on_progress=None, on_log=None):
    """Shift the whole file so its loudest peak sits at target_peak_db (dBFS) -
    i.e. match by PEAK level rather than perceived loudness. Returns
    (returncode, stderr_text); rc is -1 if stopped."""
    _mean, mx = probe_volume(input_file)
    if mx is None:
        return 1, "could not measure the file's peak level"
    if stop_event is not None and stop_event.is_set():
        return -1, ""
    gain = target_peak_db - mx
    return change_gain(input_file, output_file, f"volume={gain:.2f}dB", keep_video,
                       audio_out, stop_event=stop_event, on_progress=on_progress, on_log=on_log)


def probe_streams(input_file):
    """Inventory a file's streams for the Compare tab. Returns a dict:
    {duration, size, video:[...], audio:[...], subtitle:[...]} or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-print_format", "json", input_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=POPEN_FLAGS, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    fmt = data.get("format", {}) or {}
    info = {"duration": None, "size": None, "video": [], "audio": [], "subtitle": []}
    try:
        info["duration"] = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    try:
        info["size"] = int(fmt.get("size"))
    except (TypeError, ValueError):
        pass
    for st in data.get("streams", []):
        ct = st.get("codec_type")
        lang = ((st.get("tags") or {}).get("language") or "und")
        if ct == "video":
            info["video"].append({"codec": st.get("codec_name", "?"),
                                  "w": st.get("width"), "h": st.get("height")})
        elif ct == "audio":
            info["audio"].append({"codec": st.get("codec_name", "?"), "lang": lang,
                                  "channels": st.get("channels")})
        elif ct == "subtitle":
            tags = st.get("tags") or {}
            disp = st.get("disposition") or {}
            info["subtitle"].append({
                "codec": st.get("codec_name", "?"), "lang": lang,
                "title": tags.get("title", ""),
                "forced": bool(disp.get("forced")),
                "default": bool(disp.get("default"))})
    return info


def probe_subtitle_inventory(files, sample=None, progress=None, stop_event=None):
    """Scan the given files and return the distinct subtitle LANGUAGES present,
    each as {"lang","title","forced","count"} where count is how many files
    carry that language. sample=None scans ALL files (default); an int caps it.
    progress(done, total) is called as it goes; stop_event aborts early. Lets
    the UI show what subtitle tracks actually exist (tags are unreliable, so
    und/unknown shows up too) so the user can pick which to keep."""
    seen = {}
    total = len(files) if sample is None else min(sample, len(files))
    for i, f in enumerate(files):
        if sample is not None and i >= sample:
            break
        if stop_event is not None and stop_event.is_set():
            break
        if progress:
            progress(i, total)
        info = probe_streams(f)
        if not info:
            continue
        here = set()
        for s in info["subtitle"]:
            lang = (s.get("lang") or "und").lower()
            d = seen.setdefault(lang, {"lang": lang, "title": s.get("title", ""),
                                       "forced": s.get("forced", False), "count": 0})
            if s.get("title") and not d["title"]:
                d["title"] = s["title"]
            if s.get("forced"):
                d["forced"] = True
            here.add(lang)
        for lang in here:
            seen[lang]["count"] += 1
    if progress:
        progress(total, total)
    return sorted(seen.values(), key=lambda d: (-d["count"], d["lang"]))


def build_chapters(total_duration, drops):
    """drops: [(start, end, title), ...] of the detected segments. Returns a full
    timeline partition [(start, end, title), ...], gaps titled 'Content'."""
    clean = sorted((max(0.0, s), min(total_duration, e), t) for s, e, t in drops if e > s)
    segs, cur = [], 0.0
    for s, e, t in clean:
        if e <= cur:            # wholly inside an earlier drop - no chapter
            continue
        if s > cur + 0.1:
            segs.append((cur, s, "Content"))
        segs.append((max(s, cur), e, t))
        cur = max(cur, e)
    if total_duration > cur + 0.1:
        segs.append((cur, total_duration, "Content"))
    return segs


def add_chapters(input_file, output_file, chapters):
    """Write chapter markers into a copy of the file (stream copy, no re-encode)
    so players can offer Skip Intro / Skip Credits. Returns (rc, stderr_text)."""
    if _same_path(input_file, output_file):
        return 1, "output path is the input file - refusing to overwrite it"
    os.makedirs(TEMP_DIR, exist_ok=True)
    meta = os.path.join(TEMP_DIR, f"chapters_{os.getpid()}.txt")
    lines = [";FFMETADATA1"]
    for s, e, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(s * 1000))}", f"END={int(round(e * 1000))}",
                  f"title={title}"]
    with open(meta, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    cmd = ["ffmpeg", "-y", "-i", input_file, "-i", meta,
           "-map", "0", "-map_metadata", "0", "-map_chapters", "1",
           "-c", "copy", output_file]
    r = _run_exempt(cmd)
    try:
        os.remove(meta)
    except OSError:
        pass
    return r.returncode, r.stderr.decode(errors="replace")


def verify_output(src, out):
    """Compare source vs output streams; returns a list of warning strings
    (empty if audio/subtitle tracks, languages and resolution are preserved).
    Codec and duration changes are expected and not reported."""
    a, b = probe_streams(src), probe_streams(out)
    if not a or not b:
        return ["could not probe for verification"]
    warns = []
    if len(a["audio"]) != len(b["audio"]):
        warns.append(f"audio tracks {len(a['audio'])} -> {len(b['audio'])}")
    if len(a["subtitle"]) != len(b["subtitle"]):
        warns.append(f"subtitle tracks {len(a['subtitle'])} -> {len(b['subtitle'])}")
    la, lb = sorted(x["lang"] for x in a["audio"]), sorted(x["lang"] for x in b["audio"])
    if la != lb:
        warns.append(f"audio languages {la} -> {lb}")
    sa, sb = sorted(x["lang"] for x in a["subtitle"]), sorted(x["lang"] for x in b["subtitle"])
    if sa != sb:
        warns.append(f"subtitle languages {sa} -> {sb}")
    if a["video"] and b["video"]:
        va, vb = a["video"][0], b["video"][0]
        if va["w"] != vb["w"] or va["h"] != vb["h"]:
            warns.append(f"resolution {va['w']}x{va['h']} -> {vb['w']}x{vb['h']}")
    return warns


def quick_check(input_file):
    """Fast integrity check - does ffprobe open the file with valid streams and a
    duration? Catches truncated/corrupt headers, zero-byte files, wrong container.
    Returns (ok, detail)."""
    if not os.path.isfile(input_file) or os.path.getsize(input_file) == 0:
        return False, "missing or zero-byte file"
    info = probe_streams(input_file)
    if info is None:
        return False, "ffprobe could not read it (corrupt header / not media)"
    ns = len(info["video"]) + len(info["audio"]) + len(info["subtitle"])
    if ns == 0:
        return False, "no readable streams"
    if not info["duration"] or info["duration"] <= 0:
        return False, "no valid duration"
    return True, (f"{info['duration']:.0f}s, {len(info['video'])}v/"
                  f"{len(info['audio'])}a/{len(info['subtitle'])}s")


def full_check(input_file, stop_event=None, timeout=1800):
    """Thorough integrity check - decode the ENTIRE file and report any decode
    errors (catches corruption anywhere, not just the header). Slow: reads the
    whole file. Returns (ok, detail); ok is None if stopped.

    stderr is drained in a background thread so a chatty file (e.g. one with a
    broken/missing index, which makes ffmpeg emit lots of warnings) can't fill
    the pipe buffer and deadlock the process - that was making the tool hang.
    A wall-clock *timeout* is a final safety net for a file that never finishes.
    """
    # Decode only the video + audio streams. Mapping subtitle/data streams
    # (-map 0) makes the null muxer try to *encode* the subtitle track, which it
    # can't, so it aborts with "encoder selection failed for format null" - a
    # false alarm that has nothing to do with corruption. -sn -dn drop subs/data;
    # the "?" makes v/a optional so audio-only or video-only files still work.
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", input_file,
         "-map", "0:v?", "-map", "0:a?", "-sn", "-dn", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)

    lines = []

    def _drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                lines.append(raw.decode(errors="replace"))
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    def _kill():
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except OSError:
            pass

    t0 = time.time()
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            _kill()
            return None, "stopped"
        if timeout and (time.time() - t0) > timeout:
            _kill()
            return False, (f"timed out after {int(timeout)}s - file may have a "
                           "broken index or be unreadable")
        time.sleep(0.1)
    reader.join(timeout=2)

    err = "".join(lines)
    # the null muxer emits benign "non monotonically increasing dts" noise even
    # for good files - ignore it; real corruption shows other decode/demux errors.
    _benign = ("monotonically increasing dts", "Last message repeated",
               "Automatic encoder selection failed", "for format null",
               "probably disabled")
    real = [ln.strip() for ln in err.splitlines()
            if ln.strip() and not any(b in ln for b in _benign)]
    if real:
        extra = f"  (+{len(real) - 1} more)" if len(real) > 1 else ""
        return False, real[0][:140] + extra
    if proc.returncode not in (0, None):
        return False, f"ffmpeg exited with code {proc.returncode}"
    return True, "decoded clean, no errors"


# ======================================================================
# Template-free auto-detect: find intro / credits by what recurs across
# the episodes of a season (no reference clip needed). Handles a show
# that uses more than one intro by CLUSTERING the recurring segments -
# each distinct opening becomes its own cluster with its own timing.
# ======================================================================

def _decode_window_wav(src, dst, window, from_end=False, track=None):
    """Decode just the first (or last) `window` seconds of src to mono
    22.05 kHz wav - fast, because ffmpeg only touches that slice. track=None
    uses the default audio; an int picks that audio stream (0:a:N)."""
    if from_end:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-sseof", f"-{window:.3f}", "-i", src]
    else:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", "0", "-t", f"{window:.3f}", "-i", src]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-ar", "22050", "-ac", "1", "-y", dst]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=POPEN_FLAGS)


def _znorm(M, np):
    return (M - M.mean(axis=1, keepdims=True)) / (M.std(axis=1, keepdims=True) + 1e-8)


def _episode_features(src, window, from_end, np, librosa, sr=22050, hop=2048,
                      n_mfcc=13, lang=None):
    """Return (features, feat_per_sec, time_offset). features is a z-normed
    MFCC matrix for the decoded window; time_offset converts a local frame
    time back to an absolute time in the source file (needed for credits,
    which are read from the end). lang (e.g. 'eng') fingerprints that language
    track when present, so a Japanese default track doesn't skew detection."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    dst = os.path.join(TEMP_DIR, f"_detect_{abs(hash(src)) % 10**8}.wav")
    track = audio_track_for_lang(src, lang) if lang else None
    try:
        _decode_window_wav(src, dst, window, from_end, track=track)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return None, 0.0, 0.0
        y, _ = librosa.load(dst, sr=sr, mono=True)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    if y is None or len(y) < sr:               # < 1 s decoded -> unusable
        return None, 0.0, 0.0
    M = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc, hop_length=hop)
    M = _znorm(M, np)
    fps = sr / hop
    if from_end:
        # -sseof starts decoding at (container end - window), so that is local
        # 0 even if the audio stops before the container ends (less is decoded
        # then, and dur - decoded_len would push every time late). A file shorter
        # than the window decodes from 0.
        dur = probe_duration(src)
        offset = max(0.0, dur - window) if dur else 0.0
    else:
        offset = 0.0
    return M, fps, offset


def _shared_segment(Fi, Fj, np, thresh=0.8, smooth=5, max_gap=6, max_shift=None):
    """Find the longest run of near-identical frames between two feature
    matrices - the segment they share. Small dips are bridged (up to max_gap
    frames) so one quiet moment in the intro does not split it. Returns
    (score, (i0,i1), (j0,j1), run_len_frames) for the best run at/above thresh,
    or None if nothing reaches thresh at all. The caller decides if the run is
    long enough. Lower thresh = more forgiving (real re-encoded audio of the
    same intro tends to sit around 0.75-0.9, not a perfect 1.0).

    Seed-and-grow: a run must REACH thresh somewhere (the seed), but is then
    extended outward while similarity stays above a looser grow threshold.
    The same segment in two episodes is rarely aligned to an exact multiple
    of the MFCC hop (~93 ms), and that sub-hop misalignment drags the whole
    run down to ~0.6-0.75 - real, but below the seed threshold. Without the
    grow step that truncated e.g. a 110 s opening to just its first ~14 s
    (the part that happened to be hop-aligned because both files start at 0)."""
    A = Fi / (np.linalg.norm(Fi, axis=0, keepdims=True) + 1e-8)
    B = Fj / (np.linalg.norm(Fj, axis=0, keepdims=True) + 1e-8)
    S = A.T @ B
    Ti, Tj = S.shape
    kmin, kmax = -(Ti - 1), (Tj - 1)
    if max_shift is not None:
        kmin, kmax = max(kmin, -max_shift), min(kmax, max_shift)
    kern = np.ones(smooth) / smooth if smooth > 1 else None
    grow = max(0.5, thresh - 0.2)

    def _grow_run(ds, a, b):
        gap = 0
        i = a - 1
        while i >= 0:
            if ds[i] >= grow:
                a, gap = i, 0
            else:
                gap += 1
                if gap > max_gap:
                    break
            i -= 1
        gap = 0
        j = b
        while j < ds.size:
            if ds[j] >= grow:
                b, gap = j + 1, 0
            else:
                gap += 1
                if gap > max_gap:
                    break
            j += 1
        return a, b

    best = None                       # (rlen, score, segi, segj)
    for k in range(kmin, kmax + 1):
        diag = np.diagonal(S, offset=k)
        if diag.size < 2:
            continue
        ds = np.convolve(diag, kern, mode="same") if kern is not None else diag
        idx = np.flatnonzero(ds >= thresh)
        if idx.size == 0:
            continue
        # split into groups, allowing gaps of up to max_gap frames within a run
        splits = np.flatnonzero(np.diff(idx) > (max_gap + 1))
        for g in np.split(idx, splits + 1):
            a, b = _grow_run(ds, int(g[0]), int(g[-1]) + 1)
            rlen = b - a
            score = float(diag[a:b].mean())
            if best is None or (rlen, score) > (best[0], best[1]):
                i0, j0 = (a, a + k) if k >= 0 else (a - k, a)
                best = (rlen, score, (i0, i0 + rlen), (j0, j0 + rlen))
    if best is None:
        return None
    return best[1], best[2], best[3], best[0]


def _pairwise_clusters(feats, fps_ref, kind, min_len, max_shift, thresh, np,
                       progress=None, stop_event=None, diag_out=None,
                       prog_lo=0.6, prog_hi=0.98):
    """Match every pair of episodes in `feats` ({path: (M, fps, offset)}) and
    union-find them into clusters that share a recurring segment. Returns a
    list of cluster dicts (see detect_recurring_segments) or None if stopped.
    diag_out[kind] = (best_len_sec, best_score) of the best near-miss."""
    paths = list(feats.keys())
    min_frames = max(3, int(min_len * fps_ref))
    max_frames = int(max_shift * fps_ref)
    max_gap = max(3, int(0.5 * fps_ref))          # bridge dips up to ~0.5 s
    parent = {p: p for p in paths}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    best_seen = (0.0, 0.0)                          # (len_sec, score) - diagnostics
    pair = {}
    total = len(paths) * (len(paths) - 1) // 2
    done = 0
    for a in range(len(paths)):
        for b in range(a + 1, len(paths)):
            if stop_event is not None and stop_event.is_set():
                return None
            done += 1
            if progress and total:
                progress(prog_lo + (prog_hi - prog_lo) * done / total,
                         f"Matching episodes ({kind})")
            pa, pb = paths[a], paths[b]
            r = _shared_segment(feats[pa][0], feats[pb][0], np, thresh=thresh,
                                max_gap=max_gap, max_shift=max_frames)
            if not r:
                continue
            score, segi, segj, rlen = r
            seg_sec = rlen / fps_ref
            if seg_sec > best_seen[0]:
                best_seen = (seg_sec, score)
            if rlen >= min_frames:
                pair[(pa, pb)] = (score, segi, segj)
                parent[find(pa)] = find(pb)

    if diag_out is not None:
        diag_out[kind] = best_seen

    groups = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ranges, scores = {}, []
        for m in members:
            seg = None
            for (pa, pb), r in pair.items():
                if pa == m:
                    seg = r[1]
                    break
                if pb == m:
                    seg = r[2]
                    break
            if seg is None:
                continue
            M, fps, off = feats[m]
            ranges[m] = (off + seg[0] / fps, off + seg[1] / fps)
        for (pa, pb), r in pair.items():
            if pa in ranges and pb in ranges:
                scores.append(r[0])
        if not ranges:
            continue
        clusters.append({
            "kind": kind,
            "members": [m for m in members if m in ranges],
            "ranges": ranges,
            "score": float(sum(scores) / len(scores)) if scores else 0.0,
            "count": len(ranges),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def _match_known_templates(feats, tpl_paths, kind, fps_ref, thresh, np, librosa,
                           hop, progress=None, stop_event=None,
                           prog_lo=0.0, prog_hi=0.0, min_cover=0.8, lang=None):
    """Match episodes against EXISTING template clips (the ones already cut
    into the input folders). An episode 'covered' by a template joins that
    template's cluster (marked with 'known' = template filename) and is
    excluded from new-variant clustering - so a full-season scan stays fast
    and the result cleanly separates 'already have' from 'new variant'.
    Returns (clusters, covered_paths) or (None, None) if stopped."""
    if not tpl_paths:
        return [], set()
    max_gap = max(3, int(0.5 * fps_ref))
    tplf = {}
    for p in tpl_paths:
        M, fps, _off = _episode_features(p, 600.0, False, np, librosa, hop=hop, lang=lang)
        if M is not None and M.shape[1] >= 4:
            tplf[p] = M
    if not tplf:
        return [], set()
    best_for = {}                 # episode -> (score, tpl_path, (start, end))
    n = len(feats)
    for i, (ep, (Me, fps, off)) in enumerate(feats.items()):
        if stop_event is not None and stop_event.is_set():
            return None, None
        if progress:
            progress(prog_lo + (prog_hi - prog_lo) * i / max(1, n),
                     f"Matching known {kind} templates")
        for tp, Mt in tplf.items():
            r = _shared_segment(Mt, Me, np, thresh=thresh, max_gap=max_gap,
                                max_shift=None)
            if not r:
                continue
            score, segi, segj, rlen = r
            if rlen < min_cover * Mt.shape[1]:   # needs to cover most of the template
                continue
            if ep not in best_for or score > best_for[ep][0]:
                tpl_dur = Mt.shape[1] / fps
                s = max(0.0, off + (segj[0] - segi[0]) / fps)
                best_for[ep] = (score, tp, (s, s + tpl_dur))
    clusters = []
    for tp in tplf:
        mem = {ep: b for ep, b in best_for.items() if b[1] == tp}
        if not mem:
            continue
        clusters.append({
            "kind": kind,
            "known": os.path.basename(tp),
            "members": list(mem),
            "ranges": {ep: b[2] for ep, b in mem.items()},
            "score": float(sum(b[0] for b in mem.values()) / len(mem)),
            "count": len(mem),
        })
    return clusters, set(best_for)


def detect_recurring_segments(files, kinds=("intro", "credits"), window=240.0,
                              min_lens=None, max_shift=None, thresh=0.8, hop=2048,
                              progress=None, stop_event=None, diag_out=None,
                              known=None, lang=None):
    """Find the segment(s) that recur across `files`. kinds may include:
        'intro'        - near the start of each episode
        'preintro'     - a recurring bit BEFORE each episode's intro
                         (recap jingle / studio logo)
        'credits'      - near the end
        'aftercredits' - a recurring bit AFTER the credits (teaser/preview)
    Returns a list of clusters, each:
        {"kind", "members":[path...], "ranges":{path:(start,end)},
         "score", "count"}
    A show with several different openings yields several clusters. Times are
    absolute seconds in each file. min_lens is a {kind: seconds} dict.
    Pre-intro needs the intro's position to bound its search region (and
    after-credits the credits'), so those are detected internally even when
    not requested. diag_out (a dict) gets diag_out[kind] = (best_len,
    best_score) of the best near-miss, plus '<kind>_files' = how many episodes
    had a usable search region for preintro/aftercredits.

    known: optional {kind: [template file paths]} of templates the user has
    already cut. Episodes matching one are grouped under that template
    (cluster gets 'known' = its filename) and skipped by the new-variant
    clustering - see _match_known_templates."""
    import numpy as np
    import librosa

    min_lens = min_lens or {}
    known = known or {}
    # episodes' recap lengths can differ by minutes, so the allowed start-time
    # shift between two episodes' shared segment must scale with the window
    # (the old fixed 90 s rejected real matches in shows with long recaps)
    if max_shift is None:
        max_shift = max(90.0, 0.75 * window)
    kinds = [k for k in ("preintro", "intro", "credits", "aftercredits") if k in kinds]
    want_start = any(k in ("preintro", "intro") for k in kinds)
    want_end = any(k in ("credits", "aftercredits") for k in kinds)
    if not (want_start or want_end):
        return []
    MARGIN = 0.5     # seconds kept clear of the intro/credits boundary

    # progress bands per side; decoding is the slower part of each
    bands, lo = {}, 0.02
    span = 0.96 / ((1 if want_start else 0) + (1 if want_end else 0))
    if want_start:
        bands["start"] = (lo, lo + span * 0.55, lo + span)
        lo += span
    if want_end:
        bands["end"] = (lo, lo + span * 0.55, lo + span)

    def _decode(from_end, b0, b1):
        feats, fps_ref = {}, None
        for idx, f in enumerate(files):
            if stop_event is not None and stop_event.is_set():
                return None, None
            if progress:
                progress(b0 + (b1 - b0) * idx / max(1, len(files)),
                         f"Analysing {os.path.basename(f)}")
            M, fps, off = _episode_features(f, window, from_end, np, librosa, hop=hop, lang=lang)
            if M is not None and M.shape[1] >= 4:
                feats[f] = (M, fps, off)
                fps_ref = fps
        return feats, fps_ref

    out = []

    if want_start:
        b0, bm, b1 = bands["start"]
        feats, fps_ref = _decode(False, b0, bm)
        if feats is None:
            return []
        if len(feats) >= 2 and fps_ref:
            km = bm + (b1 - bm) * 0.15
            kn_intro, cov = _match_known_templates(
                feats, known.get("intro", []), "intro", fps_ref, thresh, np,
                librosa, hop, progress, stop_event, bm, km, lang=lang)
            if kn_intro is None:
                return []
            fresh = {p: f for p, f in feats.items() if p not in cov}
            new_intro = []
            if len(fresh) >= 2:
                new_intro = _pairwise_clusters(fresh, fps_ref, "intro",
                                               min_lens.get("intro", 10.0), max_shift,
                                               thresh, np, progress, stop_event,
                                               diag_out, km, b1)
                if new_intro is None:
                    return []
            intro_cl = kn_intro + new_intro
            if "intro" in kinds:
                out += intro_cl
            if "preintro" in kinds:
                min_pre = min_lens.get("preintro", 4.0)
                kn_pre, cov_p = _match_known_templates(
                    feats, known.get("preintro", []), "preintro", fps_ref,
                    thresh, np, librosa, hop, progress, stop_event, b1, b1, lang=lang)
                if kn_pre is None:
                    return []
                out += kn_pre
                starts = {}      # each episode's earliest detected intro start
                for c in intro_cl:
                    for m, (s, _e) in c["ranges"].items():
                        starts[m] = min(starts.get(m, s), s)
                sub = {}
                for m, bnd in starts.items():
                    if m in cov_p:
                        continue
                    M, fps, off = feats[m]
                    cut = int((bnd - off - MARGIN) * fps)
                    if cut >= max(3, int(min_pre * fps)):
                        sub[m] = (M[:, :cut], fps, off)
                if diag_out is not None:
                    diag_out["preintro_files"] = len(sub) + len(cov_p)
                if len(sub) >= 2:
                    pre = _pairwise_clusters(sub, fps_ref, "preintro", min_pre,
                                             max_shift, thresh, np, progress,
                                             stop_event, diag_out, b1 - 0.01, b1)
                    if pre is None:
                        return []
                    out += pre

    if want_end:
        b0, bm, b1 = bands["end"]
        feats, fps_ref = _decode(True, b0, bm)
        if feats is None:
            return []
        if len(feats) >= 2 and fps_ref:
            km = bm + (b1 - bm) * 0.15
            kn_cred, cov = _match_known_templates(
                feats, known.get("credits", []), "credits", fps_ref, thresh, np,
                librosa, hop, progress, stop_event, bm, km, lang=lang)
            if kn_cred is None:
                return []
            fresh = {p: f for p, f in feats.items() if p not in cov}
            new_cred = []
            if len(fresh) >= 2:
                new_cred = _pairwise_clusters(fresh, fps_ref, "credits",
                                              min_lens.get("credits", 10.0), max_shift,
                                              thresh, np, progress, stop_event,
                                              diag_out, km, b1)
                if new_cred is None:
                    return []
            cred_cl = kn_cred + new_cred
            if "credits" in kinds:
                out += cred_cl
            if "aftercredits" in kinds:
                min_ac = min_lens.get("aftercredits", 4.0)
                kn_ac, cov_a = _match_known_templates(
                    feats, known.get("aftercredits", []), "aftercredits", fps_ref,
                    thresh, np, librosa, hop, progress, stop_event, b1, b1, lang=lang)
                if kn_ac is None:
                    return []
                out += kn_ac
                ends = {}        # each episode's latest detected credits end
                for c in cred_cl:
                    for m, (_s, e) in c["ranges"].items():
                        ends[m] = max(ends.get(m, e), e)
                sub = {}
                for m, bnd in ends.items():
                    if m in cov_a:
                        continue
                    M, fps, off = feats[m]
                    start_f = int((bnd - off + MARGIN) * fps)
                    if M.shape[1] - start_f >= max(3, int(min_ac * fps)):
                        sub[m] = (M[:, start_f:], fps, off + start_f / fps)
                if diag_out is not None:
                    diag_out["aftercredits_files"] = len(sub) + len(cov_a)
                if len(sub) >= 2:
                    ac = _pairwise_clusters(sub, fps_ref, "aftercredits", min_ac,
                                            max_shift, thresh, np, progress,
                                            stop_event, diag_out, b1 - 0.01, b1)
                    if ac is None:
                        return []
                    out += ac

    return out


def representative_member(cluster):
    """Pick the cluster member whose detected length is the median - the most
    'typical' episode to cut a template from or preview."""
    members = cluster["members"]
    lens = {m: cluster["ranges"][m][1] - cluster["ranges"][m][0] for m in members}
    ordered = sorted(members, key=lambda m: lens[m])
    return ordered[len(ordered) // 2]
