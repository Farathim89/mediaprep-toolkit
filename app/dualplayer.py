"""Dual Player - two video previews side by side for visual comparison
(e.g. original vs cleaned). Frame-accurate, with sound - each side has its own
mute button and volume slider (mute one side to A/B the audio).
The 'both' buttons drive the two players together for frame-by-frame checking."""
import os
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .player import VideoPlayer
from .widgets import add_tooltip, enable_file_drop, enable_file_drop_deep

_VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")]


class DualPlayerTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.pA = self._make_column(0, "A")
        self.pB = self._make_column(1, "B")

        ctr = ttk.Frame(self)
        ctr.grid(row=1, column=0, columnspan=2, pady=(8, 0))

        def tb(txt, cmd, tip, w=8):
            b = ttk.Button(ctr, text=txt, width=w, command=cmd)
            b.pack(side="left", padx=2)
            add_tooltip(b, tip)
            return b

        tb("|< both", lambda: self._both("to_start"), "Both to the first frame")
        tb("<< both", lambda: self._both_step(-10), "Both back 10 frames")
        tb("< both", lambda: self._both_step(-1), "Both back 1 frame")
        self.both_btn = ttk.Button(ctr, text="Play both", width=10, command=self._play_both)
        self.both_btn.pack(side="left", padx=4)
        add_tooltip(self.both_btn, "Play / pause both videos at once")
        tb("> both", lambda: self._both_step(1), "Both forward 1 frame")
        tb(">> both", lambda: self._both_step(10), "Both forward 10 frames")
        tb(">| both", lambda: self._both("to_end"), "Both to the last frame")
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
        ttk.Label(top, text=f"Video {tag}:").pack(side="left", padx=(0, 4))
        ent = ttk.Entry(top, textvariable=var)
        ent.pack(side="left", fill="x", expand=True)

        player = VideoPlayer(frame, width=440, height=248, resizable=True, log_fn=lambda m: None)
        player.grid(row=1, column=0, sticky="nsew")

        def drop(pth):
            pth = (pth or "").strip().strip('"')
            if pth and os.path.isfile(pth):
                var.set(pth)
                player.load(pth)

        def browse():
            p = filedialog.askopenfilename(title=f"Select video {tag}", filetypes=_VIDEO_TYPES)
            if p:
                var.set(p)
                player.load(p)

        def load():
            p = var.get().strip().strip('"')
            if p and os.path.isfile(p):
                player.load(p)
            else:
                messagebox.showerror("Error", "Type or browse to a valid video first.")

        ttk.Button(top, text="Browse...", command=browse).pack(side="left", padx=4)
        ttk.Button(top, text="Load", command=load).pack(side="left")
        enable_file_drop_deep(player, drop)                       # drop anywhere in the player
        enable_file_drop(ent, drop)
        return player

    # ---- combined controls ----
    def _both(self, method):
        for p in (self.pA, self.pB):
            getattr(p, method)()

    def _both_step(self, n):
        for p in (self.pA, self.pB):
            p.step(n)

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
        text = "Pause both" if playing else "Play both"
        if self.both_btn.cget("text") != text:
            self.both_btn.configure(text=text)
        self._label_after = self.after(300, self._sync_both_label)
