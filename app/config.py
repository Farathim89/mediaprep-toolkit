"""Constants, codec/audio maps, folder layout and settings persistence.
(The GUI themes live in ui/themes.py.)"""
import json
import os
import subprocess
import sys
import time

from .i18n import N_

# ======================= version =======================
APP_VERSION = "2.1.0"
APP_NAME = "MediaPrep Toolkit"
# GitHub repo whose latest release the update check looks at
UPDATE_REPO = "Farathim89/mediaprep-toolkit"

# ======================= folder layout =======================
# APP_ROOT is the folder the user sees: next to "MediaPrep Toolkit.exe" when
# frozen (PyInstaller), else the folder holding intro_credits_toolkit.py (the
# parent of this app/ package). Every working folder is an ABSOLUTE path under
# it, so the app works no matter what the current working directory is.
#
#   <APP_ROOT>\Media\  videos (+done), output, templates\<kind>, Audio Gain\in/out
#   <APP_ROOT>\Data\   settings.json, presets, logs, temp (+trash), backups
#
# app/migrate.py moves an old-layout install (videos\, input\, logs\,
# toolkit_settings.json ...) into these places at startup.
#
# Portable build (tools/build_exe.py --portable): a small launcher exe
# carries this folder build as a zip, unpacks it ONCE to
# <launcher folder>\mediaprep-data\runtime\<version>-<hash>\ and starts it
# with MEDIAPREP_PORTABLE_DATA=<...>\mediaprep-data; the user data then lives
# in that mediaprep-data\ (Media\, Data\) instead of next to this exe.
# The older one-file build (--portable-onefile) flags itself with a runtime
# hook (sys.mediaprep_portable) and unpacks to sys._MEIPASS for each run.
APP_DIR = os.path.dirname(os.path.abspath(__file__))          # the app/ package
PORTABLE_DATA = "mediaprep-data"

# messages from the import of this module (the session log isn't open yet);
# applog.init() records them (take_notes)
_NOTES = []


def take_notes():
    """The startup messages collected so far (the list is emptied)."""
    out = list(_NOTES)
    del _NOTES[:]
    return out


def _rt_marked_data(exe_dir):
    """The data folder for a program in the shared fallback runtime
    (%LOCALAPPDATA%\\MediaPrep\\rt\\<version>-<hash>) started BY HAND (no
    MEDIAPREP_PORTABLE_DATA). Several portable copies of the same version
    share that folder, so the launcher writes one marker per data folder:
    owners\\<sha8 of the data folder>.txt (its path, re-written at every
    start -> the newest file = the copy used most recently). Older launchers
    wrote a single portable-data.txt (UTF-8 with BOM). A marker must name an
    existing absolute folder, else it is skipped; none -> None."""
    cands = []
    odir = os.path.join(exe_dir, "owners")
    try:
        names = os.listdir(odir)
    except OSError:
        names = []
    for n in names:
        if n.lower().endswith(".txt"):
            p = os.path.join(odir, n)
            try:
                cands.append((os.path.getmtime(p), p))
            except OSError:
                pass
    cands.sort(reverse=True)
    cands.append((0.0, os.path.join(exe_dir, "portable-data.txt")))     # legacy
    valid = []
    for _mt, p in cands:
        try:
            with open(p, encoding="utf-8-sig") as f:
                d = f.read().strip()
        except (OSError, ValueError):
            continue
        if d and os.path.isabs(d) and os.path.isdir(d):
            valid.append(os.path.normpath(d))
    if not valid:
        return None
    if len(set(os.path.normcase(v) for v in valid)) > 1:
        _NOTES.append(f"[portable] started without the launcher: {len(valid)} portable "
                      f"copies use this program folder - using the data folder of the "
                      f"one used most recently: {valid[0]}")
    return valid[0]


def _portable_root():
    """The mediaprep-data folder of a portable build, else None."""
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "mediaprep_portable", False):              # one-file build
        return os.path.join(exe_dir, PORTABLE_DATA)
    # launcher build: this exe sits in <data>\runtime\<version>-<hash>\ - the
    # env var names <data>; without it (exe started by hand) the folder
    # layout itself tells
    runtime = os.path.dirname(exe_dir)
    env = os.environ.get("MEDIAPREP_PORTABLE_DATA", "").strip()
    if os.path.basename(runtime).lower() == "rt":
        # launcher fallback for a deeply nested exe folder (path limit): the
        # program sits in %LOCALAPPDATA%\MediaPrep\rt\<version>-<hash>\ and
        # the data folder comes from the env var or the marker file it writes
        if env and os.path.isabs(env):
            return os.path.normpath(env)
        return _rt_marked_data(exe_dir)
    if os.path.basename(runtime).lower() != "runtime":
        return None
    data = os.path.dirname(runtime)
    if env and os.path.normcase(os.path.abspath(env)) == os.path.normcase(data):
        return data
    return data if os.path.basename(data).lower() == PORTABLE_DATA else None


_PORTABLE_ROOT = _portable_root()
PORTABLE = _PORTABLE_ROOT is not None
if PORTABLE:
    APP_ROOT = _PORTABLE_ROOT
elif getattr(sys, "frozen", False):
    APP_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_ROOT = os.path.dirname(APP_DIR)


def resource_path(rel):
    """Absolute path of a bundled read-only resource given relative to the
    app/ package (e.g. "assets/icon.ico"). Frozen builds unpack them under
    sys._MEIPASS (as app/<rel>, or <rel> at the bundle root)."""
    rel = rel.replace("/", os.sep)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (os.path.join(base, "app", rel), os.path.join(base, rel)):
            if os.path.exists(cand):
                return cand
        return os.path.join(base, "app", rel)
    return os.path.join(APP_DIR, rel)


def _bundled_exe(sub, name):
    """Absolute path of a program bundled with the frozen build
    (<bundle>\\<sub>\\<name>), else None."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    p = os.path.join(base, sub, name)
    return p if os.path.isfile(p) else None


# the programs the app runs. Frozen: ABSOLUTE paths of the bundled copies
# (resolved once), so a planted ffmpeg.exe in the working folder can never
# win; from source: the bare names (PATH). Code that still runs the bare
# names is covered by intro_credits_toolkit._frozen_setup (bundled folder
# first on PATH + NoDefaultCurrentDirectoryInExePath=1).
FFMPEG = _bundled_exe("ffmpeg", "ffmpeg.exe") or "ffmpeg"
FFPROBE = _bundled_exe("ffmpeg", "ffprobe.exe") or "ffprobe"
MPV_BUNDLED = _bundled_exe("mpv", "mpv.exe")          # None = not bundled

MEDIA_DIR = os.path.join(APP_ROOT, "Media")
DATA_DIR = os.path.join(APP_ROOT, "Data")

TEMPLATES_DIR = os.path.join(MEDIA_DIR, "templates")   # parent of the template clips
INPUT_DIR = TEMPLATES_DIR                               # old name, kept for imports
INTRO_DIR = os.path.join(TEMPLATES_DIR, "intro")
CREDITS_DIR = os.path.join(TEMPLATES_DIR, "credits")
PREINTRO_DIR = os.path.join(TEMPLATES_DIR, "preintro")        # clips BEFORE the intro
AFTERCREDITS_DIR = os.path.join(TEMPLATES_DIR, "aftercredits")  # clips AFTER the credits
VIDEO_DIR = os.path.join(MEDIA_DIR, "videos")
OUTPUT_DIR = os.path.join(MEDIA_DIR, "output")
AUDIOGAIN_DIR = os.path.join(MEDIA_DIR, "Audio Gain")
AUDIOGAIN_INPUT = os.path.join(AUDIOGAIN_DIR, "input")
AUDIOGAIN_OUTPUT = os.path.join(AUDIOGAIN_DIR, "output")

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PRESETS_DIR = os.path.join(DATA_DIR, "presets")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
TRASH_DIR = os.path.join(TEMP_DIR, "trash")
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")

# folders created at startup
WORK_DIRS = (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR, VIDEO_DIR,
             OUTPUT_DIR, AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT, PRESETS_DIR, LOGS_DIR,
             TEMP_DIR)

# settings keys holding a folder / file path (a relative value is resolved
# against APP_ROOT on load, and migrate.py rewrites old-layout values)
PATH_KEYS = ("video_dir", "intro_dir", "credits_dir", "preintro_dir",
             "aftercredits_dir", "output_dir", "detect_dir", "theme_out",
             "ag_in", "ag_out", "manual_video", "last_video", "multi_files")


def short_path(path):
    """`path` shown relative to APP_ROOT when it lies inside it (for labels),
    e.g. 'Data\\temp\\trash'."""
    try:
        rel = os.path.relpath(path, APP_ROOT)
    except ValueError:            # other drive
        return path
    return path if rel.startswith("..") else rel


INTRO_SEARCH_WINDOW = 600
CREDITS_SEARCH_WINDOW = 600

VALID_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]

# ---- video codec selection ----
CODECS = {
    N_("Auto - CPU (match source)"): "auto",
    N_("Auto - GPU / NVENC (match source)"): "auto_gpu",
    # Plex-ready: NVENC if usable else CPU, near-lossless quality capped at
    # the source's video bitrate (never bigger), Plex direct-play audio, MKV
    N_("Plex-ready HEVC (near-lossless, never bigger)"): "plex_hevc",
    N_("Plex-ready H.264 (near-lossless, never bigger)"): "plex_h264",
    N_("H.264 (libx264) - most compatible"): "libx264",
    N_("H.265 / HEVC (libx265) - smaller files"): "libx265",
    N_("H.264 NVENC (NVIDIA GPU, fast)"): "h264_nvenc",
    N_("H.265 NVENC (NVIDIA GPU, fast)"): "hevc_nvenc",
    N_("AV1 (libsvtav1) - smallest, slow"): "libsvtav1",
}

# label used before the CPU/GPU auto split -> its replacement, so old saved
# settings still select a valid dropdown entry (normalized on load).
LEGACY_CODEC_LABELS = {"Auto (match source)": "Auto - CPU (match source)"}

# "Auto - CPU": pick the CPU encoder for the same codec family as the source, so
# the output doesn't balloon (e.g. HEVC source -> libx265).
AUTO_CODEC_MAP = {"hevc": "libx265", "av1": "libsvtav1", "vp9": "libx265"}
# "Auto - GPU": same idea but with the NVENC hardware encoders. NVENC has no AV1
# on most cards, so AV1/VP9 sources fall back to the efficient hevc_nvenc; H.264
# and anything unknown stay on the most-compatible h264_nvenc.
AUTO_GPU_CODEC_MAP = {"hevc": "hevc_nvenc", "h264": "h264_nvenc",
                      "av1": "hevc_nvenc", "vp9": "hevc_nvenc"}
DEFAULT_CODEC_LABEL = "H.264 (libx264) - most compatible"

# audio-track language choices for detection/matching (label -> ISO code or None
# for "all / default"). Used by Cut / Edit and Templates -> Auto-detect.
AUDIO_LANG_CHOICES = {
    N_("All / default track"): None,
    N_("English"): "eng",
    N_("Japanese"): "jpn",
    N_("Spanish"): "spa",
    N_("Portuguese"): "por",
    N_("French"): "fre",
    N_("German"): "ger",
    N_("Arabic"): "ara",
}

# NVENC uses p1-p7 presets and -cq instead of -crf
NVENC_PRESET_MAP = {"ultrafast": "p2", "superfast": "p2", "veryfast": "p3",
                    "faster": "p4", "fast": "p4", "medium": "p5",
                    "slow": "p6", "slower": "p7", "veryslow": "p7"}
# SVT-AV1 uses numeric presets 0(slowest)-13(fastest)
SVT_PRESET_MAP = {"ultrafast": "12", "superfast": "11", "veryfast": "10",
                  "faster": "9", "fast": "9", "medium": "8",
                  "slow": "6", "slower": "5", "veryslow": "4"}


BIT_DEPTHS = {N_("Auto (match source)"): "auto", N_("8-bit"): "8", N_("10-bit"): "10"}

# every video container the toolkit accepts (lower-case, with the dot) - shared
# by the engines and the file pickers so they always agree
MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
              ".ts", ".mpg", ".mpeg", ".wmv", ".flv")

# ffmpeg subprocesses must not flash a console window on Windows
POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# "Limit CPU use" (Settings -> Performance): ffmpeg / ffprobe / mpv run at
# below-normal priority and encodes / decodes get a thread cap. Keys of the
# "Max encoder threads" choice (the labels are built in the Settings dialog).
CPU_THREAD_CHOICES = ("auto", "2", "4", "6", "8", "12", "16", "all")
# hardware encoders - their work is on the GPU, a -threads cap is pointless
_HW_ENCODER_TAGS = ("_nvenc", "_qsv", "_amf", "_vaapi", "_mf", "_videotoolbox")


def auto_threads():
    """'Auto' thread cap: half the logical cores, at least 1, at most 8."""
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def cpu_threads(prefs=None):
    """The thread cap N of the "Limit CPU use" setting, or 0 = no cap (the
    setting is off, or 'All' threads)."""
    p = PREFS if prefs is None else prefs
    if not p.get("cpu_limit", True):
        return 0
    v = str(p.get("cpu_threads", "auto")).strip().lower()
    if v == "all":
        return 0
    if v.isdigit() and int(v) > 0:
        return int(v)
    return auto_threads()


def popen_flags():
    """creationflags for every ffmpeg / ffprobe / mpv the app starts: no
    console window, plus below-normal priority while "Limit CPU use" is on
    (read live, so a change applies to the next process)."""
    f = POPEN_FLAGS
    if os.name == "nt" and PREFS.get("cpu_limit", True):
        f |= BELOW_NORMAL_PRIORITY_CLASS
    return f


def limit_cmd(cmd):
    """An ffmpeg argv with the "Limit CPU use" thread caps added (a copy; any
    other program, or the setting off / 'All', -> cmd unchanged):
      * -filter_threads / -filter_complex_threads min(N, 4)   (global)
      * -threads N before every -i                    (decoder threads)
      * software video encoders: -threads N; libx265 also -x265-params
        pools=N, libsvtav1 -svtav1-params lp=N; GPU encoders untouched.
    A command that already sets -threads is left alone."""
    n = cpu_threads()
    if not n or not cmd:
        return cmd
    prog = os.path.splitext(os.path.basename(str(cmd[0])))[0].lower()
    if prog != "ffmpeg" or "-threads" in cmd:
        return cmd
    fn = str(min(n, 4))
    out = [cmd[0], "-filter_threads", fn, "-filter_complex_threads", fn]
    x265_at = svt_at = None
    i = 1
    while i < len(cmd):
        a = cmd[i]
        if a == "-i":
            out += ["-threads", str(n)]
        elif (a in ("-c:v", "-vcodec", "-codec:v") or a.startswith("-c:v:"))                 and i + 1 < len(cmd):
            enc = str(cmd[i + 1])
            out += [a, enc]
            i += 2
            low = enc.lower()
            if low == "copy" or any(t in low for t in _HW_ENCODER_TAGS):
                continue
            if low == "libsvtav1":
                svt_at = len(out)
                continue
            out += ["-threads", str(n)]
            if low == "libx265":
                x265_at = len(out)
            continue
        out.append(a)
        i += 1
    todo = [(at, flag, key) for at, flag, key in ((x265_at, "-x265-params", "pools"),
                                                   (svt_at, "-svtav1-params", "lp"))
            if at is not None]
    for at, flag, key in sorted(todo, reverse=True):      # inserts from the back
        if flag in out:
            j = out.index(flag) + 1
            if j < len(out) and f"{key}=" not in out[j]:
                out[j] = f"{out[j]}:{key}={n}" if out[j] else f"{key}={n}"
        else:
            out[at:at] = [flag, f"{key}={n}"]
    return out

AUDIO_RECODE_MAP = {
    "ac3": ("ac3", "640k"),
    "eac3": ("eac3", "640k"),
    # ffmpeg's DTS encoder (dca) is experimental and refuses without -strict -2;
    # E-AC3 is a stable, widely supported surround codec instead
    "dts": ("eac3", "640k"),
    "truehd": ("flac", None),
    "flac": ("flac", None),
    "aac": ("aac", "320k"),
    "mp3": ("libmp3lame", "320k"),
    "opus": ("libopus", "320k"),
    "vorbis": ("libvorbis", "320k"),
    "pcm_s16le": ("pcm_s16le", None),
    "pcm_s24le": ("pcm_s24le", None),
}
DEFAULT_AUDIO_RECODE = ("aac", "320k")

# audio output choices for the Audio Gain tool (None = match the source codec,
# preserving 5.1 / Dolby / DTS and channel layout per track)
AUDIO_OUT_CHOICES = {
    N_("Match source (keep codec & channels)"): None,
    N_("AAC 320k"): ("aac", "320k"),
    N_("AC3 640k (Dolby Digital)"): ("ac3", "640k"),
    N_("E-AC3 640k"): ("eac3", "640k"),
    N_("FLAC (lossless)"): ("flac", None),
    N_("Opus 320k"): ("libopus", "320k"),
}


# ======================= settings persistence =======================
def _abs_setting(v):
    """A relative path value from the settings -> absolute under APP_ROOT."""
    if isinstance(v, str) and v.strip() and not os.path.isabs(v):
        return os.path.normpath(os.path.join(APP_ROOT, v))
    return v


def _rel_setting(v):
    """An absolute path value inside APP_ROOT -> relative with forward slashes
    ("Media/videos"), so the app folder can be moved or renamed. Paths
    elsewhere (other folders / drives) stay absolute."""
    if not (isinstance(v, str) and v.strip() and os.path.isabs(v)):
        return v
    try:
        rel = os.path.relpath(os.path.normpath(v), APP_ROOT)
    except ValueError:            # other drive
        return v
    if rel == os.curdir or rel.startswith(os.pardir):
        return v
    return rel.replace("\\", "/")


def resolve_paths(d):
    """In place: relative PATH_KEYS values of a settings/preset dict ->
    absolute under APP_ROOT. Returns d."""
    for k in PATH_KEYS:
        if isinstance(d.get(k), list):
            d[k] = [_abs_setting(v) for v in d[k]]
        elif k in d:
            d[k] = _abs_setting(d[k])
    return d


def relativize_paths(d):
    """A copy of a settings/preset dict with PATH_KEYS values inside APP_ROOT
    stored relative (see _rel_setting)."""
    d = dict(d)
    for k in PATH_KEYS:
        if isinstance(d.get(k), list):
            d[k] = [_rel_setting(v) for v in d[k]]
        elif k in d:
            d[k] = _rel_setting(d[k])
    return d


# Data\settings.json is written via settings.json.tmp (swapped in, so a
# crash mid-write can't truncate it) and then copied to settings.json.bak
# (= the last good save). A settings.json that can't be parsed is renamed to
# settings.json.corrupt-<date-time> (kept for the user) and the .bak is
# restored - never silently replaced by the defaults. Problems are logged
# (applog) and kept for the UI:
#   settings_load_problem() -> message about a damaged settings file found at
#                              startup (and what was done), else None
#   settings_save_error()   -> the error of the LAST save, None = it worked
# (app.py can show either; save_settings() itself never raises.)
SETTINGS_BAK = SETTINGS_FILE + ".bak"
_SETTINGS_STATE = {"load_problem": None, "save_error": None, "load_info": None}


def settings_load_problem():
    """A message when the settings file was damaged / unreadable at startup
    (and what was done about it), else None."""
    return _SETTINGS_STATE["load_problem"]


def settings_load_info():
    """The same for the UI as a dict, else None: kind = "restored" (the last
    good save was put back), "reset" (no usable backup: the defaults) or
    "unreadable" (left alone, the defaults this session); kept = the name
    the damaged file was kept under ("" when it couldn't be moved aside);
    error = the reason."""
    return _SETTINGS_STATE["load_info"]


def settings_save_error():
    """The error text of the last save_settings() (None = saved fine)."""
    return _SETTINGS_STATE["save_error"]


def _log(msg):
    """Record a message in the session log (applog keeps it in memory until
    the log file is open). Never raises."""
    try:
        from . import applog
        applog.record(msg)
    except Exception:
        pass


def _read_settings_file(path):
    """The dict in `path`. Raises FileNotFoundError / OSError (can't read) /
    ValueError (not valid JSON / UTF-8, or not an object)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("not a JSON object")
    return d


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings():
    try:
        d = _read_settings_file(SETTINGS_FILE)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        d = _recover_settings(f"it is damaged ({exc})")
    except OSError as exc:
        # there but unreadable (locked / no permission): leave it alone,
        # start with the defaults and say so
        msg = f"[settings] could not read {SETTINGS_FILE}: {exc} - using the defaults"
        _SETTINGS_STATE["load_problem"] = msg
        _SETTINGS_STATE["load_info"] = {"kind": "unreadable", "kept": "", "error": str(exc)}
        _log(msg)
        return {}
    # relative folder values mean "next to the app", not "wherever the
    # working directory happens to be"
    return resolve_paths(d)


def _recover_settings(why):
    """settings.json can't be parsed: move it aside and fall back to the last
    good save (.bak, restored as settings.json). Returns the dict ({} when
    there is no usable backup)."""
    bad = f"{SETTINGS_FILE}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(SETTINGS_FILE, bad)
        kept = f"kept as {os.path.basename(bad)}"
        kept_name = os.path.basename(bad)
    except OSError as exc:
        kept = f"could not be moved aside ({exc})"
        kept_name = ""
    try:
        d = _read_settings_file(SETTINGS_BAK)
    except (OSError, ValueError):
        d = None
    name = os.path.basename(SETTINGS_FILE)
    restored = d is not None
    if d is not None:
        try:
            _write_atomic(SETTINGS_FILE, json.dumps(d, indent=2))
        except OSError:
            pass
        msg = (f"[settings] {name} could not be loaded - {why}; {kept}. Restored the "
               f"last good save ({os.path.basename(SETTINGS_BAK)}).")
    else:
        d = {}
        msg = (f"[settings] {name} could not be loaded - {why}; {kept}. No usable "
               "backup - starting with the default settings.")
    _SETTINGS_STATE["load_problem"] = msg
    _SETTINGS_STATE["load_info"] = {"kind": "restored" if restored else "reset",
                                    "kept": kept_name, "error": why}
    _log(msg)
    return d


def save_settings(d):
    """Write the settings (see the comment above). Returns True when saved;
    never raises - an error is logged and kept for settings_save_error()."""
    try:
        text = json.dumps(relativize_paths(d), indent=2)
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        _write_atomic(SETTINGS_FILE, text)
    except (OSError, TypeError, ValueError) as exc:
        err = f"{type(exc).__name__}: {exc}"
        if _SETTINGS_STATE["save_error"] != err:        # each new error once
            _log(f"[settings] saving {SETTINGS_FILE} failed: {err}")
        _SETTINGS_STATE["save_error"] = err
        return False
    try:
        _write_atomic(SETTINGS_BAK, text)               # = the last good save
    except OSError:
        pass
    if _SETTINGS_STATE["save_error"]:
        _log("[settings] saving the settings works again")
    _SETTINGS_STATE["save_error"] = None
    return True


# app-wide preferences (notifications, update check, log retention) - kept in
# the same Data\settings.json. app.py loads them once (load_prefs), the
# Settings dialog edits PREFS, and persist() writes them back with the tabs'
# settings (prefs_snapshot).
PREF_DEFAULTS = {
    "notify_on": True,            # done-notifications at all
    "notify_toast": True,         # Windows toast (else / on failure: in-app popup)
    "notify_sound": True,         # ok / error sound
    "notify_min_minutes": 1.0,    # only for jobs that ran at least this long
    "update_check": True,         # look for a newer release at startup
    "update_last_check": 0.0,     # epoch seconds of the last check
    "log_keep_days": 30,          # session logs older than this go to the trash
    "language": "en",             # UI language code ("auto" = Windows language), see i18n.py
    "player_engine": "auto",      # video preview engine: auto (mpv if found) / mpv / opencv
    "cpu_limit": True,            # Limit CPU use: low-priority ffmpeg/mpv + thread caps
    "cpu_threads": "auto",        # thread cap: a CPU_THREAD_CHOICES key
    "font_scale": 1.0,            # UI font size factor (ui/themes.FONT_SCALES), at startup
}
PREFS = dict(PREF_DEFAULTS)


def load_prefs(saved):
    """Fill PREFS from a loaded settings dict (bad/missing values -> default)."""
    for k, dflt in PREF_DEFAULTS.items():
        v = saved.get(k, dflt)
        try:
            if isinstance(dflt, bool):
                v = bool(v)
            elif isinstance(dflt, int):
                v = int(v)
            elif isinstance(dflt, float):
                v = float(v)
            elif isinstance(dflt, str):
                v = v if isinstance(v, str) else dflt
        except (TypeError, ValueError):
            v = dflt
        PREFS[k] = v
    return PREFS


def prefs_snapshot():
    return dict(PREFS)
