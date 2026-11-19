"""Join engine: several videos -> one file. No Tk.

    probe_join(files, container)   what each file holds and whether they can be
                                   joined losslessly (+ why not)
    join_copy(files, out, ...)     stream copy through the concat demuxer
    join_reencode(files, out, ...) every file re-encoded to one format
                                   (join_encode.py - Convert's encoders)
    join(files, out, settings)     "auto": copy when lossless, else re-encode

Both ways write `<out>.part<ext>` first and only put it in place after the
safety gate (frames > 0, length = the sum of the parts, ±0.5 s per join);
Stop / a failure removes the partial file. Each file's own chapters are
moved to where the file starts in the joined one; stream languages, titles
and flags come from the first file; fonts of every file are kept (MKV),
each name once."""
import math
import os
import re
import shutil
import threading

from ..config import TEMP_DIR
from .chapters import write_ffmetadata
from .files import _commit_part, _part_path, _same_path
from .formatting import format_size
from .join_subs import merge_text_track
from .probe import _probe, _probe_json, output_problem, probe_video_duration, stats_tag_args
from .process import _run_capture_stoppable, run_ffmpeg_with_progress, temp_tag
from .recode import TEXT_SUBS, _MP4_AUDIO_BAD

# what an MP4 can hold as a stream copy (video); audio MP4 can't hold is
# re-encoded to E-AC3 (like Convert), subtitles become mov_text / are dropped
MP4_VIDEO = ("h264", "hevc", "av1", "mpeg4", "mpeg2video", "mpeg1video", "vp9", "mjpeg")
# sources whose packets may lack timestamps (the concat demuxer needs them)
_GENPTS_FORMATS = ("avi", "mpeg", "mpegts", "flv", "asf", "h264", "hevc", "m4v")
# why the files can't be joined losslessly: code -> English label (the UI
# shows its own translated labels for the same codes)
REASONS = {
    "video_codec": "video codec", "profile": "video profile", "resolution": "resolution",
    "sar": "pixel aspect ratio", "pix_fmt": "pixel format", "fps": "frame rate",
    "field_order": "interlacing", "extradata": "video encoder settings",
    "audio_count": "number of audio tracks", "audio_codec": "audio codec",
    "audio_channels": "audio channels", "audio_rate": "audio sample rate",
    "sub_count": "number of subtitle tracks", "sub_codec": "subtitle format",
    "layout": "stream order", "container": "container",
}
_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1",
            8: "7.1"}


def _noop(_m):
    pass


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _rate(text):
    """'24000/1001' -> (23.976..., '24000/1001'), (None, '') if unusable."""
    t = str(text or "")
    n, _, d = t.partition("/")
    try:
        n, d = float(n), float(d or 1)
    except ValueError:
        return None, ""
    if n <= 0 or d <= 0:
        return None, ""
    return n / d, t if "/" in t else f"{t}/1"


def default_layout(channels):
    """A channel layout name for `channels` (ffmpeg's default)."""
    return _LAYOUTS.get(int(channels or 2), f"{int(channels or 2)}c")


# ======================= probe =======================
def _packets(path, interval):
    """[(stream index, pts, end)] of every packet in one -read_intervals
    range (raw timestamps)."""
    r = _probe(["ffprobe", "-v", "error", "-read_intervals", interval, "-show_entries",
                "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", path])
    out = []
    if r is None or r.returncode != 0:
        return out
    for ln in r.stdout.decode(errors="replace").split():
        a = ln.split(",")
        try:
            idx, pts = int(a[0]), float(a[1])
        except (ValueError, IndexError):
            continue
        try:
            dur = float(a[2]) if len(a) > 2 else 0.0
        except ValueError:
            dur = 0.0
        out.append((idx, pts, pts + dur))
    return out


def _timing(path, start, dur, vidx, aidx):
    """(video end, end of video + audio, earliest video / audio packet) in
    raw timestamps - from the first 0.5 s and the last ~15 s of packets
    (two seeks, not the whole file)."""
    av = {vidx} | set(aidx)
    tail = [p for p in _packets(path, f"{max(0.0, start + (dur or 0) - 15.0):.3f}%")
            if p[0] in av] if dur else []
    if not any(p[0] == vidx for p in tail):
        tail = [p for p in _packets(path, "%") if p[0] in av]
    head = [p for p in _packets(path, "%+0.5") if p[0] in av]
    vend = max((e for i, _p, e in tail if i == vidx), default=None)
    end = max((e for _i, _p, e in tail), default=vend)
    first = min((p for _i, p, _e in head), default=start)
    return vend, end, first


def probe_file(path):
    """One file for the Join page / engine, or None (unreadable / no video).
    Times are from `start` (the container's start, raw): `duration` = the
    container's length, `vduration` = where the video ends, `end` = where
    video and audio end, `lead` = how far a packet starts before `start`
    (an AAC priming packet: -0.021); chapters [(start, end, title)]."""
    data = _probe_json(path, ["-show_streams", "-show_format", "-show_chapters",
                              "-show_data_hash", "sha256"])
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        return None
    start = _num(fmt.get("start_time")) or 0.0
    dur = _num(fmt.get("duration"))
    if not dur:
        dur = max((_num(s.get("duration")) or 0.0 for s in streams), default=0.0) or None
    v = vids[0]

    def tags(s):
        return {str(k).lower(): val for k, val in (s.get("tags") or {}).items()}

    def disp(s, key):
        return bool((s.get("disposition") or {}).get(key))

    fps, fps_str = _rate(v.get("avg_frame_rate"))
    if fps is None or fps > 300:
        fps, fps_str = _rate(v.get("r_frame_rate"))
    pix = v.get("pix_fmt") or ""
    bits = str(v.get("bits_per_raw_sample") or "")
    depth = int(bits) if bits.isdigit() and int(bits) > 0 else (
        12 if "12" in pix else 10 if "10" in pix else 8)
    sar = str(v.get("sample_aspect_ratio") or "1:1")
    sn, _, sd = sar.partition(":")
    try:
        sar_f = float(sn) / float(sd) if float(sn) > 0 and float(sd) > 0 else 1.0
    except ValueError:
        sar_f = 1.0
    fo = (v.get("field_order") or "").lower()
    vt = tags(v)
    bps = None
    for val in [v.get("bit_rate")] + [x for k, x in vt.items() if k.split("-")[0] == "bps"]:
        if str(val or "").isdigit() and int(val) > 0:
            bps = int(val)
            break
    video = {"idx": v.get("index"), "codec": (v.get("codec_name") or "?").lower(),
             "profile": v.get("profile") or "", "width": int(v.get("width") or 0),
             "height": int(v.get("height") or 0), "sar": "1:1" if sar in ("0:1", "N/A") else sar,
             "sar_f": sar_f, "pix_fmt": pix, "bit_depth": depth, "fps": fps,
             "fps_str": fps_str, "field_order": "" if fo in ("", "unknown") else fo,
             "color": {"transfer": (v.get("color_transfer") or "").lower(),
                       "primaries": (v.get("color_primaries") or "").lower(),
                       "matrix": (v.get("color_space") or "").lower()},
             "extradata": v.get("extradata_hash") or "", "bps": bps,
             "lang": (vt.get("language") or "").strip(), "title": (vt.get("title") or "").strip(),
             "default": disp(v, "default")}
    audio, subs, atts, layout = [], [], [], []
    for s in streams:
        ct, t, idx = s.get("codec_type"), tags(s), s.get("index")
        if ct == "audio":
            ch = int(s.get("channels") or 0)
            audio.append({"idx": idx, "codec": (s.get("codec_name") or "?").lower(),
                          "profile": s.get("profile") or "", "channels": ch,
                          "layout": s.get("channel_layout") or "",
                          "rate": int(_num(s.get("sample_rate")) or 0),
                          "lang": (t.get("language") or "und").lower(),
                          "title": (t.get("title") or "").strip(),
                          "default": disp(s, "default")})
            layout.append((idx, "a"))
        elif ct == "subtitle":
            c = (s.get("codec_name") or "?").lower()
            subs.append({"idx": idx, "codec": c, "lang": (t.get("language") or "und").lower(),
                         "title": (t.get("title") or "").strip(), "forced": disp(s, "forced"),
                         "default": disp(s, "default"), "text": c in TEXT_SUBS,
                         "extradata": s.get("extradata_hash") or "",
                         "size": (s.get("width"), s.get("height")) if s.get("width") else None})
            layout.append((idx, "s"))
        elif ct == "attachment":
            name = os.path.basename(str(t.get("filename") or "").replace("\\", "/"))
            atts.append({"idx": idx, "filename": name or f"attachment_{idx}",
                         "mimetype": t.get("mimetype") or "application/octet-stream"})
        elif s is v:
            layout.append((idx, "v"))
    chapters = []
    for c in data.get("chapters") or []:
        a, b = _num(c.get("start_time")), _num(c.get("end_time"))
        if a is None or b is None:
            continue
        chapters.append((a - start, b - start, (c.get("tags") or {}).get("title") or ""))
    vend, aend, first = _timing(path, start, dur, video["idx"], [a["idx"] for a in audio])
    if vend is None:
        vend = (probe_video_duration(path) or dur or 0.0) + start
    aend = max(vend, aend or vend)
    try:
        size = int(fmt.get("size") or os.path.getsize(path))
    except (TypeError, ValueError, OSError):
        size = 0
    return {"path": path, "start": start, "duration": dur or (vend - start),
            "vduration": max(0.0, vend - start), "end": max(0.0, aend - start),
            "lead": min(0.0, first - start), "size": size,
            "format": (fmt.get("format_name") or "").lower(),
            "tags": {str(k): str(val) for k, val in (fmt.get("tags") or {}).items()},
            "video": video, "audio": audio, "subs": subs, "attachments": atts,
            "chapters": chapters, "layout": layout}


def _fo_group(fo):
    return "progressive" if fo in ("", "progressive") else fo


def compare(files, container="mkv"):
    """[(code, detail)]: why `files` (probe_file dicts, the first is the
    reference) can't be joined by stream copy. [] = they can."""
    out = {}

    def add(code, k, b, a):
        out.setdefault(code, []).append(f"#{k + 1} {b} ≠ {a}")

    ref = files[0]
    rv = ref["video"]
    if container == "mp4" and rv["codec"] not in MP4_VIDEO:
        out["container"] = [f"MP4 ≠ {rv['codec']}"]
    for k, f in enumerate(files[1:], 1):
        v = f["video"]
        if v["codec"] != rv["codec"]:
            add("video_codec", k, v["codec"], rv["codec"])
            continue
        if (v["profile"] or "") != (rv["profile"] or ""):
            add("profile", k, v["profile"] or "?", rv["profile"] or "?")
        if (v["width"], v["height"]) != (rv["width"], rv["height"]):
            add("resolution", k, f"{v['width']}x{v['height']}",
                f"{rv['width']}x{rv['height']}")
        if abs(v["sar_f"] - rv["sar_f"]) > 0.001:
            add("sar", k, v["sar"], rv["sar"])
        if v["pix_fmt"] != rv["pix_fmt"]:
            add("pix_fmt", k, v["pix_fmt"] or "?", rv["pix_fmt"] or "?")
        if v["fps"] is None or rv["fps"] is None or abs(v["fps"] - rv["fps"]) > 0.01:
            add("fps", k, f"{v['fps'] or 0:.3f}", f"{rv['fps'] or 0:.3f}")
        if _fo_group(v["field_order"]) != _fo_group(rv["field_order"]):
            add("field_order", k, v["field_order"] or "progressive",
                rv["field_order"] or "progressive")
        if (v["extradata"] and rv["extradata"] and v["extradata"] != rv["extradata"]
                and "resolution" not in out and "profile" not in out):
            add("extradata", k, "≠", "#1")
    for k, f in enumerate(files[1:], 1):
        if len(f["audio"]) != len(ref["audio"]):
            add("audio_count", k, len(f["audio"]), len(ref["audio"]))
            continue
        for i, (a, r) in enumerate(zip(f["audio"], ref["audio"])):
            if (a["codec"], a["profile"]) != (r["codec"], r["profile"]):
                add("audio_codec", k, f"{i + 1}: {a['codec']}", r["codec"])
            elif a["channels"] != r["channels"]:
                add("audio_channels", k, f"{i + 1}: {a['channels']}", r["channels"])
            elif a["rate"] != r["rate"]:
                add("audio_rate", k, f"{i + 1}: {a['rate']}", r["rate"])
    for k, f in enumerate(files[1:], 1):
        if len(f["subs"]) != len(ref["subs"]):
            add("sub_count", k, len(f["subs"]), len(ref["subs"]))
            continue
        for i, (x, r) in enumerate(zip(f["subs"], ref["subs"])):
            if x["codec"] != r["codec"]:
                add("sub_codec", k, f"{i + 1}: {x['codec']}", r["codec"])
    if not out:
        # the concat demuxer pairs streams by their index in each file
        for k, f in enumerate(files[1:], 1):
            if f["layout"] != ref["layout"]:
                add("layout", k, "≠", "#1")
    reasons = []
    for code in REASONS:
        if code in out:
            d = out[code]
            reasons.append((code, ", ".join(d[:3]) + (", …" if len(d) > 3 else "")))
    return reasons


def probe_join(files, container="mkv"):
    """{"files": [probe_file dict per readable file], "bad": [unreadable
    paths], "lossless": bool, "differences": ["<reason> (<details>)"],
    "reasons": [(code, details)]}. container ('mkv' / 'mp4') = the output's:
    MP4 can't take every video codec as a copy."""
    infos, bad = [], []
    for p in files:
        try:
            fi = probe_file(p)
        except Exception:
            fi = None
        if fi is None:
            bad.append(p)
        else:
            infos.append(fi)
    reasons = compare(infos, container) if len(infos) >= 2 else []
    return {"files": infos, "bad": bad, "reasons": reasons,
            "lossless": len(infos) >= 2 and not bad and not reasons,
            "differences": [f"{REASONS[c]} ({d})" for c, d in reasons]}


# ======================= shared helpers =======================
def concat_quote(path):
    """A path as a quoted concat-demuxer list value ('it'\\''s.mkv')."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def write_concat_list(path, files, durations=None):
    lines = ["ffconcat version 1.0"]
    for k, f in enumerate(files):
        lines.append(f"file {concat_quote(os.path.abspath(f))}")
        if durations and durations[k]:
            lines.append(f"duration {durations[k]:.6f}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def offsets_of(durations):
    out, o = [], 0.0
    for d in durations:
        out.append(o)
        o += d
    return out


def merge_chapters(infos, offsets, spans):
    """Every file's own chapters on the joined timeline: shifted to where
    the file starts (offsets), clipped to its part (spans); duplicates
    (same start) dropped, none overlapping or past the end. Returns
    [(start, end, title)]."""
    raw = []
    for fi, off, span in zip(infos, offsets, spans):
        for a, b, title in fi["chapters"]:
            a, b = max(0.0, a), min(b, span)
            if b - a > 0.0005:
                raw.append((off + a, off + b, title))
    total = (offsets[-1] + spans[-1]) if offsets else 0.0
    raw.sort(key=lambda c: (c[0], -c[1]))
    out = []
    for k, (a, b, title) in enumerate(raw):
        if out and abs(out[-1][0] - a) < 0.001:
            continue                         # same start: keep the first
        nxt = next((c[0] for c in raw[k + 1:] if c[0] - a >= 0.001), total)
        b = min(b, nxt, total)
        if b - a > 0.0005:
            out.append((a, b, title))
    if out and 0 < out[0][0] < 0.25:         # (a timeline nudged by a few ms)
        out[0] = (0.0,) + out[0][1:]
    return out


def part_progress(progress, on_part, offsets, total):
    """progress(frac, text) wrapper that also reports which part (1-based)
    ffmpeg is in: on_part(k, n)."""
    seen = {"k": 0}

    def cb(frac, text):
        t = frac * total
        k = max(1, sum(1 for o in offsets if o <= t + 1e-6))
        if on_part and k != seen["k"]:
            seen["k"] = k
            on_part(k, len(offsets))
        if progress:
            progress(frac, text)
    return cb


def join_tolerance(n):
    """Allowed length difference of the joined file: 0.5 s per join."""
    return 0.5 * max(1, n - 1)


def gate(expected, n):
    return lambda p: output_problem(p, expected, need_video=True, tol=join_tolerance(n))


def global_tags(info):
    """The first file's global tags for the joined file - minus its title
    (that names one episode, not the whole)."""
    return {k: v for k, v in info["tags"].items() if k.lower() != "title"}


def stream_meta_args(kind, n, lang, title):
    args = []
    if lang:
        args += [f"-metadata:s:{kind}:{n}", f"language={lang}"]
    args += [f"-metadata:s:{kind}:{n}", f"title={title or ''}"]
    return args


def sub_disposition(x):
    return "+".join(f for f, on in (("default", x["default"]), ("forced", x["forced"]))
                    if on) or "0"


def unique_attachments(infos, log=None, skip_first=False):
    """[(file index, attachment)] of every file's fonts / attachments, each
    file name once (the first file that has it wins)."""
    seen, out, dup = set(), [], 0
    for k, fi in enumerate(infos):
        for a in fi["attachments"]:
            key = a["filename"].lower()
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            if not (skip_first and k == 0):
                out.append((k, a))
    if dup and log:
        log(f"   attachments: {dup} duplicate(s) skipped (same file name)")
    return out


def check_joins(out, offsets, log, stop_event=None):
    """Decode 3 s of video around each join of a stream copy: decoder errors
    there mean the parts don't fit together. Returns the number of joins
    with errors."""
    bad = 0
    for k, o in enumerate(offsets[1:], 2):
        if stop_event is not None and stop_event.is_set():
            break
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss",
               f"{max(0.0, o - 1.5):.3f}", "-i", out, "-t", "3", "-map", "0:V:0", "-f", "null",
               "-"]
        rc, text = _run_capture_stoppable(cmd, stop_event)
        if rc == -1:
            break
        errs = [ln for ln in (text or "").splitlines() if ln.strip()]
        if rc != 0 or errs:
            bad += 1
            log(f"   [WARN] join {k - 1}/{len(offsets) - 1} (part {k}): the video shows "
                f"decoder errors there - {errs[0][:120] if errs else f'code {rc}'}")
    return bad


def verify(out, infos, n_audio, n_subs, size, log):
    """Log the output's track counts / size. Returns warnings."""
    o = probe_file(out)
    warns = []
    if not o:
        return ["could not probe the output"]
    if len(o["audio"]) != n_audio:
        warns.append(f"audio tracks: expected {n_audio}, got {len(o['audio'])}")
    if len(o["subs"]) != n_subs:
        warns.append(f"subtitle tracks: expected {n_subs}, got {len(o['subs'])}")
    if size and (o["video"]["width"], o["video"]["height"]) != size:
        warns.append(f"resolution: expected {size[0]}x{size[1]}, got "
                     f"{o['video']['width']}x{o['video']['height']}")
    for w in warns:
        log(f"   [WARN] verify: {w}")
    if not warns:
        log(f"   [OK] verify: {o['video']['width']}x{o['video']['height']}, "
            f"{len(o['audio'])} audio / {len(o['subs'])} subtitle track(s), "
            f"{len(o['chapters'])} chapter(s), {len(o['attachments'])} attachment(s), "
            f"{o['vduration']:.2f} s")
    return warns


def _tmp_dir():
    d = os.path.join(TEMP_DIR, f"join_{temp_tag()}")
    os.makedirs(d, exist_ok=True)
    return d


def _finish(rc, part, out, n, expected, log, stop_event, tmp):
    """Commit `part` through the gate, clean up; (ok, placed path)."""
    placed = _commit_part(rc, part, out, log, gate(expected, n))
    shutil.rmtree(tmp, ignore_errors=True)
    if rc == -1 or stop_event.is_set():
        if placed is None:
            log("   [STOPPED] partial output removed")
            return False, None
    if not placed:
        if rc not in (0, -1):
            log(f"   [FAIL] ffmpeg failed (code {rc})")
        return False, None
    return True, placed


# ======================= stream copy =======================
def _dump_attachment(path, att, dst_dir, stop_event=None):
    """Write one attachment of `path` to dst_dir; returns its path or None."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", att["filename"]) or f"att_{att['idx']}"
    dst = os.path.join(dst_dir, f"{att['idx']}_{safe}")
    # ffmpeg dumps at open time and then complains there is no output (rc 1)
    _run_capture_stoppable(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
                            f"-dump_attachment:{att['idx']}", dst, "-i", path], stop_event)
    return dst if os.path.isfile(dst) and os.path.getsize(dst) > 0 else None


def copy_timeline(infos):
    """How long each part lasts in the joined file (the concat list's
    `duration`): its video plus the fewest whole frames so its audio is
    over before the next part's first packet - the video stays on one
    frame grid (no half-frame jump at a join), nothing overlaps, nothing is
    cut. The last part keeps its full length."""
    out = []
    for k, fi in enumerate(infos):
        if k == len(infos) - 1:
            out.append(max(fi["end"], fi["vduration"]))
            break
        fps = fi["video"]["fps"] or 25.0
        fd = 1.0 / fps
        over = fi["end"] - fi["vduration"] - infos[k + 1]["lead"]
        frames = max(0, math.ceil(over / fd - 0.01)) if over > 0.0005 else 0
        out.append(fi["vduration"] + frames * fd)
    return out


def join_copy(files, out, info=None, progress=None, stop_event=None, log=None, on_part=None):
    """Join `files` into `out` without re-encoding (they must match - see
    probe_join). Returns (ok, output path or None)."""
    log = log or _noop
    stop_event = stop_event or threading.Event()
    mp4 = out.lower().endswith(".mp4")
    info = info or probe_join(files, "mp4" if mp4 else "mkv")
    infos = info["files"]
    n = len(infos)
    ref = infos[0]
    tmp = _tmp_dir()
    durs = copy_timeline(infos)
    offsets = offsets_of(durs)
    total = offsets[-1] + infos[-1]["end"]
    expected = offsets[-1] + infos[-1]["vduration"]
    lst = os.path.join(tmp, "list.txt")
    write_concat_list(lst, [fi["path"] for fi in infos], durs)
    chapters = merge_chapters(infos, offsets, durs)
    meta = os.path.join(tmp, "meta.txt")
    write_ffmetadata(meta, chapters, global_tags(ref))
    log(f"   stream copy: {n} parts, {total:.2f} s, {len(chapters)} chapter(s)")

    # -copyts: the concat timeline as it is (ffmpeg would otherwise move it to
    # start at 0 - e.g. by +0.021 s for an AAC priming packet - while the
    # chapters / merged subtitles, inputs of their own, stay put)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-copyts"]
    if any(any(g in fi["format"].split(",") for g in _GENPTS_FORMATS) for fi in infos):
        cmd += ["-fflags", "+genpts"]
    if ref["video"]["extradata"] and len({fi["video"]["extradata"] for fi in infos}) == 1:
        # same codec setup in every part: copy the packets as they are (by
        # default the demuxer rewrites H.264 keyframes with in-band SPS / PPS)
        cmd += ["-auto_convert", "0"]
    cmd += ["-f", "concat", "-safe", "0", "-i", lst, "-f", "ffmetadata", "-i", meta]
    maps, opts = ["-map", f"0:{ref['video']['idx']}"], ["-c", "copy"]
    opts += stream_meta_args("v", 0, ref["video"]["lang"], ref["video"]["title"])
    opts += ["-disposition:v:0", "default" if ref["video"]["default"] else "0"]
    if mp4 and ref["video"]["codec"] == "hevc":
        opts += ["-tag:v", "hvc1"]          # (Apple / Plex direct play want it)
    for i, a in enumerate(ref["audio"]):
        maps += ["-map", f"0:{a['idx']}"]
        if mp4 and (a["codec"] in _MP4_AUDIO_BAD or a["codec"].startswith("pcm_")):
            opts += [f"-c:a:{i}", "eac3", f"-b:a:{i}", "640k"]
            log(f"   audio #{i + 1}: MP4 can't hold {a['codec']} -> E-AC3 640k")
        opts += stream_meta_args("a", i, a["lang"], a["title"])
        opts += [f"-disposition:a:{i}", "default" if a["default"] else "0"]
    ns, dropped, sub_in = 0, [], []
    for i, x in enumerate(ref["subs"]):
        if mp4 and not x["text"]:
            dropped.append(f"#{x['idx']} {x['codec']}")
            continue
        merged = None
        if x["codec"] in ("ass", "ssa") and len({fi["subs"][i]["extradata"]
                                                  for fi in infos}) > 1:
            # the concat demuxer would keep the FIRST part's styles only
            merged = merge_text_track(infos, i, offsets, durs, tmp, log, stop_event)
            if merged:
                log(f"   subtitle track {i + 1}: the parts' ASS styles differ - merged "
                    "as text (every part's styles kept)")
        if merged:
            sub_in += ["-i", merged[0]]
            maps += ["-map", f"{1 + len(sub_in) // 2}:0"]
        else:
            maps += ["-map", f"0:{x['idx']}"]
        if mp4 and x["codec"] != "mov_text":
            opts += [f"-c:s:{ns}", "mov_text"]
            log(f"   subtitle #{ns + 1}: {x['codec']} -> mov_text (MP4)")
        elif not mp4 and x["codec"] == "mov_text":
            opts += [f"-c:s:{ns}", "srt"]
        opts += stream_meta_args("s", ns, x["lang"], x["title"])
        opts += [f"-disposition:s:{ns}", sub_disposition(x)]
        ns += 1
    if dropped:
        log(f"   [WARN] MP4 can't hold picture subtitles - dropped {', '.join(dropped)}")
    attach = []
    n_att = len({a["filename"].lower() for fi in infos for a in fi["attachments"]})
    if n_att and mp4:
        log(f"   [WARN] MP4 can't hold attachments - {n_att} font(s) / attachment(s) dropped")
    elif n_att:
        for a in ref["attachments"]:
            maps += ["-map", f"0:{a['idx']}"]
        k = len(ref["attachments"])
        for fidx, a in unique_attachments(infos, log, skip_first=True):
            if fidx == 0:
                continue
            p = _dump_attachment(infos[fidx]["path"], a, tmp, stop_event)
            if p is None:
                log(f"   [WARN] could not copy the attachment {a['filename']} of part "
                    f"{fidx + 1}")
                continue
            attach += ["-attach", p]
            opts += [f"-metadata:s:t:{k}", f"mimetype={a['mimetype']}",
                     f"-metadata:s:t:{k}", f"filename={a['filename']}"]
            k += 1
        log(f"   attachments: {k} (fonts of every part, each name once)")
    part = _part_path(out)
    cmd += sub_in + attach + maps + opts + ["-map_metadata", "1", "-map_chapters", "1"]
    cmd += stats_tag_args(*[fi["path"] for fi in infos])
    if mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "4096", part]
    if stop_event.is_set():
        shutil.rmtree(tmp, ignore_errors=True)
        return False, None
    rc = run_ffmpeg_with_progress(cmd, total, on_progress=part_progress(
        progress, on_part, offsets, total), on_log=log, stop_event=stop_event)
    ok, placed = _finish(rc, part, out, n, expected, log, stop_event, tmp)
    if not ok:
        return False, None
    check_joins(placed, offsets, log, stop_event)
    verify(placed, infos, len(ref["audio"]), ns, (ref["video"]["width"],
                                                  ref["video"]["height"]), log)
    log(f"   Size: {format_size(os.path.getsize(placed))}")
    return True, placed


# ======================= entry point =======================
def check_inputs(files, out):
    """A reason the job can't run, or None: fewer than 2 files, a missing
    file, the output = one of the inputs."""
    if len(files) < 2:
        return "add at least 2 files"
    for p in files:
        if not os.path.isfile(p):
            return f"file not found: {p}"
        if _same_path(p, out):
            return f"the output would replace a source: {p}"
    return None


def join(files, out, settings=None, mode="auto", info=None, progress=None, stop_event=None,
         log=None, on_part=None):
    """Join `files` (in this order) into `out` (MKV / MP4 from its extension).
    mode: "auto" = stream copy when the files match, else every file is
    re-encoded with `settings` (Convert settings); "copy" / "reencode" force
    one. progress(frac, text) covers the whole job, on_part(k, n) says which
    part is being written. Returns (ok, output path or None)."""
    from .join_encode import join_reencode
    log = log or _noop
    stop_event = stop_event or threading.Event()
    why = check_inputs(files, out)
    if why:
        log(f"   [FAIL] {why}")
        return False, None
    mp4 = out.lower().endswith(".mp4")
    info = info or probe_join(files, "mp4" if mp4 else "mkv")
    if info["bad"]:
        log(f"   [FAIL] not a readable video: {', '.join(info['bad'])}")
        return False, None
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    copy = mode == "copy" or (mode == "auto" and info["lossless"])
    if copy:
        log("   Lossless join: the files match - stream copy, no re-encode")
        return join_copy(files, out, info, progress, stop_event, log, on_part)
    if info["differences"]:
        log("   The files differ: " + "; ".join(info["differences"]))
    log("   Re-encoding every file to one format")
    return join_reencode(files, out, settings or {}, info, progress, stop_event, log, on_part)
