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
still runs (manual entry only) without them."""
import os
import time
import tkinter as tk
from tkinter import ttk

from .playback import AudioPlayer
from ..engine.formatting import fmt_time, parse_time
from ..engine.probe import probe_duration
from .widgets import add_tooltip, fixed_label
from ..i18n import tr
from . import icons, mpvplayer, themes
from .. import applog
from ..config import PREFS

# Video player engines (Settings): "auto" = mpv if found + it starts, else OpenCV
ENGINES = ("auto", "mpv", "opencv")
_MPV = {"exe": False, "failed": False, "logged": set()}   # session-wide mpv state


def _mpv_exe():
    if _MPV["exe"] is False:
        _MPV["exe"] = mpvplayer.find_mpv()
    return _MPV["exe"]


def _log_engine_once(key, msg):
    if key not in _MPV["logged"]:
        _MPV["logged"].add(key)
        applog.record(msg)


def pick_engine():
    """The engine the next load uses: "mpv" or "opencv" (from the setting)."""
    pref = str(PREFS.get("player_engine", "auto") or "auto").lower()
    if pref == "opencv":
        _log_engine_once("opencv", "[player] engine: OpenCV (set in Settings)")
        return "opencv"
    exe = _mpv_exe()
    if exe and (pref == "mpv" or not _MPV["failed"]):     # Auto gives up after a failure
        _log_engine_once("mpv", f"[player] engine: mpv ({exe}; "
                                f"{mpvplayer.mpv_version(exe) or 'unknown version'})")
        return "mpv"
    why = "mpv did not start" if exe else "mpv not found"
    _log_engine_once("fallback", f"[player] engine: OpenCV ({why})")
    return "opencv"


def _snap_fps(fps):
    """mpv reports e.g. 23.976025 (from a rounded per-frame duration): snap
    it to the exact NTSC / integer rate the OpenCV engine reports, so both
    engines number frames identically."""
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        return None
    if not fps > 0:
        return None
    for cand in (round(fps), round(fps * 1.001) * 1000 / 1001):
        if cand > 0 and abs(fps - cand) / cand < 2e-5:
            return float(cand)
    return fps

# segment band colours on the timeline, per theme (palette marker_<kind>).
# Pass the KIND ("intro", ...) to set_markers so the bands follow theme
# changes; MARKER_COLORS (kind -> current colour) is kept for old callers.
MARKER_KINDS = ("preintro", "intro", "credits", "aftercredits")

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
MARKER_COLORS = {}


def _sync_marker_colors(_name=None, pal=None):
    pal = pal or themes.current()
    MARKER_COLORS.update({k: pal["marker_" + k] for k in MARKER_KINDS})


_sync_marker_colors()
themes.subscribe(_sync_marker_colors)


def marker_color(c, pal=None):
    """A marker colour given as a kind ("intro"), a palette key ("warn") or a
    literal colour -> the colour to draw with the current theme."""
    pal = pal or themes.current()
    if c in MARKER_KINDS:
        return pal["marker_" + c]
    if isinstance(c, str) and c.startswith("#"):
        return c
    return pal.get(c, pal["border"])


class TimelineBar(tk.Canvas):
    """A slim seek bar: rounded track with the played part in the accent
    colour, the marked segments as coloured range bands with small edge
    handles, a round playhead thumb and a time tooltip under the pointer.
    Click or drag to seek (calls on_seek(fraction)); on_release() is called
    when the mouse button is let go."""

    def __init__(self, master, on_seek=None, height=30, on_release=None, **kw):
        super().__init__(master, height=themes.px(height), highlightthickness=0,
                         borderwidth=0, **kw)
        self.on_seek = on_seek
        self.on_release = on_release
        self.duration = 0.0
        self.pos = 0.0
        self.markers = []   # list of (start_sec, end_sec, color)
        self._size = None
        self._thumb = None
        self._tip = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._click)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self._hide_tip())
        self._pal = themes.current()
        themes.on_palette(self, self._repaint)

    def _repaint(self, pal):
        self._pal = pal
        self._thumb = None
        self.configure(bg=pal["bg"])
        self._redraw()

    def _on_configure(self, e):
        # a redraw is cheap, but only do it when the size really changed
        if (e.width, e.height) != self._size:
            self._size = (e.width, e.height)
            self._redraw()

    def set_duration(self, dur):
        self.duration = max(0.0, dur or 0.0)
        self._redraw()

    def set_position(self, sec):
        self.pos = max(0.0, min(sec, self.duration)) if self.duration else 0.0
        self._place_head()

    def set_markers(self, markers):
        self.markers = markers or []
        self._redraw()

    # geometry: the track runs between two insets so the thumb never clips
    def _inset(self):
        return themes.px(8)

    def _x(self, sec):
        i = self._inset()
        w = max(1, self.winfo_width() - 2 * i)
        return i + (int((sec / self.duration) * w) if self.duration else 0)

    def _frac(self, x):
        i = self._inset()
        w = max(1, self.winfo_width() - 2 * i)
        return min(max((x - i) / w, 0.0), 1.0)

    def _click(self, e):
        if not self.duration or not self.on_seek:
            return
        self.on_seek(self._frac(e.x))
        self._hover(e)

    def _release(self, _e=None):
        if self.on_release:
            self.on_release()

    def _thumb_img(self):
        """Anti-aliased round thumb (Pillow), cached per theme."""
        if self._thumb is not None:
            return self._thumb or None
        self._thumb = False
        try:
            import base64
            import io
            from PIL import Image, ImageDraw
            pal = self._pal
            d = themes.px(14)
            ss = 4
            # opaque on the bar's background: a plain blit for Tk (alpha
            # photos are blended per pixel on every redraw)
            im = Image.new("RGB", (d * ss, d * ss), pal["bg"])
            dr = ImageDraw.Draw(im)
            dr.ellipse((0, 0, d * ss - 1, d * ss - 1), fill=pal["fg"])
            o = int(d * ss * 0.2)
            dr.ellipse((o, o, d * ss - 1 - o, d * ss - 1 - o), fill=pal["accent"])
            im = im.resize((d, d), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            self._thumb = tk.PhotoImage(master=self, data=base64.b64encode(buf.getvalue()))
        except Exception:
            self._thumb = False
        return self._thumb or None

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        mid = h // 2
        pal = self._pal
        i = self._inset()
        track = max(2, themes.px(4))
        # base track (round caps) + played part
        self.create_line(i, mid, max(i + 1, w - i), mid, fill=pal["progress_track"],
                         width=track, capstyle="round", tags=("track",))
        self.create_line(i, mid, i, mid, fill=pal["accent"], width=track, capstyle="round",
                         tags=("played",))
        # coloured marker bands with a small handle at each end
        band = themes.px(12)
        for s, e, color in self.markers:
            if self.duration and e > s:
                x0, x1 = self._x(s), self._x(e)
                color = marker_color(color, pal)
                self.create_rectangle(x0, mid - band // 2, max(x1, x0 + 2), mid + band // 2,
                                      fill=color, outline="", tags=("band",))
                hw = max(1, themes.px(2))
                for x in (x0, max(x1, x0 + 2)):
                    self.create_rectangle(x - hw // 2, mid - band // 2 - themes.px(3),
                                          x - hw // 2 + hw, mid + band // 2 + themes.px(3),
                                          fill=color, outline="", tags=("band",))
        img = self._thumb_img()
        if img is not None:
            self.create_image(i, mid, image=img, tags=("head",))
        else:
            r = themes.px(6)
            self.create_oval(i - r, mid - r, i + r, mid + r, fill=pal["accent"],
                             outline=pal["fg"], tags=("head",))
        self._place_head()

    def _place_head(self):
        """Move the played bar + thumb (cheap - runs on every played frame)."""
        try:
            h = max(1, self.winfo_height())
        except tk.TclError:
            return
        mid = h // 2
        i = self._inset()
        px = self._x(self.pos)
        self.coords("played", i, mid, max(i, px), mid)
        self.itemconfigure("played", state="normal" if px > i else "hidden")
        bb = self.bbox("head")
        if bb:
            cx = (bb[0] + bb[2]) // 2
            self.move("head", px - cx, 0)
        self.tag_raise("head")

    # hover: the time under the pointer
    def _hover(self, e):
        if not self.duration:
            self._hide_tip()
            return
        text = fmt_time(self._frac(e.x) * self.duration)
        pal = self._pal
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            try:
                self._tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            self._tip_lbl = tk.Label(self._tip, font="MPMono", borderwidth=0,
                                     padx=themes.px(6), pady=themes.px(2))
            self._tip_lbl.pack(padx=1, pady=1)
        self._tip.configure(bg=pal["stroke"])
        self._tip_lbl.configure(text=text, bg=pal["tooltip_bg"], fg=pal["tooltip_fg"])
        self._tip.update_idletasks()
        tw = self._tip.winfo_reqwidth()
        self._tip.wm_geometry(f"+{e.x_root - tw // 2}+{self.winfo_rooty() - self._tip.winfo_reqheight() - themes.px(4)}")

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


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
        self._step(n, notify=notify)

    def play_range(self, start_sec, end_sec):
        """Seek to start_sec, play, and auto-pause on the last frame BEFORE
        end_sec (section preview; end_sec is the exclusive end = the first
        frame kept, so the preview plays through the last removed frame).
        Any manual transport action (pause, seek, step, jump) cancels the stop
        point, so the player behaves normally again afterwards."""
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
        if self.cap is None or not self.fps:
            return
        self._goto(int(round(sec * self.fps)), play=play, notify=False)

    def to_start(self):
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

    def _load_cv(self, path):
        if not self._ensure_libs():
            self._placeholder(tr("Preview needs opencv-python + Pillow\n"
                                 "(run Install Requirements.bat)\n"
                                 "- you can still type times manually"))
            self._log("[player] opencv-python / Pillow missing - preview disabled,"
                      " manual entry still works.")
            return False
        self._stop_play()
        self._clear_loaded(tr("Loading..."))
        cap = self._cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            self._placeholder(tr("Could not open video"))
            self._log(f"[player] could not open {os.path.basename(path)}")
            return False
        self.cap = cap
        self._engine = "opencv"
        self._path = path
        fps = cap.get(self._cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else 25.0
        frames = int(cap.get(self._cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frames <= 0:   # some containers don't report it: derive from duration
            dur = probe_duration(path)
            frames = int(dur * self.fps) if dur else 0
        self.total_frames = max(frames, 0)
        if len(str(self.total_frames)) > 6:
            self._time_box.remeasure()
        self.cur_frame = 0
        self.timeline.set_duration(self.total_frames / self.fps if self.fps else 0.0)
        self._seek_show(0)
        if self._cur_bgr is None:     # first frame unreadable
            self._clear_loaded(tr("Could not read video"))
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
        top.bind("<Configure>", lambda e: e.widget is top and self._af_schedule(350),
                 add="+")
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

    def _start_clock_and_audio(self):
        """Anchor the playback clock at the current frame and start the sound.
        The clock starts only once audio.start() has returned (ffmpeg spin-up +
        first decoded block), otherwise the picture runs ahead of the sound."""
        self._play_start_frame = self.cur_frame
        self.audio.start(self._path, self.current_seconds() or 0.0)
        self._play_t0 = time.monotonic()

    def _stop_play(self):
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

    # ------------------------------------------------------------ mpv engine
    # mpv works asynchronously: a seek / step sets cur_frame to the target at
    # once (so Set, Go and the Dual Player link see it immediately) and sends
    # an exact seek; when mpv reports the seek has landed (playback-restart)
    # the player asks for mpv's time-pos and takes the shown frame's number
    # from it - normally the same frame, corrected if the file disagrees
    # (e.g. past the last frame). Events are drained on the Tk thread by
    # _mpv_pump; every event carries "_seq" (see mpvplayer) so position /
    # pause / eof news from before the latest seek or play is ignored.
    def _mpv_palette(self, pal):
        bg = pal.get("video_bg", "#000000")
        try:
            self._mpv_host.configure(bg=bg)
        except tk.TclError:
            pass
        if self._mpv is not None:
            self._mpv.set("background-color", bg)

    def _mpv_volume(self):
        """mpv volume for the slider: the OpenCV engine's gain is v^2, mpv's
        is (volume/100)^3 - so the same slider position sounds the same."""
        g = max(0.0, min(float(self.audio.volume), 1.0)) ** 2
        return round(100.0 * g ** (1.0 / 3.0), 2)

    def _mpv_surface(self, on):
        try:
            if on:
                self._mpv_host.place(x=0, y=0, relwidth=1, relheight=1)
            else:
                self._mpv_host.place_forget()
        except (tk.TclError, AttributeError):
            pass

    def _mpv_start(self):
        """Start this player's mpv (once; reused for later loads)."""
        b = self._mpv
        if b is not None and b.alive():
            return True
        self._mpv_close()
        exe = _mpv_exe()
        if not exe:
            return False
        bg = themes.current().get("video_bg", "#000000")
        extra = [f"--background-color={bg}", f"--volume={self._mpv_volume()}",
                 "--mute=" + ("yes" if self.audio.muted else "no")]
        if self.audio_track is not None:
            extra.append(f"--aid={self.audio_track + 1}")
        try:
            b = mpvplayer.MpvProcess(exe, self._mpv_host.winfo_id(), extra=extra)
        except (OSError, ValueError, tk.TclError) as exc:
            _MPV["failed"] = True
            self._log(f"[player] mpv could not start ({exc}) - using OpenCV instead")
            applog.record(f"[player] mpv could not start ({exc}) - using OpenCV instead")
            return False
        for prop in ("time-pos", "pause", "eof-reached"):
            b.observe(prop)
        self._mpv = b
        return True

    def _mpv_close(self):
        """Quit this player's mpv (frees the file); safe to call any time."""
        b, self._mpv = self._mpv, None
        if self._mpv_job is not None:
            try:
                self.after_cancel(self._mpv_job)
            except (tk.TclError, ValueError):
                pass
            self._mpv_job = None
        if self._engine == "mpv":
            self.cap = None
            self._engine = None
            self.playing = False
        self._mpv_pending_end = None
        self._mpv_end_armed = False
        if b is not None:
            b.close()

    def _mpv_load(self, path):
        """Open path in mpv. True / False like load(); None = mpv is not
        usable (the caller falls back to OpenCV)."""
        self._stop_play()
        if not self._mpv_start():
            return None
        b = self._mpv
        self._clear_loaded(tr("Loading..."))
        if not path or not os.path.isfile(path):
            self._placeholder(tr("Could not open video"))
            self._log(f"[player] could not open {os.path.basename(path or '')}")
            return False
        b.set("pause", True)
        b.set("end", "none")
        rid = b.command("loadfile", path)
        self._mpv_seek_rid, self._mpv_settled, self._mpv_play_rid = rid, False, 0

        def done(m):
            ev = m.get("event")
            return m.get("_seq", 0) >= rid and (
                ev in ("file-loaded", "ipc-closed")
                or (ev == "end-file" and m.get("reason") == "error"))
        ev = b.wait_event(done, 20.0)
        kind = ev.get("event") if ev else None
        if kind != "file-loaded":
            if kind == "end-file":
                self._placeholder(tr("Could not open video"))
                self._log(f"[player] could not open {os.path.basename(path)}")
                return False
            # mpv died or hung on this file: let OpenCV try it
            self._log(f"[player] mpv did not open {os.path.basename(path)} - trying OpenCV")
            self._mpv_close()
            return None
        if not b.get("vid"):
            self._clear_loaded(tr("Could not read video"))
            self._log(f"[player] could not read a frame from {os.path.basename(path)}")
            return False
        fps = _snap_fps(b.get("container-fps")) or _snap_fps(b.get("estimated-vf-fps"))
        if not fps:
            try:
                from ..engine.probe import probe_video_fps
                fps = _snap_fps(probe_video_fps(path))
            except Exception:
                fps = None
        self.fps = fps or 25.0
        dur = b.get("duration")
        if not dur:
            dur = probe_duration(path)
        try:
            # the OpenCV engine's frame count: duration x fps, rounded
            frames = int(float(dur) * self.fps + 0.5) if dur else 0
        except (TypeError, ValueError):
            frames = 0
        self.cap = b
        self._engine = "mpv"
        self._path = path
        self.total_frames = max(frames, 0)
        if len(str(self.total_frames)) > 6:
            self._time_box.remeasure()
        self.cur_frame = 0
        self.mpv_time_pos = None
        self.timeline.set_duration(self.total_frames / self.fps if self.fps else 0.0)
        self._update_time()
        self._mpv_surface(True)
        mpvplayer.disable_child_windows(self._mpv_host.winfo_id())
        self.canvas.focus_set()
        self._mpv_kick()
        return True

    def _mpv_kick(self, delay=1):
        """Run the event pump soon (after a command)."""
        if self._mpv is None:
            return
        if self._mpv_job is not None:
            try:
                self.after_cancel(self._mpv_job)
            except (tk.TclError, ValueError):
                pass
        self._mpv_job = self.after(delay, self._mpv_pump)

    def _mpv_at_end(self):
        return self.total_frames > 0 and self.cur_frame >= self.total_frames - 1

    def _mpv_goto(self, idx, play=None):
        b = self._mpv
        if b is None or not self.fps:
            return
        idx = max(0, int(idx))
        if self.total_frames > 0:
            idx = min(idx, self.total_frames - 1)
        was = self.playing
        want = was if play is None else bool(play)
        # any manual move cancels a section preview's stop point
        self._stop_at_frame = None
        self._mpv_pending_end = None
        self._mpv_disarm_end()
        if was and not want:
            self.playing = False
            self._show_playing(False)
            b.set("pause", True)
        self.cur_frame = idx
        self._update_time()
        self._mpv_hold = False
        self._mpv_seek_rid = b.command("seek", idx / self.fps, "absolute+exact")
        self._mpv_settled = False
        if want and not was:
            self._mpv_play()
        self._mpv_kick()

    def _mpv_play(self):
        b = self._mpv
        if b is None or self.cap is None or self._mpv_at_end():
            return
        self._mpv_pending_end = None
        self._mpv_disarm_end()
        self.playing = True
        self._show_playing(True)
        self._mpv_hold = False
        self._mpv_play_rid = b.set("pause", False)
        self._mpv_kick()

    def _mpv_arm_end(self, end_frame):
        """play_range: let mpv pause by itself on end_frame (frames at or
        after the 'end' time are not shown; half a frame keeps rounding safe)."""
        self._mpv.set("end", f"{(end_frame + 0.5) / self.fps:.6f}")
        self._mpv_end_armed = True
        self._mpv_end_frame = end_frame

    def _mpv_disarm_end(self):
        if self._mpv_end_armed and self._mpv is not None:
            self._mpv.set("end", "none")
        self._mpv_end_armed = False

    def _mpv_set_pos(self, pos):
        if pos is None or self.cap is None or not self.fps:
            return
        try:
            pos = float(pos)
        except (TypeError, ValueError):
            return
        self.mpv_time_pos = pos
        f = max(0, int(round(pos * self.fps)))
        if self.total_frames > 0:
            f = min(f, self.total_frames - 1)
        if f != self.cur_frame:
            self.cur_frame = f
            self._update_time()

    def _mpv_pump(self):
        """Drain mpv's events on the Tk thread (position, seek landed,
        auto-pause at a section end / the end of the file, mpv gone)."""
        self._mpv_job = None
        b = self._mpv
        if b is None:
            return
        pos_dirty = stopped = False
        for m in b.drain():
            seq = m.get("_seq", 0)
            ev = m.get("event")
            if ev == "ipc-closed":
                self._mpv_died()
                return
            if "reply" in m:
                if (m["reply"] == "pos" and seq >= self._mpv_seek_rid
                        and m.get("error") == "success"):
                    self._mpv_set_pos(m.get("data"))
                continue
            if ev == "playback-restart":
                if seq >= self._mpv_seek_rid and not self._mpv_settled:
                    self._mpv_settled = True
                    b.command("get_property", "time-pos", tag="pos")
                    if self._mpv_pending_end is not None and self.playing:
                        self._mpv_arm_end(self._mpv_pending_end)
                        self._mpv_pending_end = None
                        self._mpv_play_rid = b.set("pause", False)
            elif ev == "property-change":
                name = m.get("name")
                if name == "time-pos":
                    if self._mpv_settled and not self._mpv_hold and seq >= self._mpv_seek_rid:
                        pos_dirty = True
                elif name in ("pause", "eof-reached"):
                    # mpv paused by itself: section end ('end') or end of file
                    if (m.get("data") is True and self.playing and self._mpv_pending_end is None
                            and seq >= self._mpv_play_rid):
                        stopped = True
        if pos_dirty:
            self._mpv_set_pos(b.props.get("time-pos"))
        if stopped:
            self.playing = False
            self._stop_at_frame = None
            self._show_playing(False)
            # at the 'end' point mpv holds the frame but does not set pause:
            # pause first, or lifting 'end' would let it play on
            b.set("pause", True)
            end_frame = self._mpv_end_frame if self._mpv_end_armed else None
            self._mpv_disarm_end()
            if end_frame is not None:
                # held on the last frame before 'end' - its time-pos reads
                # as the 'end' time itself, so take the frame we asked for
                self._mpv_set_pos(end_frame / self.fps)
                self._mpv_hold = True        # ignore that time-pos until the next move
            else:
                b.command("get_property", "time-pos", tag="pos")
        if self.cap is None:
            return
        delay = 10 if not self._mpv_settled else 30 if self.playing else 250
        self._mpv_job = self.after(delay, self._mpv_pump)

    def _mpv_died(self):
        self._log("[player] mpv stopped unexpectedly - load the video again")
        applog.record("[player] mpv stopped unexpectedly")
        self._mpv_close()
        self._clear_loaded(tr("Could not read video"))
