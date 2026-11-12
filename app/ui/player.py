"""VideoPlayer - a frame-accurate preview widget shared by both tabs.

Two engines behind one widget (Settings -> Video player engine):
  mpv     - mpv.exe draws into the video box and decodes on the GPU; it also
            plays the sound (ui/mpvplayer.py). Smooth playback, fast steps.
  OpenCV  - frames decoded by cv2 and painted as Tk images; sound via
            ui/playback.py (silent if sounddevice is missing). Wall-clock
            driven so picture and sound stay in step.
"Auto" uses mpv when it is found and starts, else OpenCV. Both number frames
the same way (frame k = time k / fps), so Set / Go / the time boxes behave
identically. Includes a TimelineBar that shows the marked segments as coloured
bands with a draggable playhead, keyboard shortcuts, a jump-to-time box, and a
mute button + volume slider. cv2 and Pillow are imported lazily so the app
still runs (manual entry only) without them.

The engines are mixins of VideoPlayer: ui/player_mpv.py (and the engine
choice), ui/player_cv.py; the seek bar is ui/timeline.py."""
import os
import tkinter as tk
from tkinter import ttk

from .playback import AudioPlayer
from ..engine.formatting import fmt_time, parse_time
from .tooltips import add_tooltip
from .widgets import bind_window, fixed_label
from ..i18n import tr
from . import icons, themes
from .player_cv import CvEngine
from .player_mpv import MpvEngine, pick_engine
from .timeline import TimelineBar

# The video area is always a standard 16:9 box: 16k x 9k pixels at 100 %
# scaling (k=30 -> 480x270, 40 -> 640x360, 48 -> 768x432, 60 -> 960x540,
# 80 -> 1280x720), k scaled with the display DPI so it stays exactly 16:9.
# The frame is letter- / pillarboxed inside it (never stretched).
VIDEO_K = (20, 24, 25, 28, 30, 32, 36, 40, 44, 48, 52, 56, 60, 64, 72, 80, 90, 100, 120)


def video_size(k):
    """A 16:9 size for ladder step k (100 % units) at the current DPI."""
    k = max(1, int(round(k * themes.scale())))
    return 16 * k, 9 * k


def fit_16x9(avail_w, avail_h, min_k=None, max_k=None):
    """The largest ladder size that fits avail_w x avail_h (DPI-scaled),
    clamped to [min_k, max_k] (100 % ladder steps). Returns (w, h)."""
    best = None
    for k in VIDEO_K:
        if (min_k is not None and k < min_k) or (max_k is not None and k > max_k):
            continue
        w, h = video_size(k)
        if best is None or (w <= avail_w and h <= avail_h):
            best = (w, h)
    return best


class VideoPlayer(MpvEngine, CvEngine, ttk.Frame):
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
        # width/height are 100 % sizes: snapped to the 16:9 ladder, DPI-scaled
        base_k = max(k for k in VIDEO_K if k <= max(VIDEO_K[0], min(width / 16, height / 9)))
        self._base_k = base_k
        self.VW, self.VH = video_size(base_k)
        self._af = None                # auto_fit() settings
        self._af_job = None
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
        self._resume_after_drag = False   # timeline dragged while playing
        # called as on_user_seek(seconds, playing) after a seek/step/Go made in
        # THIS player's own controls (the Dual Player uses it to link A and B)
        self.on_user_seek = None
        self.audio = AudioPlayer(log_fn=self._log)
        self.audio_track = None      # audio stream index (0:a:N); None = default
        # mpv engine state (see _mpv_* below)
        self._engine = None          # "mpv" / "opencv" while a video is loaded
        self._mpv = None             # mpvplayer.MpvProcess (started on first load)
        self._mpv_job = None         # Tk after() id of the event pump
        self._mpv_seek_rid = 0       # id of the last seek: older position events are stale
        self._mpv_settled = True     # the last seek has landed (playback-restart seen)
        self._mpv_play_rid = 0       # id of the last unpause: older pause/eof events are stale
        self._mpv_pending_end = None  # play_range: stop frame to arm once the seek lands
        self._mpv_end_armed = False  # mpv 'end' is set (auto-pause point)
        self._mpv_end_frame = None   # the frame that 'end' stops on
        self._mpv_hold = False       # stopped at 'end': keep cur_frame, ignore time-pos
        self.mpv_time_pos = None     # mpv's exact time-pos of the shown frame (diagnostics)
        self._mpv_ld = None          # a file mpv is still opening (see _mpv_load)

        self.canvas = tk.Canvas(self, width=self.VW, height=self.VH,
                                highlightthickness=1, borderwidth=0,
                                takefocus=1)
        # a clean dark video surface; the focus ring (accent) shows when the
        # video has the keyboard (Space / arrows drive it)
        themes.on_palette(self.canvas, lambda p: (
            self.canvas.configure(bg=p["video_bg"],
                                  highlightbackground=p["border"] if p.get("hc")
                                  else p["stroke"],
                                  highlightcolor=p["accent"]),
            self.canvas.itemconfigure("placeholder", fill="#a0a0a0")))
        self._resize_job = None
        self._canvas_size_seen = None
        if resizable:
            # a resizable player (Dual Player) keeps a 16:9 canvas centred in
            # the room it gets; the room around it is plain background
            self._holder = ttk.Frame(self, width=self.VW, height=self.VH)
            self._holder.pack(fill="both", expand=True)
            self.canvas.place(in_=self._holder, relx=0.5, rely=0.5, anchor="center",
                              width=self.VW, height=self.VH)
            self.tk.call("raise", self.canvas._w, self._holder._w)   # above its holder
            self._holder.bind("<Configure>", self._on_holder_configure)
            self.canvas.bind("<Configure>", self._on_canvas_configure)
        else:
            self.canvas.pack()
        # mpv draws into this frame (placed over the canvas, inside its focus
        # ring, only while mpv has a video loaded). mpv's own window in it is
        # disabled, so clicks and file drops land on this frame.
        self._mpv_host = tk.Frame(self.canvas, highlightthickness=0, borderwidth=0,
                                  takefocus=0)
        self._mpv_host.bind("<Button-1>", lambda e: self.canvas.focus_set())
        themes.on_palette(self._mpv_host, self._mpv_palette)
        self.bind("<Destroy>", lambda e: e.widget is self and self._mpv_close(), add="+")
        self._placeholder()
        from . import dpi
        dpi.on_change(self, self._dpi_changed)

        self.timeline = TimelineBar(self, on_seek=self._on_seek,
                                    on_release=self._on_seek_release)
        self.timeline.pack(fill="x", pady=themes.pad(6, 2))

        # transport: icon buttons grouped in a pill bar
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ctr = ttk.Frame(bar, style="Pill.TFrame", padding=themes.pad(4, 3))
        ctr.pack(side="left")
        self._icons = icons.available()

        # The transport buttons must NOT keep keyboard focus: otherwise, after you
        # click one, Space (and Enter) would re-fire that button instead of
        # play/pause. takefocus=0 keeps them out of Tab order, and after a click we
        # hand focus back to the video so Space always means play/pause.
        def _defocus(b):
            b.configure(takefocus=0)
            b.bind("<ButtonRelease-1>", lambda e: self.canvas.focus_set(), add="+")
            return b

        def _tb(txt, cmd, tip, icon, w=3):
            if self._icons:
                b = ttk.Button(ctr, text="", style="Pill.Subtle.TButton", command=cmd)
                icons.decorate(b, icon)
            else:
                b = ttk.Button(ctr, text=txt, width=w, command=cmd)
            b.pack(side="left", padx=themes.px(1))
            add_tooltip(b, tip)
            return _defocus(b)

        _tb("|<", self.to_start, tr("Jump to the first frame  (Home)"), "to_start")
        _tb("<<", lambda: self._step(-10), tr("Step back 10 frames  (Shift+Left)"), "back10")
        _tb("<", lambda: self._step(-1), tr("Step back 1 frame  (Left)"), "step_back")
        if self._icons:
            self.play_btn = ttk.Button(ctr, text="", style="Pill.Subtle.TButton",
                                       command=self._toggle_play)
            icons.decorate(self.play_btn, "play", size=20)
        else:
            # wide enough for both words, so Play <-> Pause doesn't shift the row
            pw = max(6, len(tr("Play")) + 1, len(tr("Pause")) + 1)
            self.play_btn = ttk.Button(ctr, text=tr("Play"), width=pw,
                                       command=self._toggle_play)
        self.play_btn.pack(side="left", padx=themes.px(3))
        add_tooltip(self.play_btn, tr("Play / pause  (Space)"))
        _defocus(self.play_btn)
        _tb(">", lambda: self._step(1), tr("Step forward 1 frame  (Right)"), "step_fwd")
        _tb(">>", lambda: self._step(10), tr("Step forward 10 frames  (Shift+Right)"), "fwd10")
        _tb(">|", self.to_end, tr("Jump to the last frame  (End)"), "to_end")

        # sound controls: mute button + volume slider (audio plays during Play)
        ttk.Separator(ctr, orient="vertical").pack(side="left", fill="y",
                                                   padx=themes.px(6), pady=themes.px(4))
        if self._icons:
            self.mute_btn = ttk.Button(ctr, text="", style="Pill.Subtle.TButton",
                                       command=self.toggle_mute)
            icons.decorate(self.mute_btn, "volume")
        else:
            self.mute_btn = ttk.Button(ctr, text="\U0001f50a", width=3,
                                       command=self.toggle_mute)
        self.mute_btn.pack(side="left", padx=themes.px(1))
        add_tooltip(self.mute_btn, tr("Mute / unmute the sound  (M)"))
        _defocus(self.mute_btn)
        self.vol_var = tk.DoubleVar(value=self.audio.volume * 100)
        vol = ttk.Scale(ctr, from_=0, to=100, variable=self.vol_var, style="Pill.Horizontal.TScale",
                        length=themes.px(84), command=self._on_volume)
        vol.pack(side="left", padx=themes.pad(2, 1))
        add_tooltip(vol, tr("Volume (moving the slider also unmutes)"))
        self.vol_lbl = ttk.Label(ctr, text=f"{int(self.audio.volume * 100)}%", width=5,
                                 style="Pill.Hint.TLabel")
        self.vol_lbl.pack(side="left", padx=themes.pad(2, 4))

        # time + Go to + Unload. The time sits in a FIXED-size box (the widest
        # possible text in the current font): its digits change every frame
        # while playing, and a label that resized with them made the whole
        # page jitter (Segoe UI Variable's "1" is narrower than the others,
        # the frame number grows a digit now and then).
        info = ttk.Frame(self)
        info.pack(fill="x", pady=themes.pad(6, 0))
        self.time_var = tk.StringVar(value=self._time_text(None, None, 0))
        def time_samples():
            # frame numbers up to 6 digits (4.6 h at 60 fps), more for a
            # longer file - re-measured when one is loaded
            frame = int("8" * max(6, len(str(self.total_frames))))
            return [self._time_text(None, None, frame),
                    tr("{cur}  /  {total}   (frame {frame})", cur="88:88:88:888",
                       total="88:88:88:888", frame=frame)]
        tbox, self.time_lbl = fixed_label(info, time_samples, style="Hint.TLabel",
                                          textvariable=self.time_var)
        self._time_box = tbox
        tbox.pack(side="left", padx=themes.pad(2, 12))
        ttk.Label(info, text=tr("Go to:")).pack(side="left", padx=themes.pad(0, 4))
        self.jump_var = tk.StringVar()
        je = ttk.Entry(info, textvariable=self.jump_var, width=11)
        je.pack(side="left")
        je.bind("<Return>", lambda e: self._jump_to())
        add_tooltip(je, tr("Type a time (e.g. 21:30, 0:01:05.5 or 00:01:05:500) and press "
                           "Enter or Go"))
        gb = icons.decorate(ttk.Button(info, text=tr("Go"), command=self._jump_to), "go")
        gb.pack(side="left", padx=themes.px(4))
        add_tooltip(gb, tr("Jump to the typed time"))
        if self._icons:
            # icon + label like Go - a bare eject glyph was easy to miss
            ub = icons.decorate(ttk.Button(bar, text=tr("Unload"), command=self.unload),
                                "eject")
        else:
            ub = ttk.Button(bar, text="⏏ " + tr("Unload"), command=self.unload)
        # at the right end of the transport row (the time row below is wide
        # enough with the fixed-size time box)
        ub.pack(side="right")
        add_tooltip(ub, tr("Unload") + " - " + tr(
            "Close the video and free the file so it can be moved or deleted "
            "(e.g. by the batch). Load a file again to reopen."))

        # keyboard control (focus the video by clicking it)
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self._key_actions = (
            ("<space>", self._toggle_play),
            ("<Left>", lambda: self._step(-1)),
            ("<Right>", lambda: self._step(1)),
            ("<Shift-Left>", lambda: self._step(-10)),
            ("<Shift-Right>", lambda: self._step(10)),
            ("<Home>", self.to_start),
            ("<End>", self.to_end),
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
        """True when a video is loaded - or still being opened by mpv: seeks
        and Play asked for meanwhile are applied once it is open."""
        return self.cap is not None or self._mpv_ld is not None

    def loading(self):
        """True while mpv is still opening the file (fps / length unknown)."""
        return self._mpv_ld is not None

    def current_seconds(self):
        if self._mpv_ld is not None:
            pend = self._mpv_ld["pending"]
            return pend["range"][0] if "range" in pend else pend.get("seek", 0.0)
        if self.cap is None:
            return None
        return self.cur_frame / self.fps if self.fps else 0.0

    def set_markers(self, markers):
        """markers: list of (start_sec, end_sec, color)."""
        self.timeline.set_markers(markers)

    # ---- sound controls ----
    def toggle_mute(self):
        self.audio.muted = not self.audio.muted
        if self._mpv is not None:
            self._mpv.set("mute", bool(self.audio.muted))
        if self._icons:
            icons.set_icon(self.mute_btn, "mute" if self.audio.muted else "volume")
        else:
            self.mute_btn.configure(text="\U0001f507" if self.audio.muted else "\U0001f50a")

    def _show_playing(self, playing):
        """Play button shows Pause while playing (icon, or the word)."""
        if self._icons:
            icons.set_icon(self.play_btn, "pause" if playing else "play", size=20)
        else:
            self.play_btn.configure(text=tr("Pause") if playing else tr("Play"))

    def _on_volume(self, _v=None):
        vol = max(0.0, min(self.vol_var.get(), 100.0))
        self.audio.volume = vol / 100.0
        self.vol_lbl.configure(text=f"{int(round(vol))}%")
        if self._mpv is not None:
            self._mpv.set("volume", self._mpv_volume())
        if self.audio.muted and vol > 0:      # moving the slider unmutes
            self.toggle_mute()

    def set_audio_track(self, index):
        """Play audio stream 0:a:<index> (None = the file's default). Applies
        to the running playback with mpv, from the next Play with OpenCV."""
        self.audio_track = None if index is None else max(0, int(index))
        self.audio.track = self.audio_track
        if self._mpv is not None:
            self._mpv.set("aid", "auto" if self.audio_track is None else self.audio_track + 1)

    # public transport controls (used by the Dual Player)
    def play(self):
        if self._mpv_ld is not None:
            self._mpv_ld["pending"]["play"] = True
            return
        if self.cap is not None and not self.playing:
            if self._engine == "mpv":
                self._mpv_play()
                return
            self.playing = True
            self._show_playing(True)
            self._start_clock_and_audio()
            self._play_loop()

    def prepare_play(self):
        """Dual Player: get the sound ready (decoder primed, stream not yet
        running) so two players can start together. True if it can play."""
        if self._mpv_ld is not None:        # still opening: plays once it is open
            self._mpv_ld["pending"]["play"] = True
            return False
        if self.cap is None or self.playing:
            return False
        if self._engine == "mpv":      # mpv starts picture + sound itself
            return not self._mpv_at_end()
        self._play_start_frame = self.cur_frame
        self.audio.prepare(self._path, self.current_seconds() or 0.0)
        return True

    def start_prepared(self, t0):
        """Begin playback set up by prepare_play() (call audio.go() first);
        t0 is the shared wall-clock start, so both players stay in step."""
        if self._engine == "mpv":
            self._mpv_play()
            return
        self.playing = True
        self._show_playing(True)
        self._play_t0 = t0
        self._play_loop()

    def pause(self):
        self._resume_after_drag = False
        self._stop_play()

    def is_playing(self):
        return self.playing

    def step(self, n, notify=True):
        if self._mpv_ld is None:
            self._step(n, notify=notify)

    def play_range(self, start_sec, end_sec):
        """Seek to start_sec, play, and auto-pause on the last frame BEFORE
        end_sec (section preview; end_sec is the exclusive end = the first
        frame kept, so the preview plays through the last removed frame).
        Any manual transport action (pause, seek, step, jump) cancels the stop
        point, so the player behaves normally again afterwards."""
        if self._mpv_ld is not None:        # still opening: done once it is open
            pend = self._mpv_ld["pending"]
            pend.clear()
            pend["range"] = (start_sec, end_sec)
            return
        if self.cap is None or not self.fps:
            return
        end_frame = int(round(end_sec * self.fps)) - 1
        if self.total_frames > 0:
            end_frame = min(end_frame, self.total_frames - 1)
        start_frame = max(0, int(round(start_sec * self.fps)))
        if end_frame <= start_frame:
            return
        self._stop_play()
        if self._engine == "mpv":
            # seek, then (once it has landed) arm mpv's end point + play
            self._goto(start_frame, play=False, notify=False)
            self._mpv_pending_end = end_frame
            self._stop_at_frame = end_frame
            self.playing = True
            self._show_playing(True)
            self._mpv_kick()
            return
        self._seek_show(start_frame)
        self._stop_at_frame = end_frame
        self.playing = True
        self._show_playing(True)
        self._start_clock_and_audio()
        self._play_loop()

    def seek_seconds(self, sec, play=None):
        """Jump the playhead to an absolute time in seconds (clamped). Rounds to
        the nearest frame so Go lands on exactly the frame Set captured (a floor
        here would land one frame early because of float/fps rounding).
        play=None keeps playing if it was (restarted from the new position);
        True / False forces playback on / off afterwards."""
        if self._mpv_ld is not None:        # still opening: done once it is open
            pend = self._mpv_ld["pending"]
            pend.pop("range", None)
            pend["seek"] = max(0.0, float(sec or 0.0))
            if play is not None:
                pend["play"] = bool(play)
            return
        if self.cap is None or not self.fps:
            return
        self._goto(int(round(sec * self.fps)), play=play, notify=False)

    def to_start(self):
        if self._mpv_ld is not None:
            self.seek_seconds(0.0)
            return
        self._goto(0)

    def to_end(self):
        self._goto(self.total_frames - 1)

    def _goto(self, idx, play=None, notify=True):
        """Seek to frame idx. A seek during playback restarts playback (picture
        clock + sound) from the new position - _seek_show alone would be undone
        by the running play loop, which still counts from the old start."""
        if self.cap is None:
            return
        if self._engine == "mpv":
            self._mpv_goto(idx, play)
            if notify:
                self._notify_seek()
            return
        was = self.playing
        self._stop_play()
        self._seek_show(idx)    # clamps
        if was if play is None else play:
            self._start_playing()
        if notify:
            self._notify_seek()

    def _start_playing(self):
        if self.cap is None or self.playing:
            return
        if self._engine == "mpv":
            self._mpv_play()
            return
        self.playing = True
        self._show_playing(True)
        self._start_clock_and_audio()
        self._play_loop()

    def _notify_seek(self):
        if callable(self.on_user_seek) and self.cap is not None:
            try:
                self.on_user_seek(self.current_seconds() or 0.0, self.playing)
            except Exception:
                pass

    def unload(self):
        """Release the open video file so the OS lock is freed and another part
        of the app (e.g. the batch's 'move to done') can move or delete it.
        Safe to call when nothing is loaded."""
        self._stop_play()
        self._release_cap()
        self._mpv_close()        # mpv quits -> the file handle is released
        self._path = None
        self._cur_bgr = None
        self.total_frames = 0
        self.cur_frame = 0
        if hasattr(self, "timeline"):
            self.timeline.set_duration(0.0)
        self._placeholder(tr("Video unloaded\n(freed so the batch can move it)"))

    def _release_cap(self):
        """Close the open video (OpenCV capture / mpv's current file)."""
        if self.cap is not None:
            if self._engine == "mpv":
                if self._mpv is not None:
                    self._mpv.command("stop")       # mpv lets go of the file
            else:
                try:
                    self.cap.release()
                except Exception:
                    pass
            self.cap = None
        self._engine = None

    def _clear_loaded(self, msg):
        """Forget the previous video entirely (frame, markers, time label) so a
        failed load doesn't leave the old clip on screen looking loaded."""
        self._release_cap()
        self._path = None
        self._cur_bgr = None
        self.total_frames = 0
        self.cur_frame = 0
        self.timeline.set_duration(0.0)
        self.timeline.set_markers([])
        self.time_var.set(self._time_text(None, None, 0))
        self._placeholder(msg)

    def load(self, path):
        if path and self._norm(path) in VideoPlayer.locked_paths:
            self._log(f"[player] {os.path.basename(path)} is being cut right now (it will be "
                      "moved when done) - preview it after the run finishes.")
            if self.cap is None:
                self._placeholder(tr("This file is being cut right now\n"
                                     "- load it again when the run finishes"))
            return False
        if pick_engine() == "mpv":
            ok = self._mpv_load(path)
            if ok is not None:                  # None = mpv could not start
                return ok
        self._mpv_close()
        return self._load_cv(path)

    # ---- video size (always 16:9) ----
    def set_video_size(self, w, h):
        """Give the video area a new fixed size (a 16:9 ladder size) and
        redraw the current frame / placeholder in it."""
        w, h = int(w), int(h)
        if (w, h) == (self.VW, self.VH):
            return False
        self.VW, self.VH = w, h
        if self._resizable:
            self.canvas.place_configure(width=w, height=h)
        else:
            self.canvas.configure(width=w, height=h)
        if self._engine == "mpv":
            pass                     # mpv follows its frame's size by itself
        elif self._cur_bgr is not None:
            self._show(self._cur_bgr)
        else:
            self._placeholder(getattr(self, "_placeholder_msg", None))
        return True

    def _dpi_changed(self, ratio):
        """The display scaling changed (ui/dpi.py): the same 16:9 ladder
        step at the new DPI (an auto-fit / resizable player then picks the
        best one for the room it has at the new size)."""
        old = themes.scale() / ratio
        k = min(VIDEO_K, key=lambda k: abs(16 * max(1, int(round(k * old))) - self.VW))
        self.set_video_size(*video_size(k))
        if self._af is not None:
            self._af_schedule(60)
        elif self._resizable:
            if self._af_job is not None:
                self.after_cancel(self._af_job)
            self._af_job = self.after(120, self._fit_holder)   # once laid out

    def _on_holder_configure(self, e):
        """Resizable player: once the room settles, take the largest 16:9
        ladder size that fits in it."""
        self._holder_size = (e.width, e.height)
        if self._af_job is not None:
            self.after_cancel(self._af_job)
        self._af_job = self.after(120, self._fit_holder)

    def _fit_holder(self):
        self._af_job = None
        w, h = getattr(self, "_holder_size", (0, 0))
        if w > 1 and h > 1:
            self.set_video_size(*fit_16x9(w, h, min_k=VIDEO_K[0]))

    def auto_fit(self, row, min_k=None, max_k=80):
        """Size the video from the page it lives in: the largest 16:9 ladder
        size whose width fits next to the other columns of `row` (the frame
        holding this player and e.g. the section column) inside the visible
        page area, and whose height keeps the player (video + controls) on
        screen. Never below min_k (default: the size it was built with).
        Re-evaluated when the player is shown and after a window resize has
        settled - between those the video size stays fixed."""
        self._af = {"row": row, "min_k": self._base_k if min_k is None else min_k,
                    "max_k": max_k}
        top = self.winfo_toplevel()
        bind_window(top, "<Configure>", lambda e: self._af_schedule(350))
        self.bind("<Map>", lambda e: self._af_schedule(60), add="+")

    def _af_schedule(self, delay):
        if self._af is None:
            return
        if self._af_job is not None:
            try:
                self.after_cancel(self._af_job)
            except tk.TclError:
                pass
        self._af_job = self.after(delay, self._af_eval)

    def _page_view(self):
        w = self.master
        while w is not None:
            if type(w).__name__ == "PageView":
                return w
            w = w.master
        return None

    def _af_eval(self):
        self._af_job = None
        af = self._af
        try:
            if af is None or not self.winfo_viewable():
                return
            pv = self._page_view()
            page = pv.current() if pv is not None else None
            if page is None:
                return
            if pv.live_resizing():          # the window is being dragged:
                self._af_schedule(200)      # size the video once it ends
                return
            vw, vh = pv._view_total()
            if vw <= 1 or vh <= 1:
                return
            ix, iy = pv.inset
            sb = pv._sbw()
            row = af["row"]
            # width: what the row needs besides this player's video
            x_row = row.winfo_rootx() - page.winfo_rootx()
            other = row.winfo_reqwidth() - self.winfo_reqwidth()
            avail_w = vw - sb - 2 * ix - x_row - other - themes.px(8)
            # height: the whole player (video + timeline + controls) on screen
            y_can = self.canvas.winfo_rooty() - page.winfo_rooty()
            below = self.winfo_reqheight() - (self.canvas.winfo_rooty() - self.winfo_rooty())                 - self.canvas.winfo_reqheight()
            avail_h = vh - 2 * iy - y_can - below - themes.px(8)
            hl = 2 * int(self.canvas.cget("highlightthickness") or 0)
            w, h = fit_16x9(avail_w - hl, avail_h - hl, af["min_k"], af["max_k"])
            if self.set_video_size(w, h):
                pv.refit(page)
        except tk.TclError:
            return

    def _canvas_size(self):
        if self._resizable:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            return (w if w > 1 else self.VW, h if h > 1 else self.VH)
        return self.VW, self.VH

    def _placeholder(self, msg=None):
        if msg is None:
            msg = tr("No video loaded")
        self._mpv_surface(False)
        self.canvas.delete("all")
        cw, ch = self._canvas_size()
        self._placeholder_msg = msg
        self.canvas.create_text(cw // 2, ch // 2, text=msg, tags=("placeholder",),
                                fill="#a0a0a0", justify="center",
                                font="MPSubtitle")

    def _on_canvas_configure(self, e):
        """Resizable player: re-render the frame once the resize settles (a
        window drag sends a storm of <Configure>; scaling a video frame per
        event is what made resizing lag)."""
        size = (e.width, e.height)
        if size == self._canvas_size_seen:
            return
        self._canvas_size_seen = size
        if self._resize_job is None:
            self._resize_job = self.after(80, self._on_canvas_resize)

    def _on_canvas_resize(self, _e=None):
        self._resize_job = None
        if self._engine == "mpv":
            return
        if self._cur_bgr is not None:
            self._show(self._cur_bgr)
        else:
            self._placeholder(getattr(self, "_placeholder_msg", None))

    def _on_seek(self, frac):
        """Timeline click/drag: pause while dragging (restarting the sound on
        every motion event would stutter) and resume on release if it was
        playing - the same 'keep playing from the new spot' as the buttons."""
        if self.cap is None:
            return
        if self.playing:
            self._resume_after_drag = True
        self._goto(int(frac * max(self.total_frames - 1, 0)), play=False)

    def _on_seek_release(self):
        if self._resume_after_drag:
            self._resume_after_drag = False
            self._start_playing()
            self._notify_seek()

    def _jump_to(self):
        if self.cap is None or not self.fps:
            return
        secs = parse_time(self.jump_var.get())
        if secs is None:
            return
        self._goto(int(round(secs * self.fps)))

    def _update_time(self):
        cur = self.cur_frame / self.fps if self.fps else 0
        total = self.total_frames / self.fps if self.fps else 0
        self.time_var.set(self._time_text(cur, total, self.cur_frame))
        self.timeline.set_position(cur)

    @staticmethod
    def _time_text(cur, total, frame):
        """'0:01:05.500  /  0:22:10.000   (frame 1637)' (dashes before a load)."""
        dash = "--:--:--.---"
        return tr("{cur}  /  {total}   (frame {frame})",
                  cur=dash if cur is None else fmt_time(cur),
                  total=dash if total is None else fmt_time(total), frame=frame)

    def _step(self, delta, notify=True):
        """Frame step - always pauses (stepping while playing makes no sense)."""
        if self.cap is None:
            return
        self._goto(self.cur_frame + delta, play=False, notify=notify)

    def _toggle_play(self):
        if self.cap is None:
            return
        if self.playing:
            self._stop_play()
        else:
            self._start_playing()

    def _stop_play(self):
        if self._mpv_ld is not None:        # nothing plays yet: drop a queued Play
            self._mpv_ld["pending"].pop("play", None)
            self._mpv_ld["pending"].pop("range", None)
        was = self.playing
        self.playing = False
        self._stop_at_frame = None
        self._show_playing(False)
        if self._engine == "mpv":
            self._mpv_pending_end = None
            if was and self._mpv is not None:
                self._mpv.set("pause", True)
            self._mpv_disarm_end()
            return
        self.audio.stop()
        if self._play_after is not None:
            try:
                self.after_cancel(self._play_after)
            except Exception:
                pass
            self._play_after = None
