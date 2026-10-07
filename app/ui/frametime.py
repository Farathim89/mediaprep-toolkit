"""Inclusive "To" boxes: the UI shows the LAST frame a section removes (like a
video editor's out-point), the engine keeps its exclusive end (= the time of
the first frame KEPT). These helpers convert between the two by stepping one
frame on the file's frame grid (the player's fps for a loaded file, else the
file's fps from ffprobe, cached per file)."""
import os
import threading

_fps_cache = {}
_lock = threading.Lock()


def _key(path):
    try:
        st = os.stat(path)
        return (os.path.normcase(os.path.abspath(path)), st.st_size, int(st.st_mtime))
    except OSError:
        return None


def file_fps(path, player=None):
    """Frames per second of `path`: the player's fps when it has exactly
    that file loaded, else ffprobe (cached per file). None if unknown."""
    if not path:
        return None
    if player is not None:
        try:
            pp = getattr(player, "_path", None)
            if player.has_video() and pp and (os.path.normcase(os.path.abspath(pp))
                                              == os.path.normcase(os.path.abspath(path))):
                if player.fps and player.fps > 0:
                    return float(player.fps)
        except (TypeError, ValueError, AttributeError):
            pass
    k = _key(path)
    if k is None:
        return None
    with _lock:
        if k in _fps_cache:
            return _fps_cache[k]
    try:
        from ..engine.probe import probe_video_fps
        fps = probe_video_fps(path)
    except Exception:
        fps = None
    fps = float(fps) if fps and fps > 0 else None
    with _lock:
        _fps_cache[k] = fps
    return fps


def excl_to_shown(end, fps):
    """Engine end (first kept frame) -> the last removed frame's time, on
    the fps grid. fps None = unknown: returned unchanged."""
    if end is None:
        return None
    end = float(end)
    if not fps:
        return end
    k = int(round(end * fps))
    return max(0, k - 1) / fps


def shown_to_excl(t, fps):
    """Shown To (last removed frame) -> engine end (the frame after it)."""
    if t is None:
        return None
    t = float(t)
    if not fps:
        return t
    k = int(round(t * fps))
    return (k + 1) / fps


_MISSING = object()


def cached_fps(path, player=None):
    """Like file_fps() but never runs ffprobe: (known, fps). known=False =
    not probed yet (use prefetch_fps)."""
    if player is not None:
        f = file_fps(path, player) if _player_has(player, path) else None
        if f:
            return True, f
    k = _key(path) if path else None
    if k is None:
        return True, None
    with _lock:
        v = _fps_cache.get(k, _MISSING)
    return (False, None) if v is _MISSING else (True, v)


def _player_has(player, path):
    try:
        pp = getattr(player, "_path", None)
        return bool(player.has_video() and pp and path
                    and os.path.normcase(os.path.abspath(pp))
                    == os.path.normcase(os.path.abspath(path)))
    except Exception:
        return False


_pending = set()


def prefetch_fps(paths, done=None):
    """Probe the fps of `paths` in a background thread (skips cached ones);
    done(path) is called from that thread after each newly probed file."""
    todo = []
    with _lock:
        for p in paths:
            k = _key(p) if p else None
            if k is not None and k not in _fps_cache and k not in _pending:
                _pending.add(k)
                todo.append((p, k))
    if not todo:
        return

    def work():
        for p, k in todo:
            try:
                file_fps(p)
            finally:
                with _lock:
                    _pending.discard(k)
            if done is not None:
                try:
                    done(p)
                except Exception:
                    pass
    threading.Thread(target=work, daemon=True).start()
