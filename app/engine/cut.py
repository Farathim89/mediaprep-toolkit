"""The cut engines: run_batch (auto-detect a whole folder) and run_manual
(one video, given keep ranges), plus the keep/drop segment maths, segment
naming, duration / size result lines and output verification."""
import os
import glob
import time
from collections import namedtuple

from ..config import MEDIA_EXTS, TEMP_DIR
from .chapters import (add_chapters, build_chapters, probe_chapters, remap_chapters,
                       write_ffmetadata)
from .detect import _detect_core, _want_seg, load_templates_for
from .encode import (PLEX_PRESETS, _SHORT_PIECE, audio_track_codecs, build_audio_pass,
                     build_ffmpeg_cut, build_ffmpeg_inject, build_stream_maps,
                     cover_streams, crf_for_encoder, fit_container, plex_plan,
                     plex_size_check, resolve_encoder, restore_covers)
from .files import (_cleanup, _commit_part, _move_to_done, _part_path, _path_key,
                    _place_output, _same_path)
from .formatting import fmt_time, format_seconds, format_size
from .probe import (output_problem, probe_audio_streams, probe_duration,
                    probe_first_frame_at, probe_piece_timing, probe_start_time,
                    probe_streams, probe_video_duration, probe_video_fps, probe_video_info,
                    stats_tag_args)
from .process import run_ffmpeg_with_progress
from .smartcut import smart_cut, smart_cut_problem
from .subs import _disposition_args, trim_container_to_video


class ManualResult(namedtuple("ManualResult", "produced output_path")):
    """run_manual's result: (produced: bool, output_path: str or None).
    Truthy only when an output was produced."""
    __slots__ = ()

    def __bool__(self):
        return bool(self.produced)


def duration_check_line(final_output, expected_sec):
    """Verifies no VIDEO content was lost: the video length must match the sum
    of the kept segments. Measures the video stream (not the container), so a
    subtitle event lingering past the picture doesn't trip a false alarm. Falls
    back to container duration if the frame count can't be read."""
    got = probe_video_duration(final_output)
    basis = "video"
    if got is None:
        got = probe_duration(final_output)
        basis = "file"
    if got is None:
        return "   Duration check: could not probe output\n"
    diff = got - expected_sec
    tag = "[OK]" if abs(diff) < 1.0 else "[WARN]"
    return (f"   {tag} Duration check ({basis}): expected {expected_sec:.2f}s,"
            f" got {got:.2f}s ({diff:+.2f}s)\n")


def saved_size_line(final_output, src_size, wall_start):
    """Log line with before -> after size comparison for a finished video."""
    try:
        out_size = os.path.getsize(final_output)
    except OSError:
        out_size = 0
    pct = f" ({out_size / src_size * 100:.0f}% of original)" if src_size and out_size else ""
    return (f"   [OK] Saved: {final_output}\n"
            f"   Size: {format_size(src_size)} -> {format_size(out_size)}{pct}"
            f" - total {format_seconds(time.time() - wall_start)}\n")


def _range_kf_times(a, b, interval):
    """a, b and (interval > 0) a + k*interval below b - computed per k, not
    summed up, so no float drift creeps in (0.1 added 1000 times != 100.0)."""
    times = {round(a, 6), round(b, 6)}
    if interval and interval > 0:
        k = 0
        while a + k * interval < b:
            times.add(round(a + k * interval, 6))
            k += 1
    return times


def build_forced_keyframe_times(valid_intro, intro_start, intro_end,
                                valid_credits, credits_start, credits_end,
                                keyframe_interval, total_duration):
    return keyframe_times_from_ranges(
        [(valid_intro, intro_start, intro_end), (valid_credits, credits_start, credits_end)],
        keyframe_interval, total_duration)


def compute_keep_segments(total_duration, pre_intro=None, intro=None,
                          credits=None, after_credits=None, min_seg=0.5):
    """Turn boundary points into the list of segments to KEEP.

    pre_intro:     keep starts here (drop everything before). None -> 0.
    after_credits: keep ends here (drop everything after).   None -> end.
    intro/credits: (start, end) ranges to DROP, or None.

    Returns [(start, end), ...] left-to-right, tiny slivers (< min_seg) removed.
    Overlapping drops are merged, so it is safe if a manual intro range bleeds
    into the credits range or past the trim points.
    """
    start = 0.0 if pre_intro is None else max(0.0, pre_intro)
    end = total_duration if after_credits is None else min(total_duration, after_credits)
    if end <= start:
        return []
    drops = []
    for rng in (intro, credits):
        if rng and rng[1] > rng[0]:
            ds, de = max(rng[0], start), min(rng[1], end)
            if de > ds:
                drops.append((ds, de))
    drops.sort()
    keep, cur = [], start
    for ds, de in drops:
        if ds > cur:
            keep.append((cur, ds))
        cur = max(cur, de)
    if end > cur:
        keep.append((cur, end))
    return [(s, e) for s, e in keep if e - s > min_seg]


def keep_from_drops(total_duration, drops, min_seg=0.5):
    """Complement of a set of DROP ranges within [0, total]: returns the KEEP
    segments [(s, e), ...] with overlaps merged and slivers (< min_seg) removed."""
    clean = []
    for rng in drops:
        if rng and rng[1] > rng[0]:
            a, b = max(0.0, rng[0]), min(total_duration, rng[1])
            if b > a:
                clean.append((a, b))
    clean.sort()
    keep, cur = [], 0.0
    for ds, de in clean:
        if ds > cur:
            keep.append((cur, ds))
        cur = max(cur, de)
    if total_duration > cur:
        keep.append((cur, total_duration))
    return [(s, e) for s, e in keep if e - s > min_seg]


def keyframe_times_from_ranges(ranges, keyframe_interval, total_duration):
    """Forced-keyframe times from any number of (valid, start, end) ranges."""
    times = set()
    for valid, a, b in ranges:
        if valid:
            times |= _range_kf_times(a, b, keyframe_interval)
    return sorted(t for t in times if 0.0 < t < total_duration)


# ======================= output safety =======================
def _plan_output_ext(cfg_encoder, mode, video, ext, vinfo):
    """The extension the output of `video` gets: chapters mode keeps the
    source container (stream copy), Plex-ready is MKV, a re-encode keeps
    the source's unless it can't hold the codecs (fit_container -> .mkv)."""
    if mode == "chapters":
        return ext
    if mode == "smartcut" and not smart_cut_problem(video):
        return ext                     # stream copy + same-codec joins
    if cfg_encoder in PLEX_PRESETS:
        return ".mkv"
    enc = resolve_encoder(cfg_encoder, vinfo["codec"])
    return fit_container(ext, enc, audio_track_codecs(probe_audio_streams(video)))


def _gate(expected_sec, need_video):
    """The safety gate for _commit_part / _place_output: an output that is
    empty, has no video frame (when the source has video) or is far off the
    expected length is rejected - never placed, its source never moved."""
    return lambda p: output_problem(p, expected_sec, need_video)


def _kept_sec(keep, src_vdur):
    """Length of the kept ranges [(name, start, end)], each clipped to the
    source's VIDEO length (audio / a subtitle may run on past the picture)."""
    return sum(max(0.0, (min(e, src_vdur) if src_vdur else e) - s) for _n, s, e in keep)


def _move_source_to_done(video, final_output, protect_keys, log):
    """Move a finished source into <its folder>/done - unless that path is
    this run's own output (output folder = <source>/done) or another
    protected file (protect_keys: _path_key()s)."""
    done_dir = os.path.join(os.path.dirname(video) or ".", "done")
    dst = os.path.join(done_dir, os.path.basename(video))
    if _same_path(dst, video):
        return
    if _same_path(dst, final_output) or _path_key(dst) in protect_keys:
        log(f"   [NOTE] source not moved to done: {dst} is an output of this run")
        return
    try:
        os.makedirs(done_dir, exist_ok=True)
        _move_to_done(video, dst, log)
    except OSError as e:
        log(f"   [WARN] could not move source to done: {e}")


# ======================= batch remover engine =======================
def run_batch(cfg, ui, stop_event):
    """cfg: dict of settings. ui: object with .log(msg), .status(text),
    .progress(frac, text). stop_event: threading.Event.
    Returns a per-file summary [(source, status, output_path or None)] with
    status "ok" / "failed" / "skipped" / "stopped" ([] if nothing ran)."""
    results = []
    try:
        import importlib
        for _mod in ("numpy", "librosa", "scipy.signal"):
            importlib.import_module(_mod)
    except ImportError as e:
        ui.log(f"[FAIL] Missing package: {e}")
        ui.log("Install with:  pip install librosa numpy scipy")
        return results

    os.makedirs(cfg["output_dir"], exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    cb = {"on_progress": ui.progress, "on_log": ui.log, "stop_event": stop_event}
    subs_lang = {s.lower() for s in cfg["subs_langs"]} if cfg.get("subs_langs") else None
    # Plex-ready presets re-encode to MKV (chapters mode is a stream copy)
    plex_key = (cfg.get("encoder") if cfg.get("encoder") in PLEX_PRESETS
                and cfg["mode"] != "chapters" else None)
    try:
        templates = load_templates_for(cfg, ui.log, stop_event)
        intros_data = templates["intro"]
        credits_data = templates["credits"]
        preintro_data = templates["preintro"]
        aftercredits_data = templates["aftercredits"]
        if stop_event.is_set():
            ui.log("STOPPED by user.")
            return results
        skipped = [k for k in ("preintro", "intro", "credits", "aftercredits")
                   if not _want_seg(cfg, k)]
        if skipped:
            ui.log(f"Segment type(s) disabled for this run: {', '.join(skipped)}")
        if not (intros_data or credits_data or preintro_data or aftercredits_data):
            ui.log("[FAIL] No usable templates found - nothing to detect with.")
            return results

        video_files, unsupported = [], []
        for f in glob.glob(os.path.join(cfg["video_dir"], "*")):
            if not os.path.isfile(f) or f.lower().endswith(".part" + os.path.splitext(f)[1].lower()):
                continue
            (video_files if f.lower().endswith(MEDIA_EXTS) else unsupported).append(f)
        video_files.sort()
        ui.log(f"\nFound {len(video_files)} video(s)\n")
        if unsupported:
            names = sorted(os.path.basename(f) for f in unsupported)
            ui.log(f"Skipped {len(names)} file(s) of unsupported type: "
                   + ", ".join(names[:5]) + (" ..." if len(names) > 5 else "") + "\n")
        if not video_files:
            return results

        n_videos = len(video_files)
        batch_t0 = time.time()
        # no output may replace a source of this batch (Plex mode with the
        # output folder = the source folder: ep.mp4 -> ep.mkv would replace
        # the SOURCE ep.mkv) or an output this run already wrote
        source_keys = {_path_key(v) for v in video_files}
        outputs_made = set()

        def batch_progress(base, span, vb):
            """The BAR shows this video's progress (continuous across its
            segments); the TEXT adds the whole batch's percentage + estimated
            time left. base/span are the ffmpeg call's slice in batch units."""
            def _cb(frac, text):
                f = min(1.0, base + frac * span)              # batch fraction
                vf = min(1.0, max(0.0, (f - vb) * n_videos))  # this video's
                elapsed = time.time() - batch_t0
                left = (format_seconds(elapsed * (1 - f) / f)
                        if f > 0.003 and elapsed > 5 else "--:--")
                ui.progress(vf, f"{text} | all {f * 100:.0f}% ~{left}")
            return _cb

        for vi, video in enumerate(video_files, 1):
            if stop_event.is_set():
                break
            status, out_path = "failed", None
            try:
                status, out_path = _batch_one(
                    cfg, ui, stop_event, templates, video, vi, n_videos, subs_lang,
                    plex_key, cb, batch_progress, source_keys, outputs_made)
            finally:
                results.append((video, status, out_path))

        if not stop_event.is_set():
            ui.progress(1.0, "done")
        ui.log("STOPPED by user." if stop_event.is_set() else "FINISHED!")
    finally:
        # templates and episode audio are decoded straight into memory and
        # their temp WAVs removed at once (load_templates_for/_detect_core)
        pass
    return results


def _batch_one(cfg, ui, stop_event, templates, video, vi, n_videos, subs_lang,
               plex_key, cb, batch_progress, source_keys, outputs_made):
    """One video of run_batch. Returns (status, output path or None)."""
    intros_data = templates["intro"]
    credits_data = templates["credits"]
    preintro_data = templates["preintro"]
    aftercredits_data = templates["aftercredits"]
    filename = os.path.basename(video)
    name, ext = os.path.splitext(filename)
    vinfo = probe_video_info(video)
    out_ext = _plan_output_ext(cfg.get("encoder"), cfg["mode"], video, ext, vinfo)
    final_output = os.path.join(cfg["output_dir"], f"{name}{out_ext}")
    temp_segs = []
    temp_audio = ""         # episode audio is decoded + removed by _detect_core
    produced = False        # True only if THIS run wrote the output ok
    video_wall = time.time()
    vb, vspan = (vi - 1) / n_videos, 1.0 / n_videos
    if _same_path(final_output, video):
        ui.log(f"[{vi}/{n_videos}] {filename}\n   [FAIL] output folder is"
               " the source folder - would overwrite the source, skipped\n")
        return "failed", None
    if _path_key(final_output) in source_keys | outputs_made:
        ui.log(f"[{vi}/{n_videos}] {filename}\n   [FAIL] output {final_output} would"
               " overwrite another source / output of this batch, skipped\n")
        return "failed", None

    ui.status(f"Video {vi}/{n_videos}: {filename}")
    ui.progress(0.0, f"analysing video {vi}/{n_videos}")
    src_size = os.path.getsize(video)
    ui.log(f"[{vi}/{n_videos}] {filename}  ({format_size(src_size)})")

    # the same matcher the Manual cut "Auto-detect" / review dry run
    # use (detect_segments) - best candidate of each kind decides
    det = _detect_core(video, cfg, templates, stop_event, ui.log)
    if stop_event.is_set():
        _cleanup(temp_audio, temp_segs)
        return "stopped", None
    if det is None:
        ui.log("   [FAIL] audio extract failed - skipping\n")
        return "failed", None
    total_duration = det["duration"]

    def _top(kind):
        c = det.get(kind) or []
        return c[0] if c and c[0]["ok"] else None

    c_in, c_cr, c_pre, c_aft = (_top("intro"), _top("credits"),
                                _top("preintro"), _top("aftercredits"))
    valid_intro, valid_credits = c_in is not None, c_cr is not None
    valid_preintro, valid_aftercredits = c_pre is not None, c_aft is not None
    intro_start, intro_end = (c_in["start"], c_in["end"]) if c_in else (0.0, 0.0)
    # chapters mode marks the intro where it REALLY starts (the part
    # before it becomes its own chapter); the cut modes drop from 0
    intro_real_start = c_in["match_start"] if c_in else intro_start
    credits_start, credits_end = ((c_cr["start"], c_cr["end"]) if c_cr
                                  else (total_duration, total_duration))
    preintro_start, preintro_end = ((c_pre["start"], c_pre["end"]) if c_pre
                                    else (0.0, 0.0))
    aftercredits_start, aftercredits_end = (
        (c_aft["start"], c_aft["end"]) if c_aft
        else (total_duration, total_duration))

    if not (valid_intro or valid_credits or valid_preintro or valid_aftercredits):
        ui.log("   [SKIP] no matches\n")
        _cleanup(temp_audio, temp_segs)
        return "skipped", None
    # optional: if a segment the user enabled (and has templates for) was
    # NOT found above the confidence, skip the WHOLE episode instead of
    # producing a partial cut - so weak-credits episodes are left intact
    # for you to handle, not output with only the intro removed
    if cfg.get("skip_incomplete"):
        missing = []
        for kind, have_tpl, is_valid in (
                ("pre-intro", preintro_data, valid_preintro),
                ("intro", intros_data, valid_intro),
                ("credits", credits_data, valid_credits),
                ("after-credits", aftercredits_data, valid_aftercredits)):
            if have_tpl and not is_valid:
                missing.append(kind)
        if missing:
            ui.log(f"   [SKIP] enabled segment(s) not found: {', '.join(missing)}"
                   " - leaving this episode untouched for review\n")
            _cleanup(temp_audio, temp_segs)
            return "skipped", None
    if stop_event.is_set():
        _cleanup(temp_audio, temp_segs)
        return "stopped", None

    fps = probe_video_fps(video)
    has_video = vinfo["codec"] not in ("?", None)
    src_vdur = probe_video_duration(video)
    covers = cover_streams(video)

    # --- source info + output bit depth + size warning ---
    br = f", ~{vinfo['bitrate_mbps']:.1f} Mbit/s" if vinfo["bitrate_mbps"] else ""
    ui.log(f"   Source video: {vinfo['codec']} {vinfo['bit_depth']}-bit"
           f" ({vinfo['pix_fmt']}){br}")
    start_time = probe_start_time(video)
    if abs(start_time) >= 0.05:
        ui.log(f"   Source timestamps start at {start_time:.3f}s - cut points"
               " count from the file start")
    # smart cut: copy the source's packets, re-encode only at the joins;
    # a file it can't handle (or a smart cut that fails) is re-encoded
    smart = cfg["mode"] == "smartcut"
    if smart:
        why = smart_cut_problem(video)
        if why:
            ui.log(f"   [WARN] Smart cut not possible: {why} - re-encoding the whole video")
            smart = False
        else:
            ui.log("   Output: smart cut - the source's video / audio copied,"
                   " re-encoded only around the cut points")

    def setup_encoder():
        """(encoder, crf, output bit depth, plex plan) - logs the choice."""
        if cfg["mode"] == "chapters":       # pure stream copy, no encoder
            ui.log("   Output: stream copy (no re-encode)")
            return None, None, None, None
        if plex_key:
            if ext.lower() != ".mkv":
                ui.log(f"   Plex-ready: source is {ext.lower()} - writing .mkv")
            plan = plex_plan(plex_key, video, cfg["preset"], ui.log)
            return plan["encoder"], plan["crf"], plan["bit_depth"], plan
        enc = resolve_encoder(cfg["encoder"], vinfo["codec"], ui.log)
        q = crf_for_encoder(enc, cfg)
        if cfg["bit_depth"] == "auto":
            depth = 10 if vinfo["bit_depth"] >= 10 else 8
        else:
            depth = int(cfg["bit_depth"])
        if enc == "h264_nvenc" and depth >= 10:
            ui.log("   [WARN] H.264 NVENC cannot encode 10-bit - using 8-bit.")
            depth = 8
        src_eff = 2 if vinfo["codec"] in ("hevc", "av1", "vp9") else 1
        tgt_eff = 2 if enc in ("libx265", "hevc_nvenc", "libsvtav1") else 1
        if tgt_eff < src_eff or (vinfo["bit_depth"] >= 10 and depth == 8):
            ui.log("   [WARN] Source codec/bit depth is more efficient than the"
                   " chosen output - the file may get MUCH bigger."
                   " Consider H.265, 10-bit, CRF 22-24, preset slow.")
        ui.log(f"   Output: {enc} {depth}-bit,"
               f" CRF/CQ {q}, preset {cfg['preset']}")
        ext_out = _plan_output_ext(cfg.get("encoder"), "cut", video, ext, vinfo)
        if ext_out.lower() != ext.lower():
            ui.log(f"   Output container: {ext.lower()} can't hold {enc} + this"
                   f" audio - writing {ext_out}")
        return enc, q, depth, None

    encoder = crf = out_depth = plex = None
    if not smart:
        encoder, crf, out_depth, plex = setup_encoder()

    keep = []
    if cfg["mode"] in ("cut", "smartcut"):
        drops = []
        if valid_preintro:
            drops.append((preintro_start, preintro_end))
        if valid_intro:
            drops.append((intro_start, intro_end))
        if valid_credits:
            drops.append((credits_start, credits_end))
        if valid_aftercredits:
            drops.append((aftercredits_start, aftercredits_end))
        keep = _seg_names(keep_from_drops(total_duration, drops), total_duration)
        gate = _gate(_kept_sec(keep, src_vdur), has_video)
        if smart:
            ui.log("   Keep: " + " -> ".join(f"{n} [{fmt_time(s)}-{fmt_time(e)}]"
                                             for n, s, e in keep))
            ui.status(f"Video {vi}/{n_videos}: {filename}  -  Smart cut")
            placed, why = _run_smart(video, keep, final_output, cfg, ui, stop_event,
                                     batch_progress(vb, vspan, vb), subs_lang, gate)
            if placed:
                final_output, produced = placed, True
                ui.log(duration_check_line(
                    final_output, _kept_sec(keep, src_vdur)).rstrip("\n"))
                ui.log(saved_size_line(final_output, src_size, video_wall))
            elif not stop_event.is_set():
                # the normal re-encode instead (its container may differ:
                # Plex-ready writes .mkv)
                ui.log(f"   [WARN] Smart cut failed: {why} - re-encoding the whole video")
                smart = False
                encoder, crf, out_depth, plex = setup_encoder()
                alt = os.path.join(cfg["output_dir"], name + _plan_output_ext(
                    cfg.get("encoder"), "cut", video, ext, vinfo))
                if not _same_path(alt, final_output):
                    if _same_path(alt, video) or _path_key(alt) in source_keys | outputs_made:
                        ui.log(f"   [FAIL] output {alt} would overwrite a source / output"
                               " of this batch, skipped\n")
                        _cleanup(temp_audio, temp_segs)
                        return "failed", None
                    final_output, out_ext = alt, os.path.splitext(alt)[1]

    if (cfg["mode"] in ("cut", "smartcut") and not smart and not produced
            and not stop_event.is_set()):
        steps = [n for n, _, _ in keep] + (["Stitching"] if len(keep) > 1 else [])
        n_steps = len(steps)
        ui.log(f"   Steps: {' -> '.join(steps)}")
        audio_streams = probe_audio_streams(video)

        # weight each segment's share of the batch bar by its duration
        total_d = sum(e - s for _, s, e in keep) or 1.0
        multi = len(keep) > 1
        # a multi-piece cut encodes the audio in one seamless pass
        # (_stitch); per-piece audio only for a single piece / Plex
        piece_audio = not multi or bool(plex)
        enc_span = vspan * (0.85 if multi else 1.0)
        done_d = 0.0

        ok = True
        for i, (seg_name, s0, s1) in enumerate(keep, 1):
            if stop_event.is_set():
                ok = False
                break
            ui.status(f"Video {vi}/{n_videos}: {filename}  -  [{i}/{n_steps}] {seg_name}")
            seg_out = os.path.join(TEMP_DIR, f"seg_{vi}_{i}{out_ext}")
            temp_segs.append(seg_out)
            t0 = time.time()
            seg_cb = dict(cb, on_progress=batch_progress(
                vb + done_d / total_d * enc_span,
                (s1 - s0) / total_d * enc_span, vb))
            rc = build_ffmpeg_cut(video, seg_out, s0, s1, audio_streams,
                                  crf, cfg["preset"], cfg["kf_interval"],
                                  seg_name=seg_name, fps=fps,
                                  encoder=encoder, subs_lang=subs_lang,
                                  bit_depth=out_depth, plex=plex,
                                  audio=piece_audio, **seg_cb)
            if rc != 0:
                if rc != -1:  # -1 = stopped by user, already logged
                    ui.log(f"   [FAIL] {seg_name} failed")
                ok = False
                break
            done_d += s1 - s0
            ui.log(f"   [{i}/{n_steps}] {seg_name}: done in {format_seconds(time.time()-t0)}")

        if ok and len(temp_segs) == 1:
            placed = _place_output(temp_segs[0], final_output, ui.log, gate)
            if placed:
                final_output, produced = placed, True
                ui.log(duration_check_line(
                    final_output, _kept_sec(keep, src_vdur)).rstrip("\n"))
                ui.log(saved_size_line(final_output, src_size, video_wall))
        elif ok and len(temp_segs) > 1:
            ui.status(f"Video {vi}/{n_videos}: {filename}  -  [{n_steps}/{n_steps}] Stitching")
            a_span = (vspan - enc_span) * 0.7
            placed, rc = _stitch(
                video, temp_segs, keep, final_output, audio_streams, plex,
                f"{vi}", cb,
                batch_progress(vb + enc_span, a_span, vb),
                batch_progress(vb + enc_span + a_span,
                               vspan - enc_span - a_span, vb), ui.log, gate)
            if placed:
                final_output, produced = placed, True
                ui.log(duration_check_line(
                    final_output, _kept_sec(keep, src_vdur)).rstrip("\n"))
                ui.log(saved_size_line(final_output, src_size, video_wall))
            elif rc not in (0, -1):    # rc 0 = placing failed, logged already
                ui.log("   [FAIL] merge failed\n")
        elif not stop_event.is_set():
            ui.log("   [FAIL] skipping this video\n")
    elif cfg["mode"] in ("cut", "smartcut"):
        pass                                # smart cut done (or stopped) above
    elif cfg["mode"] == "chapters":
        ch_drops = []
        if valid_preintro:
            ch_drops.append((preintro_start, preintro_end, "Pre-intro"))
        if valid_intro:
            ch_drops.append((intro_real_start, intro_end, "Intro"))
        if valid_credits:
            ch_drops.append((credits_start, credits_end, "Credits"))
        if valid_aftercredits:
            ch_drops.append((aftercredits_start, aftercredits_end, "After-credits"))
        # "Intro: cut from file start": keep its meaning (everything
        # before the intro is skippable) by titling that lead-in
        # chapter "Cold Open", while the Intro marker sits where the
        # intro really begins
        lead = None
        if valid_intro and cfg.get("intro_from_start"):
            if intro_real_start > 0.05:
                lead = "Cold Open"
                ui.log(f"   Intro chapter at its real start {intro_real_start:.1f}s;"
                       " the part before it is a 'Cold Open' chapter")
            else:
                ui.log("   Intro chapter starts at 0 (the intro opens the file)")
        chapters = build_chapters(total_duration, ch_drops, lead_title=lead)
        # Chapter markers are metadata only: stream-copy the source
        # (no re-encode, no quality loss). A player's skip lands on the
        # source's nearest keyframe at each boundary.
        ui.log(f"   Adding {len(chapters)} chapter marker(s) (stream copy)")
        ui.status(f"Video {vi}/{n_videos}: {filename}  -  Add Chapters")
        part = _part_path(final_output)
        maps = build_stream_maps(video, subs_lang,
                                 data=out_ext.lower() not in (".mkv", ".webm"))
        rc, err = add_chapters(video, part, chapters, maps=maps,
                               **dict(cb, on_progress=batch_progress(vb, vspan, vb)))
        placed = _commit_part(rc, part, final_output, ui.log,
                              _gate(src_vdur or total_duration, has_video))
        if placed:
            final_output, produced = placed, True
            ui.log(saved_size_line(final_output, src_size, video_wall))
        elif rc not in (0, -1):
            for ln in err.strip().splitlines()[-4:]:
                ui.log(f"      | {ln}")
            ui.log("   [FAIL] adding chapters failed\n")
    else:
        kf_times = keyframe_times_from_ranges(
            [(valid_preintro, preintro_start, preintro_end),
             (valid_intro, intro_start, intro_end),
             (valid_credits, credits_start, credits_end),
             (valid_aftercredits, aftercredits_start, aftercredits_end)],
            cfg["kf_interval"], total_duration)
        ui.log(f"   Injecting {len(kf_times)} forced keyframe(s), no cutting")
        ui.status(f"Video {vi}/{n_videos}: {filename}  -  Inject Keyframes")
        part = _part_path(final_output)
        rc = build_ffmpeg_inject(video, part, kf_times, total_duration,
                                 crf, cfg["preset"], fps=fps,
                                 encoder=encoder, bit_depth=out_depth, subs_lang=subs_lang,
                                 plex=plex,
                                 **dict(cb, on_progress=batch_progress(vb, vspan, vb)))
        placed = _commit_part(rc, part, final_output, ui.log,
                              _gate(src_vdur or total_duration, has_video))
        if placed:
            final_output, produced = placed, True
            ui.log(duration_check_line(
                final_output, src_vdur or total_duration).rstrip("\n"))
            ui.log(saved_size_line(final_output, src_size, video_wall))
        elif rc not in (0, -1):
            ui.log("   [FAIL] keyframe injection failed\n")

    if produced:
        outputs_made.add(_path_key(final_output))
        # cut mode can leave a lingering subtitle past the picture; clip it
        if cfg["mode"] in ("cut", "smartcut") and trim_container_to_video(final_output):
            ui.log("   Trimmed a lingering subtitle tail to the video length")
        if covers:
            restore_covers(video, final_output, ui.log, covers)
        if plex:
            kept = (sum(e - s for _, s, e in keep) if cfg["mode"] in ("cut", "smartcut")
                    else total_duration)
            plex_size_check(final_output, src_size, kept, total_duration, ui.log)
        warns = verify_output(video, final_output, subs_lang)
        if warns:
            ui.log("   [VERIFY] WARNING: " + "; ".join(warns))
        else:
            ui.log("   [VERIFY] OK - tracks & resolution preserved")

    # move the SOURCE of a finished video into videos/done so it's easy
    # to see what's left. Skipped/failed videos stay put on purpose.
    if produced and cfg.get("move_done") and not stop_event.is_set():
        _move_source_to_done(video, final_output, outputs_made, ui.log)

    _cleanup(temp_audio, temp_segs)
    if produced:
        return "ok", final_output
    return ("stopped" if stop_event.is_set() else "failed"), None


def _seg_names(keep, total_duration=None):
    """Label kept segments by what they actually are, not just their position:
      Cold Open    - the first piece, only if it starts at the file start and
                     is short (<= _SHORT_PIECE) and something follows it
      Post Credits - the last piece, only if it is short, runs to the end of
                     the file, and follows real content (not a cold open)
      Main Content - everything else (a whole episode is never a cold open)
    total_duration: the video length (default: end of the last piece)."""
    n = len(keep)
    if not n:
        return []
    end = total_duration if total_duration else keep[-1][1]
    names = ["Main Content"] * n
    if n > 1:
        s0, s1 = keep[0]
        if s0 < 1.0 and s1 - s0 <= _SHORT_PIECE:
            names[0] = "Cold Open"
        s0, s1 = keep[-1]
        if (names[-2] == "Main Content" and s1 - s0 <= _SHORT_PIECE
                and s1 >= end - 1.0):
            names[-1] = "Post Credits"
    return [(nm, s0, s1) for nm, (s0, s1) in zip(names, keep)]


def _stitch(video, temp_segs, keep, final_output, audio_streams, plex, tag, cb,
            audio_progress, merge_progress, log, check=None):
    """Join the encoded pieces (keep = [(name, start, end)], one per piece)
    into final_output via its .part file. check: the safety gate (see
    _commit_part). Returns (output path or None, ffmpeg rc).

    Video (+ subtitles / fonts / Plex stream-copied audio) comes from the
    pieces through the concat demuxer, each pinned to the span its VIDEO
    really covers: a copied subtitle event can linger past a piece's video
    and inflate its container, and the container usually starts a frame or
    two in (B-frame delay), so the plain video duration would leave a held
    frame at every seam. Re-encoded audio is NOT taken from the pieces - each
    piece's own encode starts with encoder priming/padding, an audible
    ~80-120 ms dropout at every join - but made in ONE pass over the source
    (build_audio_pass) whose ranges are cut to exactly those video spans, so
    it is seamless and stays in sync across any number of joins. The
    source's chapters are clipped to those spans too (remap_chapters)."""
    expected = sum(e - s for _, s, e in keep)
    codecs = audio_track_codecs(audio_streams, plex)
    tracks = [(k, c) for k, c in enumerate(codecs) if c]
    spans, ranges = [], []
    base = probe_start_time(video)
    for t, (_n, s0, s1) in zip(temp_segs, keep):
        tm = probe_piece_timing(t)
        first = probe_first_frame_at(video, s0, base=base) if tm else None
        if tm and first is not None and tm[2] > tm[0]:
            cstart, vstart, vend = tm
            # piece timestamp T shows source time T - vstart + first, and the
            # concat demuxer lays the piece out over [cstart, vend]
            spans.append(vend - cstart)
            ranges.append((first + cstart - vstart, first + vend - vstart))
        else:                              # unprobeable: the requested range
            vd = probe_video_duration(t) or (s1 - s0)
            spans.append(vd)
            ranges.append((s0, s0 + vd))

    audio_file = ""
    if tracks:
        audio_file = os.path.join(TEMP_DIR, f"audio_{tag}.mka")
        rc = build_audio_pass(video, audio_file, ranges, tracks,
                              **dict(cb, on_progress=audio_progress))
        if rc != 0:
            _cleanup(audio_file, [])
            if rc != -1:
                log("   [FAIL] audio pass failed")
            return None, rc

    # the source's chapters, clipped to the kept ranges and moved onto the
    # joined timeline (the concat demuxer carries none of the pieces')
    chap_file, offs, o = "", [], 0.0
    for sp in spans:
        offs.append(o)
        o += sp
    chapters = remap_chapters(probe_chapters(video), base,
                              [(a, b, off) for (a, b), off in zip(ranges, offs)])
    if chapters:
        chap_file = os.path.join(TEMP_DIR, f"chapters_{tag}.txt")
        write_ffmetadata(chap_file, chapters)

    concat_list = os.path.join(TEMP_DIR, f"concat_{tag}.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for t, d in zip(temp_segs, spans):
            f.write(f"file '{os.path.basename(t)}'\n")
            f.write(f"duration {d:.6f}\n")
    # audio track k: from the single-pass file, or (Plex stream copy) from
    # the pieces - in the source's track order
    if codecs:
        amaps, j = [], 0
        for k, c in enumerate(codecs):
            if c:
                amaps += ["-map", f"1:a:{j}"]
                j += 1
            else:
                amaps += ["-map", f"0:a:{k}"]
    else:
        amaps = ["-map", "0:a?"]
    # concat drops flags like 'forced': subtitle flags from the first piece,
    # audio flags from the source (every source audio track is kept)
    pairs = list(zip(*[iter(_disposition_args(temp_segs[0]))] * 2))
    if codecs:
        pairs = ([p for p in pairs if p[0].startswith("-disposition:s:")]
                 + [p for p in zip(*[iter(_disposition_args(video))] * 2)
                    if p[0].startswith("-disposition:a:")])
    disp = [x for p in pairs for x in p]
    part = _part_path(final_output)
    chap_in = ((["-f", "ffmetadata", "-i", chap_file]
                + ["-map_chapters", "2" if audio_file else "1"]) if chap_file else [])
    rc = run_ffmpeg_with_progress(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list]
        + (["-i", audio_file] if audio_file else []) + chap_in
        # map only real media types (video/audio/subs/fonts). A stray
        # DATA/unknown stream some sources carry (e.g. #0:13) can't be
        # copied into matroska and would abort the whole merge, so we
        # never select it. -ignore_unknown is a belt-and-braces backup.
        + ["-map", "0:v?"] + amaps + ["-map", "0:s?", "-map", "0:t?", "-c", "copy"]
        + disp + stats_tag_args(video) + ["-ignore_unknown", part], expected,
        **dict(cb, on_progress=merge_progress))
    placed = _commit_part(rc, part, final_output, log, check)
    _cleanup(audio_file, [concat_list] + ([chap_file] if chap_file else []))
    return placed, rc


def _run_smart(video, keep, final_output, cfg, ui, stop_event, on_progress, subs_lang,
               gate):
    """Smart-cut `video` (keep: [(name, start, end)]) into final_output via
    its .part file, through the safety gate. Returns (output path or None,
    why) - why: what went wrong (the caller then re-encodes the video)."""
    part = _part_path(final_output)
    rc, why = smart_cut(video, keep, part, cfg, ui.log, stop_event,
                        on_progress=on_progress, subs_lang=subs_lang)
    if rc == 0:
        placed = _commit_part(0, part, final_output, ui.log, gate)
        return placed, ("" if placed else "the output did not pass the safety check")
    _commit_part(1, part, final_output, ui.log)        # removes a partial file
    return None, why


def run_manual(cfg, keep, ui, stop_event, protect=None):
    """Cut ONE video into the given keep-segments and stitch them, using the
    same encoder settings and ffmpeg helpers as the batch engine. `keep` is a
    list of (start, end) seconds from compute_keep_segments().
    cfg["smart_cut"] = True: smart cut (smartcut.py) - the source's video /
    audio packets are copied and only the frames around each cut point are
    re-encoded; a video it can't handle (or a smart cut that fails its
    checks) is re-encoded the normal way with the cfg encoder settings.
    protect: optional paths (e.g. every source of a multi-file run) the
    output must never replace, nor the source be moved onto.
    Returns ManualResult(produced, output_path) - a (bool, str or None)
    tuple that is truthy only when an output was produced."""
    video = cfg["video"]
    if not os.path.isfile(video):
        ui.log("[FAIL] video not found")
        return ManualResult(False, None)
    os.makedirs(cfg["output_dir"], exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    cb = {"on_progress": ui.progress, "on_log": ui.log, "stop_event": stop_event}
    name, ext = os.path.splitext(os.path.basename(video))
    # optional "[15/38] " prefix so a multi-file run shows which file this is
    prefix = cfg.get("job_label", "")
    prefix = f"{prefix} " if prefix else ""
    plex_key = cfg.get("encoder") if cfg.get("encoder") in PLEX_PRESETS else None
    vinfo = probe_video_info(video)
    smart = bool(cfg.get("smart_cut"))
    smart_why = smart_cut_problem(video) if smart else None
    smart = smart and not smart_why
    # smart cut keeps the source's container; Plex-ready output is always
    # MKV; a re-encode keeps the source's container unless it can't hold
    # the codecs (.webm + x265, .mpg)
    out_ext = ext if smart else _plan_output_ext(cfg.get("encoder"), "cut", video, ext, vinfo)
    final_output = os.path.join(cfg["output_dir"], f"{name}{out_ext}")
    video_wall = cfg.get("_wall") or time.time()
    src_size = os.path.getsize(video)
    if not cfg.get("_smart_fallback"):
        ui.log(f"{prefix}[MANUAL] {name}{ext}  ({format_size(src_size)})")
    if smart_why:
        ui.log(f"   [WARN] Smart cut not possible: {smart_why} - re-encoding the whole video")
    if not smart and plex_key and ext.lower() != ".mkv":
        ui.log(f"   Plex-ready: source is {ext.lower()} - writing .mkv")
    elif not smart and out_ext.lower() != ext.lower():
        ui.log(f"   Output container: {ext.lower()} can't hold the chosen codecs"
               f" - writing {out_ext}")
    if _same_path(final_output, video):
        ui.log("   [FAIL] output folder is the source folder - would overwrite"
               " the source, skipped\n")
        return ManualResult(False, None)
    protect_keys = {_path_key(p) for p in protect or ()}
    if _path_key(final_output) in protect_keys:
        ui.log(f"   [FAIL] output {final_output} would overwrite another file of"
               " this run, skipped\n")
        return ManualResult(False, None)

    src_total = probe_video_duration(video) or probe_duration(video)
    named = _seg_names(keep, src_total)
    if not named:
        ui.log("   [SKIP] those points leave nothing to keep\n")
        return ManualResult(False, None)

    fps = probe_video_fps(video)
    start_time = probe_start_time(video)
    if abs(start_time) >= 0.05:
        ui.log(f"   Source timestamps start at {start_time:.3f}s - cut points"
               " count from the file start")

    # Multi cut runs one run_manual per file: cfg["batch"] = {"done": seconds
    # of source already handled, "total": seconds of the whole run, "t0":
    # wall-clock start of the run} makes the "all" part of the text cover the
    # WHOLE list (weighted by duration), not just this file
    batch = cfg.get("batch") or None
    this_sec = src_total or 0.0

    def overall_progress(base, span):
        """One 0-1 bar for the whole job instead of resetting per segment."""
        def _cb(frac, text):
            f = min(1.0, base + frac * span)
            if batch and batch.get("total"):
                fa = min(1.0, (batch.get("done", 0.0) + f * this_sec) / batch["total"])
                elapsed = time.time() - batch.get("t0", video_wall)
            else:
                fa, elapsed = f, time.time() - video_wall
            left = (format_seconds(elapsed * (1 - fa) / fa)
                    if fa > 0.003 and elapsed > 5 else "--:--")
            ui.progress(f, f"{text} | all {fa * 100:.0f}% ~{left}")
        return _cb

    subs_lang = {s.lower() for s in cfg["subs_langs"]} if cfg.get("subs_langs") else None
    covers = cover_streams(video)
    expected = _kept_sec(named, src_total)     # (the video can't run past its end)
    gate = _gate(_kept_sec(named, src_total), vinfo["codec"] not in ("?", None))
    if smart:
        ui.log(f"   Keep: {' -> '.join(f'{n} [{fmt_time(s)}-{fmt_time(e)}]' for n, s, e in named)}")
        ui.status(f"{prefix}[MANUAL] {name}{ext}  -  Smart cut")
        placed, why = _run_smart(video, named, final_output, cfg, ui, stop_event,
                                 overall_progress(0.0, 1.0), subs_lang, gate)
        if not placed and not stop_event.is_set():
            ui.log(f"   [WARN] Smart cut failed: {why} - re-encoding the whole video")
            return run_manual(dict(cfg, smart_cut=False, _smart_fallback=True,
                                   _wall=video_wall), keep, ui, stop_event, protect)
        return _manual_finish(cfg, ui, stop_event, video, placed, expected, src_size,
                              video_wall, covers, None, src_total, subs_lang,
                              protect_keys, [])

    plex = plex_plan(plex_key, video, cfg.get("preset"), ui.log) if plex_key else None
    encoder = plex["encoder"] if plex else resolve_encoder(cfg["encoder"], vinfo["codec"], ui.log)
    crf = plex["crf"] if plex else crf_for_encoder(encoder, cfg)
    if plex:
        out_depth = plex["bit_depth"]
    elif cfg["bit_depth"] == "auto":
        out_depth = 10 if vinfo["bit_depth"] >= 10 else 8
    else:
        out_depth = int(cfg["bit_depth"])
    if encoder == "h264_nvenc" and out_depth >= 10:
        ui.log("   [WARN] H.264 NVENC cannot encode 10-bit - using 8-bit.")
        out_depth = 8
    audio_streams = probe_audio_streams(video)
    ui.log(f"   Keep: {' -> '.join(f'{n} [{fmt_time(s)}-{fmt_time(e)}]' for n, s, e in named)}")

    total_d = sum(e - s for _, s, e in named) or 1.0
    multi = len(named) > 1
    # a multi-piece cut encodes the audio in one seamless pass (_stitch)
    piece_audio = not multi or bool(plex)
    enc_span = 0.85 if multi else 1.0
    done_d = 0.0

    temp_segs, ok = [], True
    for i, (seg_name, s0, s1) in enumerate(named, 1):
        if stop_event.is_set():
            ok = False
            break
        ui.status(f"{prefix}[MANUAL] {name}{ext}  -  [{i}/{len(named)}] {seg_name}")
        seg_out = os.path.join(TEMP_DIR, f"manual_{i}{out_ext}")
        temp_segs.append(seg_out)
        seg_cb = dict(cb, on_progress=overall_progress(
            done_d / total_d * enc_span, (s1 - s0) / total_d * enc_span))
        rc = build_ffmpeg_cut(video, seg_out, s0, s1, audio_streams,
                              crf, cfg["preset"], cfg["kf_interval"],
                              seg_name=seg_name, fps=fps, subs_lang=subs_lang,
                              encoder=encoder, bit_depth=out_depth, plex=plex,
                              audio=piece_audio, **seg_cb)
        if rc != 0:
            if rc != -1:
                ui.log(f"   [FAIL] {seg_name} failed")
            ok = False
            break
        done_d += s1 - s0

    placed = None
    if ok and len(temp_segs) == 1:
        placed = _place_output(temp_segs[0], final_output, ui.log, gate)
    elif ok and len(temp_segs) > 1:
        ui.status(f"{prefix}[MANUAL] {name}{ext}  -  Stitching")
        a_span = (1.0 - enc_span) * 0.7
        # a failed/stopped merge must not count as produced (no trim, no move)
        placed, rc = _stitch(video, temp_segs, named, final_output, audio_streams, plex,
                             "manual", cb, overall_progress(enc_span, a_span),
                             overall_progress(enc_span + a_span, 1.0 - enc_span - a_span),
                             ui.log, gate)
        if not placed and rc not in (0, -1):    # rc 0 = placing failed, logged already
            ui.log("   [FAIL] merge failed\n")
    elif not stop_event.is_set():
        ui.log("   [FAIL] could not produce output\n")

    return _manual_finish(cfg, ui, stop_event, video, placed, expected, src_size,
                          video_wall, covers, plex, src_total, subs_lang, protect_keys,
                          temp_segs)


def _manual_finish(cfg, ui, stop_event, video, placed, expected, src_size, video_wall,
                   covers, plex, src_total, subs_lang, protect_keys, temp_segs):
    """run_manual's last steps for a placed output (or None): checks and log
    lines, cover art, move the source to done/, clean up. -> ManualResult"""
    produced = bool(placed) and os.path.exists(placed)
    if produced:
        final_output = placed
        ui.log(duration_check_line(final_output, expected).rstrip("\n"))
        ui.log(saved_size_line(final_output, src_size, video_wall))
        if trim_container_to_video(final_output):
            ui.log("   Trimmed a lingering subtitle tail to the video length")
        if covers:
            restore_covers(video, final_output, ui.log, covers)
        if plex:
            plex_size_check(final_output, src_size, expected, src_total, ui.log)
        warns = verify_output(video, final_output, subs_lang)
        if warns:
            ui.log("   [VERIFY] WARNING: " + "; ".join(warns))
        else:
            ui.log("   [VERIFY] OK - tracks & resolution preserved")

    # move the SOURCE of a finished video into <source>/done, exactly like the
    # auto (batch) tool. Only when the cut actually produced an output and we
    # weren't stopped; never onto the output itself or a protected path.
    if produced and cfg.get("move_done") and not stop_event.is_set():
        _move_source_to_done(video, final_output, protect_keys, ui.log)

    _cleanup("", temp_segs)
    ui.log("STOPPED by user." if stop_event.is_set() else "FINISHED!")
    return ManualResult(produced, final_output if produced else None)


def verify_output(src, out, subs_lang=None):
    """Compare source vs output streams; returns a list of warning strings
    (empty if audio/subtitle tracks, languages and resolution are preserved).
    Codec and duration changes are expected and not reported. subs_lang:
    the subtitle-language filter the run used - only those tracks are
    expected. Cover art is ignored, and an odd frame size cropped to even
    (4:2:0 encoders) is expected too."""
    a, b = probe_streams(src), probe_streams(out)
    if not a or not b:
        return ["could not probe for verification"]
    warns = []
    a_subs = [x for x in a["subtitle"]
              if not subs_lang or (x["lang"] or "und").lower() in subs_lang]
    if len(a["audio"]) != len(b["audio"]):
        warns.append(f"audio tracks {len(a['audio'])} -> {len(b['audio'])}")
    if len(a_subs) != len(b["subtitle"]):
        warns.append(f"subtitle tracks {len(a_subs)} -> {len(b['subtitle'])}")
    la, lb = sorted(x["lang"] for x in a["audio"]), sorted(x["lang"] for x in b["audio"])
    if la != lb:
        warns.append(f"audio languages {la} -> {lb}")
    sa, sb = sorted(x["lang"] for x in a_subs), sorted(x["lang"] for x in b["subtitle"])
    if sa != sb:
        warns.append(f"subtitle languages {sa} -> {sb}")
    va = [x for x in a["video"] if not x.get("cover")]
    vb = [x for x in b["video"] if not x.get("cover")]
    if va and vb:
        w, h = va[0]["w"] or 0, va[0]["h"] or 0
        if (vb[0]["w"], vb[0]["h"]) not in ((w, h), (w - w % 2, h - h % 2)):
            warns.append(f"resolution {w}x{h} -> {vb[0]['w']}x{vb[0]['h']}")
    return warns
