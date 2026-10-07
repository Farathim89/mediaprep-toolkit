"""MediaPrep Toolkit - intro / credits detection accuracy suite (frame-exact).

    python tools/accuracy_test.py --episodes <folder> --gt <ground_truth.json>
                                  [--work <temp dir>] [--templates <dir>]
                                  [--cases 1,2,3,4,5] [--quick]

Runs the engine's detectors on real episodes and compares every detected
start / end with the ground truth as FRAME indices (each file's own frame
timestamps, end exclusive):

  1  every detector (template match, Plex-style scan, recurring, Templates ->
     Auto-detect season path) x every mode (audio / visual / both) x both
     margin settings (template 'follow' + template-free margin 1; template
     'exact' + margin 0)
  2  cross-template: templates cut from each other episode at ground truth
     +-1 frame, detected on the remaining episodes
  3  robustness copies of one episode (shifted, 720p H.264, 25 fps, OP audio
     replaced, black padding, OP cut out, unrelated content)
  4  determinism: each detector 3x - identical results
  5  join check: a real cut (run_manual) -> no intro / credits frame survives
     at a join, the frames on both sides are the expected episode frames,
     no audio dropout at the joins
  6  silence / black edges (synthetic copies): a silent gap with a picture
     cut inside -> the cut; a silent gap over black frames with no cut ->
     flagged approximate ('fade' / 'audio') and inside the gap; the episode
     fading to black right before the OP -> the OP's own first frame
     (margin-adjusted) .. its first non-black frame
  Cases 1 / 2 also require every edge to be picture-placed (edge_src
  'visual') - in Audio mode too (audio only finds the segment coarsely).

ground_truth.json: {"episodes": {"<id>": {"file": "<name in --episodes>",
    "intro": {"start_frame": a, "end_frame_excl": b},
    "credits": {"start_frame": c, "end_frame_excl": d}}, ...}}
--templates: a folder with intro/ and credits/ sub-folders of hand-cut
template clips (optional; their margins are measured against the episode
they were cut from).

Everything (code copy, temp files, copies, cut outputs, report) goes to
--work (default: <system temp>/mp_accuracy); nothing is written next to the
media or into the project (the engine runs from a copy of app/ with
MEDIAPREP_NO_MIGRATE=1, so its Data/temp live under --work too).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

FPS_NUM, FPS_DEN = 24000, 1001
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MODES = ("audio", "visual", "both")
KINDS = ("intro", "credits")


# ------------------------------------------------------------------ helpers
def sh(cmd, timeout=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, creationflags=NOWIN, timeout=timeout)


# gentle on the machine: few threads per ffmpeg, NVENC for the copies,
# below-normal priority (inherited by every ffmpeg the engine starts), one
# step at a time with short pauses
THREADS = ["-threads", "4"]
PAUSE = 2.0
NV = lambda cq: ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(cq),
                 "-b:v", "0", "-pix_fmt", "yuv420p"]


def low_priority():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
        except Exception:
            pass
    else:
        try:
            os.nice(5)
        except Exception:
            pass


def ffmpeg(args, label=""):
    time.sleep(PAUSE)
    args = list(args)
    out = args.pop()
    p = sh(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-filter_threads", "2"]
           + THREADS + args + THREADS + [out])
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({label}): "
                           + p.stderr.decode("utf-8", "replace")[-800:])


_PTS = {}


def frame_pts(path):
    """Every video frame's time on the app timeline (pts - container start),
    sorted - from the packets (no decoding)."""
    key = (os.path.abspath(path), os.path.getmtime(path))
    if key in _PTS:
        return _PTS[key]
    p = sh(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of",
            "csv=p=0", path])
    try:
        st = float(p.stdout.decode().strip() or 0.0)
    except ValueError:
        st = 0.0
    p = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "packet=pts_time", "-of", "csv=p=0", path])
    t = [float(x) for x in p.stdout.decode().split() if x.strip() not in ("", "N/A")]
    a = np.array(sorted(t)) - st
    _PTS[key] = a
    return a


def t2f(path, t):
    """Frame index of time t (nearest frame start); past the last frame =
    the frame count (end exclusive)."""
    pts = frame_pts(path)
    if t is None:
        return None
    if t > pts[-1] + 0.5 * float(np.median(np.diff(pts[-50:]))):
        return len(pts)
    return int(np.argmin(np.abs(pts - t)))


def decode_gray(path, t0, t1, w=48, h=27):
    """(times, frames[N, w*h] float) of [t0, t1] at native fps."""
    t0 = max(0.0, t0)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info"]
    if t0 > 0:
        cmd += ["-ss", f"{t0:.3f}"]
    cmd += THREADS + ["-i", path, "-t", f"{t1 - t0:.3f}", "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"scale={w}:{h}:flags=area,format=gray,showinfo", "-fps_mode",
            "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = sh(cmd)
    times = [float(m) + t0 for m in re.findall(
        r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?[0-9.]+)", p.stderr.decode("utf-8", "replace"))]
    n = min(len(times), len(p.stdout) // (w * h))
    fr = np.frombuffer(p.stdout[:n * w * h], dtype=np.uint8).reshape(n, w * h).astype(np.float32)
    return np.array(times[:n]), fr


def fsim(a, b):
    """1.0 = same picture. Flat frames compare by brightness."""
    sa, sb = a.std(), b.std()
    if sa < 6 or sb < 6:
        return 1.0 if (sa < 6 and sb < 6 and abs(a.mean() - b.mean()) < 8) else 0.0
    x, y = (a - a.mean()) / sa, (b - b.mean()) / sb
    return float((x * y).mean())


class UI:
    def __init__(self):
        self.lines = []

    def log(self, m):
        self.lines.append(m)

    def status(self, t):
        pass

    def progress(self, f, t=""):
        pass


# ------------------------------------------------------------------ suite
class Suite:
    def __init__(self, a):
        self.a = a
        self.work = os.path.abspath(a.work)
        os.makedirs(self.work, exist_ok=True)
        self.gt = json.load(open(a.gt, encoding="utf-8"))["episodes"]
        self.ids = sorted(self.gt)
        self.files = {e: os.path.join(a.episodes, self.gt[e]["file"]) for e in self.ids}
        self.rows = []
        self.es = {}                 # (path, kind) -> edge_src of the last run
        self.need_visual = False     # cases 1 / 2: every edge must be picture-placed
        self.dets = set(a.detectors.split(","))
        self.logf = open(os.path.join(self.work, "engine.log"), "w", encoding="utf-8")
        self._load_engine(a.code)

    # the engine runs from a copy (its Data / temp live under APP_ROOT)
    def _load_engine(self, code):
        src = os.path.join(code, "app")
        dst = os.path.join(self.work, "code")
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, os.path.join(dst, "app"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.makedirs(os.path.join(dst, "Data", "temp"), exist_ok=True)
        os.environ["MEDIAPREP_NO_MIGRATE"] = "1"
        sys.path.insert(0, dst)
        from app.engine import cut, detect, plexscan, recurring, vfp
        self.detect, self.plexscan, self.recurring, self.vfp, self.cut = (
            detect, plexscan, recurring, vfp, cut)

    def L(self, m):
        self.logf.write(m + "\n")
        self.logf.flush()

    def say(self, m):
        print(m, flush=True)
        self.L("## " + m)

    # ---------------- ground truth per file
    def gt_frames(self, e, kind):
        g = self.gt[e][kind]
        return int(g["start_frame"]), int(g["end_frame_excl"])

    # ---------------- detectors -> {path: {kind: (s, e) | None}}
    def run_template(self, paths, tpl_dir, mode, tmode, margin):
        time.sleep(PAUSE)
        cfg = {"intro_dir": os.path.join(tpl_dir, "intro"),
               "credits_dir": os.path.join(tpl_dir, "credits"),
               "preintro_dir": os.path.join(tpl_dir, "preintro"),
               "aftercredits_dir": os.path.join(tpl_dir, "aftercredits"),
               "confidence": 0.32, "detect_mode": mode, "template_margin": tmode,
               "margin_frames": margin}
        tpls = self.detect.load_templates_for(cfg, self.L)
        out, tm = {}, {}
        for p in paths:
            t0 = time.time()
            r = self.detect.detect_segments(p, cfg, log=self.L, templates=tpls)
            out[p] = {}
            for k in KINDS:
                b = next((c for c in r.get(k, []) if c["ok"]), None)
                out[p][k] = (b["start"], b["end"]) if b else None
                self.es[(p, k)] = (b or {}).get("edge_src")
            tm[p] = time.time() - t0
        return out, tm

    def run_plex(self, paths, mode, margin):
        time.sleep(PAUSE)
        t0 = time.time()
        r = self.plexscan.scan_season(paths, {"intro_mode": mode, "credits_mode": mode,
                                              "margin_frames": margin}, log=self.L)
        dt = time.time() - t0
        for p in paths:
            for k in KINDS:
                self.es[(p, k)] = r[p].get(k + "_edge_src")
        return ({p: {k: r[p][k] for k in KINDS} for p in paths},
                {p: dt / len(paths) for p in paths})

    def run_recurring(self, paths, mode, margin, kinds=KINDS):
        time.sleep(PAUSE)
        t0 = time.time()
        cl = self.recurring.detect_recurring(
            paths, mode=mode, kinds=kinds, window=420.0,
            min_lens={"intro": 10.0, "credits": 10.0, "preintro": 4.0, "aftercredits": 4.0},
            thresh=0.8, log=self.L, margin_frames=margin)
        dt = time.time() - t0
        out = {p: {} for p in paths}
        for k in KINDS:
            ck = [c for c in cl if c["kind"] == k]
            pe = self.vfp.per_episode(ck)
            for p in paths:
                v = pe.get(p)
                out[p][k] = (v[0], v[1]) if v else None
                self.es[(p, k)] = None
                for c in ck:
                    if v and tuple(c["ranges"].get(p, ())[:2]) == (v[0], v[1]):
                        self.es[(p, k)] = (c.get("edge_src") or {}).get(p)
        return out, {p: dt / len(paths) for p in paths}

    # ---------------- scoring
    def score(self, case, det, mode, setting, path, label, kind, got, exp, tol=0, timing=None,
              note=""):
        """exp: (s, e) frame indices or None (= must NOT be found)."""
        gs = ge = None
        es = self.es.get((path, kind))
        if got:
            gs, ge = t2f(path, got[0]), t2f(path, got[1])
        if exp is None:
            ok = got is None
            err = "-" if ok else "FALSE POSITIVE"
        elif got is None:
            ok, err = False, "MISS"
        else:
            ds, de = gs - exp[0], ge - exp[1]
            ok = abs(ds) <= tol and abs(de) <= tol
            err = f"{ds:+d}/{de:+d}"
            if self.need_visual and es and any(v not in ("visual", "file")
                                               for v in es.values()):
                ok = False
                err += " (edge not picture-placed)"
        row = {"case": case, "detector": det, "mode": mode, "setting": setting, "file": label,
               "kind": kind, "expected": exp, "got": (gs, ge) if got else None, "err": err,
               "pass": ok, "tol": tol, "time": round(timing, 1) if timing else None,
               "note": note, "edge_src": es}
        self.rows.append(row)
        print(f"  [{'PASS' if ok else 'FAIL'}] {case:4s} {det:9s} {mode:6s} {setting:14s} "
              f"{label:14s} {kind:7s} exp {exp} got {row['got']} err {err}"
              + (f" [{es.get('start', '?')}/{es.get('end', '?')}]" if es else "")
              + (f" tol {tol}" if tol else "") + (f"  {note}" if note else ""), flush=True)
        return ok

    # ---------------- template origin (hand-cut templates)
    def template_origin(self, tpl, kind):
        """(episode id, first frame index, frame count) of a template clip cut
        from one of the ground-truth episodes, or None."""
        n = len(frame_pts(tpl))
        tt, tf = decode_gray(tpl, 0.0, 0.5, 96, 54)
        best = None
        for e in self.ids:
            s, _ = self.gt_frames(e, kind)
            pts = frame_pts(self.files[e])
            a = pts[max(0, s - 15)]
            et, ef = decode_gray(self.files[e], a, a + 1.6, 96, 54)
            if len(ef) < 8 or len(tf) < 6:
                continue
            for off in range(0, len(ef) - 6):
                d = float(np.mean([np.abs(ef[off + j] - tf[j]).mean() for j in range(6)]))
                if best is None or d < best[0]:
                    best = (d, e, int(np.argmin(np.abs(pts - et[off]))))
        if best is None or best[0] > 6.0:
            return None
        return best[1], best[2], n

    # ---------------- case 1
    def case1(self, tpl_sets):
        paths = [self.files[e] for e in self.ids]
        lab = {self.files[e]: f"E{e}" for e in self.ids}
        settings = [("follow", 1), ("exact", 0)]
        for tname, tdir, origin in (tpl_sets if "template" in self.dets else ()):
            for mode in MODES:
                for tmode, m in settings:
                    self.say(f"case 1: template '{tname}' {mode} {tmode}")
                    res, tm = self.run_template(paths, tdir, mode, tmode, m)
                    for e in self.ids:
                        p = self.files[e]
                        for k in KINDS:
                            exp = self.expect_tpl(e, k, origin.get(k), tmode)
                            self.score("1", "template", mode, f"{tname}/{tmode}", p, lab[p], k,
                                       res[p][k], exp, timing=tm[p])
        for mode in MODES:
            for _tmode, m in settings:
                if "plex" not in self.dets:
                    break
                self.say(f"case 1: plex {mode} margin {m}")
                res, tm = self.run_plex(paths, mode, m)
                for e in self.ids:
                    p = self.files[e]
                    for k in KINDS:
                        s, en = self.gt_frames(e, k)
                        self.score("1", "plex", mode, f"margin {m}", p, lab[p], k, res[p][k],
                                   (s - m, en + m), timing=tm[p])
        for mode in MODES:
            for _tmode, m in settings:
                if "recurring" not in self.dets:
                    break
                self.say(f"case 1: recurring {mode} margin {m}")
                res, tm = self.run_recurring(paths, mode, m)
                for e in self.ids:
                    p = self.files[e]
                    for k in KINDS:
                        s, en = self.gt_frames(e, k)
                        self.score("1", "recurring", mode, f"margin {m}", p, lab[p], k,
                                   res[p][k], (s - m, en + m), timing=tm[p])
        for mode in MODES:
            for _tmode, m in settings:
                if "autodetect" not in self.dets or self.a.quick:
                    break
                self.say(f"case 1: autodetect (season, all kinds) {mode} margin {m}")
                res, tm = self.run_recurring(paths, mode, m,
                                             kinds=("preintro", "intro", "credits",
                                                    "aftercredits"))
                for e in self.ids:
                    p = self.files[e]
                    for k in KINDS:
                        s, en = self.gt_frames(e, k)
                        self.score("1", "autodetect", mode, f"margin {m}", p, lab[p], k,
                                   res[p][k], (s - m, en + m), timing=tm[p])

    def expect_tpl(self, e, kind, origin, tmode, margin=1):
        """Expected frames of a template match in episode e. origin = (src
        episode, first frame, count) of the template; 'follow' keeps the
        template's own margins (measured in its source episode)."""
        s, en = self.gt_frames(e, kind)
        if origin is not None and origin[0] == e and tmode != "follow":
            # the template's source episode: its margin frames ARE this
            # episode's frames, so the content walk can't tell them from
            # the segment - the content edge is the template's own extent
            s, en = origin[1], origin[1] + origin[2]
        if tmode == "exact":
            return s, en
        if tmode == "exact+margin":
            return s - margin, en + margin
        if origin is None:
            return s, en
        se, f0, n = origin
        gs, ge = self.gt_frames(se, kind)
        return s - (gs - f0), en + (f0 + n - ge)

    # ---------------- case 2: templates cut from each episode at GT +-1
    def make_template(self, e, kind, margin, out):
        p = self.files[e]
        s, en = self.gt_frames(e, kind)
        a, b = s - margin, en + margin
        pts = frame_pts(p)
        t0 = pts[a] - 0.25 * (pts[a + 1] - pts[a])
        if not os.path.isfile(out):
            ffmpeg(["-ss", f"{t0:.4f}", "-i", p, "-frames:v", str(b - a), "-map", "0:v:0",
                    "-map", "0:a:0", *NV(16), "-c:a", "aac", "-b:a", "192k",
                    "-t", f"{pts[b] - pts[a]:.4f}", out + ".part.mkv"], f"template {e} {kind}")
            os.replace(out + ".part.mkv", out)
        n = len(frame_pts(out))
        if n != b - a:
            raise RuntimeError(f"template {out}: {n} frames, wanted {b - a}")
        return (e, a, n)

    def case2(self):
        for src in self.ids:
            d = os.path.join(self.work, f"tpl_E{src}")
            origin = {}
            for k in KINDS:
                os.makedirs(os.path.join(d, k), exist_ok=True)
                origin[k] = self.make_template(src, k, 1, os.path.join(d, k, f"E{src}_{k}.mkv"))
            for k in ("preintro", "aftercredits"):
                os.makedirs(os.path.join(d, k), exist_ok=True)
            others = [e for e in self.ids if e != src]
            paths = [self.files[e] for e in others]
            for mode in MODES:
                self.say(f"case 2: template from E{src} (GT +-1) {mode} follow")
                res, tm = self.run_template(paths, d, mode, "follow", 1)
                for e in others:
                    p = self.files[e]
                    for k in KINDS:
                        exp = self.expect_tpl(e, k, origin[k], "follow")
                        self.score("2", "template", mode, f"E{src}+-1/follow", p, f"E{e}", k,
                                   res[p][k], exp, timing=tm[p])

    # ---------------- case 3: robustness copies
    def _variant(self, name, build, ext=".mkv"):
        out = os.path.join(self.work, "variants", name + ext)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if not os.path.isfile(out):
            self.say(f"building {name}")
            build(out + ".part" + ext)
            os.replace(out + ".part" + ext, out)
        return out

    def case3(self, tpl_dir, origin):
        e = self.a.robust_ep if self.a.robust_ep in self.ids else self.ids[len(self.ids) // 2]
        p = self.files[e]
        pts = frame_pts(p)
        n_all = len(pts)
        f = lambda t: int(np.searchsorted(pts, t - 1e-6))       # first frame at / after t
        F1, F2 = f(360.0), f(pts[-1] - 180.0)
        i0, i1 = self.gt_frames(e, "intro")
        c0, c1 = self.gt_frames(e, "credits")
        assert i1 < F1 and c0 > F2

        def tfr(a):          # frame index -> time (one past the end = its end)
            return pts[a] if a < n_all else pts[-1] + (pts[-1] - pts[-2])

        def concat_build(pieces, out, vf_extra="", af_extra="", vcodec=None, rate=None,
                         abitrate="192k"):
            """pieces: ('src', a, b) frame ranges of p / ('black', n) black +
            silence / ('srcaudio', a, b, ta) video a..b with audio from time ta."""
            fc, n = [], 0
            for pc in pieces:
                if pc[0] == "black":
                    d = pc[1] * FPS_DEN / FPS_NUM
                    fc.append(f"color=black:s=1920x1080:r={FPS_NUM}/{FPS_DEN}:d={d:.6f},"
                              f"format=yuv420p,setsar=1[v{n}]")
                    fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.6f}[a{n}]")
                else:
                    a, b = pc[1], pc[2]
                    fc.append(f"[0:v:0]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS,"
                              f"format=yuv420p,setsar=1[v{n}]")
                    ta = tfr(a) if pc[0] == "src" else pc[3]
                    tb = ta + (tfr(b) - tfr(a))
                    fc.append(f"[0:a:0]atrim=start={ta:.6f}:end={tb:.6f},asetpts=PTS-STARTPTS,"
                              f"aformat=sample_rates=48000:channel_layouts=stereo[a{n}]")
                n += 1
            fc.append("".join(f"[v{i}][a{i}]" for i in range(n))
                      + f"concat=n={n}:v=1:a=1[vc][ac]")
            fc.append(f"[vc]{vf_extra or 'null'}[vo]")
            fc.append(f"[ac]{af_extra or 'anull'}[ao]")
            args = ["-i", p, "-filter_complex", ";".join(fc), "-map", "[vo]", "-map", "[ao]"]
            args += vcodec or NV(18)
            args += ["-c:a", "aac", "-b:a", abitrate]
            if rate:
                args += ["-r", rate]
            ffmpeg(args + [out], os.path.basename(out))

        base = [("src", 0, F1), ("src", F2, n_all)]
        shift_c = lambda x: F1 + (x - F2)                       # credits index in a base copy
        variants = []
        # a. shifted: the first 7.3 s dropped
        S = f(7.3)
        va = self._variant(f"E{e}_a_shift7.3", lambda o: concat_build(
            [("src", S, F1), ("src", F2, n_all)], o))
        variants.append(("a shifted -7.3s", va, (i0 - S, i1 - S),
                         (shift_c(c0) - S, shift_c(c1) - S), 0, ()))
        # b. different release: 720p H.264 CRF 26, AAC 128k
        vb = self._variant(f"E{e}_b_720p", lambda o: concat_build(
            base, o, vf_extra="scale=1280:720:flags=bicubic",
            vcodec=NV(26), abitrate="128k"))
        variants.append(("b 720p h264", vb, (i0, i1), (shift_c(c0), shift_c(c1)), 0, ()))
        # h. MP4 with a 1/24000 time base: rational frame times (4.170833 s),
        # while the detectors report milliseconds
        vh = self._variant(f"E{e}_h_mp4", lambda o: concat_build(
            base, o, vf_extra="scale=1280:720:flags=bicubic",
            vcodec=NV(24) + ["-video_track_timescale", "24000"], abitrate="160k"), ext=".mp4")
        variants.append(("h mp4 1/24000", vh, (i0, i1), (shift_c(c0), shift_c(c1)), 0, ()))
        # c. 25 fps PAL speed-up
        k25 = FPS_NUM / FPS_DEN / 25.0
        vc = self._variant(f"E{e}_c_25fps", lambda o: concat_build(
            base, o, vf_extra="setpts=N/25/TB", af_extra=f"atempo={1 / k25:.8f}",
            rate="25"))
        variants.append(("c 25fps", vc, (i0, i1), (shift_c(c0), shift_c(c1)), 1, ()))
        # d. the OP's audio replaced with other episode audio (dialogue)
        ta_other = 600.0
        vd = self._variant(f"E{e}_d_opaudio", lambda o: concat_build(
            [("src", 0, i0), ("srcaudio", i0, i1, ta_other), ("src", i1, F1),
             ("src", F2, n_all)], o))
        variants.append(("d OP audio replaced", vd, (i0, i1), (shift_c(c0), shift_c(c1)), 0,
                         ("audio",)))
        # e. 0.5 s black + silence before the OP (12 frames)
        ve = self._variant(f"E{e}_e_blackpad", lambda o: concat_build(
            [("src", 0, i0), ("black", 12), ("src", i0, F1), ("src", F2, n_all)], o))
        variants.append(("e black pad", ve, (i0 + 12, i1 + 12),
                         (shift_c(c0) + 12, shift_c(c1) + 12), 0, ()))
        # f. the OP cut out
        vf = self._variant(f"E{e}_f_noop", lambda o: concat_build(
            [("src", 0, i0), ("src", i1, F1), ("src", F2, n_all)], o))
        dop = i1 - i0
        variants.append(("f OP removed", vf, None, (shift_c(c0) - dop, shift_c(c1) - dop), 0,
                         ()))
        # g. unrelated content: another episode's middle (no OP / ED), and a test pattern
        other = next(x for x in self.ids if x != e)
        po = self.files[other]
        ptso = frame_pts(po)
        a0 = int(np.searchsorted(ptso, 400.0))
        a1 = int(np.searchsorted(ptso, 700.0))
        vg1 = self._variant(f"E{other}_g_middle", lambda o: ffmpeg(
            ["-i", po, "-filter_complex",
             f"[0:v:0]trim=start_frame={a0}:end_frame={a1},setpts=PTS-STARTPTS,format=yuv420p[v];"
             f"[0:a:0]atrim=start={ptso[a0]:.6f}:end={ptso[a1]:.6f},asetpts=PTS-STARTPTS[a]",
             "-map", "[v]", "-map", "[a]", *NV(20),
             "-c:a", "aac", "-b:a", "160k", o], "g1"))
        variants.append(("g1 other content", vg1, None, None, 0, ()))
        vg2 = self._variant("g_testsrc", lambda o: ffmpeg(
            ["-f", "lavfi", "-i", f"testsrc2=s=1280x720:r={FPS_NUM}/{FPS_DEN}:d=300",
             "-f", "lavfi", "-i", "sine=f=440:r=48000:d=300", *NV(23), "-c:a", "aac", "-b:a", "128k",
             "-shortest", o], "g2"))
        variants.append(("g2 test pattern", vg2, None, None, 0, ()))

        partners = [self.files[x] for x in self.ids if x != e]
        for label, v, ei, ec, tol, may_fail in variants:
            self.say(f"case 3: {label}")
            exp = {"intro": ei, "credits": ec}
            for mode in MODES:
                res, tm = self.run_template([v], tpl_dir, mode, "follow", 1)
                for k in KINDS:
                    x = exp[k]
                    if x is not None:
                        x = self.expect_tpl_variant(e, k, x, origin.get(k))
                    note = "(audio may fail: expected)" if mode in may_fail else ""
                    self.score("3", "template", mode, "follow", v, label[:14], k, res[v][k], x,
                               tol=tol, timing=tm[v], note=note)
            if label.startswith("g2"):
                continue                         # a test pattern has no season to scan with
            for mode in (MODES if not self.a.quick else ("both",)):
                res, tm = self.run_plex([v] + partners, mode, 1)
                for k in KINDS:
                    x = exp[k]
                    if x is not None:
                        x = (x[0] - 1, x[1] + 1)
                    note = "(audio may fail: expected)" if mode in may_fail else ""
                    self.score("3", "plex", mode, "margin 1", v, label[:14], k, res[v][k], x,
                               tol=tol, timing=tm[v], note=note)
        return vb

    def expect_tpl_variant(self, e, kind, x, origin):
        """Ground-truth frames x of a copy -> follow-mode expectation."""
        s, en = self.gt_frames(e, kind)
        fs, fe = self.expect_tpl(e, kind, origin, "follow")
        return x[0] + (fs - s), x[1] + (fe - en)

    # ---------------- case 6: silence / black at the boundary
    def case6(self, tpl_dir, origin):
        e = self.a.robust_ep if self.a.robust_ep in self.ids else self.ids[len(self.ids) // 2]
        p = self.files[e]
        pts = frame_pts(p)
        i0, i1 = self.gt_frames(e, "intro")
        F1 = int(np.searchsorted(pts, 480.0 - 1e-6))
        assert i1 < F1
        fd = FPS_DEN / FPS_NUM
        tf = lambda a: pts[a]
        # the OP's first non-black frame
        st, sf = decode_gray(p, pts[i0] - 0.02, pts[i0 + 12])
        nb = next(int(np.argmin(np.abs(pts - t))) for t, f in zip(st, sf)
                  if t >= pts[i0] - 0.01 and f.std() >= 6.0)
        self.say(f"case 6: E{e} OP frames {i0}..{i1}, first non-black OP frame {nb}")
        enc = NV(18) + [
               "-c:a", "aac", "-b:a", "192k"]

        def build_gapcut(o):
            # the episode's last 12 frames before the OP go silent: a silent
            # gap (with the OP's own ~0.5 s of silence) and the picture cut
            # inside it
            ffmpeg(["-i", p, "-filter_complex",
                    f"[0:v:0]trim=end_frame={F1},setpts=PTS-STARTPTS,format=yuv420p[v];"
                    f"[0:a:0]atrim=end={tf(F1):.6f},asetpts=PTS-STARTPTS,volume=enable="
                    f"'between(t,{tf(i0 - 12):.4f},{tf(i0) - 0.001:.4f})':volume=0[a]",
                    "-map", "[v]", "-map", "[a]"] + enc + [o], "gapcut")

        def build_blackgap(o, silent):
            # the episode fades to black over its last 12 frames before the
            # OP, then (silent=True) 24 black frames of silence: no picture
            # cut anywhere in the gap; silent=False: the fade only, sound
            # unchanged
            d = 24 * fd
            fc = [f"[0:v:0]trim=end_frame={i0},setpts=PTS-STARTPTS,format=yuv420p,"
                  f"fade=t=out:start_frame={i0 - 12}:nb_frames=12[v0]",
                  f"[0:v:0]trim=start_frame={i0}:end_frame={F1},setpts=PTS-STARTPTS,"
                  f"format=yuv420p[v2]"]
            a0 = (f"[0:a:0]atrim=end={tf(i0):.6f},asetpts=PTS-STARTPTS,"
                  f"aformat=sample_rates=48000:channel_layouts=stereo")
            if silent:
                a0 += f",volume=enable='gte(t,{tf(i0 - 12):.4f})':volume=0"
            fc.append(a0 + "[a0]")
            fc.append(f"[0:a:0]atrim=start={tf(i0):.6f}:end={tf(F1):.6f},asetpts=PTS-STARTPTS,"
                      f"aformat=sample_rates=48000:channel_layouts=stereo[a2]")
            if silent:
                fc.append(f"color=black:s=1920x1080:r={FPS_NUM}/{FPS_DEN}:d={d:.6f},"
                          f"format=yuv420p[v1]")
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.6f}[a1]")
                fc.append("[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]")
            else:
                fc.append("[v0][a0][v2][a2]concat=n=2:v=1:a=1[v][a]")
            ffmpeg(["-i", p, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]"]
                   + enc + [o], "blackgap")

        tpl_s = self.expect_tpl(e, "intro", origin.get("intro"), "follow")[0] - i0
        cases = [
            ("6.2 gap+cut", self._variant(f"E{e}_s2_gapcut", build_gapcut), 0,
             lambda s, m: s == i0 + m, ("visual",), f"= OP start {i0} (cut in the gap)"),
            ("6.3 black gap", self._variant(f"E{e}_s3_blackgap",
                                            lambda o: build_blackgap(o, True)), 24,
             lambda s, m: i0 - 12 + m <= s <= nb + 24 + 1, ("fade", "audio"),
             "inside the gap, flagged approximate"),
            ("6.4 black fade", self._variant(f"E{e}_s4_fade",
                                             lambda o: build_blackgap(o, False)), 0,
             lambda s, m: i0 + m <= s <= nb, None,
             f"OP start {i0} (margin-adjusted) .. first non-black {nb}"),
        ]
        partners = [self.files[x] for x in self.ids if x != e]
        for label, v, shift, ok_fn, kinds, why in cases:
            self.say(f"case {label}")
            runs = [("template", m, "follow", tpl_s) for m in MODES]
            runs += [("plex", m, "margin 1", -1) for m in ("audio", "both")]
            for det, mode, setting, m in runs:
                if det == "template":
                    res, tm = self.run_template([v], tpl_dir, mode, "follow", 1)
                else:
                    res, tm = self.run_plex([v] + partners, mode, 1)
                got = res[v]["intro"]
                es = self.es.get((v, "intro")) or {}
                gs = t2f(v, got[0]) if got else None
                ok = gs is not None and ok_fn(gs, m)
                if ok and kinds:
                    ok = es.get("start") in kinds
                ge = t2f(v, got[1]) if got else None
                exp_e = i1 + shift + (1 if det == "plex" else
                                      self.expect_tpl(e, "intro", origin.get("intro"),
                                                      "follow")[1] - i1)
                ok = ok and ge == exp_e
                row = {"case": label, "detector": det, "mode": mode, "setting": setting,
                       "file": f"E{e} synthetic", "kind": "intro start",
                       "expected": f"{why}" + (f", edge_src in {kinds}" if kinds else "")
                       + f"; end {exp_e}",
                       "got": (gs, ge), "err": "-" if ok else "out of range", "pass": ok,
                       "tol": 0, "time": round(tm[v], 1), "note": "", "edge_src": es}
                self.rows.append(row)
                print(f"  [{'PASS' if ok else 'FAIL'}] {label} {det:9s} {mode:6s} start {gs} "
                      f"end {ge} edge_src {es}  expected: {row['expected']}", flush=True)

    # ---------------- case 4: determinism
    def case4(self, tpl_dir):
        paths = [self.files[x] for x in self.ids]
        e = self.ids[len(self.ids) // 2]
        runs = {"template": lambda: self.run_template([self.files[e]], tpl_dir, "both",
                                                      "follow", 1)[0],
                "plex": lambda: self.run_plex(paths, "both", 1)[0],
                "recurring": lambda: self.run_recurring(paths, "both", 1)[0]}
        for name, fn in runs.items():
            if name not in self.dets:
                continue
            self.say(f"case 4: determinism {name} x3")
            got = [fn() for _ in range(3)]
            same = all(g == got[0] for g in got[1:])
            diff = "" if same else json.dumps([{os.path.basename(p): v for p, v in g.items()}
                                               for g in got])[:600]
            self.rows.append({"case": "4", "detector": name, "mode": "both", "setting": "x3",
                              "file": "all", "kind": "-", "expected": "identical",
                              "got": "identical" if same else "DIFFERENT", "err": diff,
                              "pass": same, "tol": 0, "time": None, "note": ""})
            print(f"  [{'PASS' if same else 'FAIL'}] 4    {name:9s} 3 runs identical={same} {diff}",
                  flush=True)

    # ---------------- case 5: real cut + join check
    def case5(self, tpl_dir, origin, extra=()):
        e = self.ids[len(self.ids) // 2]
        jobs = [(f"E{e}", self.files[e], e, None)] + list(extra)
        for label, src, ge, xmap in jobs:
            self.say(f"case 5: cut + join check {label}")
            res, _ = self.run_template([src], tpl_dir, "both", "follow", 1)
            r = res[src]
            if not (r["intro"] and r["credits"]):
                self.rows.append({"case": "5", "detector": "cut", "mode": "both",
                                  "setting": "follow", "file": label, "kind": "-",
                                  "expected": "intro+credits", "got": r, "err": "MISS",
                                  "pass": False, "tol": 0, "time": None, "note": ""})
                continue
            self.join_check(label, src, ge, xmap, r)

    def join_check(self, label, src, ge, xmap, r):
        pts = frame_pts(src)
        dur = float(pts[-1] + (pts[-1] - pts[-2]))
        keep = self.cut.compute_keep_segments(dur, intro=r["intro"], credits=r["credits"])
        out_dir = os.path.join(self.work, "cut_" + re.sub(r"\W+", "_", label))
        os.makedirs(out_dir, exist_ok=True)
        cfg = {"video": src, "output_dir": out_dir, "encoder": "h264_nvenc", "crf": 20,
               "crf_h265": 24, "bit_depth": "8", "preset": "veryfast", "kf_interval": 2,
               "subs_langs": [], "move_done": False}
        ui = UI()
        t0 = time.time()
        self.cut.run_manual(cfg, keep, ui, threading.Event())
        out = os.path.join(out_dir, os.path.basename(src))
        for x in ui.lines:
            self.L("[cut] " + x)
        if not os.path.isfile(out):
            self.rows.append({"case": "5", "detector": "cut", "mode": "-", "setting": "-",
                              "file": label, "kind": "-", "expected": "output", "got": None,
                              "err": "no output", "pass": False, "tol": 0, "time": None,
                              "note": ""})
            return
        cut_t = time.time() - t0
        # source frame ranges kept / removed
        ki = [(t2f(src, s), t2f(src, e_)) for s, e_ in keep]
        seg_frames = {k: (t2f(src, r[k][0]), t2f(src, r[k][1])) for k in KINDS}
        # the real intro / credits frames (ground truth, mapped into this file)
        gt = {}
        for k in KINDS:
            s, en = self.gt_frames(ge, k)
            gt[k] = xmap(k, (s, en)) if xmap else (s, en)
        opts = frame_pts(out)
        n_exp = sum(b - a for a, b in ki)
        problems = []
        if abs(len(opts) - n_exp) > 0:
            problems.append(f"frames {len(opts)} vs {n_exp} expected")
        # joins: output frame j <-> source frame
        o2s, j = [], 0
        for a, b in ki:
            o2s.append((j, a, b))
            j += b - a
        joins = [(o2s[i][0], o2s[i - 1][2] - 1, o2s[i][1]) for i in range(1, len(o2s))]
        details = []
        for oj, s_before, s_after in joins:
            # output frames oj-3 .. oj+2 and the source frames they should be
            ot0 = opts[max(0, oj - 3)] - 0.02
            ot1 = opts[min(len(opts) - 1, oj + 3)] + 0.02
            ot, of = decode_gray(out, ot0, ot1)
            oi = [int(np.argmin(np.abs(opts - t))) for t in ot]
            # candidate source frames: around both sides of the join + the
            # removed segment's edges
            cand = {}
            for c in (s_before, s_after):
                a, b = max(0, c - 6), min(len(pts) - 1, c + 6)
                st, sf = decode_gray(src, pts[a] - 0.02, pts[b] + 0.05)
                for t, fr in zip(st, sf):
                    cand[int(np.argmin(np.abs(pts - t)))] = fr
            for k in KINDS:
                s, en = gt[k]
                for c in (s, en - 1):
                    if abs(c - s_before) < 40 or abs(c - s_after) < 40:
                        a, b = max(0, c - 3), min(len(pts) - 1, c + 3)
                        st, sf = decode_gray(src, pts[a] - 0.02, pts[b] + 0.05)
                        for t, fr in zip(st, sf):
                            cand[int(np.argmin(np.abs(pts - t)))] = fr
            in_seg = lambda i: any(gt[k][0] <= i < gt[k][1] for k in KINDS)
            seq = []
            for oidx, fr in zip(oi, of):
                if not oj - 3 <= oidx < oj + 3:
                    continue
                want = s_before - (oj - 1 - oidx) if oidx < oj else s_after + (oidx - oj)
                sims = {i: fsim(fr, c) for i, c in cand.items()}
                best = max(sims, key=lambda i: (sims[i], -abs(i - want)))
                ok_w = sims.get(want, 0.0) >= 0.97 and sims[want] >= sims[best] - 0.01
                seg_hit = [i for i, v in sims.items() if in_seg(i) and v >= 0.97
                           and v > sims.get(want, 0.0) + 0.005]
                seq.append((oidx, want, best, round(sims.get(want, 0.0), 3)))
                if in_seg(want):
                    problems.append(f"join@{oj}: output {oidx} is source frame {want} "
                                    "(inside the intro/credits)")
                elif not ok_w:
                    problems.append(f"join@{oj}: output {oidx} looks like source {best} "
                                    f"(sim {sims[best]:.3f}), expected {want} "
                                    f"({sims.get(want, 0.0):.3f})")
                elif seg_hit:
                    problems.append(f"join@{oj}: output {oidx} matches intro/credits frame "
                                    f"{seg_hit[0]} better than the expected {want}")
            details.append({"join_out_frame": oj, "src_before": s_before, "src_after": s_after,
                            "frames": seq})
            # audio: no dropout at the join (envelope vs the source's)
            jt = float(opts[oj])
            drop = self.audio_dropout(out, jt, src, float(pts[s_before + 1])
                                      if s_before + 1 < len(pts) else dur, float(pts[s_after]))
            if drop:
                problems.append(f"join@{oj}: audio {drop}")
        for k in KINDS:
            s, en = gt[k]
            ds, de = seg_frames[k][0] - s, seg_frames[k][1] - en
            details.append({k: {"cut": seg_frames[k], "gt": (s, en), "margin": (-ds, de)}})
        ok = not problems
        self.rows.append({"case": "5", "detector": "cut+join", "mode": "both", "setting": "follow",
                          "file": label, "kind": "joins", "expected": "clean joins",
                          "got": f"{len(joins)} joins", "err": "; ".join(problems)[:900] or "-",
                          "pass": ok, "tol": 0, "time": round(cut_t, 1), "note": "",
                          "details": details})
        print(f"  [{'PASS' if ok else 'FAIL'}] 5    cut+join {label}: {len(joins)} joins, "
              f"removed {seg_frames}, gt {gt}" + (f"  PROBLEMS: {problems}" if problems else ""),
              flush=True)
        for d in details:
            self.L("[join] " + json.dumps(d))

    def audio_dropout(self, out, jt, src, sb_end, sa_start, w=1.0, blk=0.01):
        """'' or a description: output audio around the join vs the source
        audio it was made of ([sb_end - w, sb_end) + [sa_start, sa_start + w))."""
        def pcm(path, t0, d):
            p = sh(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t0):.4f}", "-i", path,
                    "-t", f"{d:.4f}", "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "s16le",
                    "-"])
            return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        o = pcm(out, jt - w, 2 * w)
        s = np.concatenate([pcm(src, sb_end - w, w), pcm(src, sa_start, w)])
        B = int(48000 * blk)
        n = min(len(o), len(s)) // B
        if n < 10:
            return "could not read the audio"
        db = lambda x: 20 * np.log10(np.sqrt((x[:n * B].reshape(n, B) ** 2).mean(axis=1)) + 1e-9)
        eo, es = db(o), db(s)
        bad, run, worst = 0, 0, 0
        for i in range(n):
            ref = es[max(0, i - 3):i + 4].min()      # source loud around here (+-30 ms)
            if eo[i] < -60 and ref > -40:
                run += 1
                worst = max(worst, run)
            else:
                run = 0
        bad = worst
        if bad >= 2:
            return f"dropout {bad * blk * 1000:.0f} ms (silent where the source has sound)"
        return ""

    # ---------------- report
    def report(self):
        path = os.path.join(self.work, "accuracy_report.json")
        json.dump(self.rows, open(path, "w", encoding="utf-8"), indent=1, default=str)
        n_ok = sum(1 for r in self.rows if r["pass"])
        print(f"\n=== {n_ok}/{len(self.rows)} passed  (report: {path})")
        for r in self.rows:
            if not r["pass"]:
                print(f"  FAIL case {r['case']} {r['detector']} {r['mode']} {r['setting']} "
                      f"{r['file']} {r['kind']}: exp {r['expected']} got {r['got']} {r['err']} "
                      f"{r.get('note', '')}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episodes", required=True, help="folder with the episodes")
    ap.add_argument("--gt", required=True, help="ground_truth.json")
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "mp_accuracy"))
    ap.add_argument("--code", default=os.path.dirname(here),
                    help="project folder that contains app/ (copied to --work)")
    ap.add_argument("--templates", default=None,
                    help="folder with intro/ and credits/ hand-cut templates (optional)")
    ap.add_argument("--cases", default="1,2,3,4,5,6")
    ap.add_argument("--robust-ep", default=None, help="episode id for the robustness copies")
    ap.add_argument("--quick", action="store_true", help="fewer mode combinations")
    ap.add_argument("--detectors", default="template,plex,recurring,autodetect",
                    help="cases 1 / 4: which detectors to run")
    a = ap.parse_args()
    low_priority()
    for d in (a.episodes, os.path.dirname(os.path.abspath(a.gt))):
        if os.path.abspath(a.work).lower().startswith(os.path.abspath(d).lower()):
            sys.exit("--work must not be inside the media folders")
    S = Suite(a)
    cases = set(a.cases.split(","))
    # template sets: the hand-cut ones (if given) + one cut by the suite from
    # the first episode at ground truth +-1 frame
    tpl_sets = []
    if a.templates:
        d = os.path.join(S.work, "tpl_user")
        origin = {}
        for k in ("intro", "credits", "preintro", "aftercredits"):
            os.makedirs(os.path.join(d, k), exist_ok=True)
            sd = os.path.join(a.templates, k)
            for fn in (os.listdir(sd) if os.path.isdir(sd) else []):
                dst = os.path.join(d, k, fn)
                src = os.path.join(sd, fn)
                st = os.stat(src)
                if (not os.path.isfile(dst) or os.path.getsize(dst) != st.st_size
                        or abs(os.path.getmtime(dst) - st.st_mtime) > 1):
                    shutil.copy2(src, dst)          # read-only use of the originals
                if k in KINDS:
                    origin[k] = S.template_origin(dst, k)
        S.say(f"hand-cut templates: origin (episode, first frame, frames) = {origin}")
        tpl_sets.append(("user", d, origin))
    first = S.ids[0]
    d = os.path.join(S.work, f"tpl_E{first}")
    origin = {}
    for k in ("intro", "credits", "preintro", "aftercredits"):
        os.makedirs(os.path.join(d, k), exist_ok=True)
    for k in KINDS:
        origin[k] = S.make_template(first, k, 1, os.path.join(d, k, f"E{first}_{k}.mkv"))
    if not tpl_sets:
        tpl_sets.append((f"E{first}+-1", d, origin))
    main_tpl = tpl_sets[0]
    t0 = time.time()
    S.need_visual = True
    if "1" in cases:
        S.case1(tpl_sets)
    if "2" in cases:
        S.case2()
    S.need_visual = False
    if "6" in cases:
        S.case6(main_tpl[1], main_tpl[2])
    if "3" in cases:
        S.case3(main_tpl[1], main_tpl[2])
    if "4" in cases:
        S.case4(main_tpl[1])
    if "5" in cases:
        extra = []
        e = a.robust_ep if a.robust_ep in S.ids else S.ids[len(S.ids) // 2]
        pts = frame_pts(S.files[e])
        F1 = int(np.searchsorted(pts, 360.0 - 1e-6))
        F2 = int(np.searchsorted(pts, pts[-1] - 180.0 - 1e-6))
        xm = lambda k, x: x if k == "intro" else (F1 + x[0] - F2, F1 + x[1] - F2)
        for lab, fn in (("720p copy", f"E{e}_b_720p.mkv"), ("mp4 1/24000", f"E{e}_h_mp4.mp4")):
            vp = os.path.join(S.work, "variants", fn)
            if os.path.isfile(vp):
                extra.append((lab, vp, e, xm))
        S.case5(main_tpl[1], main_tpl[2], extra)
    S.say(f"total {time.time() - t0:.0f}s")
    S.report()


if __name__ == "__main__":
    main()
