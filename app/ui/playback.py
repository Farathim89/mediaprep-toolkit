"""AudioPlayer - sound for the preview players.

ffmpeg decodes the video's audio track to raw 32-bit float PCM on a pipe and
the `sounddevice` library (PortAudio) plays it. Volume and mute are applied
live in the audio callback, so the slider works while playing. Each player has
its own AudioPlayer, so the Dual Player can play two soundtracks at once.

Everything is optional: if sounddevice (or an audio device, or ffmpeg) is
missing, start() simply does nothing and the player stays silent, exactly as
before."""
import shutil
import subprocess
import threading

from ..config import limit_cmd, popen_flags

_RATE = 48000
_CHANNELS = 2
_BYTES_PER_FRAME = _CHANNELS * 4          # float32 stereo
_BUFFER_MAX = _RATE * _BYTES_PER_FRAME    # read ahead at most ~1 s of sound
_READ_CHUNK = 8192 * _BYTES_PER_FRAME

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


class _PcmReader:
    """Reads ffmpeg's PCM pipe on its own thread into a bounded buffer (about
    a second ahead), so neither the Tk thread nor the audio callback ever
    waits on the pipe."""

    def __init__(self, proc):
        self._out = proc.stdout
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        self.eof = False
        threading.Thread(target=self._run, name="audio-pcm", daemon=True).start()

    def _run(self):
        read = getattr(self._out, "read1", None) or self._out.read
        try:
            while True:
                with self._cv:
                    while len(self._buf) >= _BUFFER_MAX and not self._closed:
                        self._cv.wait(0.5)
                    if self._closed:
                        return
                try:
                    chunk = read(_READ_CHUNK)
                except (ValueError, OSError):       # pipe closed by stop()
                    chunk = b""
                if not chunk:
                    return
                with self._cv:
                    self._buf += chunk
        finally:
            with self._cv:
                self.eof = True

    def take(self, n):
        """(up to n bytes of whole frames available now, finished) - never
        waits; finished = the stream ended and everything was taken."""
        with self._cv:
            k = min(n, len(self._buf)) // _BYTES_PER_FRAME * _BYTES_PER_FRAME
            data = bytes(self._buf[:k])
            del self._buf[:k]
            self._cv.notify()
            return data, self.eof and len(self._buf) < _BYTES_PER_FRAME

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


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
        self._reader = None
        self._lock = threading.Lock()
        self._warned = False

    # ---- public ----
    def available(self):
        return _ensure_libs() and shutil.which("ffmpeg") is not None

    def start(self, path, offset_sec):
        if self.prepare(path, offset_sec):
            self.go()

    def prepare(self, path, offset_sec):
        """Start ffmpeg (decoding on its own thread) and set up the output
        stream, but don't start it yet (go() does) - so the Dual Player can
        start both sides together. Never waits for ffmpeg: until its first
        sound arrives the stream plays silence, and as much sound as that
        silence lasted is skipped once it comes in, so the sound stays on
        the wall clock the picture follows. Returns True when a stream is
        ready."""
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
            proc = subprocess.Popen(limit_cmd(cmd), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=popen_flags())
        except Exception as exc:
            self._log(f"[audio] could not start ffmpeg: {exc}")
            return False
        reader = _PcmReader(proc)
        owed = [0]          # bytes of sound played as silence while ffmpeg lagged

        def callback(outdata, frames, _time, _status):
            need = frames * _BYTES_PER_FRAME
            while owed[0]:                          # catch up with the clock first
                skipped, _done = reader.take(owed[0])
                if not skipped:
                    break
                owed[0] -= len(skipped)
            buf, done = reader.take(need) if not owed[0] else (b"", reader.eof)
            if not buf and done:
                raise _sd.CallbackStop              # end of the sound (or none)
            if len(buf) < need:
                if not done:
                    owed[0] += need - len(buf)      # ffmpeg is behind: silence now
                buf += bytes(need - len(buf))
            arr = _np.frombuffer(buf, dtype=_np.float32)
            gain = 0.0 if self.muted else self.volume * self.volume
            outdata[:] = (arr * gain).reshape(-1, _CHANNELS)

        try:
            stream = _sd.OutputStream(samplerate=_RATE, channels=_CHANNELS,
                                      dtype="float32", callback=callback)
        except Exception as exc:
            reader.close()
            self._kill_proc(proc)
            if not self._warned:
                self._warned = True
                self._log(f"[audio] no audio output available: {exc}")
            return False
        with self._lock:
            self._proc, self._stream, self._reader = proc, stream, reader
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
            proc, stream, reader = self._proc, self._stream, self._reader
            self._proc = self._stream = self._reader = None
        # kill ffmpeg first: the reader thread's pending read then returns
        if reader is not None:
            reader.close()
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
