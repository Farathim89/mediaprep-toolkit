"""Per-show presets for the Cut / Edit tab.

A preset is a plain JSON dict of settings (folders, encoding, detection and
mode options, the Templates tab's Auto-detect settings) saved as
Data\\presets\\<name>.json next to Data\\settings.json. UI-free:

    presets.save("One Piece", {...})
    presets.names()                 # ["One Piece", ...]
    d = presets.load("One Piece")   # dict or None
    presets.trash("One Piece")      # moved to Data\\temp\\trash\\<stamp>_presets

Nothing is ever deleted outright - "Delete" moves the file into the shared
Data\\temp\\trash folder (like the Templates manager and Clean up folders).
PRESETS_DIR / TRASH_DIR are module globals so tests can redirect them."""
import json
import os
import re
import time

from .config import PRESETS_DIR, TRASH_DIR, relativize_paths, resolve_paths
from .engine.files import move_into_trash, trash_folder
from .i18n import tr

FORMAT_VERSION = 1

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def sanitize(name):
    """A safe file-name stem for `name` ('' if nothing usable is left).
    Windows-forbidden characters become '_', surrounding dots/spaces go,
    reserved device names get a trailing '_' and the length is capped."""
    s = _BAD_CHARS.sub("_", str(name or "")).strip().strip(".").strip()
    s = re.sub(r"\s+", " ", s)[:80].strip()
    if s.lower() in _RESERVED:
        s += "_"
    return s


def _path(name):
    stem = sanitize(name)
    return os.path.join(PRESETS_DIR, stem + ".json") if stem else None


def names():
    """Saved preset names, sorted case-insensitively."""
    try:
        files = os.listdir(PRESETS_DIR)
    except OSError:
        return []
    out = [os.path.splitext(f)[0] for f in files
           if f.lower().endswith(".json") and os.path.isfile(os.path.join(PRESETS_DIR, f))]
    return sorted(out, key=str.lower)


def exists(name):
    p = _path(name)
    return bool(p) and os.path.isfile(p)


def load(name):
    """The preset's settings dict, or None if missing / unreadable."""
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
    return resolve_paths(d)        # relative folders -> under the app folder


def save(name, data):
    """Write `data` as preset `name` (overwrites). Returns the saved name
    (sanitised). Raises ValueError for an unusable name, OSError on I/O."""
    stem = sanitize(name)
    if not stem:
        raise ValueError(tr("Give the preset a name (letters / numbers)."))
    os.makedirs(PRESETS_DIR, exist_ok=True)
    p = os.path.join(PRESETS_DIR, stem + ".json")
    d = relativize_paths(data or {})
    d["_preset"] = {"name": stem, "version": FORMAT_VERSION,
                    "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return stem


def trash(name):
    """Move the preset file into Data\\temp\\trash\\<YYYYMMDD-HHMMSS>_presets\\.
    Returns the new path, or None if there was no such preset."""
    p = _path(name)
    if not p or not os.path.isfile(p):
        return None
    return move_into_trash(p, trash_folder("presets", TRASH_DIR))
