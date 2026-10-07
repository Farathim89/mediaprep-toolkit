"""AudioPlayer - sound for the preview players.

ffmpeg decodes the video's audio track to raw 32-bit float PCM on a pipe and
the `sounddevice` library (PortAudio) plays it. Volume and mute are applied
live in the audio callback, so the slider works while playing. Each player has
its own AudioPlayer, so the Dual Player can play two soundtracks at once.

Everything is optional: if sounddevice (or an audio device, or ffmpeg) is
missing, start() simply does nothing and the player stays silent, exactly as
before."""
import os
import shutil
import subprocess
import threading

_RATE = 48000
_CHANNELS = 2
_BYTES_PER_FRAME = _CHANNELS * 4          # float32 stereo
_PREFETCH_BYTES = (_RATE // 20) * _BYTES_PER_FRAME   # first 50 ms of sound

# hide the ffmpeg console window on Windows
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_sd = None            # sounddevice module (lazy)
_np = None            # numpy module (lazy)
_import_failed = False


def _ensure_libs():
    """Import sounddevice + numpy on first use. Returns True when available."""
    global _sd, _np, _import_failed
    if _sd is not None:
        return True
    if _import_failed:
        return False
    try:
        import sounddevice as sd
        import numpy as np
        _sd, _np = sd, np
        return True
    except Exception:
        _import_failed = True
        return False


class AudioPlayer:
    """Plays the audio track of a video file from a given offset.

    start(path, offset)  - begin playback (stops any previous playback)
    stop()               - stop playback
    volume  (0.0-1.0)    - applied live; perceptual (squared) scaling
    muted   (bool)       - applied live
    """

    def __init__(self, log_fn=None):
        self._log = log_fn or (lambda m: None)
        self.volume = 0.8
        self.muted = False
        self.track = None        # audio stream 0:a:N to play (None = ffmpeg's pick)
        self._proc = None
        self._stream = None
        self._lock = threading.Lock()
        self._warned = False

    # ---- public ----
    def available(self):
        return _ensure_libs() and shutil.which("ffmpeg") is not None

    def start(self, path, offset_sec):
        if self.prepare(path, offset_sec):
            self.go()

    def prepare(self, path, offset_sec):
        """Start ffmpeg and wait for the first block of sound, but don't start
        the output stream yet (go() does). Lets callers start the picture clock
        - and the Dual Player both sides - only once sound can flow at once.
        Returns True when a stream is ready."""
        self.stop()
        if not path:
            return False
        if not self.available():
            if not self._warned:
                self._warned = True
                if not _ensure_libs():
                    self._log("[audio] sounddevice not installed - playback is "
                              "silent. Run Install Requirements.bat to add sound.")
                else:
                    self._log("[audio] ffmpeg not found on PATH - playback is silent.")
            return False
        cmd = ["ffmpeg", "-v", "error", "-nostdin",
               "-ss", f"{max(0.0, offset_sec):.3f}", "-i", path]
        if self.track is not None:
            cmd += ["-map", f"0:a:{int(self.track)}?"]
        cmd += ["-vn", "-sn", "-dn",
                "-f", "f32le", "-ac", str(_CHANNELS), "-ar", str(_RATE),
                "pipe:1"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW)
        except Exception as exc:
            self._log(f"[audio] could not start ffmpeg: {exc}")
            return False

        # pre-read the first block so the first callback doesn't sit waiting
        # for ffmpeg to spin up (that wait used to make the sound lag the video)
        try:
            pending = [proc.stdout.read(_PREFETCH_BYTES)]
        except Exception:
            pending = [b""]
        if not pending[0]:                 # no audio track / ffmpeg failed
            self._kill_proc(proc)
            return False

        def callback(outdata, frames, _time, _status):
            need = frames * _BYTES_PER_FRAME
            buf = pending[0][:need]
            pending[0] = pending[0][len(buf):]
            if len(buf) < need:
                try:
                    buf += proc.stdout.read(need - len(buf))
                except (ValueError, OSError):      # pipe closed by stop()
                    raise _sd.CallbackStop
            buf = buf[:len(buf) // 4 * 4]           # whole float32 samples only
            if not buf:
                raise _sd.CallbackStop
            arr = _np.frombuffer(buf, dtype=_np.float32)
            if len(arr) < frames * _CHANNELS:      # end of file: pad the tail
                arr = _np.concatenate(
                    [arr, _np.zeros(frames * _CHANNELS - len(arr), dtype=_np.float32)])
            gain = 0.0 if self.muted else self.volume * self.volume
            outdata[:] = (arr * gain).reshape(-1, _CHANNELS)

        try:
            stream = _sd.OutputStream(samplerate=_RATE, channels=_CHANNELS,
                                      dtype="float32", callback=callback)
        except Exception as exc:
            self._kill_proc(proc)
            if not self._warned:
                self._warned = True
                self._log(f"[audio] no audio output available: {exc}")
            return False
        with self._lock:
            self._proc, self._stream = proc, stream
        return True

    def go(self):
        """Start the stream set up by prepare(). Safe if nothing is prepared."""
        with self._lock:
            stream = self._stream
        if stream is None:
            return
        try:
            stream.start()
        except Exception as exc:
            self.stop()
            if not self._warned:
                self._warned = True
                self._log(f"[audio] no audio output available: {exc}")

    @staticmethod
    def _kill_proc(proc):
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass

    def stop(self):
        with self._lock:
            proc, stream = self._proc, self._stream
            self._proc = self._stream = None
        # kill ffmpeg first: a callback blocked reading its pipe then returns
        # at once, so abort() below can't hang waiting for it
        if proc is not None:
            self._kill_proc(proc)
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def is_active(self):
        return self._stream is not None
