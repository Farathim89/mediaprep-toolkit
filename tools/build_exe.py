#!/usr/bin/env python3
"""Build "MediaPrep Toolkit.exe" (PyInstaller onedir, windowed) from
tools/MediaPrep.spec.

    python tools/build_exe.py [--ffmpeg-bin DIR] [--mpv-exe FILE] [--out DIR]
                              [--portable | --portable-onefile]
                              [--portable-dir DIR] [--reuse-build]

Build files go OUTSIDE the project: <out>/work and <out>/dist (default
%TEMP%/mp_build). The result is <out>/dist/MediaPrep Toolkit/:
    MediaPrep Toolkit.exe, README.txt, LICENSE.txt,
    _internal/ (with ffmpeg/ and mpv/ - mpv.exe + README-mpv.txt)
--mpv-exe "" builds without mpv (the preview players then use OpenCV).

--portable builds the single portable exe MediaPrep-Toolkit-Portable-
<version>.exe and copies ONLY that into --portable-dir (default
D:/Ai - Programs/MediaPrep-Portable). It is the same folder build
(<out>/work_portable, <out>/dist_portable) zipped and appended to a small C#
launcher (tools/portable_launcher, compiled with the csc.exe of the .NET
Framework that ships with Windows). The launcher unpacks it ONCE to
<exe folder>/mediaprep-data/runtime/<version>-<hash8>/ and from then on just
starts it (user data: mediaprep-data/Media + Data, see app/config.py).
--reuse-build skips PyInstaller when that folder build already exists.

--portable-onefile builds the older PyInstaller one-file exe instead (unpacks
to %TEMP%/_MEIxxxx on every start - slow; kept just in case).
"""
import argparse
import glob
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "tools", "MediaPrep.spec")
LAUNCHER_DIR = os.path.join(ROOT, "tools", "portable_launcher")
NAME = "MediaPrep Toolkit"
PORTABLE_DIR = r"D:\Ai - Programs\MediaPrep-Portable"
CSC_CANDIDATES = (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                  r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
# payload trailer (must match Launcher.cs): version(16) sha256(32) offset(q)
# length(q) magic(8)
TRAILER_MAGIC = b"MPPAYLD1"
# zlib level of the payload zip: unpacking speed hardly depends on it, 6 is
# a few % smaller than 1 for about twice the (one-off) build time
PAYLOAD_LEVEL = 6


def _app_version():
    with open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def _default_ffmpeg_bin():
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(os.path.realpath(found))
    pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    hits = sorted(glob.glob(os.path.join(pkgs, "Gyan.FFmpeg*", "ffmpeg-*", "bin")))
    return hits[-1] if hits else ""


def _default_mpv_exe():
    """mpv.exe to bundle: MEDIAPREP_MPV, PATH or the usual install folders."""
    sys.path.insert(0, ROOT)
    try:
        from app.ui.mpvplayer import find_mpv
        return find_mpv() or ""
    except Exception:
        return ""
    finally:
        sys.path.remove(ROOT)


LICENSE_HEADER = r"""MediaPrep Toolkit
Copyright (C) 2026 Farathim

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License below for details.

Source code: https://github.com/Farathim89/mediaprep-toolkit

The bundled ffmpeg / ffprobe (_internal\ffmpeg) are separate programs under
the GNU GPL v3 - see _internal\ffmpeg\LICENSE and README-ffmpeg.txt.
The bundled mpv (_internal\mpv) is a separate program under the GNU GPL v2
or later - see _internal\mpv\README-mpv.txt.

==============================================================================

"""


def _license_text():
    """GPL notice + the full GNU GPL v3 text from the project's LICENSE file."""
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as f:
        return LICENSE_HEADER + f.read()



def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


# ======================= portable (launcher + payload) =======================
def _version4(version):
    """'2.0.1-test' -> '2.0.1.0' (numeric a.b.c.d for the exe version info)."""
    nums = [int(n) for n in re.findall(r"\d+", version.split("-")[0])][:4]
    return ".".join(str(n) for n in nums + [0] * (4 - len(nums)))


def make_payload(app_dir, zip_path, level=PAYLOAD_LEVEL):
    """Zip the folder build (paths relative to app_dir, forward slashes)."""
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=level,
                         strict_timestamps=False) as z:
        for base, dirs, files in os.walk(app_dir):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, app_dir).replace(os.sep, "/"))
                count += 1
    return count


def compile_launcher(out_exe, version, work):
    """Compile tools/portable_launcher/Launcher.cs into a small winexe."""
    csc = next((c for c in CSC_CANDIDATES if os.path.isfile(c)), None)
    if not csc:
        sys.exit("csc.exe of the .NET Framework 4 not found (" + CSC_CANDIDATES[0] + ")")
    v4 = _version4(version)
    ver_cs = os.path.join(work, "VersionInfo.cs")
    with open(ver_cs, "w", encoding="utf-8") as f:
        f.write("using System.Reflection;\n"
                f'[assembly: AssemblyVersion("{v4}")]\n'
                f'[assembly: AssemblyFileVersion("{v4}")]\n'
                f'[assembly: AssemblyInformationalVersion("{version}")]\n')
    cmd = [csc, "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
           "/codepage:65001", f"/out:{out_exe}",
           "/win32icon:" + os.path.join(ROOT, "app", "assets", "icon.ico"),
           "/win32manifest:" + os.path.join(LAUNCHER_DIR, "launcher.manifest"),
           "/reference:System.dll", "/reference:System.Drawing.dll",
           "/reference:System.Windows.Forms.dll", "/reference:System.IO.Compression.dll",
           "/reference:System.IO.Compression.FileSystem.dll",
           os.path.join(LAUNCHER_DIR, "Launcher.cs"), ver_cs]
    subprocess.check_call(cmd)
    return out_exe


def pack_portable(app_dir, version, work, target, level=PAYLOAD_LEVEL):
    """launcher.exe + zip of app_dir + trailer -> target (written via a temp
    file, then swapped in). Returns (payload bytes, file count)."""
    os.makedirs(work, exist_ok=True)
    vb = version.encode("ascii")
    if len(vb) > 16:
        sys.exit(f"version {version!r} is longer than 16 characters")
    payload = os.path.join(work, "payload.zip")
    count = make_payload(app_dir, payload, level)
    launcher = compile_launcher(os.path.join(work, "launcher.exe"), version, work)
    tmp = target + ".part"
    sha = hashlib.sha256()
    with open(tmp, "wb") as out:
        with open(launcher, "rb") as f:
            shutil.copyfileobj(f, out)
        offset = out.tell()
        with open(payload, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
                out.write(chunk)
        length = out.tell() - offset
        out.write(vb.ljust(16, b"\0") + sha.digest() + struct.pack("<qq", offset, length)
                  + TRAILER_MAGIC)
    os.replace(tmp, target)
    os.remove(payload)
    return length, count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ffmpeg-bin", default=_default_ffmpeg_bin())
    ap.add_argument("--mpv-exe", default=_default_mpv_exe(),
                    help='mpv.exe to bundle ("" = none)')
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "mp_build"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--portable", action="store_true",
                      help="build the portable exe (launcher + unpack-once payload)")
    mode.add_argument("--portable-onefile", action="store_true",
                      help="build the old PyInstaller one-file portable exe")
    ap.add_argument("--portable-dir", default=PORTABLE_DIR,
                    help="where the portable exe is copied to (only the exe)")
    ap.add_argument("--reuse-build", action="store_true",
                    help="--portable: reuse an existing folder build (skip PyInstaller)")
    args = ap.parse_args()

    ff_bin = args.ffmpeg_bin
    if not (ff_bin and os.path.isfile(os.path.join(ff_bin, "ffmpeg.exe"))
            and os.path.isfile(os.path.join(ff_bin, "ffprobe.exe"))):
        sys.exit(f"ffmpeg.exe / ffprobe.exe not found in {ff_bin!r} (use --ffmpeg-bin)")
    r = subprocess.run([os.path.join(ff_bin, "ffmpeg.exe"), "-version"],
                       capture_output=True, text=True)
    first = (r.stdout.splitlines() or [""])[0]          # "ffmpeg version 8.1.2-full_build-..."
    ff_version = first.split(" Copyright")[0].replace("ffmpeg version ", "").strip()

    mpv_exe = args.mpv_exe
    mpv_version = ""
    if mpv_exe:
        if not os.path.isfile(mpv_exe):
            sys.exit(f"mpv.exe not found: {mpv_exe!r} (use --mpv-exe FILE, or --mpv-exe \"\")")
        r = subprocess.run([mpv_exe, "--no-config", "--version"], capture_output=True,
                           text=True, errors="replace")
        mpv_version = (r.stdout.splitlines() or ["mpv (unknown version)"])[0].strip()
    else:
        print("WARNING: building without mpv - the preview players will use OpenCV")

    out = os.path.abspath(args.out)
    if (os.path.normcase(out) + os.sep).startswith(os.path.normcase(ROOT) + os.sep):
        sys.exit("refusing to build inside the project folder")
    suffix = ("_portable" if args.portable else
              "_portable_onefile" if args.portable_onefile else "")
    work, dist = os.path.join(out, "work" + suffix), os.path.join(out, "dist" + suffix)
    version = _app_version()
    portable_name = f"MediaPrep-Toolkit-Portable-{version}"
    app_dir = os.path.join(dist, NAME)
    mpv_short = mpv_version.split(" Copyright")[0] if mpv_version else "no mpv"

    if not (args.reuse_build and args.portable
            and os.path.isfile(os.path.join(app_dir, NAME + ".exe"))):
        env = dict(os.environ, MEDIAPREP_FFMPEG_BIN=ff_bin, MEDIAPREP_FFMPEG_VERSION=ff_version,
                   MEDIAPREP_MPV_EXE=mpv_exe or "", MEDIAPREP_MPV_VERSION=mpv_version or "",
                   MEDIAPREP_PORTABLE="1" if args.portable_onefile else "",
                   MEDIAPREP_PORTABLE_NAME=portable_name)
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--workpath", work, "--distpath", dist, SPEC]
        print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        os.makedirs(out, exist_ok=True)
        subprocess.check_call(cmd, env=env, cwd=out)
    else:
        print(f"reusing the folder build {app_dir}")

    if args.portable_onefile:
        built = os.path.join(dist, portable_name + ".exe")
        os.makedirs(args.portable_dir, exist_ok=True)
        target = os.path.join(args.portable_dir, portable_name + ".exe")
        shutil.copyfile(built, target)
        print(f"\nBuilt: {target}  ({os.path.getsize(target) / 2**20:.0f} MB, ffmpeg "
              f"{ff_version}, {mpv_short})")
        return

    shutil.copyfile(os.path.join(ROOT, "README.txt"), os.path.join(app_dir, "README.txt"))
    with open(os.path.join(app_dir, "LICENSE.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_license_text())

    if args.portable:
        os.makedirs(args.portable_dir, exist_ok=True)
        target = os.path.join(args.portable_dir, portable_name + ".exe")
        length, count = pack_portable(app_dir, version, os.path.join(out, "pack_portable"),
                                      target)
        print(f"\nBuilt: {target}  ({os.path.getsize(target) / 2**20:.0f} MB, {count} files "
              f"/ {_dir_size(app_dir) / 2**20:.0f} MB unpacked, ffmpeg {ff_version}, "
              f"{mpv_short})")
        return

    print(f"\nBuilt: {app_dir}  ({_dir_size(app_dir) / 2**20:.0f} MB, ffmpeg {ff_version}"
          f", {mpv_short})")


if __name__ == "__main__":
    main()
