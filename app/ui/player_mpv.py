"""The VideoPlayer's mpv engine (ui/player.py): which engine a load uses
(Settings -> Video player engine: "auto" = mpv if found + it starts, else
OpenCV) and the player's mpv side - mpv.exe draws into the video box,
decodes on the GPU and plays the sound (ui/mpvplayer.py runs the process
and its JSON IPC)."""
import os
import threading
import time
import tkinter as tk

from ..engine.probe import probe_duration
from ..i18n import tr
from . import mpvplayer, themes
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
        if "mpv" not in _MPV["logged"]:
            _MPV["logged"].add("mpv")
            # `mpv --version` takes a while: asked on a thread, not the Tk one
            threading.Thread(target=lambda: applog.record(
                f"[player] engine: mpv ({exe}; "
                f"{mpvplayer.mpv_version(exe) or 'unknown version'})"),
                name="mpv-version", daemon=True).start()
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


class MpvEngine:
    """VideoPlayer's mpv engine (a mixin: the state lives in VideoPlayer)."""

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
            # starts mpv only - the pipe opens in the background (a failure
            # arrives as the "start-failed" event, see _mpv_load_step)
            b = mpvplayer.MpvProcess(exe, self._mpv_host.winfo_id(), extra=extra,
                                     log_fn=self._log)
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
        self._mpv_ld = None
        if b is not None:
            b.close()

    def _mpv_load(self, path):
        """Start opening path in mpv - the event pump finishes it
        (_mpv_load_step), so the Tk thread never waits on mpv. Returns True
        when it is being opened (the player shows "Loading..." until then;
        seeks / Play asked for meanwhile are applied once it is open),
        False when there is no such file, None when mpv is not usable (the
        caller falls back to OpenCV)."""
        self._stop_play()
        if not self._mpv_start():
            return None
        b = self._mpv
        self._mpv_ld = None
        self._clear_loaded(tr("Loading..."))
        if not path or not os.path.isfile(path):
            self._placeholder(tr("Could not open video"))
            self._log(f"[player] could not open {os.path.basename(path or '')}")
            return False
        b.set("pause", True)
        b.set("end", "none")
        rid = b.command("loadfile", path)
        self._mpv_seek_rid, self._mpv_settled, self._mpv_play_rid = rid, False, 0
        self._path = path
        self._mpv_ld = {"path": path, "rid": rid, "t0": time.monotonic(), "stage": "open",
                        "props": {}, "restart": False, "pending": {}}
        self._mpv_kick()
        return True

    _LOAD_PROPS = ("vid", "container-fps", "estimated-vf-fps", "duration")
    LOAD_TIMEOUT = 20.0              # s for mpv to open a file (else OpenCV tries)

    def _mpv_load_step(self, b, events):
        """One pump step while mpv opens a file. Returns the events that
        belong to the playback after it (the pump handles them as usual)."""
        ld = self._mpv_ld
        rid = ld["rid"]
        name = os.path.basename(ld["path"])
        rest = []
        for m in events:
            ev = m.get("event")
            seq = m.get("_seq", 0)
            if ev == "start-failed":
                return self._mpv_load_failed(ld, "start")
            if ev == "ipc-closed":
                return self._mpv_load_failed(ld, "died")
            reply = m.get("reply")
            if isinstance(reply, tuple) and reply[0] == "ld":
                if seq >= rid and ld["stage"] == "props":
                    ok = m.get("error") == "success"
                    ld["props"][reply[1]] = m.get("data") if ok else None
                continue
            if seq < rid:
                continue                    # about the previous file
            if ev == "end-file" and m.get("reason") == "error" and ld["stage"] == "open":
                self._mpv_ld = None
                self._clear_loaded(tr("Could not open video"))
                self._log(f"[player] could not open {name}")
                return []
            if ev == "file-loaded" and ld["stage"] == "open":
                ld["stage"] = "props"
                for prop in self._LOAD_PROPS:
                    b.command("get_property", prop, tag=("ld", prop))
                continue
            if ev == "_probed" and ld["stage"] == "probe":
                ld["probed"] = (m.get("fps"), m.get("dur"))
                continue
            if ev == "playback-restart":
                ld["restart"] = True        # the first frame is shown
                continue
            rest.append(m)
        if ld["stage"] == "props" and len(ld["props"]) == len(self._LOAD_PROPS):
            self._mpv_props_in(b, ld)
        elif ld["stage"] == "probe" and "probed" in ld:
            self._mpv_loaded(b, ld, *ld["probed"])
        elif time.monotonic() - ld["t0"] > self.LOAD_TIMEOUT:
            return self._mpv_load_failed(ld, "hangs")
        return rest

    def _mpv_props_in(self, b, ld):
        props = ld["props"]
        if not props.get("vid"):
            self._mpv_ld = None
            self._clear_loaded(tr("Could not read video"))
            self._log(f"[player] could not read a frame from {os.path.basename(ld['path'])}")
            return
        fps = _snap_fps(props.get("container-fps")) or _snap_fps(props.get("estimated-vf-fps"))
        dur = props.get("duration")
        if fps and dur:
            self._mpv_loaded(b, ld, fps, dur)
            return
        # mpv doesn't know: ask ffprobe - on a thread, the answer comes
        # back through mpv's event queue as "_probed"
        ld["stage"] = "probe"
        path, rid = ld["path"], ld["rid"]

        def probe():
            f, d = fps, dur
            try:
                if not f:
                    from ..engine.probe import probe_video_fps
                    f = _snap_fps(probe_video_fps(path))
            except Exception:
                f = None
            try:
                if not d:
                    d = probe_duration(path)
            except Exception:
                d = None
            b.events.put({"event": "_probed", "fps": f, "dur": d, "_seq": rid})
        threading.Thread(target=probe, name="player-probe", daemon=True).start()

    def _mpv_loaded(self, b, ld, fps, dur):
        """mpv has the file open: show it, then do what was asked meanwhile."""
        self._mpv_ld = None
        self.fps = fps or 25.0
        try:
            # the OpenCV engine's frame count: duration x fps, rounded
            frames = int(float(dur) * self.fps + 0.5) if dur else 0
        except (TypeError, ValueError):
            frames = 0
        self.cap = b
        self._engine = "mpv"
        self._path = ld["path"]
        self.total_frames = max(frames, 0)
        if len(str(self.total_frames)) > 6:
            self._time_box.remeasure()
        self.cur_frame = 0
        self.mpv_time_pos = None
        self.timeline.set_duration(self.total_frames / self.fps if self.fps else 0.0)
        self._update_time()
        self._mpv_surface(True)
        mpvplayer.disable_child_windows(self._mpv_host.winfo_id())
        if ld["restart"] and not self._mpv_settled:
            self._mpv_settled = True
            b.command("get_property", "time-pos", tag="pos")
        if not self.typing_focused():
            self.canvas.focus_set()
        self._apply_pending(ld["pending"])

    def _apply_pending(self, pend):
        if "range" in pend:
            self.play_range(*pend["range"])
        elif "seek" in pend:
            self.seek_seconds(pend["seek"], play=pend.get("play"))
        elif pend.get("play"):
            self.play()

    def _mpv_load_failed(self, ld, why):
        """mpv could not start / died / hangs on this file: OpenCV tries it."""
        path, pend = ld["path"], ld["pending"]
        self._mpv_ld = None
        name = os.path.basename(path)
        if why == "start":
            _MPV["failed"] = True
            msg = "[player] mpv could not start (no IPC pipe) - using OpenCV instead"
            self._log(msg)
            applog.record(msg)
        else:
            self._log(f"[player] mpv did not open {name} - trying OpenCV")
        self._mpv_close()
        if self._load_cv(path):
            self._apply_pending(pend)
        return []

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
        """Drain mpv's events on the Tk thread (a file being opened,
        position, seek landed, auto-pause at a section end / the end of the
        file, mpv gone)."""
        self._mpv_job = None
        b = self._mpv
        if b is None:
            return
        events = b.drain()
        if self._mpv_ld is not None:
            events = self._mpv_load_step(b, events)
            if self._mpv_ld is not None:            # still opening
                if self._mpv is b and self._mpv_job is None:
                    self._mpv_job = self.after(15, self._mpv_pump)
                return
            if self._mpv is not b or self.cap is None:
                return                              # failed / OpenCV took over
        pos_dirty = stopped = False
        for m in events:
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
        if self.cap is None or self._mpv_job is not None:
            return      # gone - or kicked meanwhile (a queued seek / Play): runs again
        delay = 10 if not self._mpv_settled else 30 if self.playing else 250
        self._mpv_job = self.after(delay, self._mpv_pump)

    def _mpv_died(self):
        self._log("[player] mpv stopped unexpectedly - load the video again")
        applog.record("[player] mpv stopped unexpectedly")
        self._mpv_close()
        self._clear_loaded(tr("Could not read video"))
