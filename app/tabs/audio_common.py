"""Helpers shared by the Theme Audio and Audio Gain tabs."""
import os
import re as _re
import threading
import tkinter as tk
from tkinter import ttk

from ..engine.probe import probe_audio_tracks
from ..i18n import N_, tr
from ..ui.dialogs import ask_choice
from ..ui.tkthread import _call_tk
from ..ui.helpwin import help_button
from ..ui.tooltips import add_tooltip


_MEDIA_PATTERN = "*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.aac *.flac *.wav *.ogg *.opus"


def media_types():
    """filedialog filetypes= for media files (translated descriptions)."""
    return [(tr("Video / audio"), _MEDIA_PATTERN), (tr("All files"), "*.*")]


# allowed ranges for typed values (checked before a job starts)
LUFS_RANGE = (-70.0, -5.0)
GAIN_RANGE = (-60.0, 30.0)
GAIN_WARN = 12.0                 # above this a flat gain will likely clip
PEAK_RANGE = (-60.0, 0.0)
TP_RANGE = (-9.0, 0.0)           # loudnorm true-peak ceiling (dBTP)
DEFAULT_TP = "-1.5"

# which audio tracks an Audio Gain job changes
TRACK_SCOPES = {N_("All tracks (each on its own measurement)"): "all",
                N_("Selected track only (others copied untouched)"): "sel"}


def _nb_help(nb, keys, default):
    """A '?' help button on the right end of notebook nb's tab strip that
    opens the help of the selected sub-tab (keys: tab index -> helpdocs key;
    other tabs, e.g. Log, get `default`)."""
    btns = {}

    def sync(_e=None):
        try:
            i = nb.index("current")
        except tk.TclError:
            i = 0
        key = keys.get(i, default)
        for k, b in btns.items():
            if k != key:
                b.place_forget()
        if key not in btns:
            btns[key] = help_button(nb.master, key)
        btns[key].place(in_=nb, relx=1.0, x=0, y=0, anchor="ne")
        btns[key].lift()
    nb.bind("<<NotebookTabChanged>>", sync, add="+")
    sync()


def _track_by_lang(tracks, lang):
    """Position (0:a:N) of the first track in `tracks` (probe_audio_tracks)
    whose language is `lang`; None if there's none (or no lang)."""
    if not lang:
        return None
    for t in tracks:
        if t.get("lang") == lang:
            return t["track"]
    return None


class _TrackPicker:
    """'Audio track:' Combobox for one file. load(path) probes the file's
    audio tracks in the background and pre-selects the first track in the
    preferred language (the language the user last picked), else track #1."""

    def __init__(self, parent, owner, pref_lang="", width=44):
        self.owner = owner                      # a widget, for .after()
        self.var = tk.StringVar(value="")
        self.combo = ttk.Combobox(parent, textvariable=self.var, state="readonly",
                                  width=width)
        self.combo.bind("<<ComboboxSelected>>", self._picked)
        self.tracks = []
        self.path = None
        self.pref_lang = (pref_lang or "").lower()
        self._token = 0
        add_tooltip(self.combo, tr("Which audio track to use. The language you pick is "
                                   "remembered and chosen again for the next file if it has it."))

    def load(self, path):
        path = (path or "").strip().strip('"')
        if not path or not os.path.isfile(path):
            self._token += 1
            self.path, self.tracks = None, []
            self.combo.configure(values=[])
            self.var.set("")
            return
        if self.path and self.tracks and _same_file(self.path, path):
            return
        self._token += 1
        tok = self._token
        self.path, self.tracks = path, []
        self.combo.configure(values=[])
        self.var.set(tr("Reading audio tracks..."))

        def work():
            try:
                tracks = probe_audio_tracks(path)
            except Exception:
                tracks = []
            _call_tk(lambda: self._fill(tok, tracks))
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, tok, tracks):
        if tok != self._token:
            return
        self.tracks = tracks
        labels = [t["label"] for t in tracks]
        self.combo.configure(values=labels)
        if not tracks:
            self.var.set(tr("(no audio track found)"))
            return
        pick = _track_by_lang(tracks, self.pref_lang)
        self.var.set(labels[pick if pick is not None else 0])

    def _picked(self, _e=None):
        t = self.selected_info()
        if t:
            self.pref_lang = t["lang"]

    def selected_info(self):
        for t in self.tracks:
            if t["label"] == self.var.get():
                return t
        return None

    def track_for(self, path):
        """Selected track (0:a:N) if the picker shows `path`, else None (not
        probed yet - resolve with resolve_track() in the worker)."""
        if self.path and self.tracks and _same_file(self.path, path):
            t = self.selected_info()
            return t["track"] if t else 0
        return None

    def resolve_track(self, path, picked):
        """Worker-side: the picked track, or the preferred-language one."""
        if picked is not None:
            return picked
        try:
            pick = _track_by_lang(probe_audio_tracks(path), self.pref_lang)
        except Exception:
            pick = None
        return pick or 0


def _job_tracks(scope, keep, track):
    """tracks= argument for the media gain/normalize helpers: only the chosen
    track for 'Selected track only' - and always for an audio-only output,
    which can hold one track; None (= every track) otherwise."""
    if scope == "sel" or not keep:
        return [track]
    return None


def _check_range(value, rng, what):
    """Raise ValueError (UI message) unless lo <= value <= hi; `what` is the
    already-translated field name."""
    lo, hi = rng
    if not lo <= value <= hi:
        raise ValueError(tr("{what} must be between {lo:g} and {hi:g} (got {value:g}).",
                            what=what, lo=lo, hi=hi, value=value))
    return value


def _first_number(s):
    """Pull the first number out of a string like '-16 LUFS (streaming)' or '3'."""
    m = _re.search(r"-?\d+(?:\.\d+)?", str(s))
    if not m:
        raise ValueError(tr("no number found"))
    return float(m.group(0))


def _same_file(a, b):
    """True if paths a and b point at the same file (case-insensitive on
    Windows, symlinks resolved) - used so an output never overwrites its input."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


def _mtime(path):
    """mtime_ns of path, or None if it doesn't exist."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def path_key(p):
    """Case-/form-insensitive key of a path (Windows: A.mkv == a.MKV)."""
    return os.path.normcase(os.path.abspath(p))


def part_path(out):
    """'<name>.part<ext>' next to `out`: a job writes there and only a finished
    file replaces `out` - Stop or a failure never costs an older file of that
    name (the real extension stays last, so ffmpeg picks the right format)."""
    root, ext = os.path.splitext(out)
    return f"{root}.part{ext}"


def commit_part(ok, part, out, log):
    """After a job wrote `part`: ok -> move it over `out` (only now replacing
    an older file); otherwise delete the partial file. True if `out` now
    holds the new output."""
    if ok and os.path.exists(part):
        try:
            os.replace(part, out)
            return True
        except OSError as e:
            log(f"  [FAIL] could not write {os.path.basename(out)} (open elsewhere?): {e}")
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError as e:
        log(f"  could not remove the unfinished {os.path.basename(part)}: {e}")
    return False


def free_path(path, taken=()):
    """'name (2).ext', 'name (3).ext' ... - the first that neither exists nor
    is in `taken` (path_key()s of outputs / sources of this run)."""
    root, ext = os.path.splitext(path)
    n = 2
    while True:
        cand = f"{root} ({n}){ext}"
        if not os.path.exists(cand) and path_key(cand) not in taken:
            return cand
        n += 1


def ask_existing(parent, path, batch=False):
    """Tk thread: what to do with an output that exists already - 'over' /
    'both' / 'skip', or None (Cancel / closed). batch: the answer is used for
    every existing output of the run."""
    msg = (tr("The output file exists already:\n\n{path}\n\nWhat should happen to it - and "
              "to every other output of this run that exists already?",
              path=os.path.basename(path)) if batch else
           tr("The output file exists already:\n\n{path}\n\nWhat should happen to it?",
              path=os.path.basename(path)))
    choice = ask_choice(parent, tr("Files exist"), msg,
                        [("over", tr("Overwrite")), ("both", tr("Keep both")),
                         ("skip", tr("Skip")), ("cancel", tr("Cancel"))],
                        default="both", cancel="cancel")
    return choice if choice in ("over", "both", "skip") else None


def ask_from_worker(fn, stop_event=None):
    """Worker thread: run fn() (e.g. a dialog) on the Tk thread and wait for
    its result. None if it failed - or once stop_event is set."""
    box, ev = {}, threading.Event()

    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = None
        finally:
            ev.set()
    _call_tk(run)
    while not ev.wait(0.25):
        if stop_event is not None and stop_event.is_set():
            return None
    return box.get("v")


def _discard_partial(out, before, log):
    """Remove a stopped/failed output so a truncated file isn't left under the
    final name. *before* is _mtime(out) taken before the job: a file that was
    already there and was never touched (stopped before writing) is kept."""
    if before is not None and _mtime(out) == before:
        return
    try:
        if os.path.exists(out):
            os.remove(out)
            log(f"  removed incomplete output: {os.path.basename(out)}")
    except OSError as e:
        log(f"  could not remove incomplete output {out}: {e}")
