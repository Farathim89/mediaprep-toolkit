"""mpv backend for the preview players.

The video is drawn by an mpv.exe child process straight into a Tk frame
(--wid=<frame's window handle>) and decoded on the GPU (--hwdec=auto-safe),
so playback, seeks and frame steps cost almost no CPU on the Tk side. mpv also
plays the sound itself (the ffmpeg + sounddevice AudioPlayer is not used).

The player talks to mpv over its JSON IPC on a named pipe
(\\\\.\\pipe\\mediaprep-mpv-<pid>-<n>). A reader thread parses replies and
events into a queue that the player drains on the Tk thread (Tk calls are
never made from the reader thread). Every queued event carries "_seq" = the
highest request_id answered before it arrived: mpv handles commands in order,
so an event with _seq >= the id of a command reflects the state AFTER that
command (used to ignore stale position / pause / eof events).

One mpv process per player, started on the first load, killed on unload,
widget destroy and app exit (atexit + a Windows Job object with
KILL_ON_JOB_CLOSE, so no mpv is left behind even if the app crashes).

The pipe is opened for OVERLAPPED I/O (ctypes, no pywin32): on a synchronous
handle a blocking read would stall every write on the same handle until mpv
sends something, and polling would add timer-granularity latency (~15 ms) to
every reply. With overlapped I/O the reader thread waits on an event and wakes
the moment mpv writes, while commands are written concurrently."""
import atexit
import ctypes
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

from ..config import popen_flags

_IS_WIN = os.name == "nt"
_NO_WINDOW = 0x08000000 if _IS_WIN else 0
_seq = itertools.count(1)
_live = set()                     # running MpvProcess objects (atexit cleanup)
_live_lock = threading.Lock()
_job = None                       # Windows Job object handle (kill-on-close)
_version_cache = {}

if _IS_WIN:
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                                  wintypes.LPCWSTR]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.ResetEvent.argtypes = [wintypes.HANDLE]
    _k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ERROR_IO_PENDING = 997
    _ERROR_MORE_DATA = 234

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _INVALID = wintypes.HANDLE(-1).value
    _u32 = ctypes.WinDLL("user32")
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUMPROC, wintypes.LPARAM]
    _u32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]


# ---------------------------------------------------------------- locating mpv
def _candidates():
    env = os.environ.get("MEDIAPREP_MPV")
    if env:
        yield env
    base = getattr(sys, "_MEIPASS", None)
    if base:                                     # frozen build: _internal\mpv\mpv.exe
        yield os.path.join(base, "mpv", "mpv.exe")
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        yield os.path.join(exe_dir, "_internal", "mpv", "mpv.exe")
        yield os.path.join(exe_dir, "mpv", "mpv.exe")
    found = shutil.which("mpv")
    if found:
        # mpv.com is the console wrapper - the real program sits next to it
        alt = os.path.splitext(found)[0] + ".exe"
        yield alt if os.path.isfile(alt) else found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield os.path.join(local, "Programs", "mpv", "mpv.exe")
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
               r"C:\Program Files"):
        if pf:
            yield os.path.join(pf, "MPV Player", "mpv.exe")
            yield os.path.join(pf, "mpv", "mpv.exe")


def find_mpv():
    """Path of mpv.exe, or None."""
    for c in _candidates():
        if c and os.path.isfile(c):
            return c
    return None


def mpv_version(exe):
    """'mpv v0.41.0 ...' first line of --version (cached), or ''."""
    if exe in _version_cache:
        return _version_cache[exe]
    v = ""
    try:
        r = subprocess.run([exe, "--no-config", "--version"], capture_output=True,
                           timeout=5, creationflags=_NO_WINDOW)
        v = r.stdout.decode(errors="replace").splitlines()[0].strip()
    except Exception:
        pass
    _version_cache[exe] = v
    return v


def _job_handle():
    """A Job object that kills its processes when the app's handle closes."""
    global _job
    if _job is not None or not _IS_WIN:
        return _job
    try:
        h = _k32.CreateJobObjectW(None, None)
        if h:
            info = _ExtLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
            if _k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                _job = h
            else:
                _k32.CloseHandle(h)
    except Exception:
        _job = None
    return _job


def disable_child_windows(hwnd):
    """Disable mpv's window inside our frame so mouse clicks and file drops
    go to the Tk frame (Windows passes a disabled child's input to its
    parent) and mpv never takes the keyboard focus. Returns the count."""
    if not _IS_WIN or not hwnd:
        return 0
    found = []
    cb = _ENUMPROC(lambda h, _lp: found.append(h) or True)
    try:
        _u32.EnumChildWindows(hwnd, cb, 0)
        for h in found:
            _u32.EnableWindow(h, False)
    except Exception:
        return 0
    return len(found)


def _kill_all():
    with _live_lock:
        procs = list(_live)
    for p in procs:
        p.close(timeout=0.3)


atexit.register(_kill_all)


# ---------------------------------------------------------------- the client
class MpvProcess:
    """One mpv.exe drawing into window `wid`, driven over JSON IPC.

    The constructor only starts the process: the IPC pipe is opened on a
    thread (mpv creates it a moment after it starts), so nothing here waits
    on mpv on the caller's (Tk) thread. Commands sent before the pipe is
    open are queued and sent once it is; if it never opens, the event
    {"event": "start-failed"} is queued (and the process killed).

    command(*args)          -> request id (fire and forget; reply discarded)
    command(*args, tag=x)   -> request id; the reply is queued as
                               {"reply": x, "data": ..., "error": ..., "_seq": id}
    request(*args, timeout) -> the reply dict (blocks the caller)
    drain()                 -> list of queued events / tagged replies
    props                   -> latest value of every observed property"""

    BASE_ARGS = (
        "--no-config", "--load-scripts=no", "--ytdl=no", "--terminal=no", "--really-quiet",
        "--idle=yes", "--force-window=yes", "--keep-open=always", "--pause=yes",
        "--no-osc", "--osd-level=0", "--no-osd-bar", "--input-default-bindings=no",
        "--input-vo-keyboard=no", "--no-input-cursor", "--cursor-autohide=no",
        "--window-dragging=no", "--taskbar-progress=no", "--sid=no", "--sub-auto=no",
        "--audio-display=no", "--audio-fallback-to-null=yes", "--hwdec=auto-safe",
        "--vo=gpu-next,gpu", "--hr-seek=yes", "--hr-seek-framedrop=yes",
        "--video-latency-hacks=yes", "--cache=yes", "--demuxer-seekable-cache=yes",
        # demuxer cache per player: ~45 s ahead / ~90 s behind at 1080p HEVC
        # rates - backward steps re-read their keyframe from memory, and
        # several loaded players don't hoard RAM
        "--demuxer-max-bytes=64MiB", "--demuxer-max-back-bytes=128MiB",
        "--volume-max=100", "--keepaspect=yes",
    )

    CONNECT_TIMEOUT = 6.0        # s for mpv to open its IPC pipe
    WRITE_TIMEOUT = 1.0          # s a command write may take (else mpv hangs: killed)

    def __init__(self, exe, wid, extra=(), log_fn=None):
        self.exe = exe
        self.name = rf"\\.\pipe\mediaprep-mpv-{os.getpid()}-{next(_seq)}"
        self._log = log_fn or (lambda m: None)
        self.props = {}
        self.events = queue.Queue()
        self._pending = {}           # rid -> [Event, reply] (blocking requests)
        self._tags = {}              # rid -> tag (replies queued for the Tk side)
        self._rid = itertools.count(1)
        self.replied = 0             # highest request_id answered so far
        self._wlock = threading.Lock()
        self._wev = _k32.CreateEventW(None, True, False, None) if _IS_WIN else None
        self._oid = itertools.count(1)
        self._h = None
        self._alive = False          # the pipe is open and being read
        self._state = "connecting"   # -> "open" -> "closed"
        self._backlog = []           # commands sent before the pipe was open
        self._grave = []             # buffers of I/O whose cancel never confirmed
        args = [exe, f"--wid={int(wid)}", f"--input-ipc-server={self.name}"]
        args += list(self.BASE_ARGS) + list(extra)
        self.proc = subprocess.Popen(args, creationflags=popen_flags(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        job = _job_handle()
        if job:
            try:
                _k32.AssignProcessToJobObject(job, int(self.proc._handle))
            except Exception:
                pass
        with _live_lock:
            _live.add(self)
        threading.Thread(target=self._connect, name="mpv-connect", daemon=True).start()

    def _connect(self):
        """Thread: open mpv's IPC pipe, send the queued commands, then read."""
        h = None
        t_end = time.monotonic() + self.CONNECT_TIMEOUT
        while self._state == "connecting" and time.monotonic() < t_end:
            if _IS_WIN:
                hh = _k32.CreateFileW(self.name, 0x80000000 | 0x40000000, 0, None, 3,
                                      0x40000000, None)          # FILE_FLAG_OVERLAPPED
                if hh and hh != _INVALID:
                    h = hh
                    break
            if self.proc.poll() is not None:
                break
            time.sleep(0.01)
        with self._wlock:
            opened = h is not None and self._state == "connecting"
            if opened:
                self._h = h
                self._state = "open"
                self._alive = True
                backlog, self._backlog = self._backlog, []
                for data in backlog:
                    if not self._send(h, data):
                        break
            elif h is not None:
                _k32.CloseHandle(h)              # closed meanwhile
        if opened:
            self._read_loop()
            return
        if self._state == "connecting":          # mpv never opened its pipe
            self._state = "closed"
            self._kill()
            self.events.put({"event": "start-failed", "_seq": 0})

    def _kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    @property
    def pid(self):
        return self.proc.pid

    # ---- io (overlapped)
    def _io(self, fn, h, buf, size, ov, ev, limit=None):
        """Start an overlapped ReadFile/WriteFile and wait for it. Returns the
        byte count, or None when the pipe is closed / broken - or when it
        took longer than `limit` seconds (the I/O is then cancelled)."""
        n = wintypes.DWORD(0)
        _k32.ResetEvent(ev)
        ok = fn(h, buf, size, None, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err not in (_ERROR_IO_PENDING, _ERROR_MORE_DATA):
                return None
            t_end = None if limit is None else time.monotonic() + limit
            while _k32.WaitForSingleObject(ev, 50 if limit else 250) != 0:   # WAIT_OBJECT_0
                if ((not self._alive and fn is _k32.ReadFile)
                        or (t_end is not None and time.monotonic() >= t_end)):
                    _k32.CancelIoEx(h, ctypes.byref(ov))
                    if _k32.WaitForSingleObject(ev, 500) != 0:
                        self._grave.append((ov, buf))    # the kernel may still use them
                    return None
        if not _k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), False):
            if ctypes.get_last_error() != _ERROR_MORE_DATA:
                return None
        return n.value

    def _send(self, h, data):
        """Write one command line (with _wlock held). A write that doesn't
        finish within WRITE_TIMEOUT means mpv hangs: it is killed (the
        reader then reports "ipc-closed")."""
        ov = _Overlapped()
        ov.hEvent = self._wev
        if self._io(_k32.WriteFile, h, data, len(data), ov, self._wev,
                    limit=self.WRITE_TIMEOUT) == len(data):
            return True
        if self._state == "open":
            self._state = "closed"          # later writes fail at once
            if self.proc.poll() is None:
                self._log("[player] mpv does not take commands any more - stopped it")
                self._kill()
        return False

    def _write(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._wlock:
            if self._state == "connecting":
                self._backlog.append(data)      # sent once the pipe is open
                return True
            h = self._h
            if h is None or self._state != "open":
                return False
            return self._send(h, data)

    def _read_loop(self):
        buf = b""
        chunk = ctypes.create_string_buffer(65536)
        ev = _k32.CreateEventW(None, True, False, None)
        try:
            while self._alive:
                h = self._h
                if h is None:
                    break
                ov = _Overlapped()
                ov.hEvent = ev
                got = self._io(_k32.ReadFile, h, chunk, 65536, ov, ev)
                if not got:
                    break
                buf += chunk.raw[:got]
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    self._dispatch(msg)
        finally:
            _k32.CloseHandle(ev)
        self._alive = False
        if self._state == "open":
            self._state = "closed"
        self.events.put({"event": "ipc-closed", "_seq": self.replied})

    def _dispatch(self, msg):
        rid = msg.get("request_id")
        if rid is not None and "event" not in msg:
            if rid > self.replied:
                self.replied = rid
            slot = self._pending.pop(rid, None)
            if slot is not None:
                slot[1] = msg
                slot[0].set()
            tag = self._tags.pop(rid, None)
            if tag is not None:
                self.events.put({"reply": tag, "data": msg.get("data"),
                                 "error": msg.get("error"), "_seq": rid})
            return
        if msg.get("event") == "property-change":
            self.props[msg.get("name")] = msg.get("data")
        msg["_seq"] = self.replied
        self.events.put(msg)

    def command(self, *args, tag=None):
        rid = next(self._rid)
        if tag is not None:
            self._tags[rid] = tag
        if not self._write({"command": list(args), "request_id": rid}):
            self._tags.pop(rid, None)
        return rid

    def request(self, *args, timeout=2.0):
        rid = next(self._rid)
        slot = [threading.Event(), None]
        self._pending[rid] = slot
        if not self._write({"command": list(args), "request_id": rid}):
            self._pending.pop(rid, None)
            return None
        slot[0].wait(timeout)
        self._pending.pop(rid, None)
        return slot[1]

    def get(self, prop, timeout=2.0):
        r = self.request("get_property", prop, timeout=timeout)
        return r.get("data") if r and r.get("error") == "success" else None

    def set(self, prop, value):
        return self.command("set_property", prop, value)

    def observe(self, prop):
        self.command("observe_property", next(self._oid), prop)

    def drain(self):
        out = []
        try:
            while True:
                out.append(self.events.get_nowait())
        except queue.Empty:
            pass
        return out

    def wait_event(self, pred, timeout):
        """Block until an event matching pred arrives (others are kept and
        re-queued in order). Returns the event or None on timeout."""
        kept = []
        found = None
        t_end = time.monotonic() + timeout
        try:
            while True:
                left = t_end - time.monotonic()
                if left <= 0:
                    break
                try:
                    m = self.events.get(timeout=left)
                except queue.Empty:
                    break
                if pred(m):
                    found = m
                    break
                kept.append(m)
                if m.get("event") == "ipc-closed":
                    break
        finally:
            # put the rest back in front of anything newer
            rest = self.drain()
            for m in kept + rest:
                self.events.put(m)
        return found

    def alive(self):
        """True while mpv runs and its pipe is open (or still being opened)."""
        return self._state != "closed" and self.proc.poll() is None

    def close(self, timeout=0.5):
        """Quit mpv (releases the file) and make sure the process is gone.
        Never blocks much longer than `timeout` + 0.5 s: the quit command
        gets 0.2 s, mpv then `timeout` to exit, else it is killed."""
        was_open = self._state == "open"
        self._state = "closed"
        self._alive = False
        got = self._wlock.acquire(timeout=0.25)
        try:
            h = self._h
            self._backlog = []
            if h is not None and got:
                self._h = None
                if was_open and self.proc.poll() is None:
                    try:
                        data = b'{"command": ["quit"]}\n'
                        ov = _Overlapped()
                        ov.hEvent = self._wev
                        self._io(_k32.WriteFile, h, data, len(data), ov, self._wev, limit=0.2)
                    except Exception:
                        pass
            if h is not None:
                try:
                    _k32.CancelIoEx(h, None)        # wakes the reader thread
                except Exception:
                    pass
                if got:
                    try:
                        _k32.CloseHandle(h)
                    except Exception:
                        pass
        finally:
            if got:
                self._wlock.release()
        try:
            self.proc.wait(timeout if was_open else 0.05)
        except Exception:
            self._kill()
            try:
                self.proc.wait(0.5)
            except Exception:
                pass
        with _live_lock:
            _live.discard(self)
