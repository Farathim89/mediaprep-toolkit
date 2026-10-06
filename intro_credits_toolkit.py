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
first on PATH, numba gets a writable cache folder, and
    "MediaPrep Toolkit.exe" --selftest <dir>
runs a small GUI-less engine check (app/selftest.py) and writes
<dir>/selftest_report.json.
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
    engine (it runs ffmpeg/ffprobe by name from PATH)."""
    if not getattr(sys, "frozen", False):
        return
    root = os.path.dirname(os.path.abspath(sys.executable))
    bundle = getattr(sys, "_MEIPASS", root)
    ff_dir = os.path.join(bundle, "ffmpeg")
    if os.path.isfile(os.path.join(ff_dir, "ffmpeg.exe")):
        # the bundled copy wins over any system ffmpeg
        os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")
    # librosa's numba functions use cache=True; the bundle has no .py sources,
    # so point numba at a writable folder
    os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(root, "Data", "temp", "numba"))
    if sys.stderr is None:
        sys.stderr = _LazyErrLog(os.path.join(root, "Data", "logs", "errors.log"))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _frozen_setup()
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        from app.selftest import run as _selftest
        sys.exit(_selftest(sys.argv[2] if len(sys.argv) > 2 else None))
    from app.app import main
    main()
