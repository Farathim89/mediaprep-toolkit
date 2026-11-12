"""Output-file plumbing: .part files, atomic placing, moving finished
sources to done/, temp cleanup and the shared move-to-trash helper."""
import os
import errno
import shutil
import time

from ..config import TRASH_DIR
from .. import applog


def _cleanup(temp_audio, temp_segs):
    for p in [temp_audio] + list(temp_segs):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _same_path(a, b):
    """True if a and b name the same file (Windows paths are case-insensitive)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _path_key(p):
    """Case-insensitive, absolute key of a path (for sets of paths)."""
    return os.path.normcase(os.path.abspath(p))


def _part_path(final):
    """Temp sibling '<name>.part<ext>' to write an output into. Keeps the real
    extension so ffmpeg still picks the right muxer."""
    root, ext = os.path.splitext(final)
    return f"{root}.part{ext}"


def _keep_new_path(final):
    """'<name> (new).<ext>' (then '(new 2)', ...) - the first that doesn't
    exist: where a finished output goes when `final` stays locked."""
    root, ext = os.path.splitext(final)
    cand, n = f"{root} (new){ext}", 2
    while os.path.exists(cand):
        cand, n = f"{root} (new {n}){ext}", n + 1
    return cand


def _commit_part(rc, part, final, log, check=None, tries=6, delay=0.5):
    """ffmpeg wrote `part` with return code rc: on success swap it into place
    (only now replacing any previous output), else delete the partial file so
    a failed/stopped run never leaves a truncated file under the final name.
    check: optional safety gate, check(part) -> None if the file is fine,
    else a reason - a rejected output is deleted and nothing is replaced.
    An old output held open by a player (WinError 32) is retried a few
    times; if it stays locked the finished file is KEPT as
    '<name> (new).<ext>' (logged) instead of being thrown away.
    Returns the path that now holds the new output (final, or the '(new)'
    name), or None - so `if _commit_part(...)` still reads as success."""
    if rc == 0 and os.path.exists(part):
        problem = check(part) if check else None
        if problem:
            log(f"   [FAIL] output rejected: {problem} - nothing was replaced\n")
        else:
            err = None
            for attempt in range(tries):
                try:
                    os.replace(part, final)
                    return final
                except PermissionError as e:       # in use - wait and retry
                    err = e
                    if attempt < tries - 1:
                        time.sleep(delay)
                except OSError as e:
                    err = e
                    break
            alt = _keep_new_path(final)
            try:
                os.replace(part, alt)
                log(f"   [WARN] {os.path.basename(final)} is in use ({err}) - the new"
                    f" output was saved as: {alt}")
                return alt
            except OSError as e:
                log(f"   [FAIL] could not write output (file open elsewhere?): {e}\n")
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError:
        pass
    return None


def _place_output(src, final, log, check=None):
    """Move a finished temp segment to `final` via a .part sibling (the temp
    folder may be on another drive, so the move itself isn't atomic). Logs
    [FAIL] instead of raising if the existing output is locked. check: the
    safety gate (see _commit_part). Returns the output path or None."""
    part = _part_path(final)
    try:
        shutil.move(src, part)
    except OSError as e:
        log(f"   [FAIL] could not write output: {e}\n")
        return _commit_part(1, part, final, log)
    return _commit_part(0, part, final, log, check)


def _move_to_done(video, dst, log, tries=6, delay=0.5):
    """Move a finished source into done/. A player/indexer/AV scanner holding
    the file briefly (WinError 32) is retried a few times before giving up.
    os.replace overwrites an old done/ copy in one step (never deleting it
    before the move can succeed); shutil.move only if it's another drive."""
    err = None
    for attempt in range(tries):
        try:
            try:
                os.replace(video, dst)
            except OSError as e:
                if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:
                    raise
                shutil.move(video, dst)            # different drive
            log(f"   [DONE] moved source -> {os.path.join('done', os.path.basename(dst))}")
            return True
        except PermissionError as e:               # in use - wait and retry
            err = e
            if attempt < tries - 1:
                time.sleep(delay)
        except OSError as e:
            err = e
            break
    log(f"   [WARN] could not move source to done: {err}")
    return False


# ======================= trash (nothing is deleted outright) =======================
# TRASH_DIR (Data\temp\trash, from config) is shared by the tabs, Clean up
# and presets


def trash_folder(label, root=None):
    """Data\\temp\\trash\\<YYYYMMDD-HHMMSS>_<label> (label made file-name safe)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in label).strip() or "items"
    return os.path.join(root or TRASH_DIR, f"{stamp}_{safe}")


def move_into_trash(path, dest, record=True):
    """Move one file / folder into the trash folder `dest` (created if needed;
    name clashes get a _2, _3...). Returns the new path; raises on failure."""
    os.makedirs(dest, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    target, n = os.path.join(dest, base + ext), 2
    while os.path.exists(target):
        target = os.path.join(dest, f"{base}_{n}{ext}")
        n += 1
    shutil.move(path, target)
    if record:
        applog.record(f"[trash] {path} -> {target}")
    return target


def move_to_trash(paths, label, log=None):
    """Nothing is deleted outright: move `paths` into
    Data\\temp\\trash\\<YYYYMMDD-HHMMSS>_<label>\\ (name clashes get a _2, _3...).
    Returns (moved_paths, [(path, error)])."""
    dest = trash_folder(label)
    moved, failed = [], []
    for p in paths:
        try:
            move_into_trash(p, dest)
            moved.append(p)
        except Exception as exc:
            failed.append((p, exc))
            if log:
                log(f"  [WARN] could not move {os.path.basename(p)} to the trash: {exc}")
    return moved, failed
