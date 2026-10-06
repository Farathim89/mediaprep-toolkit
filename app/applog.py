"""Central session log: every tab's log() also records here, which fans out to
the Log tab and auto-appends to a file so long/overnight batches are captured.
Only the last MAX_LINES lines are kept in memory (the file keeps everything);
session logs older than LOG_KEEP_DAYS are moved to Data\\temp\\trash at startup."""
import glob
import os
import shutil
import time
import threading

from .config import TRASH_DIR

MAX_LINES = 20000          # in-memory cap (the Log tab shows the same)
_TRIM_CHUNK = 2000         # trim in chunks, not one line per message
LOG_KEEP_DAYS = 30

_LOCK = threading.Lock()
_LINES = []
_LISTENERS = []
_LOGFILE = None


def archive_old_logs(logdir, days=None):
    """Move session_*.log files older than `days` (default LOG_KEEP_DAYS) into
    Data\\temp\\trash\\<timestamp>_old_logs (nothing is deleted). Returns the count."""
    if days is None:
        days = LOG_KEEP_DAYS
    cutoff = time.time() - days * 86400
    old = []
    for p in glob.glob(os.path.join(glob.escape(logdir), "session_*.log")):
        try:
            if os.path.getmtime(p) < cutoff:
                old.append(p)
        except OSError:
            pass
    if not old:
        return 0
    dest = os.path.join(TRASH_DIR, time.strftime("%Y%m%d-%H%M%S") + "_old_logs")
    moved = 0
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return 0
    for p in old:
        try:
            shutil.move(p, dest)
            moved += 1
        except (OSError, shutil.Error):
            pass
    return moved


def init(logdir, days=None):
    """Point the auto-log file at logdir/session_YYYYMMDD.log (after moving
    session logs older than `days` - default LOG_KEEP_DAYS - to the trash)."""
    global _LOGFILE
    try:
        days = max(1, int(days)) if days is not None else LOG_KEEP_DAYS
    except (TypeError, ValueError):
        days = LOG_KEEP_DAYS
    try:
        moved = archive_old_logs(logdir, days)
    except Exception:
        moved = 0
    try:
        os.makedirs(logdir, exist_ok=True)
        _LOGFILE = os.path.join(logdir, time.strftime("session_%Y%m%d.log"))
        with open(_LOGFILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n===== session started %Y-%m-%d %H:%M:%S =====\n"))
    except OSError:
        _LOGFILE = None
    if moved:
        record(f"[log] moved {moved} session log(s) older than {days} days "
               "to Data\\temp\\trash")


def add_listener(fn):
    _LISTENERS.append(fn)


def record(msg):
    line = time.strftime("%H:%M:%S  ") + str(msg)
    with _LOCK:
        _LINES.append(line)
        if len(_LINES) > MAX_LINES + _TRIM_CHUNK:
            del _LINES[:len(_LINES) - MAX_LINES]
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
