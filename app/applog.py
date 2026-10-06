"""Central session log: every tab's log() also records here, which fans out to
the Log tab and auto-appends to a file so long/overnight batches are captured."""
import os
import time
import threading

_LOCK = threading.Lock()
_LINES = []
_LISTENERS = []
_LOGFILE = None


def init(logdir):
    """Point the auto-log file at logdir/session_YYYYMMDD.log."""
    global _LOGFILE
    try:
        os.makedirs(logdir, exist_ok=True)
        _LOGFILE = os.path.join(logdir, time.strftime("session_%Y%m%d.log"))
        with open(_LOGFILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n===== session started %Y-%m-%d %H:%M:%S =====\n"))
    except OSError:
        _LOGFILE = None


def add_listener(fn):
    _LISTENERS.append(fn)


def record(msg):
    line = time.strftime("%H:%M:%S  ") + str(msg)
    with _LOCK:
        _LINES.append(line)
        if _LOGFILE:
            try:
                with open(_LOGFILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    for fn in list(_LISTENERS):
        try:
            fn(line)
        except Exception:
            pass


def all_text():
    with _LOCK:
        return "\n".join(_LINES)


def clear():
    with _LOCK:
        _LINES.clear()
