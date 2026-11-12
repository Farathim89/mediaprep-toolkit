"""The VideoPlayer's OpenCV engine (ui/player.py): frames decoded by cv2
and painted as Tk images, sound via ui/playback.py; wall-clock driven so
picture and sound stay in step. cv2 and Pillow are imported lazily."""
import os
import time

from ..engine.probe import probe_duration
from ..i18n import tr


class CvEngine:
    """VideoPlayer's OpenCV engine (a mixin: the state lives in VideoPlayer)."""

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

    def _start_clock_and_audio(self):
        """Anchor the playback clock at the current frame and start the sound.
        The clock starts only once audio.start() has returned (ffmpeg spin-up +
        first decoded block), otherwise the picture runs ahead of the sound."""
        self._play_start_frame = self.cur_frame
        self.audio.start(self._path, self.current_seconds() or 0.0)
        self._play_t0 = time.monotonic()

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
