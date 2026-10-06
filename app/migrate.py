"""One-time move from the old flat folder layout to the tidy Media + Data one.

Old (everything next to the launcher)      New
    videos\ output\                    ->  Media\videos\  Media\output\
    input\intro|credits|preintro|...   ->  Media\templates\intro|credits|...
    Audio Gain\                        ->  Media\Audio Gain\
    logs\ temp\ presets\ backups\      ->  Data\logs\ Data\temp\ ...
    toolkit_settings.json              ->  Data\settings.json
    assets\                            ->  app\assets\   (source installs only)

run() is called by app.main() BEFORE the settings are loaded. Rules:
  * nothing is ever deleted - items are MOVED; a folder that already exists at
    the new place is merged into, and a name clash keeps both (the old item
    gets " (old)" before its extension);
  * an old folder is removed only once it is EMPTY after the merge (same for a
    leftover root __pycache__);
  * path values in the settings (and in saved presets) that point at the old
    places - relative ("videos", "input/intro") or absolute
    ("D:/.../MediaPrep ToolKit/videos/x.mkv") - are rewritten; folder keys
    (config.PATH_KEYS) become RELATIVE to the app folder ("Media/videos/x.mkv",
    resolved against APP_ROOT on load) so the folder can be moved / renamed;
    the original settings file is first copied to Data\backups\;
  * a locked / failing item is left where it is with a warning, never a crash;
  * idempotent: once migrated there is nothing left to do;
  * set MEDIAPREP_NO_MIGRATE=1 to skip it entirely.
The messages are returned (the session log isn't open yet - logs\ may be one
of the things being moved) and app.main() records them."""
import glob
import json
import os
import shutil
import sys
import time

from . import config as C

# old path relative to APP_ROOT (forward slashes) -> new absolute path.
# Longest first so input/intro wins over input.
def _folder_map(root=None):
    root = root or C.APP_ROOT
    j = os.path.join
    media, data = j(root, "Media"), j(root, "Data")
    m = {
        "videos": j(media, "videos"),
        "output": j(media, "output"),
        "input/intro": j(media, "templates", "intro"),
        "input/credits": j(media, "templates", "credits"),
        "input/preintro": j(media, "templates", "preintro"),
        "input/aftercredits": j(media, "templates", "aftercredits"),
        "input": j(media, "templates"),
        "Audio Gain": j(media, "Audio Gain"),
        "logs": j(data, "logs"),
        "temp": j(data, "temp"),
        "presets": j(data, "presets"),
        "backups": j(data, "backups"),
    }
    return dict(sorted(m.items(), key=lambda kv: -len(kv[0])))


OLD_SETTINGS = "toolkit_settings.json"


class _Migrator:
    def __init__(self, root, app_dir, frozen):
        self.root = root
        self.app_dir = app_dir
        self.frozen = frozen
        self.msgs = []
        self.moved = 0
        self.warnings = 0

    # ------------------------------------------------------------ logging
    def log(self, msg):
        self.msgs.append("[migrate] " + msg)

    def warn(self, msg):
        self.warnings += 1
        self.msgs.append("[migrate] WARNING: " + msg)

    def rel(self, p):
        try:
            r = os.path.relpath(p, self.root)
            return p if r.startswith("..") else r
        except ValueError:
            return p

    # ------------------------------------------------------------ moving
    @staticmethod
    def _old_name(target):
        """A free 'name (old).ext' / 'name (old 2).ext' next to target."""
        d, base = os.path.split(target)
        stem, ext = os.path.splitext(base)
        if os.path.isdir(target):            # folders keep dots in their name
            stem, ext = base, ""
        n = 1
        while True:
            tag = " (old)" if n == 1 else f" (old {n})"
            cand = os.path.join(d, f"{stem}{tag}{ext}")
            if not os.path.exists(cand):
                return cand
            n += 1

    def _move(self, src, dst):
        """Rename src to dst (same drive - instant, never a copy + delete).
        A folder that can't be renamed as a whole (a file inside is open) is
        moved item by item, so only the locked items stay behind."""
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            self.moved += 1
            self.log(f"moved {self.rel(src)} -> {self.rel(dst)}")
            return True
        except Exception as exc:              # locked (WinError 32), access denied ...
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    os.makedirs(dst)
                except OSError:
                    pass
                else:
                    self.log(f"{self.rel(src)} can't be moved as a whole ({exc}) - "
                             "moving its items one by one")
                    self.merge(src, dst)
                    return True
            self.warn(f"could not move {self.rel(src)} -> {self.rel(dst)}: {exc} "
                      "(left in place)")
            return False

    def _rmdir_if_empty(self, d):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                self.log(f"removed empty old folder {self.rel(d)}")
        except OSError:
            pass

    def merge(self, src, dst):
        """Move src (file or folder) to dst. Existing folders are merged into
        item by item; a clash keeps both (the moved item gets ' (old)')."""
        if not os.path.exists(dst):
            self._move(src, dst)
            return
        if os.path.isdir(src) and os.path.isdir(dst):
            try:
                names = sorted(os.listdir(src))
            except OSError as exc:
                self.warn(f"could not read {self.rel(src)}: {exc}")
                return
            for n in names:
                self.merge(os.path.join(src, n), os.path.join(dst, n))
            self._rmdir_if_empty(src)
            return
        # file vs file (or file vs folder): keep both
        self._move(src, self._old_name(dst))

    # ------------------------------------------------------------ steps
    def move_folders(self):
        fmap = _folder_map(self.root)
        # the four template kinds first, then whatever else is left in input\
        for old, new in fmap.items():
            src = os.path.join(self.root, *old.split("/"))
            if not os.path.isdir(src):
                continue
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(new)):
                continue
            self.merge(src, new)

    def move_settings(self):
        old = os.path.join(self.root, OLD_SETTINGS)
        new = os.path.join(self.root, "Data", "settings.json")
        if os.path.isfile(old):
            # the old file holds the user's real settings; a Data\settings.json
            # that already exists can only be a fresh default written by an
            # earlier start, so park it in backups instead of letting it win
            if os.path.isfile(new):
                parked = os.path.join(self.root, "Data", "backups",
                                      f"settings_default_{time.strftime('%Y%m%d-%H%M%S')}.json")
                self.merge(new, parked)
            self.merge(old, new)
        tmp = old + ".tmp"                    # half-written leftover of a crash
        if os.path.isfile(tmp):
            self.merge(tmp, os.path.join(self.root, "Data", "backups", OLD_SETTINGS + ".tmp"))

    def move_assets(self):
        if self.frozen:
            return                            # bundled inside the exe
        src = os.path.join(self.root, "assets")
        if not os.path.isdir(src):
            return
        dst = os.path.join(self.app_dir, "assets")
        if os.path.isdir(dst) and os.listdir(dst):
            # the new code already ships its own icon - keep the old copy
            # out of the way instead of mixing "icon (old).ico" into app\assets
            dst = os.path.join(self.root, "Data", "backups", "old assets")
        self.merge(src, dst)

    def clean_pycache(self):
        self._rmdir_if_empty(os.path.join(self.root, "__pycache__"))

    # ------------------------------------------------------------ settings paths
    def _new_path(self, value, allow_relative):
        """The new location for a path value pointing at an old folder, or None."""
        if not isinstance(value, str) or not value.strip():
            return None
        v = value.strip()
        norm = v.replace("\\", "/")
        if os.path.isabs(v):
            root = self.root.replace("\\", "/").rstrip("/") + "/"
            if not os.path.normcase(norm).startswith(os.path.normcase(root)):
                return None
            rel = norm[len(root):]
        elif allow_relative:
            rel = norm[2:] if norm.startswith("./") else norm
        else:
            return None
        rel_cmp = os.path.normcase(rel)
        for old, new in _folder_map(self.root).items():
            o = os.path.normcase(old)
            if rel_cmp == o or rel_cmp.startswith(o + "/") or rel_cmp.startswith(o + "\\"):
                rest = rel[len(old):].lstrip("/")
                if allow_relative:
                    # a PATH_KEYS value: store it RELATIVE ("Media/videos/x.mkv")
                    # - config resolves it against APP_ROOT, so the app folder
                    # can be moved / renamed later
                    out = os.path.relpath(new, self.root).replace("\\", "/")
                else:
                    out = new.replace("\\", "/")     # unknown key: keep absolute
                return out + ("/" + rest if rest else "")
        return None

    def _rewrite(self, obj, key=None):
        """Recursively rewrite old-location paths; returns (new_obj, changes)."""
        changes = []
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                nv, ch = self._rewrite(v, k)
                out[k] = nv
                changes += ch
            return out, changes
        if isinstance(obj, list):
            out = []
            for v in obj:
                nv, ch = self._rewrite(v, key)
                out.append(nv)
                changes += ch
            return out, changes
        new = self._new_path(obj, allow_relative=key in C.PATH_KEYS)
        if new is not None and new != obj:
            changes.append((key, obj, new))
            return new, changes
        return obj, changes

    def _rewrite_json(self, path, what):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            if os.path.exists(path):
                self.warn(f"could not read {self.rel(path)} to update its paths: {exc}")
            return
        new, changes = self._rewrite(data)
        if not changes:
            return
        # keep the original next to the backups (never lose the old values)
        bdir = os.path.join(self.root, "Data", "backups")
        stem = os.path.splitext(os.path.basename(path))[0]
        backup = os.path.join(bdir, f"{stem}_before_migration_{time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(path, backup)
            tmp = path + ".migrate.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new, f, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            self.warn(f"could not update the paths in {self.rel(path)}: {exc}")
            return
        self.log(f"{what}: updated {len(changes)} path value(s) in {self.rel(path)} "
                 f"(original copied to {self.rel(backup)})")
        for k, old, nv in changes:
            self.log(f"   {k}: {old}  ->  {nv}")

    def rewrite_settings(self):
        self._rewrite_json(os.path.join(self.root, "Data", "settings.json"), "settings")
        for p in sorted(glob.glob(os.path.join(glob.escape(os.path.join(
                self.root, "Data", "presets")), "*.json"))):
            self._rewrite_json(p, "preset")


def needed(root=None):
    """True if any old-layout item is still there."""
    root = root or C.APP_ROOT
    names = list(_folder_map(root)) + [OLD_SETTINGS]
    if not getattr(sys, "frozen", False):
        names.append("assets")
    return any(os.path.exists(os.path.join(root, *n.split("/"))) for n in names)


def run(root=None, app_dir=None, frozen=None):
    """Migrate the folder layout under `root` (default APP_ROOT). Returns the
    list of log messages (empty when there was nothing to do). Never raises."""
    if os.environ.get("MEDIAPREP_NO_MIGRATE", "").strip() not in ("", "0"):
        return ["[migrate] skipped (MEDIAPREP_NO_MIGRATE is set)"]
    root = os.path.abspath(root or C.APP_ROOT)
    m = _Migrator(root, app_dir or C.APP_DIR,
                  getattr(sys, "frozen", False) if frozen is None else frozen)
    try:
        if needed(root):
            m.log(f"old folder layout found in {root} - moving it to Media\\ and Data\\")
            m.move_folders()
            m.move_settings()
            m.move_assets()
        m.clean_pycache()
        m.rewrite_settings()
        if m.moved or m.warnings:
            m.log(f"done: {m.moved} item(s) moved, {m.warnings} warning(s)")
    except Exception as exc:                  # never block the app from starting
        m.warn(f"migration stopped early: {exc!r}")
    return m.msgs
