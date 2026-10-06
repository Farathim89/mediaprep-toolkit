"""Helpers shared by the Templates and Cut / Edit tabs."""
import os
import re
import threading

from ..config import MEDIA_EXTS
from ..engine.probe import audio_track_for_lang
from ..engine.snap import find_snap
from ..i18n import tr, N_


# one shared list of accepted containers (config.MEDIA_EXTS) - the alias keeps
# every existing use in this module (Templates AND Cut / Edit) working
_MEDIA_EXTS = MEDIA_EXTS
_VIDEO_TYPES = [(tr("Video files"), " ".join("*" + x for x in _MEDIA_EXTS)),
                (tr("All files"), "*.*")]

# friendly names for the segment keys (detected-rows Kind column etc.) -
# English (also used in log lines); call tr() on a value where it is displayed
_KIND_NAMES = {"preintro": N_("Pre-intro"), "intro": N_("Intro"),
               "credits": N_("Credits"), "aftercredits": N_("After-credits")}


def _same_dir(a, b):
    """True if two folders are the same (case/abspath-insensitive)."""
    try:
        return (os.path.normcase(os.path.abspath(a or "."))
                == os.path.normcase(os.path.abspath(b or ".")))
    except (TypeError, ValueError):
        return False


def _existing_template(folder, base):
    """Files in `folder` named `base` with any extension (a template may have
    been cut as .mp4 earlier and as .mkv now)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if os.path.splitext(n)[0].lower() == base.lower()
            and os.path.isfile(os.path.join(folder, n))]


def _template_stem(name):
    """Short template name: keep the show name + S00E00 tag, drop the episode
    title ('One Piece - S09E0285 - Obtain the 5 Keys!...' -> 'One Piece -
    S09E0285'). Falls back to the full name if there is no SxxExx tag."""
    m = re.search(r"^(.*?[Ss]\d{1,4}[Ee]\d{1,4})", name)
    return m.group(1).strip() if m else name


def _list_media(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names
                  if n.lower().endswith(_MEDIA_EXTS)
                  and os.path.isfile(os.path.join(folder, n)))


def _same_file(a, b):
    try:
        return bool(a and b) and (os.path.normcase(os.path.abspath(a))
                                  == os.path.normcase(os.path.abspath(b)))
    except (TypeError, ValueError):
        return False


def _snap_in_thread(owner, video, t, edge, lang, done):
    """Run snap.find_snap(video, t, edge=...) in a thread (on the `lang`
    audio track, else the first one) and call done(new_t, reason) on the Tk
    thread via owner.after. Shared by every Snap button (Templates,
    Manual cut, Multi cut)."""
    def work():
        res = (None, "snap failed")
        try:
            track = 0
            if lang:
                try:
                    track = audio_track_for_lang(video, lang) or 0
                except Exception:
                    track = 0
            res = find_snap(video, t, radius=1.0, audio_track=track, edge=edge)
        except Exception as exc:
            res = (None, f"snap failed: {exc}")
        finally:
            try:
                owner.after(0, lambda: done(*res))
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()
