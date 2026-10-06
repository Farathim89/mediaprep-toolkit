"""Subprocess runners: power-throttling exemption, stoppable capture
and ffmpeg with a live progress callback."""
import os
import subprocess
import threading
import time

from ..config import POPEN_FLAGS, TEMP_DIR
from .formatting import format_seconds


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
    try:
        proc = subprocess.Popen(cmd, **kw)
    except OSError as e:                   # ffmpeg/ffprobe missing
        return subprocess.CompletedProcess(cmd, 127, b"",
                                           f"could not run {cmd[0]}: {e}".encode())
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
    try:
        errfh = open(errpath, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errfh,
                                creationflags=POPEN_FLAGS)
    except OSError as e:                   # ffmpeg missing
        try:
            errfh.close()
            os.remove(errpath)
        except (OSError, NameError):
            pass
        return 127, f"could not run {cmd[0]}: {e}"
    with errfh:
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
                             on_progress=None, on_log=None, stop_event=None,
                             stderr_out=None):
    """
    Runs ffmpeg, polling the -progress FILE it writes (avoids Windows pipe
    buffering). Progress derivation, verified against real ffmpeg output:
      * encoding steps (fps known): frame= counter / fps - exact and
        independent of -copyts/-ss/-output_ts_offset and ffmpeg version
      * copy steps (no fps): out_time, with an absolute-timestamp safety net
    on_progress(frac, text) is called on updates; stop_event terminates ffmpeg.
    stderr_out: optional list - ffmpeg's full stderr text is appended to it
    (e.g. to read loudnorm's report). Returns ffmpeg's return code (-1 if stopped).
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
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_fh,
                                creationflags=POPEN_FLAGS)
    except OSError as e:                   # ffmpeg missing / not on PATH
        stderr_fh.close()
        if on_log:
            on_log(f"      | could not run {cmd[0]}: {e}")
        return 127
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
    if stderr_out is not None and not stopped:
        try:
            with open(stderr_file, "r", encoding="utf-8", errors="replace") as f:
                stderr_out.append(f.read())
        except OSError:
            pass

    for p in (progress_file, stderr_file):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return -1 if stopped else proc.returncode
