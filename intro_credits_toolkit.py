#!/usr/bin/env python3
"""MediaPrep Toolkit - launcher.

The code lives in the app/ package:
    app/app.py      - main() that builds the window and wires the tabs together
    app/config.py   - constants, folder layout, codec/audio maps, settings
    app/migrate.py  - moves an old flat folder layout into Media/ + Data/
    app/engine/     - UI-free ffmpeg/ffprobe/librosa engine (probe, encode,
                      detect, recurring, cut, chapters, subs, loudness, snap ...)
    app/ui/         - shared widgets, the video/audio players, dialogs
    app/tabs/       - one module per tab (templates*, cut_*, theme_audio,
                      audio_gain, compare, check, log)

This thin launcher just starts the GUI, so the .bat files and the way you run
the tool stay exactly the same. The old single-file version is kept in
Data/backups/ as intro_credits_toolkit_legacy.py for reference.

Frozen build (PyInstaller, see tools/build_exe.py): the bundled ffmpeg is put
first on PATH (and the working folder is never searched for programs), numba
gets a per-user cache folder (see _numba_cache_setup), and
    "MediaPrep Toolkit.exe" --selftest <dir>
runs a small GUI-less engine check (app/selftest.py) and writes
<dir>/selftest_report.json.
    "MediaPrep Toolkit.exe" --set-shortcut-aumid <lnk>...
writes the app's taskbar ID (app/appid.py) into shortcuts - the installer
runs it for the Start menu / desktop shortcuts it creates.
"""
import multiprocessing
import os
import sys

# make sure the app/ package next to this file is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _LazyErrLog:
    """sys.stderr stand-in for the windowed exe (which has none): opens
    Data/logs/errors.log only when something is actually written."""
    def __init__(self, path):
        self.path, self.fh = path, None

    def write(self, s):
        try:
            if self.fh is None:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                self.fh = open(self.path, "a", encoding="utf-8", errors="replace")
            self.fh.write(s)
            self.fh.flush()
        except OSError:
            pass
        return len(s)

    def flush(self):
        pass


def _frozen_setup():
    """Environment for the PyInstaller build - before anything imports the
    engine (it runs ffmpeg/ffprobe by name from PATH). The portable build
    keeps its data in <portable exe folder>/mediaprep-data (config.APP_ROOT)."""
    if not getattr(sys, "frozen", False):
        return
    from app import config as C                 # light import (no numpy)
    # a program started by a bare name ("ffmpeg", "ffprobe", "powershell")
    # must never be picked up from the working folder: Windows' CreateProcess
    # searches it before PATH unless this is set (inherited by every child)
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    if os.path.isabs(C.FFMPEG):
        # the bundled copy wins over any system ffmpeg (config.FFMPEG /
        # FFPROBE are its absolute paths; this covers the bare names)
        os.environ["PATH"] = os.path.dirname(C.FFMPEG) + os.pathsep + os.environ.get("PATH", "")
    _numba_cache_setup(C.APP_VERSION)
    if sys.stderr is None:
        sys.stderr = _LazyErrLog(os.path.join(C.APP_ROOT, "Data", "logs", "errors.log"))


# ---- numba cache of the frozen build ----
# librosa's numba functions use cache=True (compiling them takes ~10 s, the
# cache makes it ~2 s). In the frozen build numba ignores NUMBA_CACHE_DIR
# (that locator wants the .py source, which the bundle doesn't have) and
# falls back to %LOCALAPPDATA%\numba\Cache\<pkg>_<hash of a path that
# depends on the working folder> - a new set of folders per build / working
# folder, never cleaned up. So numba gets this module's own locator
# (NUMBA_CACHE_LOCATOR_CLASSES, read by numba >= 0.62; an older numba keeps
# its old behaviour): NUMBA_CACHE_DIR = %LOCALAPPDATA%\MediaPrep\numba\
# <version>\<package>_<module folder>. Per user on purpose: the cache is
# pickled code, and an installed copy's Data\ is writable for every user.
# Cache folders of other versions unused for NUMBA_KEEP_DAYS are removed.
NUMBA_LOCATOR = "MediaPrepNumbaCacheLocator"
NUMBA_KEEP_DAYS = 30


def _numba_cache_base():
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "MediaPrep", "numba")


def _numba_cache_setup(version):
    base = _numba_cache_base()
    os.environ["NUMBA_CACHE_DIR"] = os.path.join(base, version)
    os.environ["NUMBA_CACHE_LOCATOR_CLASSES"] = (f"{__name__}.{NUMBA_LOCATOR},"
                                                 "UserWideCacheLocator")
    _prune_numba_caches(base, version)


def _prune_numba_caches(base, keep, days=NUMBA_KEEP_DAYS):
    import shutil
    import time
    try:
        names = os.listdir(base)
    except OSError:
        return
    cutoff = time.time() - days * 86400
    for n in names:
        p = os.path.join(base, n)
        if n == keep or not os.path.isdir(p):
            continue
        try:
            newest = max([os.path.getmtime(p)] + [
                os.path.getmtime(os.path.join(r, f)) for r, _d, fs in os.walk(p) for f in fs])
        except OSError:
            continue
        if newest < cutoff:
            shutil.rmtree(p, ignore_errors=True)


def _make_numba_locator():
    """numba's UserProvidedCacheLocator (NUMBA_CACHE_DIR) without its 'the
    .py file must exist' rule, and with a sub-folder that doesn't depend on
    the working folder (a frozen module's co_filename may be relative)."""
    from numba.core import caching
    from numba.core import config as nb_config

    base = (getattr(caching, "UserProvidedCacheLocator", None)
            or getattr(caching, "_UserProvidedCacheLocator"))

    class MediaPrepNumbaCacheLocator(base):
        def __init__(self, py_func, py_file):
            self._py_file = py_file
            self._lineno = py_func.__code__.co_firstlineno
            parts = [p for p in os.path.normpath(os.path.dirname(str(py_file)))
                     .replace("/", "\\").split("\\") if p not in ("", ".", "..")]
            sub = "_".join(parts[-2:]) or "main"
            sub = "".join(c if c.isalnum() or c in "-_." else "_" for c in sub)
            self._cache_path = os.path.join(nb_config.CACHE_DIR, sub)

        @classmethod
        def from_function(cls, py_func, py_file):
            if not nb_config.CACHE_DIR:
                return None
            self = cls(py_func, py_file)
            try:
                self.ensure_cache_path()
            except OSError:
                return None                 # -> numba's next locator
            return self
    return MediaPrepNumbaCacheLocator


def __getattr__(name):
    """numba looks the locator up by name (NUMBA_CACHE_LOCATOR_CLASSES) when
    librosa is imported - only then is numba itself imported here."""
    if name == NUMBA_LOCATOR:
        try:
            cls = _make_numba_locator()
        except Exception:               # never let the cache break the app
            from numba.core import caching
            cls = (getattr(caching, "UserWideCacheLocator", None)
                   or getattr(caching, "_UserWideCacheLocator"))
        globals()[name] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _wait_for_old_instance(timeout_ms=30000):
    """The in-app updater starts a new PORTABLE version while the old one is
    still closing (it saves its settings on the way out). It passes its
    process id in MEDIAPREP_WAIT_PID: wait (at most 30 s) for that process
    to end before the settings are read."""
    pid = os.environ.pop("MEDIAPREP_WAIT_PID", "").strip()
    if not pid.isdigit() or os.name != "nt" or int(pid) == os.getpid():
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000, False, int(pid))       # SYNCHRONIZE
        if not h:
            return                      # already gone
        try:
            k32.WaitForSingleObject(ctypes.c_void_p(h), int(timeout_ms))
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass


def _cpu_limit_env():
    """'Limit CPU use' (Settings -> Performance): cap the math libraries'
    thread pools (numpy BLAS / OpenMP / numba used by librosa) - must happen
    before numpy is first imported. Read straight from the settings file."""
    try:
        from app import config as C
        n = C.cpu_threads(dict(C.PREF_DEFAULTS, **C.load_settings()))
    except Exception:
        return
    if n:
        n = min(n, os.cpu_count() or n)       # numba refuses more than the cores
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                  "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS"):
            os.environ[k] = str(n)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app import appid
    if len(sys.argv) >= 2 and sys.argv[1] == appid.CLI_SWITCH:
        # the installer (elevated, after creating its shortcuts): stamp the
        # app's taskbar ID into them - no window, nothing else started
        sys.exit(appid.cli(sys.argv[2:]))
    # one taskbar identity for every way the app runs (app/appid.py), set
    # before any window exists: the pinned portable launcher and the program
    # it starts stack on one taskbar button
    appid.set_process()
    _frozen_setup()
    _wait_for_old_instance()
    _cpu_limit_env()
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        from app.selftest import run as _selftest
        sys.exit(_selftest(sys.argv[2] if len(sys.argv) > 2 else None))
    from app.app import main
    main()
