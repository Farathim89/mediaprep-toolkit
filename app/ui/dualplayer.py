"""Dual Player - two video previews side by side for visual comparison
(e.g. original vs cleaned). Frame-accurate, with sound - each side has its own
mute button and volume slider (mute one side to A/B the audio).
The 'both' buttons drive the two players together for frame-by-frame checking;
with Link on, a seek / step / Go in one player moves the other to the same
TIME (not frame number), so sources with different frame rates stay aligned."""
import os
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .player import VideoPlayer
from .widgets import add_tooltip, enable_file_drop, enable_file_drop_deep, help_button
from .. import applog
from ..i18n import tr

_VIDEO_TYPES = [(tr("Video files"), "*.mp4 *.mkv *.mov *.avi *.webm"), (tr("All files"), "*.*")]


class DualPlayerTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._mirroring = False          # guard: a mirrored seek must not echo back
        self.link_var = tk.BooleanVar(value=True)
        self.pA = self._make_column(0, "A")
        self.pB = self._make_column(1, "B")
        self.pA.on_user_seek = lambda sec, playing: self._mirror(self.pB, sec, playing)
        self.pB.on_user_seek = lambda sec, playing: self._mirror(self.pA, sec, playing)

        ctr = ttk.Frame(self)
        ctr.grid(row=1, column=0, columnspan=2, pady=(8, 0))

        def tb(sym, cmd, tip):
            b = ttk.Button(ctr, text=f"{sym} {both}", command=cmd)
            b.pack(side="left", padx=2)
            add_tooltip(b, tip)
            return b

        both = tr("both")
        tb("|<", lambda: self._both("to_start"), tr("Both to the first frame"))
        tb("<<", lambda: self._both_step(-10), tr("Both back 10 frames"))
        tb("<", lambda: self._both_step(-1), tr("Both back 1 frame"))
        # wide enough for both labels, so Play <-> Pause doesn't shift the row
        bw = max(10, len(tr("Play both")) + 1, len(tr("Pause both")) + 1)
        self.both_btn = ttk.Button(ctr, text=tr("Play both"), width=bw, command=self._play_both)
        self.both_btn.pack(side="left", padx=4)
        add_tooltip(self.both_btn, tr("Play / pause both videos at once"))
        tb(">", lambda: self._both_step(1), tr("Both forward 1 frame"))
        tb(">>", lambda: self._both_step(10), tr("Both forward 10 frames"))
        tb(">|", lambda: self._both("to_end"), tr("Both to the last frame"))
        lk = ttk.Checkbutton(ctr, text=tr("Link"), variable=self.link_var)
        lk.pack(side="left", padx=(12, 0))
        add_tooltip(lk, tr("Linked: a seek, step or Go in one player moves the other one to "
                           "the same TIME (seconds, not frame number), so videos with "
                           "different frame rates stay aligned. Untick to move them "
                           "separately."))
        self._label_after = None
        self._sync_both_label()

    def _make_column(self, col, tag):
        frame = ttk.Frame(self)
        frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="we", pady=(0, 4))
        var = tk.StringVar()
        ttk.Label(top, text=tr("Video {tag}:", tag=tag)).pack(side="left", padx=(0, 4))
        ent = ttk.Entry(top, textvariable=var)
        ent.pack(side="left", fill="x", expand=True)

        player = VideoPlayer(frame, width=440, height=248, resizable=True,
                             log_fn=lambda m: applog.record(f"[Dual Player {tag}] {m}"))
        player.grid(row=1, column=0, sticky="nsew")

        def drop(pth):
            pth = (pth or "").strip().strip('"')
            if pth and os.path.isfile(pth):
                var.set(pth)
                player.load(pth)

        def browse():
            p = filedialog.askopenfilename(title=tr("Select video {tag}", tag=tag),
                                           filetypes=_VIDEO_TYPES)
            if p:
                var.set(p)
                player.load(p)

        def load():
            p = var.get().strip().strip('"')
            if p and os.path.isfile(p):
                player.load(p)
            else:
                messagebox.showerror(tr("Error"), tr("Type or browse to a valid video first."))

        ttk.Button(top, text=tr("Browse..."), command=browse).pack(side="left", padx=4)
        ttk.Button(top, text=tr("Load"), command=load).pack(side="left")
        if tag == "B":                       # '?' at the tab's top-right corner
            help_button(top, "dual").pack(side="left", padx=(6, 0))
        enable_file_drop_deep(player, drop)                       # drop anywhere in the player
        enable_file_drop(ent, drop)
        return player

    # ---- combined controls ----
    def _mirror(self, other, sec, playing):
        """Link: move the other player to the same time (and play state)."""
        if self._mirroring or not self.link_var.get() or not other.has_video():
            return
        self._mirroring = True
        try:
            other.seek_seconds(sec, play=playing)
        finally:
            self._mirroring = False

    def _both(self, method):
        self._mirroring = True        # both move on their own - no mirroring
        try:
            for p in (self.pA, self.pB):
                getattr(p, method)()
        finally:
            self._mirroring = False

    def _both_step(self, n):
        """Step both. With different frame rates a frame is a different length
        in A and B, so step A by frames and put B at A's new TIME instead."""
        a, b = self.pA, self.pB
        fa = a.fps if a.has_video() else None
        fb = b.fps if b.has_video() else None
        self._mirroring = True
        try:
            if fa and fb and abs(fa - fb) > 0.01:
                a.step(n, notify=False)
                b.seek_seconds(a.current_seconds() or 0.0, play=False)
            else:
                for p in (a, b):
                    p.step(n, notify=False)
        finally:
            self._mirroring = False

    def _play_both(self):
        if self.pA.is_playing() or self.pB.is_playing():
            self.pA.pause()
            self.pB.pause()
        else:
            # prime both soundtracks first, then start both sounds and both
            # picture clocks together so A and B begin in step
            ready = [p for p in (self.pA, self.pB) if p.prepare_play()]
            for p in ready:
                p.audio.go()
            t0 = time.monotonic()
            for p in ready:
                p.start_prepared(t0)
        self._sync_both_label()

    def _sync_both_label(self):
        """Keep the button label true to the players (playback can end by
        itself, or be paused from a player's own controls)."""
        if self._label_after is not None:
            try:
                self.after_cancel(self._label_after)
            except Exception:
                pass
        playing = self.pA.is_playing() or self.pB.is_playing()
        text = tr("Pause both") if playing else tr("Play both")
        if self.both_btn.cget("text") != text:
            self.both_btn.configure(text=text)
        self._label_after = self.after(300, self._sync_both_label)
