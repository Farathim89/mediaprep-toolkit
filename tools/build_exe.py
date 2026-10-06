#!/usr/bin/env python3
"""Build "MediaPrep Toolkit.exe" (PyInstaller onedir, windowed) from
tools/MediaPrep.spec.

    python tools/build_exe.py [--ffmpeg-bin DIR] [--out DIR]

Build files go OUTSIDE the project: <out>/work and <out>/dist (default
%TEMP%/mp_build). The result is <out>/dist/MediaPrep Toolkit/:
    MediaPrep Toolkit.exe, README.txt, LICENSE.txt, _internal/ (with ffmpeg/)
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "tools", "MediaPrep.spec")
NAME = "MediaPrep Toolkit"


def _default_ffmpeg_bin():
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(os.path.realpath(found))
    pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    hits = sorted(glob.glob(os.path.join(pkgs, "Gyan.FFmpeg*", "ffmpeg-*", "bin")))
    return hits[-1] if hits else ""


LICENSE = """MIT License

Copyright (c) 2026 Farathim

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The bundled ffmpeg / ffprobe (_internal\\ffmpeg) are separate programs under
the GNU GPL v3 - see _internal\\ffmpeg\\LICENSE and README-ffmpeg.txt.
"""


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ffmpeg-bin", default=_default_ffmpeg_bin())
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "mp_build"))
    args = ap.parse_args()

    ff_bin = args.ffmpeg_bin
    if not (ff_bin and os.path.isfile(os.path.join(ff_bin, "ffmpeg.exe"))
            and os.path.isfile(os.path.join(ff_bin, "ffprobe.exe"))):
        sys.exit(f"ffmpeg.exe / ffprobe.exe not found in {ff_bin!r} (use --ffmpeg-bin)")
    r = subprocess.run([os.path.join(ff_bin, "ffmpeg.exe"), "-version"],
                       capture_output=True, text=True)
    first = (r.stdout.splitlines() or [""])[0]          # "ffmpeg version 8.1.2-full_build-..."
    ff_version = first.split(" Copyright")[0].replace("ffmpeg version ", "").strip()

    out = os.path.abspath(args.out)
    if (os.path.normcase(out) + os.sep).startswith(os.path.normcase(ROOT) + os.sep):
        sys.exit("refusing to build inside the project folder")
    work, dist = os.path.join(out, "work"), os.path.join(out, "dist")
    env = dict(os.environ, MEDIAPREP_FFMPEG_BIN=ff_bin, MEDIAPREP_FFMPEG_VERSION=ff_version)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--workpath", work, "--distpath", dist, SPEC]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    os.makedirs(out, exist_ok=True)
    subprocess.check_call(cmd, env=env, cwd=out)

    app_dir = os.path.join(dist, NAME)
    shutil.copyfile(os.path.join(ROOT, "README.txt"), os.path.join(app_dir, "README.txt"))
    with open(os.path.join(app_dir, "LICENSE.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(LICENSE)

    print(f"\nBuilt: {app_dir}  ({_dir_size(app_dir) / 2**20:.0f} MB, ffmpeg {ff_version})")


if __name__ == "__main__":
    main()
