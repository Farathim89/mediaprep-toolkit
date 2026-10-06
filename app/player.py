"""VideoPlayer - a frame-accurate OpenCV preview widget shared by both tabs.

Includes a TimelineBar that shows the marked segments as coloured bands with a
draggable playhead, keyboard shortcuts, a jump-to-time box, and sound with a
mute button + volume slider (audio via app/audio.py; silent if sounddevice is
missing). Playback is wall-clock driven so picture and sound stay in step.
cv2 and Pillow are imported lazily so the app still runs (manual entry only)
without them."""
import os
import time
import tkinter as tk
from tkinter import ttk

from .audio import AudioPlayer
from .media import fmt_time, parse_time, probe_duration
from .widgets import add_tooltip


# colours used for the segment bands on the timeline (work on light + dark)
MARKER_COLORS = {
    "preintro": "#e08a2e",       # orange
    "intro": "#2f6fd6",          # blue
    "credits": "#3aa657",        # green
    "aftercredits": "#9a5cd0",   # purple
}


class TimelineBar(tk.Canvas):
    """A seek bar that also draws coloured marker bands and a playhead.
    Click or drag to seek (calls on_seek(fraction))."""

    def __init__(self, master, on_seek=None, height=34, **kw):
        super().__init__(master, height=height, highlightthickness=1,
                         highlightbackground="#888", **kw)
        self.on_seek = on_seek
        self.duration = 0.0
        self.pos = 0.0
        self.markers = []   # list of (start_sec, end_sec, color)
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._click)

    def set_duration(self, dur):
        self.duration = max(0.0, dur or 0.0)
        self._redraw()

    def set_position(self, sec):
        self.pos = max(0.0, min(sec, self.duration)) if self.duration else 0.0
        self._redraw()

    def set_markers(self, markers):
        self.markers = markers or []
        self._redraw()

    def _x(self, sec):
        w = max(1, self.winfo_width() - 1)
        return int((sec / self.duration) * w) if self.duration else 0

    def _click(self, e):
        if not self.duration or not self.on_seek:
            return
        w = max(1, self.winfo_width() - 1)
        self.on_seek(min(max(e.x / w, 0.0), 1.0))

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        mid = h // 2
        # base track line
        self.create_line(1, mid, w - 1, mid, fill="#9a9a9a", width=2)
        # coloured marker bands
        for s, e, color in self.markers:
            if self.duration and e > s:
                x0, x1 = self._x(s), self._x(e)
                self.create_rectangle(x0, 5, max(x1, x0 + 2), h - 5,
                                      fill=color, outline=color)
        # playhead
        px = self._x(self.pos)
        self.create_line(px, 0, px, h, fill="#ff3030", width=2)
        self.create_polygon(px - 4, 0, px + 4, 0, px, 6, fill="#ff3030", outline="#ff3030")


class VideoPlayer(ttk.Frame):
    # Files a running cut job will move when it finishes. Every player refuses
    # to open them until the job ends: an open player locks the file on
    # Windows, so the move to "done" would fail (WinError 32).
    locked_paths = set()

    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.abspath(path))

    @classmethod
    def lock_paths(cls, paths):
        cls.locked_paths = {cls._norm(p) for p in paths if p}

    @classmethod
    def unlock_all(cls):
        cls.locked_paths = set()

    def __init__(self, master, width=480, height=270, log_fn=None, resizable=False):
        super().__init__(master)
        self.VW, self.VH = width, height
        self._log = log_fn or (lambda m: None)
        self.cap = None
        self.fps = 25.0
        self.total_frames = 0
        self.cur_frame = 0
        self.playing = False
        self._photo = None
        self._play_after = None
        self._resizable = resizable
        self._cur_bgr = None
        self._cv2 = self._Image = self._ImageTk = None
        self._path = None
        self._play_t0 = 0.0          # wall-clock start of current playback
        self._play_start_frame = 0   # frame we started playing from
        self._stop_at_frame = None   # auto-pause here (section preview)
        self.audio = AudioPlayer(log_fn=self._log)

        self.canvas = tk.Canvas(self, width=self.VW, height=self.VH,
                                highlightthickness=1, highlightbackground="#888",
                                takefocus=1)
        if resizable:
            self.canvas.pack(fill="both", expand=True)
            self.canvas.bind("<Configure>", self._on_canvas_resize)
        else:
            self.canvas.pack()
        self._placeholder()

        self.timeline = TimelineBar(self, on_seek=self._on_seek)
        self.timeline.pack(fill="x", pady=(6, 2))

        ctr = ttk.Frame(self)
        ctr.pack()

        # The transport buttons must NOT keep keyboard focus: otherwise, after you
        # click one, Space (and Enter) would re-fire that button instead of
        # play/pause. takefocus=0 keeps them out of Tab order, and after a click we
        # hand focus back to the video so Space always means play/pause.
        def _defocus(b):
            b.configure(takefocus=0)
            b.bind("<ButtonRelease-1>", lambda e: self.canvas.focus_set(), add="+")
            return b

        def _tb(txt, cmd, tip, w=3):
            b = ttk.Button(ctr, text=txt, width=w, command=cmd)
            b.pack(side="left", padx=1)
            add_tooltip(b, tip)
            return _defocus(b)

        _tb("|<", lambda: self._seek_show(0), "Jump to the first frame  (Home)")
        _tb("<<", lambda: self._step(-10), "Step back 10 frames  (Shift+Left)")
        _tb("<", lambda: self._step(-1), "Step back 1 frame  (Left)")
        self.play_btn = ttk.Button(ctr, text="Play", width=6, command=self._toggle_play)
        self.play_btn.pack(side="left", padx=3)
        add_tooltip(self.play_btn, "Play / pause  (Space)")
        _defocus(self.play_btn)
        _tb(">", lambda: self._step(1), "Step forward 1 frame  (Right)")
        _tb(">>", lambda: self._step(10), "Step forward 10 frames  (Shift+Right)")
        _tb(">|", lambda: self._seek_show(self.total_frames - 1), "Jump to the last frame  (End)")

        # sound controls: mute button + volume slider (audio plays during Play)
        self.mute_btn = ttk.Button(ctr, text="\U0001f50a", width=3, command=self.toggle_mute)
        self.mute_btn.pack(side="left", padx=(10, 1))
        add_tooltip(self.mute_btn, "Mute / unmute the sound  (M)")
        _defocus(self.mute_btn)
        self.vol_var = tk.DoubleVar(value=self.audio.volume * 100)
        vol = ttk.Scale(ctr, from_=0, to=100, variable=self.vol_var,
                        length=80, command=self._on_volume)
        vol.pack(side="left", padx=(2, 1))
        add_tooltip(vol, "Volume (moving the slider also unmutes)")
        self.vol_lbl = ttk.Label(ctr, text=f"{int(self.audio.volume * 100)}%", width=4)
        self.vol_lbl.pack(side="left")

        info = ttk.Frame(self)
        info.pack(fill="x", pady=(4, 0))
        self.time_var = tk.StringVar(value="--:--:--.---  /  --:--:--.---   (frame 0)")
        ttk.Label(info, textvariable=self.time_var, style="Hint.TLabel").pack(side="left")
        ttk.Label(info, text="Go to:").pack(side="left", padx=(12, 3))
        self.jump_var = tk.StringVar()
        je = ttk.Entry(info, textvariable=self.jump_var, width=11)
        je.pack(side="left")
        je.bind("<Return>", lambda e: self._jump_to())
        add_tooltip(je, "Type a time (e.g. 21:30, 0:01:05.5 or 00:01:05:500) and press Enter or Go")
        gb = ttk.Button(info, text="Go", width=3, command=self._jump_to)
        gb.pack(side="left", padx=3)
        add_tooltip(gb, "Jump to the typed time")
        ub = ttk.Button(info, text="⏏ Unload", width=9, command=self.unload)
        ub.pack(side="right")
        add_tooltip(ub, "Close the video and free the file so it can be moved or deleted "
                        "(e.g. by the batch). Load a file again to reopen.")

        # keyboard control (focus the video by clicking it)
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self._key_actions = (
            ("<space>", self._toggle_play),
            ("<Left>", lambda: self._step(-1)),
            ("<Right>", lambda: self._step(1)),
            ("<Shift-Left>", lambda: self._step(-10)),
            ("<Shift-Right>", lambda: self._step(10)),
            ("<Home>", lambda: self._seek_show(0)),
            ("<End>", lambda: self._seek_show(self.total_frames - 1)),
            ("<m>", self.toggle_mute),
            ("<M>", self.toggle_mute),
        )
        for seq, fn in self._key_actions:
            self.canvas.bind(seq, lambda e, f=fn: (f(), "break")[1])

    # widgets where the arrow / space keys mean "edit text", not "drive video"
    _TEXT_FOCUS = (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox, tk.Text)

    def typing_focused(self):
        """True if a text-input widget currently has focus, so tab-wide
        shortcuts should stay out of the way and let it edit text."""
        try:
            return isinstance(self.focus_get(), self._TEXT_FOCUS)
        except Exception:
            return False

    def enable_tab_shortcuts(self):
        """Make the transport keys work anywhere on this player's tab - not only
        when the video canvas has focus - by binding them on the toplevel. They
        only fire while this player is the visible one and you're NOT typing in a
        text box, so they never fight the time boxes or the file-path entry."""
        top = self.winfo_toplevel()
        for seq, fn in self._key_actions:
            top.bind(seq, self._tab_key(fn), add="+")

    def _tab_key(self, fn):
        def handler(_e):
            if self.cap is None or not self.winfo_viewable() or self.typing_focused():
                return None
            fn()
            return "break"
        return handler

    # ---- public API ----
    def has_video(self):
        return self.cap is not None

    def current_seconds(self):
        if self.cap is None:
            return None
        return self.cur_frame / self.fps if self.fps else 0.0

    def set_markers(self, markers):
        """markers: list of (start_sec, end_sec, color)."""
        self.timeline.set_markers(markers)

    # ---- sound controls ----
    def toggle_mute(self):
        self.audio.muted = not self.audio.muted
        self.mute_btn.configure(text="\U0001f507" if self.audio.muted else "\U0001f50a")

    def _on_volume(self, _v=None):
        vol = max(0.0, min(self.vol_var.get(), 100.0))
        self.audio.volume = vol / 100.0
        self.vol_lbl.configure(text=f"{int(round(vol))}%")
        if self.audio.muted and vol > 0:      # moving the slider unmutes
            self.toggle_mute()

    # public transport controls (used by the Dual Player)
    def play(self):
        if self.cap is not None and not self.playing:
            self.playing = True
            self.play_btn.configure(text="Pause")
            self._start_clock_and_audio()
            self._play_loop()

    def prepare_play(self):
        """Dual Player: get the sound ready (decoder primed, stream not yet
        running) so two players can start together. True if it can play."""
        if self.cap is None or self.playing:
            return False
        self._play_start_frame = self.cur_frame
        self.audio.prepare(self._path, self.current_seconds() or 0.0)
        return True

    def start_prepared(self, t0):
        """Begin playback set up by prepare_play() (call audio.go() first);
        t0 is the shared wall-clock start, so both players stay in step."""
        self.playing = True
        self.play_btn.configure(text="Pause")
        self._play_t0 = t0
        self._play_loop()

    def pause(self):
        self._stop_play()

    def is_playing(self):
        return self.playing

    def step(self, n):
        self._step(n)

    def play_range(self, start_sec, end_sec):
        """Seek to start_sec, play, and auto-pause at end_sec (section preview).
        Any manual transport action (pause, seek, step, jump) cancels the stop
        point, so the player behaves normally again afterwards."""
        if self.cap is None or not self.fps:
            return
        end_frame = int(end_sec * self.fps)
        if self.total_frames > 0:
            end_frame = min(end_frame, self.total_frames - 1)
        start_frame = max(0, int(start_sec * self.fps))
        if end_frame <= start_frame:
            return
        self._stop_play()
        self._seek_show(start_frame)
        self._stop_at_frame = end_frame
        self.playing = True
        self.play_btn.configure(text="Pause")
        self._start_clock_and_audio()
        self._play_loop()

    def seek_seconds(self, sec):
        """Jump the playhead to an absolute time in seconds (clamped). Rounds to
        the nearest frame so Go lands on exactly the frame Set captured (a floor
        here would land one frame early because of float/fps rounding)."""
        if self.cap is None or not self.fps:
            return
        self._stop_play()
        self._seek_show(int(round(sec * self.fps)))   # _seek_show clamps

    def to_start(self):
        self._seek_show(0)

    def to_end(self):
        self._seek_show(self.total_frames - 1)

    def unload(self):
        """Release the open video file so the OS lock is freed and another part
        of the app (e.g. the batch's 'move to done') can move or delete it.
        Safe to call when nothing is loaded."""
        self._stop_play()
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self._path = None
        self._cur_bgr = None
        self.total_frames = 0
        self.cur_frame = 0
        if hasattr(self, "timeline"):
            self.timeline.set_duration(0.0)
        self._placeholder("Video unloaded\n(freed so the batch can move it)")

    def _clear_loaded(self, msg):
        """Forget the previous video entirely (frame, markers, time label) so a
        failed load doesn't leave the old clip on screen looking loaded."""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self._path = None
        self._cur_bgr = None
        self.total_frames = 0
        self.cur_frame = 0
        self.timeline.set_duration(0.0)
        self.timeline.set_markers([])
        self.time_var.set("--:--:--.---  /  --:--:--.---   (frame 0)")
        self._placeholder(msg)

    def load(self, path):
        if not self._ensure_libs():
            self._placeholder("Preview needs opencv-python + Pillow\n(run Install Requirements.bat)\n"
                              "- you can still type times manually")
            self._log("[player] opencv-python / Pillow missing - preview disabled,"
                      " manual entry still works.")
            return False
        if path and self._norm(path) in VideoPlayer.locked_paths:
            self._log(f"[player] {os.path.basename(path)} is being cut right now (it will be "
                      "moved when done) - preview it after the run finishes.")
            if self.cap is None:
                self._placeholder("This file is being cut right now\n"
                                  "- load it again when the run finishes")
            return False
        self._stop_play()
        self._clear_loaded("Loading...")
        cap = self._cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            self._placeholder("Could not open video")
            self._log(f"[player] could not open {os.path.basename(path)}")
            return False
        self.cap = cap
        self._path = path
        fps = cap.get(self._cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else 25.0
        frames = int(cap.get(self._cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frames <= 0:   # some containers don't report it: derive from duration
            dur = probe_duration(path)
            frames = int(dur * self.fps) if dur else 0
        self.total_frames = max(frames, 0)
        self.cur_frame = 0
        self.timeline.set_duration(self.total_frames / self.fps if self.fps else 0.0)
        self._seek_show(0)
        if self._cur_bgr is None:     # first frame unreadable
            self._clear_loaded("Could not read video")
            self._log(f"[player] could not read a frame from {os.path.basename(path)}")
            return False
        self.canvas.focus_set()
        return True

    # ---- internals ----
    def _ensure_libs(self):
        if self._cv2 is not None:
            return True
        try:
            import cv2
            from PIL import Image, ImageTk
            self._cv2, self._Image, self._ImageTk = cv2, Image, ImageTk
            return True
        except ImportError:
            return False

    def _canvas_size(self):
        if self._resizable:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            return (w if w > 1 else self.VW, h if h > 1 else self.VH)
        return self.VW, self.VH

    def _placeholder(self, msg="No video loaded"):
        self.canvas.delete("all")
        cw, ch = self._canvas_size()
        self.canvas.create_text(cw // 2, ch // 2, text=msg,
                                fill="#999", justify="center", font=("Segoe UI", 11))

    def _on_canvas_resize(self, _e=None):
        if self._cur_bgr is not None:
            self._show(self._cur_bgr)
        else:
            self._placeholder()

    def _seek_show(self, idx):
        if self.cap is None:
            return
        idx = max(0, int(idx))
        if self.total_frames > 0:
            idx = min(idx, self.total_frames - 1)
        self.cap.set(self._cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self.cap.read()
        if not ok:
            return
        self.cur_frame = idx
        self._show(frame)
        self._update_time()

    def _on_seek(self, frac):
        if self.cap is None:
            return
        self._stop_play()
        self._seek_show(int(frac * max(self.total_frames - 1, 0)))

    def _jump_to(self):
        if self.cap is None:
            return
        secs = parse_time(self.jump_var.get())
        if secs is None:
            return
        self.seek_seconds(secs)

    def _show(self, frame):
        cv2 = self._cv2
        self._cur_bgr = frame
        tw, th = self._canvas_size()
        h, w = frame.shape[:2]
        scale = min(tw / w, th / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        disp = cv2.resize(frame, (nw, nh))
        disp = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
        self._photo = self._ImageTk.PhotoImage(self._Image.fromarray(disp))
        self.canvas.delete("all")
        self.canvas.create_image(tw // 2, th // 2, image=self._photo)

    def _update_time(self):
        cur = self.cur_frame / self.fps if self.fps else 0
        total = self.total_frames / self.fps if self.fps else 0
        self.time_var.set(f"{fmt_time(cur)}  /  {fmt_time(total)}   (frame {self.cur_frame})")
        self.timeline.set_position(cur)

    def _step(self, delta):
        if self.cap is None:
            return
        self._stop_play()
        self._seek_show(self.cur_frame + delta)

    def _toggle_play(self):
        if self.cap is None:
            return
        if self.playing:
            self._stop_play()
        else:
            self.playing = True
            self.play_btn.configure(text="Pause")
            self._start_clock_and_audio()
            self._play_loop()

    def _start_clock_and_audio(self):
        """Anchor the playback clock at the current frame and start the sound.
        The clock starts only once audio.start() has returned (ffmpeg spin-up +
        first decoded block), otherwise the picture runs ahead of the sound."""
        self._play_start_frame = self.cur_frame
        self.audio.start(self._path, self.current_seconds() or 0.0)
        self._play_t0 = time.monotonic()

    def _stop_play(self):
        self.playing = False
        self._stop_at_frame = None
        self.play_btn.configure(text="Play")
        self.audio.stop()
        if self._play_after is not None:
            try:
                self.after_cancel(self._play_after)
            except Exception:
                pass
            self._play_after = None

    def _play_loop(self):
        """Wall-clock driven playback: show the frame that is due *now*, so the
        picture keeps pace with real time (and therefore with the sound) even
        when decoding is slow - late frames are skipped instead of stretching
        the video out."""
        if not self.playing or self.cap is None:
            return
        elapsed = time.monotonic() - self._play_t0
        target = self._play_start_frame + int(elapsed * self.fps)
        if target > self.cur_frame:
            skip = target - self.cur_frame
            if skip > 12:   # far behind (slow seek/decode): jump instead of reading
                self.cap.set(self._cv2.CAP_PROP_POS_FRAMES, target)
                self.cur_frame = target - 1
                skip = 1
            ok, frame = False, None
            for _ in range(skip):
                ok, frame = self.cap.read()
                if not ok:
                    break
                self.cur_frame += 1
            if not ok:
                self._stop_play()
                return
            self._show(frame)
            self._update_time()
            if self._stop_at_frame is not None and self.cur_frame >= self._stop_at_frame:
                self._stop_play()
                return
            if self.total_frames > 0 and self.cur_frame >= self.total_frames - 1:
                self._stop_play()
                return
        # sleep until the next frame is due (capped so pauses stay responsive)
        next_due = (self.cur_frame + 1 - self._play_start_frame) / self.fps
        delay = int((next_due - (time.monotonic() - self._play_t0)) * 1000)
        self._play_after = self.after(min(max(delay, 1), 40), self._play_loop)
