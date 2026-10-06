# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for "MediaPrep Toolkit.exe" (onedir, windowed).
# Run it through tools/build_exe.py, which also sets the work/dist folders
# (OUTSIDE the project) and puts README.txt + LICENSE.txt next to the exe.
#
# Env: MEDIAPREP_FFMPEG_BIN = folder with ffmpeg.exe + ffprobe.exe (and a
# LICENSE one level up), bundled into _internal/ffmpeg/.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct,
                                                 VSVersionInfo)

ROOT = os.path.dirname(SPECPATH)                       # the project folder
APP = os.path.join(ROOT, "app")
VERSION = (2, 0, 0, 0)
NAME = "MediaPrep Toolkit"

FFMPEG_BIN = os.environ.get("MEDIAPREP_FFMPEG_BIN", "")
FFMPEG_VERSION = os.environ.get("MEDIAPREP_FFMPEG_VERSION", "unknown")

# ---- read-only app resources (only the language catalogs, not the
# translator template files) ----
datas = [(os.path.join(APP, "assets", f), "app/assets")
         for f in os.listdir(os.path.join(APP, "assets"))]
datas += [(os.path.join(APP, "locales", f), "app/locales")
          for f in os.listdir(os.path.join(APP, "locales"))
          if f.endswith(".json") and not f.startswith("_")]

# ---- third-party data / DLLs (the contrib hooks cover most; be explicit) ----
datas += collect_data_files("tkinterdnd2")
datas += collect_data_files("librosa")
datas += collect_data_files("lazy_loader")
datas += collect_data_files("_sounddevice_data")
datas += collect_data_files("_soundfile_data")
binaries = []
binaries += collect_dynamic_libs("tkinterdnd2")
binaries += collect_dynamic_libs("_sounddevice_data")
binaries += collect_dynamic_libs("_soundfile_data")
binaries += collect_dynamic_libs("llvmlite")

# ---- bundled ffmpeg (GPLv3 build) ----
if FFMPEG_BIN:
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        datas.append((os.path.join(FFMPEG_BIN, exe), "ffmpeg"))
    lic = os.path.join(os.path.dirname(FFMPEG_BIN), "LICENSE")
    if os.path.isfile(lic):
        datas.append((lic, "ffmpeg"))
    note = os.path.join(workpath, "README-ffmpeg.txt")
    os.makedirs(workpath, exist_ok=True)
    with open(note, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(
            f"ffmpeg / ffprobe {FFMPEG_VERSION}\n"
            "\n"
            "These are the unmodified \"full\" Windows builds by gyan.dev, bundled so\n"
            "MediaPrep Toolkit works without a separate ffmpeg install. They are\n"
            "licensed under the GNU General Public License v3 (see LICENSE in this\n"
            "folder). MediaPrep Toolkit only runs them as separate programs.\n"
            "\n"
            "Builds:            https://www.gyan.dev/ffmpeg/builds/\n"
            "FFmpeg source:     https://ffmpeg.org/download.html#get-sources\n"
            "(the gyan.dev build page also lists the exact sources and the\n"
            "configuration of each release)\n"
        )
    datas.append((note, "ffmpeg"))

hiddenimports = [
    "sklearn.utils._typedefs", "sklearn.neighbors._partition_nodes",
    "scipy.signal", "scipy.special.cython_special",
    "soundfile", "audioread", "soxr", "pooch", "decorator", "msgpack",
    "PIL.ImageTk", "PIL._tkinter_finder",
    # the Fluent look (ui/fluent.py) and the icons (ui/icons.py) draw with these;
    # the icon font itself is Windows' own (Segoe Fluent Icons / MDL2 Assets)
    "PIL.ImageDraw", "PIL.ImageFont",
]

excludes = [
    "matplotlib", "IPython", "jupyter", "notebook", "pytest", "_pytest", "nose",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "pandas", "torch", "tensorflow",
    "tkinter.test", "test", "lib2to3", "sphinx", "docutils",
]

a = Analysis(
    [os.path.join(ROOT, "intro_credits_toolkit.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION, prodvers=VERSION, mask=0x3F, flags=0x0,
                      OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", NAME),
            StringStruct("FileDescription", "MediaPrep Toolkit - intro/credits remover and media prep tools"),
            StringStruct("FileVersion", "2.0.0.0"),
            StringStruct("InternalName", NAME),
            StringStruct("LegalCopyright", "© 2026 Farathim, MIT"),
            StringStruct("OriginalFilename", f"{NAME}.exe"),
            StringStruct("ProductName", NAME),
            StringStruct("ProductVersion", "2.0.0.0"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(APP, "assets", "icon.ico"),
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
