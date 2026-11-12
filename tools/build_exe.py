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

--portable builds the single portable exe MediaPrep-Toolkit-Portable.exe and
copies ONLY that into --portable-dir (default D:/Ai - Programs/MediaPrep-
Portable). The name is the same for every version (it is also the GitHub
release asset name; the in-app updater replaces the exe in place): the
version is in its version resource (FileVersion / ProductVersion) and its
payload trailer. An older versioned MediaPrep-Toolkit-Portable-<version>.exe
in that folder is left alone (a note says so). It is the same folder build
(<out>/work_portable, <out>/dist_portable) zipped and appended to a small C#
launcher (tools/portable_launcher, compiled with the csc.exe of the .NET
Framework that ships with Windows). The launcher unpacks it ONCE to
<exe folder>/mediaprep-data/runtime/<version>-<hash8>/ and from then on just
starts it (user data: mediaprep-data/Media + Data, see app/config.py).
--reuse-build skips PyInstaller when that folder build already exists AND
is current: every PyInstaller run writes <dist>/MediaPrep Toolkit.build-
stamp.json (APP_VERSION, a hash of app/**, intro_credits_toolkit.py, the
spec and the launcher sources, the ffmpeg / mpv versions); a missing or
different stamp, or an exe whose FileVersion isn't APP_VERSION, refuses the
reuse (a stale build would ship the wrong version - endless update offers).

--portable-onefile builds the older PyInstaller one-file exe instead (unpacks
to %TEMP%/_MEIxxxx on every start - slow; kept just in case).

--installer builds the Windows installer <out>/release/MediaPrep-Toolkit-
Setup-<version>.exe from the folder build (<out>/dist; --reuse-build skips
PyInstaller when it exists) with NSIS (tools/installer/MediaPrep.nsi;
makensis from --makensis, MEDIAPREP_MAKENSIS, PATH or the electron-builder
cache). Silent use: Setup.exe /S [/D=<folder>] [/RUN] - see the .nsi.

--version-override X (tests only) packs the installer / portable exe under
another version string (the app inside keeps config.APP_VERSION).

A folder build that holds user data (Media\\, Data\\ or mediaprep-data\\ -
e.g. the exe was started from dist for a test) is never packed: the portable
payload / installer would overwrite the user's settings on an update.
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
# the portable exe's file name - the same for every version (app/updater.py
# PORTABLE_EXE = the release asset it downloads)
PORTABLE_NAME = "MediaPrep-Toolkit-Portable"
CSC_CANDIDATES = (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                  r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
# payload trailer (must match Launcher.cs): version(16) sha256(32) offset(q)
# length(q) magic(8)
TRAILER_MAGIC = b"MPPAYLD1"
# zlib level of the payload zip: unpacking speed hardly depends on it, 6 is
# a few % smaller than 1 for about twice the (one-off) build time
PAYLOAD_LEVEL = 6
# folders the app creates next to its exe for the USER (never shipped)
USER_DATA_DIRS = ("Media", "Data", "mediaprep-data")
STAMP_NAME = NAME + ".build-stamp.json"      # in <dist>, next to the folder build


def _app_version():
    with open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def _spec_version4(version):
    """The exe's numeric FileVersion as MediaPrep.spec computes it."""
    nums = [int(n) for n in re.findall(r"\d+", version)][:4]
    return tuple((nums + [0, 0, 0, 0])[:4])


# ======================= build stamp (--reuse-build) =======================
def _stamp_files(root=ROOT):
    """Project files that end up in (or shape) the folder build."""
    out = [os.path.join(root, "intro_credits_toolkit.py"),
           os.path.join(root, "tools", "MediaPrep.spec")]
    for top in (os.path.join(root, "app"), os.path.join(root, "tools", "portable_launcher")):
        for base, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            out += [os.path.join(base, f) for f in sorted(files)
                    if not f.endswith((".pyc", ".pyo"))]
    return out


def source_hash(root=ROOT):
    """SHA-256 over the relative names + contents of _stamp_files()."""
    h = hashlib.sha256()
    for path in sorted(_stamp_files(root), key=lambda p: os.path.relpath(p, root).lower()):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        h.update(rel.encode("utf-8") + b"\0")
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def make_stamp(ff_version, mpv_version, root=ROOT):
    return {"version": _app_version(), "source_hash": source_hash(root),
            "ffmpeg": ff_version or "", "mpv": mpv_version or ""}


def stamp_path(dist):
    return os.path.join(dist, STAMP_NAME)


def write_stamp(dist, stamp):
    import json
    with open(stamp_path(dist), "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)


def read_stamp(dist):
    import json
    try:
        with open(stamp_path(dist), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def exe_file_version(exe):
    """(a, b, c, d) from the exe's VS_FIXEDFILEINFO, or None."""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(exe, 0, size, buf):
            return None
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)) \
                or n.value < 13 * 4:
            return None
        fi = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 13)).contents
        if fi[0] != 0xFEEF04BD:
            return None
        return (fi[2] >> 16, fi[2] & 0xFFFF, fi[3] >> 16, fi[3] & 0xFFFF)
    except Exception:
        return None


def user_data_in(app_dir):
    """The user-data folders (USER_DATA_DIRS) found in a folder build."""
    return [d for d in USER_DATA_DIRS if os.path.exists(os.path.join(app_dir, d))]


def reuse_problems(app_dir, dist, want):
    """Why the existing folder build must not be reused ([] = it is current)."""
    exe = os.path.join(app_dir, NAME + ".exe")
    if not os.path.isfile(exe):
        return [f"{exe} does not exist"]
    have = read_stamp(dist)
    if have is None:
        return [f"no build stamp ({stamp_path(dist)}) - built by an older build_exe.py?"]
    out = []
    for key, label in (("version", "APP_VERSION"), ("source_hash", "source files"),
                       ("ffmpeg", "bundled ffmpeg"), ("mpv", "bundled mpv")):
        if have.get(key) != want.get(key):
            out.append(f"{label} changed since that build ({have.get(key)!r} -> "
                       f"{want.get(key)!r})")
    fv, need = exe_file_version(exe), _spec_version4(want["version"])
    if fv != need:
        out.append(f"the exe's FileVersion is {fv} (APP_VERSION {want['version']} "
                   f"needs {need})")
    return out


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
    """Zip the folder build (paths relative to app_dir, forward slashes) -
    never a top-level Media\\ / Data\\ / mediaprep-data\\ (user data)."""
    count = 0
    skip = {d.lower() for d in USER_DATA_DIRS}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=level,
                         strict_timestamps=False) as z:
        for base, dirs, files in os.walk(app_dir):
            if os.path.normcase(base) == os.path.normcase(app_dir):
                dirs[:] = [d for d in dirs if d.lower() not in skip]
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, app_dir).replace(os.sep, "/"))
                count += 1
    return count


def versioned_portables(folder):
    """Older versioned portable exes (MediaPrep-Toolkit-Portable-<version>.exe)
    in `folder` - from builds before the fixed name."""
    pat = re.compile(r"^" + re.escape(PORTABLE_NAME) + r"-\d[\w.\-]*\.exe$", re.I)
    try:
        return sorted(n for n in os.listdir(folder) if pat.match(n))
    except OSError:
        return []


def _portable_notes(folder):
    """The built portable exe has a fixed name; older versioned ones in the
    same folder are the user's files - never deleted, just mentioned."""
    old = versioned_portables(folder)
    if old:
        print(f"\nNote: {folder} still holds older versioned portable exe(s), left as "
              f"they are: {', '.join(old)}\n  The portable build is now always "
              f"{PORTABLE_NAME}.exe (it uses the same mediaprep-data folder); delete "
              f"the old one(s) yourself when no longer needed.")


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
    # compiled under the final name: csc writes it as OriginalFilename
    launcher = compile_launcher(os.path.join(work, PORTABLE_NAME + ".exe"), version, work)
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


# ======================= installer (NSIS) =======================
INSTALLER_NSI = os.path.join(ROOT, "tools", "installer", "MediaPrep.nsi")
MAKENSIS_CANDIDATES = (
    os.path.expandvars(r"%LOCALAPPDATA%\electron-builder\Cache\nsis\nsis-3.0.4.1\makensis.exe"),
    r"C:\Program Files (x86)\NSIS\makensis.exe",
    r"C:\Program Files\NSIS\makensis.exe",
)


def find_makensis(explicit=""):
    for cand in (explicit, os.environ.get("MEDIAPREP_MAKENSIS", ""), shutil.which("makensis"),
                 *MAKENSIS_CANDIDATES):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def build_installer(app_dir, version, out_dir, makensis):
    """makensis tools/installer/MediaPrep.nsi -> out_dir/MediaPrep-Toolkit-Setup-
    <version>.exe (written as .part, then swapped in). Returns its path."""
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, f"MediaPrep-Toolkit-Setup-{version}.exe")
    tmp = target + ".part.exe"
    cmd = [makensis, "/V2", "/INPUTCHARSET", "UTF8",
           f"/DVERSION={version}", f"/DVERSION4={_version4(version)}",
           f"/DSRCDIR={app_dir}", f"/DOUTFILE={tmp}", f"/DROOT={ROOT}", INSTALLER_NSI]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.check_call(cmd)
    os.replace(tmp, target)
    return target


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
    mode.add_argument("--installer", action="store_true",
                      help="build the NSIS installer MediaPrep-Toolkit-Setup-<version>.exe")
    ap.add_argument("--portable-dir", default=PORTABLE_DIR,
                    help="where the portable exe is copied to (only the exe)")
    ap.add_argument("--reuse-build", action="store_true",
                    help="--portable / --installer: reuse an existing folder build "
                         "(skip PyInstaller) - refused unless its build stamp matches")
    ap.add_argument("--makensis", default="", help="makensis.exe to use (--installer)")
    ap.add_argument("--version-override", default="",
                    help="tests: package the installer / portable exe as this version")
    ap.add_argument("--payload-level", type=int, default=PAYLOAD_LEVEL,
                    help="zlib level of the portable payload (tests: 1 = fast)")
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
    version = args.version_override or _app_version()
    portable_name = PORTABLE_NAME             # no version in the name (see the top)
    app_dir = os.path.join(dist, NAME)
    mpv_short = mpv_version.split(" Copyright")[0] if mpv_version else "no mpv"
    makensis = ""
    if args.installer:
        makensis = find_makensis(args.makensis)
        if not makensis:
            sys.exit("makensis.exe (NSIS 3) not found - use --makensis FILE")

    want_stamp = make_stamp(ff_version, mpv_version)
    if args.reuse_build and (args.portable or args.installer):
        problems = reuse_problems(app_dir, dist, want_stamp)
        if problems:
            sys.exit("--reuse-build refused - the folder build " + app_dir + " is not "
                     "current:\n  - " + "\n  - ".join(problems)
                     + "\nBuild again without --reuse-build.")
        print(f"reusing the folder build {app_dir} (build stamp matches)")
    else:
        try:
            os.remove(stamp_path(dist))       # a failed build must not look current
        except OSError:
            pass
        env = dict(os.environ, MEDIAPREP_FFMPEG_BIN=ff_bin, MEDIAPREP_FFMPEG_VERSION=ff_version,
                   MEDIAPREP_MPV_EXE=mpv_exe or "", MEDIAPREP_MPV_VERSION=mpv_version or "",
                   MEDIAPREP_PORTABLE="1" if args.portable_onefile else "",
                   MEDIAPREP_PORTABLE_NAME=portable_name)
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--workpath", work, "--distpath", dist, SPEC]
        print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        os.makedirs(out, exist_ok=True)
        subprocess.check_call(cmd, env=env, cwd=out)
        write_stamp(dist, want_stamp)

    if args.portable_onefile:
        built = os.path.join(dist, portable_name + ".exe")
        os.makedirs(args.portable_dir, exist_ok=True)
        target = os.path.join(args.portable_dir, portable_name + ".exe")
        shutil.copyfile(built, target)
        print(f"\nBuilt: {target}  ({os.path.getsize(target) / 2**20:.0f} MB, ffmpeg "
              f"{ff_version}, {mpv_short})")
        _portable_notes(args.portable_dir)
        return

    found = user_data_in(app_dir)
    if found:
        sys.exit("refusing to pack " + app_dir + ": it holds user data ("
                 + ", ".join(d + "\\" for d in found) + " - was the exe started from "
                 "there?). The installer / portable exe would overwrite the user's "
                 "files with it. Delete those folders or build without --reuse-build.")

    shutil.copyfile(os.path.join(ROOT, "README.txt"), os.path.join(app_dir, "README.txt"))
    with open(os.path.join(app_dir, "LICENSE.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_license_text())

    if args.portable:
        os.makedirs(args.portable_dir, exist_ok=True)
        target = os.path.join(args.portable_dir, portable_name + ".exe")
        length, count = pack_portable(app_dir, version, os.path.join(out, "pack_portable"),
                                      target, level=args.payload_level)
        fv = exe_file_version(target)
        print(f"\nBuilt: {target}  (version {version}, FileVersion "
              f"{'.'.join(map(str, fv)) if fv else '?'}; "
              f"{os.path.getsize(target) / 2**20:.0f} MB, {count} files "
              f"/ {_dir_size(app_dir) / 2**20:.0f} MB unpacked, ffmpeg {ff_version}, "
              f"{mpv_short})")
        _portable_notes(args.portable_dir)
        return

    if args.installer:
        target = build_installer(app_dir, version, os.path.join(out, "release"), makensis)
        print(f"\nBuilt: {target}  ({os.path.getsize(target) / 2**20:.0f} MB, "
              f"{_dir_size(app_dir) / 2**20:.0f} MB installed, ffmpeg {ff_version}, "
              f"{mpv_short})")
        return

    print(f"\nBuilt: {app_dir}  ({_dir_size(app_dir) / 2**20:.0f} MB, ffmpeg {ff_version}"
          f", {mpv_short})")


if __name__ == "__main__":
    main()
