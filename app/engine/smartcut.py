"""Smart cut: re-encode only around the cut points, stream-copy the rest.

Every kept range [s, e) of the source becomes up to three video parts:
  head    s .. k1    re-encoded: from s up to the first clean keyframe k1
  middle  k1 .. T    stream copy of whole GOPs - the source's own packets
  tail    T .. e     re-encoded, frame-exact end
A range shorter than ~2 GOPs is re-encoded whole. The re-encoded parts use
the source's codec (H.264 -> libx264, HEVC -> libx265) with its pixel
format, profile / level, colour tags and frame timestamps, closed GOPs and
in-band parameter sets (x264 with its own SPS/PPS id), so the joined stream
decodes as one: an H.264 copy starts at an IDR, an HEVC one at an IRAP (a
CRA is turned into a BLA - a clean splice point; its leading pictures are
dropped and re-encoded in the head instead).
Audio packets are copied as well: each track is cut at the packet nearest
every join and the pieces are laid end to end (no gaps; A/V error at most
half a packet, it never adds up). A track that can't be cut like that is
re-encoded in one seamless pass (build_audio_pass). Subtitles / fonts are
cut like the normal cut does; chapters are clipped to the kept ranges.
The joined file is checked (frame count, decode + picture check at every
join against the source). smart_cut() never leaves a doubtful file: it
returns a failure and the caller re-encodes the file the normal way.
"""
import bisect
import math
import os
import re
import statistics

from ..config import TEMP_DIR
from .chapters import remap_chapters, write_ffmetadata
from .encode import CUT_EPS, audio_track_codecs, build_audio_pass
from .files import _cleanup
from .formatting import fmt_time
from .probe import _probe, _probe_json, is_stats_tag, probe_audio_streams
from .process import _run_capture_stoppable, run_ffmpeg_with_progress, temp_tag

SMART_ENCODER = {"h264": "libx264", "hevc": "libx265"}
SMART_CONTAINERS = (".mkv", ".mp4", ".m4v", ".mov")
_SMART_PIX = ("yuv420p", "yuvj420p", "yuv420p10le", "yuv422p", "yuv422p10le",
              "yuv444p", "yuv444p10le")
_CRF = {"libx264": 16, "libx265": 18}
# NAL unit types: where a copy may start (H.264 IDR; HEVC BLA / IDR / CRA),
# the parameter sets a self-contained keyframe carries, the VCL range
_COPY_START = {"h264": (5,), "hevc": tuple(range(16, 22))}
_PARAM_SETS = {"h264": {7, 8}, "hevc": {33, 34}}
_HEVC_CRA, _HEVC_BLA_W_LP = 21, 16
# audio codecs whose packets can be cut and joined as they are (TrueHD /
# MLP need their major-sync frames - those tracks are re-encoded)
_COPY_AUDIO = ("aac", "ac3", "eac3", "dts", "mp3", "mp2", "flac", "opus", "vorbis", "alac")
_EPS = 0.0005           # < half the smallest frame / packet spacing we meet
_PSNR_MIN = 26.0        # a join frame below this (vs the source) = broken
_DISPOSITIONS = ("default", "forced", "hearing_impaired", "visual_impaired", "comment",
                 "original", "dub", "captions", "descriptions", "lyrics", "karaoke")


# ======================= probing =======================
def _source_info(path):
    """Streams / chapters / format of `path` in one ffprobe, or None."""
    d = _probe_json(path, ["-show_format", "-show_streams", "-show_chapters"])
    streams = d.get("streams") or []
    if not streams:
        return None
    fmt = d.get("format") or {}
    try:
        base = float(fmt.get("start_time"))
        base = base if math.isfinite(base) else 0.0
    except (TypeError, ValueError):
        base = 0.0

    def kind(s, t):
        return (s.get("codec_type") == t
                and not (s.get("disposition") or {}).get("attached_pic"))
    video = [s for s in streams if kind(s, "video")]
    return {"base": base, "format": fmt, "video": video[0] if video else None,
            "audio": [s for s in streams if s.get("codec_type") == "audio"],
            "subs": [s for s in streams if s.get("codec_type") == "subtitle"],
            "attachments": [s for s in streams if s.get("codec_type") == "attachment"],
            "chapters": d.get("chapters") or []}


def smart_cut_problem(video, info=None):
    """None if `video` can be smart-cut, else a short reason (the caller
    then re-encodes it the normal way). One ffprobe, no decoding."""
    ext = os.path.splitext(video)[1].lower()
    if ext not in SMART_CONTAINERS:
        return f"{ext or 'this'} files are not supported (MKV / MP4 / MOV are)"
    info = info or _source_info(video)
    v = info["video"] if info else None
    if not v:
        return "no video stream found"
    codec = (v.get("codec_name") or "").lower()
    if codec not in SMART_ENCODER:
        return f"{codec or 'unknown'} video (smart cut handles H.264 and HEVC)"
    pix = v.get("pix_fmt") or ""
    if pix not in _SMART_PIX:
        return f"pixel format {pix or 'unknown'} is not supported"
    order = (v.get("field_order") or "progressive").lower()
    if order not in ("progressive", "unknown"):
        return f"interlaced video ({order})"
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if not w or not h or ("420" in pix and (w % 2 or h % 2)):
        return f"frame size {w}x{h}"
    return None


def _csv_rows(path, select, entries, timeout=900):
    r = _probe(["ffprobe", "-v", "error", "-select_streams", select,
                "-show_entries", f"packet={entries}", "-of", "csv=p=0", path],
               timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    return [ln.split(",") for ln in r.stdout.decode(errors="replace").split()]


def _video_packets(path):
    """[(pts, keyframe)] of the first real video stream in DECODE order (raw
    seconds), or None if unreadable or a packet has no timestamp."""
    rows = _csv_rows(path, "V:0", "pts_time,flags")
    out = []
    for a in rows or ():
        try:
            out.append((float(a[0]), "K" in (a[1] if len(a) > 1 else "")))
        except (ValueError, IndexError):
            return None
    return out or None


def _audio_packets(path, audio):
    """{audio track n: sorted [(pts, duration)]} (raw seconds), every track
    in one demux pass. audio: the source's audio stream dicts."""
    idx = {int(s.get("index", -1)): n for n, s in enumerate(audio)}
    out = {n: [] for n in idx.values()}
    for a in _csv_rows(path, "a", "stream_index,pts_time,duration_time") or ():
        try:
            n, pts = idx.get(int(a[0])), float(a[1])
        except (ValueError, IndexError):
            continue
        try:
            dur = float(a[2])
        except (ValueError, IndexError):
            dur = 0.0
        if n is not None:
            out[n].append((pts, dur))
    for v in out.values():
        v.sort()
    return out


_NAL_RE = re.compile(r"nal_unit_type\s+[01]+\s*=\s*(\d+)")
_PSID_RE = re.compile(r"\b(?:seq|pic)_parameter_set_id\s+[01]+\s*=\s*(\d+)")


def _keyframe_nals(path, t_raw, base, stop_event=None):
    """The NAL unit types of the video packet whose pts is t_raw (raw s) and
    every SPS / PPS id the stream uses (extradata + that packet), from
    ffmpeg's trace_headers on just that one packet. None if not found."""
    seek = t_raw - base - 1.0
    bsf = f"noise=drop=gt(abs(pts*tb-{t_raw:.6f})\\,{_EPS}),trace_headers"
    cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-v", "info"]
           + (["-ss", f"{seek:.6f}"] if seek > 0.5 else [])
           + ["-t", "4", "-copyts", "-i", path, "-map", "0:V:0", "-c", "copy",
              "-bsf:v", bsf, "-frames:v", "1", "-f", "null", "-"])
    rc, text = _run_capture_stoppable(cmd, stop_event)
    if rc != 0 or "Packet:" not in text:
        return None
    head, _, pkt = text.partition("Packet:")
    pkt = pkt.split("Packet:")[0]
    return {"types": [int(x) for x in _NAL_RE.findall(pkt)],
            "ids": {int(x) for x in _PSID_RE.findall(head + pkt)}}


def _count_packets(path, select="V:0"):
    d = _probe_json(path, ["-select_streams", select, "-count_packets",
                           "-show_entries", "stream=nb_read_packets"])
    try:
        return int((d.get("streams") or [{}])[0]["nb_read_packets"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _fps_of(v, disp):
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(v.get(key) or "").partition("/")
        try:
            f = float(num) / float(den or 1)
            if 1.0 < f < 500.0:
                return f
        except (ValueError, ZeroDivisionError):
            pass
    gaps = [b - a for a, b in zip(disp, disp[1:]) if b > a]
    return 1.0 / statistics.median(gaps) if gaps else 25.0


# ======================= planning =======================
def _plan(vpk, keep, kf_ok, min_gops=2.0):
    """Split each kept range into parts. vpk: [(pts, key)] in decode order,
    0-based; keep: [(name, s, e)]; kf_ok(pts) -> True if a copy may start at
    that keyframe. Returns [{"name", "F", "E", "n", "parts": [{"kind":
    "enc"|"copy", "t0", "t1", "n"}]}] - F / E: the range's first frame and
    the end of its last one; parts tile [F, E) with exact frame counts."""
    disp = sorted(p for p, _ in vpk)
    kidx = [i for i, (_, k) in enumerate(vpk) if k]
    kp = [vpk[i][0] for i in kidx]
    gmin = []                   # first shown frame of each GOP (leading pictures)
    for g, i in enumerate(kidx):
        j = kidx[g + 1] if g + 1 < len(kidx) else len(vpk)
        gmin.append(min(p for p, _ in vpk[i:j]))
    gaps = [b - a for a, b in zip(disp, disp[1:]) if b > a]
    frame = statistics.median(gaps) if gaps else 0.04
    gop = statistics.median([b - a for a, b in zip(kp, kp[1:])]) if len(kp) > 1 else None
    end_all = disp[-1] + frame

    def n_between(a, b):
        return bisect.bisect_left(disp, b - _EPS) - bisect.bisect_left(disp, a - _EPS)

    def middle(F, E, to_end):
        """(start GOP, end GOP, T) of the longest clean copy inside [F, E)."""
        ga = bisect.bisect_left(kp, F - _EPS)
        for _ in range(4):                       # a few keyframes may be unclean
            if ga >= len(kp) or kp[ga] >= E - _EPS:
                return None
            if kf_ok(kp[ga]):
                break
            ga += 1
        else:
            return None
        if to_end:
            gb, T = len(kp), E
        else:
            gb = ga + 1
            while gb + 1 < len(kp) and gmin[gb + 1] <= E + _EPS:
                gb += 1
            if gb >= len(kp) or gmin[gb] > E + _EPS:
                return None
            T = gmin[gb]
        while gb > ga:
            stop = kidx[gb] if gb < len(kidx) else len(vpk)
            kept = [p for p, _ in vpk[kidx[ga]:stop] if p >= kp[ga] - _EPS]
            if kept and max(kept) < T - _EPS and len(kept) == n_between(kp[ga], T):
                return ga, gb, T
            gb -= 1                              # GOPs overlap oddly: shorter copy
            T = gmin[gb] if gb > ga else T
        return None

    plan = []
    for name, s, e in keep:
        i0 = bisect.bisect_left(disp, s - CUT_EPS)
        i1 = bisect.bisect_left(disp, e - CUT_EPS)
        if i1 <= i0:
            continue
        F = disp[i0]
        E = disp[i1] if i1 < len(disp) else end_all
        n = i1 - i0
        mid = (middle(F, E, i1 >= len(disp))
               if gop and E - F >= min_gops * gop else None)
        parts = []
        if mid:
            ga, gb, T = mid
            if kp[ga] > F + _EPS:
                parts.append({"kind": "enc", "t0": F, "t1": kp[ga], "n": n_between(F, kp[ga])})
            parts.append({"kind": "copy", "t0": kp[ga], "t1": T, "n": n_between(kp[ga], T)})
            if E > T + _EPS:
                parts.append({"kind": "enc", "t0": T, "t1": E, "n": n_between(T, E)})
            if sum(p["n"] for p in parts) != n or any(p["n"] <= 0 for p in parts):
                parts = []
        if not parts:
            parts = [{"kind": "enc", "t0": F, "t1": E, "n": n}]
        plan.append({"name": name, "F": F, "E": E, "n": n, "parts": parts})
    return plan


def _audio_plan(pk, base, ranges):
    """Packet-exact pieces of one audio track for the kept video ranges
    [(F, E, O)] (0-based source span, output offset). The pieces are laid
    end to end; each starts at the packet boundary nearest to where the
    audio must be to stay in sync, so the error stays within half a packet
    and never adds up. Returns ([(A0, A1, zero, n_packets)], max error s)
    with raw times - `zero` is the source time placed at the piece's output
    start (== A0, or earlier for audio that starts late) - or None if the
    track has no packets in a range."""
    if not pk:
        return None
    starts = [p - base for p, _ in pk]
    last_dur = pk[-1][1] or (starts[-1] - starts[-2] if len(starts) > 1 else 0.02)
    bounds = starts + [starts[-1] + last_dur]
    durs = [d for _, d in pk if d > 0]
    half = (statistics.median(durs) if durs else 0.02) / 2

    def nearest(t, lo=0):
        i = bisect.bisect_left(bounds, t, lo)
        cands = [j for j in (i - 1, i) if lo <= j < len(bounds)]
        return min(cands, key=lambda j: abs(bounds[j] - t)) if cands else None

    pos, pieces, worst = 0.0, [], 0.0
    for F, E, O in ranges:
        target = F + (pos - O)
        i = nearest(target)
        if i is None or i >= len(starts):
            return None
        zero = bounds[i]
        if zero - target > half:            # the track starts late here: keep the gap
            zero = target
        j = nearest(zero + (O + (E - F) - pos), i)
        if j is None or j <= i:
            return None
        # source time t plays at pos + t - zero but belongs at O + t - F
        worst = max(worst, abs((pos - O) - (zero - F)))
        pieces.append((bounds[i] + base, bounds[j] + base, zero + base, j - i))
        pos += bounds[j] - zero
    return pieces, worst


# ======================= ffmpeg steps =======================
def _encoder_args(v, preset, ps_id):
    """Encoder args for a re-encoded part matching the source stream v."""
    codec = v["codec_name"].lower()
    enc = SMART_ENCODER[codec]
    args = ["-c:v", enc, "-preset", preset or "medium", "-crf", str(_CRF[enc]),
            "-pix_fmt", v["pix_fmt"]]
    try:
        level = int(v.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    if codec == "h264":
        prof = {"baseline": "baseline", "constrained baseline": "baseline",
                "main": "main", "high": "high", "high 10": "high10",
                "high 4:2:2": "high422", "high 4:4:4 predictive": "high444"
                }.get((v.get("profile") or "").lower())
        if prof:
            args += ["-profile:v", prof]
        if level >= 10:
            args += ["-level:v", f"{level / 10:.1f}"]
        # own SPS/PPS ids, repeated before every IDR: the source's parameter
        # sets and these never overwrite each other at a join
        args += ["-x264-params", f"repeat-headers=1:sps-id={ps_id}"]
    else:
        xp = ["repeat-headers=1", "open-gop=0", "log-level=error"]
        if level >= 30:
            xp.append(f"level-idc={level / 30:.1f}")
        args += ["-x265-params", ":".join(xp)]
    for key, opt in (("color_range", "-color_range"), ("color_primaries", "-color_primaries"),
                     ("color_transfer", "-color_trc"), ("color_space", "-colorspace")):
        val = v.get(key)
        if val and val not in ("unknown", "reserved", "unspecified"):
            args += [opt, val]
    return args


def _drop_outside(a, b):
    """noise-bsf expression keeping the packets with a <= pts < b (raw s)."""
    return f"noise=drop=lt(pts*tb\\,{a - _EPS:.6f})+gte(pts*tb\\,{b - _EPS:.6f})"


def _concat_list(path, entries):
    """entries: [(file, duration, inpoint or None)]; files next to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for fn, dur, inp in entries:
            f.write(f"file '{os.path.basename(fn)}'\n")
            if inp is not None:
                f.write(f"inpoint {inp:.6f}\n")
            f.write(f"duration {dur:.6f}\n")


def _stream_args(spec, s, disposition=True):
    """The source stream's tags (minus statistics) + its flags, for output
    stream `spec` ('v:0', 'a:1' ...)."""
    args = []
    for k, v in (s.get("tags") or {}).items():
        if v in (None, "") or is_stats_tag(k) or k.upper() == "DURATION":
            continue
        args += [f"-metadata:s:{spec}", f"{k}={v}"]
    if disposition:
        d = s.get("disposition") or {}
        args += [f"-disposition:{spec}", "+".join(k for k in _DISPOSITIONS if d.get(k)) or "0"]
    return args


def _cra_to_bla(path):
    """Turn the CRA picture that starts copied part `path` (an MKV) into a
    BLA - the HEVC splice point: a new coded video sequence starts there and
    its (already dropped) leading pictures are not expected. Only the NAL
    header type bits of its slices change. True if patched."""
    d = _probe_json(path, ["-select_streams", "v:0", "-read_intervals", "%+#1",
                           "-show_entries", "packet=pos,size"])
    try:
        p = (d.get("packets") or [])[0]
        pos, size = int(p["pos"]), int(p["size"])
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    with open(path, "r+b") as f:
        f.seek(pos)
        blob = bytearray(f.read(size + 32))
        # pkt pos may point at the block header: find the offset / NAL length
        # size whose length-prefixed NAL units fill the packet exactly
        for off in range(0, 17):
            for ls in (4, 2, 1):
                k, slices, ok = off, [], True
                while k < off + size:
                    if k + ls > len(blob):
                        ok = False
                        break
                    n = int.from_bytes(blob[k:k + ls], "big")
                    if n < 2 or k + ls + n > off + size or blob[k + ls] & 0x80:
                        ok = False
                        break
                    if (blob[k + ls] >> 1) & 0x3F == _HEVC_CRA:
                        slices.append(k + ls)
                    k += ls + n
                if ok and k == off + size and slices:
                    for h in slices:
                        blob[h] = (blob[h] & 0x81) | (_HEVC_BLA_W_LP << 1)
                        f.seek(pos + h)
                        f.write(bytes(blob[h:h + 1]))
                    return True
    return False


class _Progress:
    """Maps each step's 0-1 onto one bar: steps have weights (work units)."""

    def __init__(self, on_progress, total):
        self.cb, self.total, self.done = on_progress, max(total, 1e-6), 0.0

    def step(self, weight):
        base = self.done
        self.done += weight

        def _cb(frac, text):
            if self.cb:
                self.cb(min(1.0, (base + frac * weight) / self.total), text)
        return _cb


# ======================= main =======================
def smart_cut(video, keep, out_path, cfg, log, stop_event, on_progress=None,
              subs_lang=None):
    """Smart-cut `video` into out_path (its extension = the source's).
    keep: [(name, start, end)] seconds from the file start. cfg: the cut
    settings ('preset' is used for the re-encoded joins). subs_lang: keep
    only these subtitle languages (None = all).
    Returns (rc, why): 0 = written and verified, -1 = stopped, 1 = not
    possible / failed - `why` says why, the caller then re-encodes."""
    temps = []
    try:
        return _smart_cut(video, keep, out_path, cfg, log, stop_event, on_progress,
                          subs_lang, temps)
    finally:
        _cleanup("", temps)


def _smart_cut(video, keep, out_path, cfg, log, stop_event, on_progress, subs_lang, temps):
    info = _source_info(video)
    why = smart_cut_problem(video, info)
    if why:
        return 1, why
    v, base = info["video"], info["base"]
    codec = v["codec_name"].lower()
    raw = _video_packets(video)
    if not raw:
        return 1, "could not read the video packets"
    vpk = [(p - base, k) for p, k in raw]
    disp = sorted(p for p, _ in vpk)
    fps = _fps_of(v, disp)

    kf_cache = {}

    def kf_info(t):
        if t not in kf_cache:
            kf_cache[t] = _keyframe_nals(video, t + base, base, stop_event)
        return kf_cache[t]

    def first_vcl(k):
        lim = 5 if codec == "h264" else 31
        return next((t for t in k["types"] if (1 if codec == "h264" else 0) <= t <= lim), None)

    def kf_ok(t):
        k = kf_info(t)
        return bool(k) and first_vcl(k) in _COPY_START[codec]

    plan = _plan(vpk, keep, kf_ok)
    if stop_event.is_set():
        return -1, "stopped"
    if not plan:
        return 1, "the kept ranges hold no video frames"

    # output timeline: range r starts at O_r; its frames keep their spacing
    offs, o = [], 0.0
    for r in plan:
        offs.append(o)
        o += r["E"] - r["F"]
    total = o
    ranges = [(r["F"], r["E"], O) for r, O in zip(plan, offs)]

    used_ids = set()
    for k in kf_cache.values():
        if k:
            used_ids |= k["ids"]
    ps_id = next(i for i in range(1, 32) if i not in used_ids)
    venc = _encoder_args(v, cfg.get("preset"), ps_id)
    enc_name = SMART_ENCODER[codec]

    # ---- audio: copy what can be cut at packet boundaries
    audio = info["audio"]
    apk = _audio_packets(video, audio) if audio else {}
    a_plan, reenc, worst = {}, [], 0.0
    for n, s in enumerate(audio):
        name = (s.get("codec_name") or "").lower()
        ap = (_audio_plan(apk.get(n), base, ranges)
              if name in _COPY_AUDIO or name.startswith("pcm_") else None)
        if ap:
            a_plan[n] = ap[0]
            worst = max(worst, ap[1])
        else:
            reenc.append(n)
    subs = [s for s in info["subs"]
            if not subs_lang or ((s.get("tags") or {}).get("language") or "und").lower()
            in subs_lang]
    atts = info["attachments"]
    out_ext = os.path.splitext(out_path)[1].lower()
    aux_ext = ".mkv" if out_ext == ".mkv" else ".mp4"

    copy_s = sum(p["t1"] - p["t0"] for r in plan for p in r["parts"] if p["kind"] == "copy")
    enc_s = total - copy_s
    log(f"   Smart cut: copying {copy_s:.1f}s of {total:.1f}s ({copy_s / total * 100:.0f}%),"
        f" re-encoding {enc_s:.1f}s at the joins ({enc_name} crf {_CRF[enc_name]},"
        f" preset {cfg.get('preset') or 'medium'})")
    for r in plan:
        log(f"      {r['name']} [{fmt_time(r['F'])}-{fmt_time(r['E'])}]: "
            + " + ".join(f"{'copy' if p['kind'] == 'copy' else 're-encode'}"
                         f" {p['t1'] - p['t0']:.1f}s" for p in r["parts"]))
    if audio:
        log(f"      audio: {len(a_plan)} track(s) copied"
            + (f" (sync within {worst * 1000:.0f} ms)" if a_plan else "")
            + (f", {len(reenc)} re-encoded" if reenc else ""))

    # ---- progress weights (work units ~ seconds of effort)
    n_win = sum(2 + sum(1 for p in r["parts"][1:]) for r in plan)
    prog = _Progress(on_progress, enc_s + 0.02 * total + (0.1 * total if reenc else 0)
                     + 0.01 * total + 0.5 * n_win)
    cb = {"on_log": log, "stop_event": stop_event}
    tag = temp_tag()
    os.makedirs(TEMP_DIR, exist_ok=True)

    def tmp(name):
        p = os.path.join(TEMP_DIR, f"smart_{tag}_{name}")
        temps.append(p)
        return p

    v_entries, a_entries, aux_entries = [], {n: [] for n in a_plan}, []
    for ri, r in enumerate(plan):
        F, E = r["F"], r["E"]
        # -- one read of the range: copied video, audio pieces, subtitles
        mid = next((p for p in r["parts"] if p["kind"] == "copy"), None)
        outs, checks = [], []
        lo, hi = F, E
        if mid:
            kinfo = kf_info(mid["t0"]) or {"types": []}
            bsf = _drop_outside(mid["t0"] + base, mid["t1"] + base)
            if not _PARAM_SETS[codec] <= set(kinfo["types"]):
                # keyframes without in-band parameter sets: put the
                # source's before each one (they decode after a join, and
                # after a seek past the output's own header)
                bsf += f",{codec}_mp4toannexb"
            mid["file"] = tmp(f"v{ri}_copy.mkv")
            outs += ["-map", "0:V:0", "-c", "copy", "-bsf:v", bsf, "-an", "-sn", "-dn",
                     "-map_metadata", "-1", "-map_chapters", "-1", mid["file"]]
            checks.append((mid["file"], "V:0", mid["n"]))
            mid["bla"] = codec == "hevc" and first_vcl(kinfo) == _HEVC_CRA
        for n, pieces in a_plan.items():
            A0, A1, zero, cnt = pieces[ri]
            lo, hi = min(lo, A0 - base), max(hi, A1 - base)
            # NUT, not MKA: an MKA piece would carry the source's codec delay
            # (AAC priming) - its start then reads a packet late and the
            # demuxer drops its first samples. inpoint pins `zero` exactly.
            f = tmp(f"a{ri}_{n}.nut")
            outs += ["-map", f"0:a:{n}", "-c", "copy", "-bsf:a", _drop_outside(A0, A1),
                     "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1", f]
            checks.append((f, "a:0", cnt))
            a_entries[n].append((f, A1 - zero, zero))
        if subs:
            # + ONE packet of another stream: a range without any subtitle
            # event would leave the piece empty, and the matroska muxer
            # then writes a broken file. (Not mapped into the output.)
            anchor = (["-map", "0:a:0", "-bsf:a", "noise=drop=gt(n\\,0)"] if audio else
                      ["-map", "0:V:0", "-copyinkf", "-bsf:v", "noise=drop=gt(n\\,0)"])
            f = tmp(f"s{ri}{aux_ext}")
            outs += (["-ss", f"{F + base - CUT_EPS:.6f}", "-t", f"{E - F:.6f}",
                      "-output_ts_offset", f"{-(F + base - CUT_EPS):.6f}"]
                     + [x for s in subs for x in ("-map", f"0:{s['index']}")] + anchor
                     + (["-map", "0:t?"] if atts and aux_ext == ".mkv" else [])
                     # (tags kept: a font attachment needs its filename)
                     + ["-c", "copy", "-map_metadata", "0", "-map_chapters", "-1", f])
            aux_entries.append((f, E - F, 0.0))
        seek = lo - 3.0 if lo > 3.5 else 0.0     # read [seek, hi + 3) only
        cmd = (["ffmpeg", "-y"] + (["-ss", f"{seek:.6f}"] if seek else [])
               + ["-t", f"{hi + 3.0 - seek:.6f}", "-copyts", "-i", video] + outs)
        rc = run_ffmpeg_with_progress(cmd, E - F, expected_offset=max(seek, 0.0) + base,
                                      **dict(cb, on_progress=prog.step(0.02 * (E - F))))
        if rc != 0:
            return (-1, "stopped") if rc == -1 else (1, "copying a range failed")
        for f, sel, want in checks:
            got = _count_packets(f, sel)
            if got != want:
                return 1, (f"a copied piece has {got} packets, expected {want}"
                           f" ({os.path.basename(f)})")
        if mid and mid["bla"] and not _cra_to_bla(mid["file"]):
            return 1, "could not mark the copied HEVC keyframe as a splice point"

        # -- re-encoded head / tail (or the whole range)
        for k, p in enumerate(r["parts"]):
            if p["kind"] != "enc":
                continue
            p["file"] = tmp(f"v{ri}_enc{k}.mkv")
            seek = p["t0"] - 10.0
            cmd = (["ffmpeg", "-y"] + (["-ss", f"{seek:.6f}"] if seek > 0.5 else [])
                   + ["-copyts", "-i", video, "-ss", f"{p['t0'] + base - _EPS:.6f}",
                      "-map", "0:V:0", "-frames:v", str(p["n"]), "-an", "-sn", "-dn"]
                   # every frame keeps its source timestamp: passthrough + the
                   # demuxer's time base (the default 1/fps would re-round
                   # them - and merge frames of an uneven / VFR source)
                   + venc + ["-fps_mode", "passthrough", "-enc_time_base:v", "demux",
                             "-map_metadata", "-1", "-map_chapters", "-1", p["file"]])
            rc = run_ffmpeg_with_progress(cmd, p["n"] / fps, fps=fps,
                                          **dict(cb, on_progress=prog.step(p["t1"] - p["t0"])))
            if rc != 0:
                return (-1, "stopped") if rc == -1 else (1, "re-encoding a join failed")
            got = _count_packets(p["file"])
            if got != p["n"]:
                return 1, f"a re-encoded join has {got} frames, expected {p['n']}"
        for p in r["parts"]:
            v_entries.append((p["file"], p["t1"] - p["t0"], None))
        if stop_event.is_set():
            return -1, "stopped"

    # ---- audio tracks that are re-encoded: one seamless pass
    a_file = None
    if reenc:
        a_file = tmp("audio.mka")
        codecs = audio_track_codecs(probe_audio_streams(video))
        rc = build_audio_pass(video, a_file, [(F, E) for F, E, _ in ranges],
                              [(n, codecs[n]) for n in reenc],
                              **dict(cb, on_progress=prog.step(0.1 * total)))
        if rc != 0:
            return (-1, "stopped") if rc == -1 else (1, "the audio pass failed")

    # ---- join everything
    inputs, maps, meta = [], [], []
    vlist = tmp("video.txt")
    _concat_list(vlist, v_entries)
    # every part holds length-prefixed NAL units (4-byte: x264 / x265 and
    # nearly every source) - joined as they are, so the copied packets stay
    # byte-identical. (The concat demuxer's automatic h264_mp4toannexb would
    # add the SPS to every packet that carries a PPS.) Another length size
    # needs that conversion to match the re-encoded parts.
    same_len = codec != "h264" or str(v.get("nal_length_size") or "4") == "4"
    inputs += ["-f", "concat", "-safe", "0", "-auto_convert", "0" if same_len else "1",
               "-i", vlist]
    maps += ["-map", "0:v:0"]
    meta += _stream_args("v:0", v)
    a_in = {}
    for n in a_plan:
        lst = tmp(f"audio{n}.txt")
        _concat_list(lst, a_entries[n])
        inputs += ["-f", "concat", "-safe", "0", "-i", lst]
        a_in[n] = sum(1 for x in inputs if x == "-i") - 1
    re_in = None
    if a_file:
        inputs += ["-i", a_file]
        re_in = sum(1 for x in inputs if x == "-i") - 1
    for n, s in enumerate(audio):
        maps += (["-map", f"{a_in[n]}:a:0"] if n in a_in
                 else ["-map", f"{re_in}:a:{reenc.index(n)}"])
        meta += _stream_args(f"a:{n}", s)
    if aux_entries:
        alist = tmp("aux.txt")
        _concat_list(alist, aux_entries)
        inputs += ["-f", "concat", "-safe", "0", "-i", alist]
        x_in = sum(1 for x in inputs if x == "-i") - 1
        maps += ["-map", f"{x_in}:s?", "-map", f"{x_in}:t?"]
        for i, s in enumerate(subs):
            meta += _stream_args(f"s:{i}", s)
    elif atts and aux_ext == ".mkv":
        # fonts without subtitles: straight from the source's header
        inputs += ["-t", "0", "-i", video]
        x_in = sum(1 for x in inputs if x == "-i") - 1
        maps += ["-map", f"{x_in}:t?"]
    if atts and aux_ext == ".mkv":
        for i, s in enumerate(atts):
            meta += _stream_args(f"t:{i}", s, disposition=False)
    ffmeta = tmp("meta.txt")
    write_ffmetadata(ffmeta, remap_chapters(info["chapters"], base, ranges),
                     fmt_tags=info["format"].get("tags"))
    inputs += ["-f", "ffmetadata", "-i", ffmeta]
    m_in = sum(1 for x in inputs if x == "-i") - 1
    extra = []
    tag_v = (v.get("codec_tag_string") or "").lower()
    if out_ext in (".mp4", ".m4v", ".mov"):
        if tag_v in ("hvc1", "hev1", "avc1", "avc3"):
            extra += ["-tag:v", tag_v]
        extra += ["-movflags", "+faststart"]
    # -copyts: every input is already on the output timeline - without it
    # ffmpeg moves each input's first packet to 0 (a late audio track or
    # the first subtitle would be pulled to the start)
    cmd = (["ffmpeg", "-y", "-copyts"] + inputs + maps
           + ["-c", "copy", "-map_metadata", str(m_in), "-map_chapters", str(m_in),
              "-map_metadata:s", "-1"]
           + meta + extra + ["-ignore_unknown", out_path])
    rc = run_ffmpeg_with_progress(cmd, total, **dict(cb, on_progress=prog.step(0.01 * total)))
    if rc != 0:
        return (-1, "stopped") if rc == -1 else (1, "joining the parts failed")

    # ---- verify: every frame there, every join decodes and matches the source
    want = sum(r["n"] for r in plan)
    got = _count_packets(out_path)
    if got != want:
        return 1, f"the joined video has {got} frames, expected {want}"
    bad = _check_joins(out_path, video, plan, offs, disp, fps, stop_event, prog, log)
    if stop_event.is_set():
        return -1, "stopped"
    if bad:
        return 1, bad
    return 0, ""


def _check_joins(out, src, plan, offs, disp, fps, stop_event, prog, log, k=8):
    """Decode each range's first / last k frames and the 2k frames around
    every join inside it, in the output AND the source, and compare them
    frame by frame (paired by index). Windows start ON a frame (disp: the
    source's sorted frame times) and never run past their range. Returns
    None if all is well, else a reason."""
    wins = []                                   # (source time, frames, output time)
    for r, O in zip(plan, offs):
        F = r["F"]
        i0 = bisect.bisect_left(disp, F - _EPS)
        i1 = i0 + r["n"]
        firsts = ([i0] + [max(i0, bisect.bisect_left(disp, p["t0"] - _EPS) - k)
                          for p in r["parts"][1:]] + [max(i0, i1 - k)])
        sizes = [k] + [2 * k] * (len(r["parts"]) - 1) + [k]
        wins += [(disp[i], min(n, i1 - i), O + disp[i] - F) for i, n in zip(firsts, sizes)]
    step = round(1e6 / fps)
    worst = None
    for t_src, n, t_out in wins:
        if stop_event.is_set():
            return None
        graph = (f"[0:V:0]trim=end_frame={n},settb=AVTB,setpts=N*{step}[a];"
                 f"[1:V:0]trim=end_frame={n},settb=AVTB,setpts=N*{step}[b];"
                 f"[a][b]psnr=shortest=1")
        half = 0.45 / fps                      # seek to just before the frame
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "level+info",
               "-ss", f"{max(0.0, t_out - half):.6f}", "-i", out,
               "-ss", f"{max(0.0, t_src - half):.6f}", "-i", src,
               "-filter_complex", graph, "-f", "null", "-"]
        rc, text = _run_capture_stoppable(cmd, stop_event)
        prog.step(0.5)(1.0, "checking the joins")
        if stop_event.is_set():
            return None
        errs = [ln.strip() for ln in text.splitlines() if "[error]" in ln]
        m = re.search(r"PSNR .*?min:(\S+)", text)
        if rc != 0 or errs:
            return f"decode error near {fmt_time(t_out)}: {(errs or ['ffmpeg failed'])[0][:120]}"
        if not m:
            return f"could not compare the frames near {fmt_time(t_out)}"
        low = float("inf") if m.group(1) == "inf" else float(m.group(1))
        if low < _PSNR_MIN:
            return (f"the picture near {fmt_time(t_out)} does not match the source"
                    f" ({low:.1f} dB)")
        worst = low if worst is None else min(worst, low)
    if worst is not None:
        log(f"   Smart cut check: every join decodes cleanly and matches the source"
            f" (lowest frame {'identical' if worst == float('inf') else f'{worst:.1f} dB'})")
    return None
