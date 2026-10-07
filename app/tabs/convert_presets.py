"""Convert presets: the built-in ones + user presets stored as
Data\\presets\\convert\\<name>.json. UI-free.

A preset holds the Settings that describe HOW to encode (video, filters,
audio / subtitle rules, container options) - not the output folder or the
naming pattern, which stay as they are when a preset is applied.
"Delete" moves the file into Data\\temp\\trash (nothing is deleted)."""
import json
import os
import time

from ..config import TRASH_DIR
from ..engine import recode
from ..engine.files import move_into_trash, trash_folder
from ..i18n import N_
from ..presets import sanitize

# keys a preset never carries (they belong to the user's output setup)
_NOT_IN_PRESET = ("out_dir", "pattern")

_PLEX = dict(rate="crf", crf=19, preset="slow", depth="auto", cap=True,
             container="mkv", audio_codec="plex", audio_bitrate="", audio_mix="keep")
BUILTIN = {
    N_("Plex-ready HEVC"): dict(_PLEX, vcodec="plex_hevc"),
    N_("Plex-ready H.264"): dict(_PLEX, vcodec="plex_h264", depth="8"),
    N_("High quality HEVC (x265 CRF 18 slow)"): dict(
        vcodec="libx265", rate="crf", crf=18, preset="slow", depth="auto",
        audio_codec="copy"),
    N_("Small file HEVC (CRF 24)"): dict(
        vcodec="libx265", rate="crf", crf=24, preset="medium", depth="auto",
        audio_codec="aac", audio_bitrate="", audio_mix="keep"),
    N_("AV1 (SVT-AV1 CRF 30)"): dict(
        vcodec="libsvtav1", rate="crf", crf=30, preset="medium", depth="10",
        audio_codec="libopus", audio_bitrate=""),
    N_("Archive (x265 CRF 16 10-bit)"): dict(
        vcodec="libx265", rate="crf", crf=16, preset="slow", depth="10",
        audio_codec="copy"),
    N_("Fast GPU (NVENC)"): dict(
        vcodec="hevc_nvenc", rate="crf", crf=24, preset="fast", depth="auto",
        audio_codec="copy"),
}

# a module global so tests can redirect it
PRESETS_DIR = recode.PRESETS_DIR


def preset_settings(base, preset):
    """`base` settings with `preset` applied on top (output folder and
    naming pattern kept from `base`). Unknown keys are ignored."""
    d = recode.default_settings()
    d.update({k: v for k, v in (preset or {}).items() if k not in _NOT_IN_PRESET})
    for k in _NOT_IN_PRESET:
        d[k] = (base or {}).get(k, d[k])
    return recode.normalize_settings(d)


def user_names():
    try:
        files = os.listdir(PRESETS_DIR)
    except OSError:
        return []
    return sorted((os.path.splitext(f)[0] for f in files
                   if f.lower().endswith(".json")
                   and os.path.isfile(os.path.join(PRESETS_DIR, f))), key=str.lower)


def _path(name):
    stem = sanitize(name)
    return os.path.join(PRESETS_DIR, stem + ".json") if stem else None


def load(name):
    """Settings dict of a built-in (by its English key) or user preset."""
    if name in BUILTIN:
        return dict(BUILTIN[name])
    p = _path(name)
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    d.pop("_preset", None)
    return d


def save(name, settings):
    """Write a user preset; returns the saved (sanitised) name. Raises
    ValueError for an unusable name / a built-in's name, OSError on I/O."""
    stem = sanitize(name)
    if not stem or stem in BUILTIN:
        raise ValueError(name)
    os.makedirs(PRESETS_DIR, exist_ok=True)
    d = {k: v for k, v in recode.normalize_settings(settings).items()
         if k not in _NOT_IN_PRESET}
    d["_preset"] = {"name": stem, "kind": "convert",
                    "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = os.path.join(PRESETS_DIR, stem + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return stem


def trash(name):
    """Move a user preset into Data\\temp\\trash\\<stamp>_convert presets\\."""
    p = _path(name)
    if not p or not os.path.isfile(p):
        return None
    return move_into_trash(p, trash_folder("convert presets", TRASH_DIR))
