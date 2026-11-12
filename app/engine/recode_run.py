"""Convert engine (engine/recode.py): running a job - encode (.part
output, commit, verify, size line) and make_preview (a 10 s sample + the
source segment + size / quality estimates). No Tk."""
import os
import re
import threading
import time

from .encode import restore_covers
from .files import _commit_part, _part_path
from .formatting import format_seconds, format_size
from .probe import output_problem, probe_video_duration
from .process import run_ffmpeg_with_progress
from .recode import PREVIEW_DIR, _crop_of
from .recode_analyze import _ffmpeg_stderr, ensure_detect, probe_job
from .recode_cmd import build_recode_cmd


# ======================= run =======================
def verify_recode(job, out, plan):
    """Check the written file: track counts, resolution, duration, colour.
    Returns a list of warning strings (empty = OK)."""
    o = probe_job(out)
    if not o:
        return ["could not probe the output"]
    warns = []
    if len(o["audio"]) != plan["audio"]:
        warns.append(f"audio tracks: expected {plan['audio']}, got {len(o['audio'])}")
    if len(o["subs"]) != plan["subs"]:
        warns.append(f"subtitle tracks: expected {plan['subs']}, got {len(o['subs'])}")
    if (o["video"]["w"], o["video"]["h"]) != (plan["w"], plan["h"]):
        warns.append(f"resolution: expected {plan['w']}x{plan['h']}, got "
                     f"{o['video']['w']}x{o['video']['h']}")
    want = plan["duration"]
    got = probe_video_duration(out) or o["duration"]
    if want and got and abs(got - want) > 1.0:
        warns.append(f"duration: expected {want:.2f}s, got {got:.2f}s")
    if plan.get("tonemap") and o["video"]["transfer"] not in ("bt709", ""):
        warns.append(f"colour: expected bt709, got {o['video']['transfer']}")
    return warns


def encode(job, output, progress=None, stop_event=None, log=None):
    """Convert one job into `output` (written as a .part sibling first, put in
    place only on success; Stop / failure removes the partial file).
    progress(frac, text). Returns {"ok", "stopped", "output", "size_in",
    "size_out", "warnings", "elapsed", "error"}."""
    log = log or (lambda m: None)
    stop_event = stop_event or threading.Event()
    t0 = time.time()
    res = {"ok": False, "stopped": False, "output": output, "size_in": job["info"]["size"],
           "size_out": 0, "warnings": [], "elapsed": 0.0, "error": ""}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    ensure_detect(job, log, stop_event)
    if stop_event.is_set():
        res["stopped"] = True
        return res
    part = _part_path(output)
    cmd, plan = build_recode_cmd(job, part, log)
    for n in plan["notes"]:
        log(f"   {n}")
    log(f"   encoder {plan['encoder']}, {plan['w']}x{plan['h']}, "
        f"{plan['audio']} audio / {plan['subs']} subtitle track(s)")
    rc = run_ffmpeg_with_progress(cmd, plan["duration"] or 1.0, fps=plan["fps"],
                                  on_progress=progress, on_log=log, stop_event=stop_event)
    # safety gate: an empty / video-less / far too short output is never put
    # in place (a frame-rate change keeps the length, so the tolerance holds)
    placed = _commit_part(rc, part, output, log,
                          lambda p: output_problem(p, plan["duration"], need_video=True))
    res["elapsed"] = time.time() - t0
    if rc == -1 or stop_event.is_set():
        res["stopped"] = True
        log("   [STOPPED] partial output removed")
        return res
    if not placed:
        res["error"] = (f"ffmpeg failed (code {rc})" if rc != 0
                        else "the output failed the safety check")
        log(f"   [FAIL] {res['error']}")
        return res
    output = res["output"] = placed            # '<name> (new).ext' if output was locked
    if job["settings"].get("attachments", True):     # cover art (only real video is mapped)
        restore_covers(job["input"], output, log)
    res["ok"] = True
    res["size_out"] = os.path.getsize(output)
    warns = verify_recode(job, output, plan)
    res["warnings"] = warns
    if warns:
        for w in warns:
            log(f"   [WARN] verify: {w}")
    else:
        log("   [OK] verify: tracks, resolution and duration as expected")
    si, so = res["size_in"], res["size_out"]
    pct = f" ({so / si * 100:.0f}%)" if si else ""
    log(f"   Size: {format_size(si)} -> {format_size(so)}{pct} - "
        f"{format_seconds(res['elapsed'])}")
    if job["settings"]["cap"] and si and so > si:
        log(f"   [WARN] output is {(so / si - 1) * 100:.1f}% larger than the source")
    return res


# ======================= preview + estimate =======================
def _ssim(sample, src, start, length, plan, job, stop_event=None):
    """SSIM of the sample vs the same source segment (None when the
    pictures can't be lined up: crop / fps / deinterlace changes)."""
    s = job["settings"]
    if (_crop_of(job) or s["fps"] != "source" or s["deint"] != "off"
            or plan.get("tonemap") or any(t.get("burn") for t in job["subs"])):
        return None
    w, h = plan["w"], plan["h"]
    graph = (f"[0:v]format=yuv420p,setpts=PTS-STARTPTS[b];"
             f"[1:{job['info']['video']['idx']}]scale={w}:{h}:flags=bicubic,format=yuv420p,"
             f"setpts=PTS-STARTPTS[a];[b][a]ssim")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", sample, "-ss", f"{start:.3f}",
           "-t", f"{length:.3f}", "-i", src, "-lavfi", graph, "-f", "null", "-"]
    text = _ffmpeg_stderr(cmd, stop_event) or ""
    m = re.findall(r"All:([0-9.]+)", text)
    return float(m[-1]) if m else None


def _scaled(progress, lo, hi):
    if not progress:
        return None
    return lambda f, t: progress(lo + f * (hi - lo), t)


def make_preview(job, start=None, length=10.0, progress=None, stop_event=None, log=None,
                 out_dir=None, ssim=True, est_points=6, est_len=4.0):
    """Encode `length` s from `start` (default: the middle) with the job's
    settings into Data\temp\convert_preview, plus the same source segment
    (near-lossless H.264, for the Dual Player's A side). The size / time
    estimate also encodes `est_points` short pieces spread over the file -
    one scene says little about a whole episode (a quiet middle vs. an
    action-heavy opening differ 5x in bitrate at the same CRF).
    Returns {"ok", "sample", "source", "start", "est_size", "est_time",
    "sample_size", "ssim", "speed", "stopped"}."""
    log = log or (lambda m: None)
    stop_event = stop_event or threading.Event()
    info = job["info"]
    dur = info.get("vduration") or info["duration"] or 0
    length = min(length, dur) if dur else length
    if start is None:
        start = max(0.0, dur / 2 - length / 2)
    start = max(0.0, min(start, max(0.0, dur - length)))
    out_dir = out_dir or PREVIEW_DIR
    os.makedirs(out_dir, exist_ok=True)
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(os.path.basename(job["input"]))[0])[:60]
    ext = job["settings"]["container"]
    sample = os.path.join(out_dir, f"{stem}_sample.{ext}")
    source = os.path.join(out_dir, f"{stem}_source.mkv")
    res = {"ok": False, "stopped": False, "sample": sample, "source": source, "start": start}
    ensure_detect(job, log, stop_event)

    def piece(path, t, n, lo, hi, quiet=False):
        cmd, plan = build_recode_cmd(job, path, None if quiet else log, sample=(t, n))
        if not quiet:
            for line in plan["notes"]:
                log(f"   {line}")
        t0 = time.time()
        rc = run_ffmpeg_with_progress(cmd, n, fps=plan["fps"],
                                      on_progress=_scaled(progress, lo, hi),
                                      on_log=log, stop_event=stop_event)
        return rc, time.time() - t0, plan

    rc, wall, plan = piece(sample, start, length, 0.0, 0.4)
    if rc != 0 or stop_event.is_set():
        res["stopped"] = rc == -1 or stop_event.is_set()
        return res
    sizes, secs, walls = [os.path.getsize(sample)], [length], [wall]
    res["sample_size"] = sizes[0]
    # the estimate: + short pieces spread over the whole file
    if dur > 3 * length and est_points:
        tmp = os.path.join(out_dir, f"{stem}_est.{ext}")
        for k in range(est_points):
            t = dur * (k + 0.5) / est_points - est_len / 2
            lo = 0.4 + 0.4 * k / est_points
            rc, w, _p = piece(tmp, max(0.0, t), est_len, lo, lo + 0.4 / est_points, quiet=True)
            if rc != 0 or stop_event.is_set():
                res["stopped"] = rc == -1 or stop_event.is_set()
                return res
            sizes.append(os.path.getsize(tmp))
            secs.append(est_len)
            walls.append(w)
        try:
            os.remove(tmp)
        except OSError:
            pass
    res["est_size"] = int(sum(sizes) / sum(secs) * dur) if dur else None
    # every run pays a fixed start-up (probe, encoder / GPU init): the 10 s
    # sample vs the short pieces separate it from the per-second cost
    if len(walls) > 1 and length > est_len:
        short = sum(walls[1:]) / (len(walls) - 1)
        per_s = (walls[0] - short) / (length - est_len)
        if per_s <= 0:
            per_s = walls[0] / length
        overhead = max(0.0, short - per_s * est_len)
    else:
        per_s, overhead = max(0.01, walls[0] - 0.6) / length, 0.6
    res["speed"] = 1.0 / max(1e-3, per_s)
    res["est_time"] = overhead + per_s * dur if dur else None
    seg = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-ss", f"{start:.3f}",
           "-i", job["input"], "-t", f"{length:.3f}", "-map", f"0:{info['video']['idx']}",
           "-map", "0:a:0?", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-avoid_negative_ts", "make_zero", source]
    rc = run_ffmpeg_with_progress(seg, length, fps=info["video"]["fps"],
                                  on_progress=_scaled(progress, 0.8, 0.95),
                                  on_log=log, stop_event=stop_event)
    if rc != 0:
        res["stopped"] = rc == -1
        return res
    res["ssim"] = (_ssim(sample, job["input"], start, length, plan, job, stop_event)
                   if ssim else None)
    if progress:
        progress(1.0, "")
    res["ok"] = True
    return res
