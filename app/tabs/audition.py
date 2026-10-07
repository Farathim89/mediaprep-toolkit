"""The Audition window (Templates -> Auto-detect): plays the detected
clusters' members one after another."""
import os
import tkinter as tk
from tkinter import ttk

from ..engine.formatting import fmt_time
from ..ui.dialogs import place_dialog
from ..ui.player import VideoPlayer
from ..ui.widgets import add_tooltip
from .common import _same_file
from ..i18n import tr, N_

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr


class _AuditionWindow(tk.Toplevel):
    """Small player window that plays a detected cluster's range in a few of
    its episodes one after another (Prev / Replay / Next / Stop). Closing it
    unloads the player so no episode stays locked."""

    def __init__(self, master, title, clips, log_fn=None):
        super().__init__(master)
        self.transient(master.winfo_toplevel())
        self.resizable(False, False)
        self._poll = None
        self._was_playing = False
        self._auto = True                 # advance to the next clip at the end
        frm = ttk.Frame(self, padding=8)
        frm.pack(fill="both", expand=True)
        self.info = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.info, wraplength=480, justify="left").pack(
            anchor="w", pady=(0, 6))
        self.player = VideoPlayer(frm, width=480, height=270, log_fn=log_fn)
        self.player.pack()
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(8, 0))
        for txt, cmd, tip in [
                (N_("◀ Prev"), lambda: self._go(self.idx - 1), N_("Previous episode")),
                (N_("Replay"), lambda: self._go(self.idx), N_("Play this episode's range again")),
                (N_("Next ▶"), lambda: self._go(self.idx + 1), N_("Next episode")),
                (N_("Stop"), self._halt, N_("Pause and stay on this episode")),
                (N_("Close"), self.close, N_("Close and release the video file"))]:
            b = ttk.Button(row, text=tr_key(txt), command=cmd)
            b.pack(side="left", padx=(0, 6))
            add_tooltip(b, tr_key(tip))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.start(title, clips)
        # themed, centred on the main window and focused (not wherever
        # Windows cascades a new window)
        place_dialog(self, master)

    def start(self, title, clips):
        self.title(title)
        self.clips = list(clips)
        self._go(0)

    def _go(self, idx):
        if not self.clips:
            return
        self.idx = max(0, min(idx, len(self.clips) - 1))
        path, a, b = self.clips[self.idx]
        self.info.set(tr("Episode {i} / {n}:  {name}", i=self.idx + 1, n=len(self.clips),
                         name=os.path.basename(path)) + "\n"
                      + f"{fmt_time(a)} -> {fmt_time(b)}  ({b - a:.1f}s)")
        self._auto = True
        if getattr(self.player, "_path", None) and _same_file(self.player._path, path):
            ok = self.player.has_video()
        else:
            ok = self.player.load(path)
        if not ok:
            self.info.set(self.info.get() + "   - " + tr("could not open, skipped"))
            if self.idx + 1 < len(self.clips):
                self._poll = self.after(1200, lambda: self._go(self.idx + 1))
            return
        self.player.set_markers([(a, b, "preintro")])
        self.player.play_range(a, b)
        self._was_playing = True
        self._schedule()

    def _halt(self):
        self._auto = False
        self.player.pause()

    def _schedule(self):
        if self._poll is not None:
            try:
                self.after_cancel(self._poll)
            except Exception:
                pass
        self._poll = self.after(250, self._tick)

    def _tick(self):
        """Advance to the next episode once this one's range has played out."""
        self._poll = None
        try:
            playing = self.player.is_playing()
        except tk.TclError:
            return
        if self._was_playing and not playing and self._auto:
            _p, _a, b = self.clips[self.idx]
            cur = self.player.current_seconds() or 0.0
            if cur >= b - 0.5 and self.idx + 1 < len(self.clips):
                self._go(self.idx + 1)
                return
        self._was_playing = playing
        self._schedule()

    def close(self):
        if self._poll is not None:
            try:
                self.after_cancel(self._poll)
            except Exception:
                pass
            self._poll = None
        try:
            self.player.unload()
        except Exception:
            pass
        self.destroy()
