"""GUI-less engine self-test for the packaged exe:

    "MediaPrep Toolkit.exe" --selftest <dir>

Generates two tiny episodes (testsrc video + synthetic audio with a known
"intro" jingle) and a template in <dir> (default: Data/temp/selftest), then
checks ffprobe, the bundled packages, template detection (librosa), a
libx264 manual cut, a loudness measure and a silence snap. Writes
<dir>/selftest_report.json and returns 0 when every step passed. Only <dir>
is written to (plus Data/temp scratch files the engine cleans up)."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import wave

SR = 22050


def _write_wav(path, y):
    import numpy as np
    pcm = (np.clip(y, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _jingle(np):
    """6 s of distinctive notes + chirps (the 'intro' to find)."""
    t = np.arange(int(6.0 * SR)) / SR
    y = np.zeros_like(t)
    notes = [523, 659, 784, 1047, 880, 698, 587, 440, 494, 988, 330, 392]
    for i, f in enumerate(notes):
        a, b = int(i * 0.5 * SR), int((i + 1) * 0.5 * SR)
        tt = t[a:b] - t[a]
        y[a:b] += 0.4 * np.sin(2 * np.pi * f * tt) * np.exp(-3 * tt)
        y[a:b] += 0.2 * np.sin(2 * np.pi * (200 + 1500 * tt) * tt)
    return y


def _episode(np, rng, total, intro_at, silence=None):
    y = 0.05 * rng.standard_normal(int(total * SR))
    t = np.arange(len(y)) / SR
    y += 0.1 * np.sin(2 * np.pi * 150 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.3 * t))
    j = _jingle(np)
    a = int(intro_at * SR)
    y[a:a + len(j)] = 0.3 * y[a:a + len(j)] + j
    if silence:
        y[int(silence[0] * SR):int(silence[1] * SR)] = 0.0
    return y


def _mux(wav, mp4, ffmpeg_flags):
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
           "-i", wav, "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", mp4]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=ffmpeg_flags)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-400:])


class _UI:
    def __init__(self):
        self.lines = []

    def log(self, m):
        self.lines.append(str(m))

    def progress(self, *_a):
        pass

    def status(self, *_a):
        pass


def run(out_dir=None):
    from . import config as C
    out_dir = os.path.abspath(out_dir or os.path.join(C.TEMP_DIR, "selftest"))
    os.makedirs(out_dir, exist_ok=True)
    report = {"app": f"{C.APP_NAME} {C.APP_VERSION}", "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "steps": {}}
    t_all = time.time()

    def step(name, fn):
        t0 = time.time()
        try:
            res = fn()
            ok = True if not isinstance(res, dict) else res.pop("_ok", True)
            report["steps"][name] = {"ok": bool(ok), "secs": round(time.time() - t0, 2),
                                     "result": res}
        except Exception:
            report["steps"][name] = {"ok": False, "secs": round(time.time() - t0, 2),
                                     "error": traceback.format_exc()[-1500:]}

    def s_ffprobe():
        r = subprocess.run(["ffprobe", "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, creationflags=C.POPEN_FLAGS)
        return {"_ok": r.returncode == 0, "ffprobe": shutil.which("ffprobe"),
                "ffmpeg": shutil.which("ffmpeg"),
                "version": r.stdout.decode(errors="replace").splitlines()[0]}

    def s_imports():
        out = {}
        import numpy, scipy, librosa, numba, llvmlite, soundfile, sklearn, cv2, PIL
        import sounddevice
        for m in (numpy, scipy, librosa, numba, llvmlite, soundfile, sklearn, cv2, PIL,
                  sounddevice):
            out[m.__name__] = getattr(m, "__version__", "?")
        out["libsndfile"] = soundfile.__libsndfile_version__
        out["portaudio"] = sounddevice.get_portaudio_version()[1]
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        root.withdraw()
        out["tkdnd"] = str(root.TkdndVersion)
        root.destroy()
        return out

    files = {}

    def s_generate():
        import numpy as np
        rng = np.random.default_rng(7)
        tpl_dir = os.path.join(out_dir, "templates", "intro")
        os.makedirs(tpl_dir, exist_ok=True)
        files["tpl_dir"] = tpl_dir
        _write_wav(os.path.join(tpl_dir, "jingle.wav"), _jingle(np))
        for name, total, at, sil in (("ep01", 20.0, 4.0, (15.0, 16.0)),
                                     ("ep02", 24.0, 10.0, None)):
            wav = os.path.join(out_dir, name + ".wav")
            _write_wav(wav, _episode(np, rng, total, at, sil))
            mp4 = os.path.join(out_dir, name + ".mp4")
            _mux(wav, mp4, C.POPEN_FLAGS)
            os.remove(wav)
            files[name] = (mp4, at)
        return {k: v[0] if isinstance(v, tuple) else v for k, v in files.items()}

    detected = {}

    def s_detect():
        from .engine.detect import detect_segments, load_templates_for
        cfg = {"intro_dir": files["tpl_dir"], "use_intro": True, "use_credits": False,
               "use_preintro": False, "use_aftercredits": False, "confidence": 0.5}
        lines = []
        tpls = load_templates_for(cfg, lines.append)
        out, ok = {}, True
        for name in ("ep01", "ep02"):
            video, expect = files[name]
            res = detect_segments(video, cfg, log=lines.append, templates=tpls)
            best = (res.get("intro") or [None])[0]
            hit = bool(best and best["ok"] and abs(best["start"] - expect) < 0.5)
            ok = ok and hit
            out[name] = {"expected_start": expect, "duration": round(res["duration"], 2),
                         "best": best and {k: (round(v, 3) if isinstance(v, float) else v)
                                           for k, v in best.items()},
                         "pass": hit}
            if best:
                detected[name] = (best["start"], best["end"], res["duration"])
        out["_ok"] = ok
        return out

    def s_cut():
        from .engine.cut import run_manual
        video = files["ep01"][0]
        s, e, total = detected.get("ep01", (4.0, 10.0, 20.0))
        keep = [(0.0, s), (e, total)]
        dst = os.path.join(out_dir, "output")
        cfg = {"video": video, "output_dir": dst, "encoder": "libx264",
               "preset": "ultrafast", "crf": 23, "kf_interval": 0, "bit_depth": "auto",
               "subs_langs": None, "move_done": False}
        ui = _UI()
        run_manual(cfg, keep, ui, threading.Event())
        out_file = os.path.join(dst, os.path.basename(video))
        from .engine.probe import probe_duration
        dur = probe_duration(out_file) if os.path.exists(out_file) else None
        expect = sum(b - a for a, b in keep)
        files["cut"] = out_file
        return {"_ok": bool(dur and abs(dur - expect) < 0.6),
                "keep": [[round(a, 2), round(b, 2)] for a, b in keep],
                "output": out_file, "duration": dur, "expected": round(expect, 2),
                "log": ui.lines[-8:]}

    def s_loudness():
        from .engine.loudness import measure_loudness
        lufs, tp = measure_loudness(files.get("cut") or files["ep01"][0])
        return {"_ok": lufs is not None, "lufs": lufs, "true_peak": tp}

    def s_snap():
        from .engine.snap import find_snap
        t, why = find_snap(files["ep01"][0], 15.4, kind="silence", radius=1.0, edge="start")
        return {"_ok": t is not None and abs(t - 16.0) < 0.2, "asked": 15.4,
                "snapped": t, "reason": why, "expected": 16.0}

    step("ffprobe", s_ffprobe)
    step("imports", s_imports)
    step("generate_clips", s_generate)
    if "ep01" in files:
        step("detect_segments", s_detect)
        step("run_manual_libx264", s_cut)
        step("loudness", s_loudness)
        step("snap", s_snap)
    report["all_ok"] = all(v["ok"] for v in report["steps"].values())
    report["total_secs"] = round(time.time() - t_all, 2)
    with open(os.path.join(out_dir, "selftest_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return 0 if report["all_ok"] else 1
