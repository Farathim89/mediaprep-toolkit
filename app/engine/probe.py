"""ffprobe helpers: durations, fps, video info, audio/subtitle stream
inventories, bitrate probes and the quick / full integrity checks."""
import os
import json
import subprocess
import threading
import time

from ..config import POPEN_FLAGS


def _probe_json(input_file, extra=(), timeout=120):
    r = _probe(["ffprobe", "-v", "error", *extra, "-of", "json", input_file],
               timeout=timeout)
    if r is None or r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout or b"{}")
    except ValueError:
        return {}


def _stream_bps(s):
    """A stream's bitrate (bit/s) from bit_rate or the MKV 'BPS' statistics tag."""
    for v in [s.get("bit_rate")] + [val for key, val in (s.get("tags") or {}).items()
                                    if key.upper().split("-")[0] == "BPS"]:
        v = str(v or "")
        if v.isdigit() and int(v) > 0:
            return int(v)
    return None


def _sampled_audio_bps(input_file, secs=120):
    """{audio stream index: bit/s} measured by summing packet sizes over the
    first `secs` seconds (demux only, quick) - for streams without a bitrate."""
    data = _probe_json(input_file, ["-select_streams", "a", "-read_intervals",
                                    f"%+{secs}", "-show_entries",
                                    "packet=stream_index,size,pts_time"])
    acc = {}
    for p in data.get("packets", []):
        try:
            idx, size, t = int(p["stream_index"]), int(p["size"]), float(p["pts_time"])
        except (KeyError, ValueError, TypeError):
            continue
        a = acc.setdefault(idx, [0, t, t])
        a[0] += size
        a[1], a[2] = min(a[1], t), max(a[2], t)
    return {i: int(b * 8 / (t1 - t0)) for i, (b, t0, t1) in acc.items() if t1 - t0 > 1.0}


def probe_source_video_bitrate(input_file, data=None):
    """Bitrate (bit/s) of the first real video stream, and how it was found.
    Stream bit_rate (or MKV BPS tag); otherwise estimated as the container
    bitrate (format bit_rate, or size*8/duration) minus every audio stream's
    bitrate (unknown ones measured from the first 2 minutes of packets).
    Returns (bps or None, how)."""
    data = data if data is not None else _probe_json(
        input_file, ["-show_streams", "-show_format"])
    streams = data.get("streams") or []
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        return None, "no video stream"
    bps = _stream_bps(vids[0])
    if bps:
        return bps, "stream"
    fmt = data.get("format") or {}
    total = str(fmt.get("bit_rate") or "")
    total = int(total) if total.isdigit() else None
    if not total:
        try:
            size = int(fmt.get("size") or os.path.getsize(input_file))
            dur = float(fmt.get("duration") or 0)
            total = int(size * 8 / dur) if dur > 0 else None
        except (ValueError, TypeError, OSError):
            total = None
    if not total:
        return None, "unknown"
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    missing = [s for s in audio if not _stream_bps(s)]
    sampled = _sampled_audio_bps(input_file) if missing else {}
    a_sum = 0
    for s in audio:
        b = _stream_bps(s) or sampled.get(s.get("index"))
        if b is None:
            return None, "audio bitrate unknown"
        a_sum += b
    est = total - a_sum
    return (est, "estimated") if est > 0 else (None, "unknown")


def _probe(cmd, timeout=120):
    """Run an ffprobe command and return its CompletedProcess, or None if it
    timed out or couldn't start at all (ffprobe missing / not on PATH) - so a
    probe never raises into, or hangs, the UI."""
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=POPEN_FLAGS, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def probe_video_info(input_file):
    """Codec, pixel format, bit depth and container bitrate of the first
    video stream - used for the source log line and 'Auto' bit depth."""
    result = _probe(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,bits_per_raw_sample",
         "-show_entries", "format=bit_rate",
         "-print_format", "json", input_file])
    info = {"codec": "?", "pix_fmt": "?", "bit_depth": 8, "bitrate_mbps": None}
    if result is None or result.returncode != 0:
        return info
    try:
        data = json.loads(result.stdout)
        s = (data.get("streams") or [{}])[0]
        info["codec"] = s.get("codec_name", "?")
        info["pix_fmt"] = s.get("pix_fmt") or "?"
        bits = str(s.get("bits_per_raw_sample") or "")
        if bits.isdigit() and int(bits) > 0:
            info["bit_depth"] = int(bits)
        elif "12" in info["pix_fmt"]:
            info["bit_depth"] = 12
        elif "10" in info["pix_fmt"]:
            info["bit_depth"] = 10
        br = str((data.get("format") or {}).get("bit_rate") or "")
        if br.isdigit():
            info["bitrate_mbps"] = int(br) / 1e6
    except (ValueError, KeyError, IndexError):
        pass
    return info


def probe_duration(path):
    r = _probe(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", path])
    if r is None:
        return None
    try:
        return float(r.stdout.decode(errors="replace").strip())
    except ValueError:
        return None


def probe_video_duration(path):
    """Duration of the VIDEO stream itself, independent of the container. A
    lingering copied subtitle can push the container duration well past the
    video, so the duration check uses this to avoid false alarms. Uses the
    per-stream duration/DURATION tag (instant); returns None if unavailable."""
    r = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration:stream_tags=DURATION",
                "-of", "default=nw=1", path])
    if r is None:
        return None
    val = None
    for line in r.stdout.decode(errors="replace").splitlines():
        v = line.split("=", 1)[-1].strip()
        if not v or v == "N/A":
            continue
        if ":" in v:                                  # HH:MM:SS.mmm tag form
            try:
                h, m, s = v.split(":")
                val = int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                continue
        else:                                         # plain seconds
            try:
                val = float(v)
            except ValueError:
                continue
    return val


def probe_video_fps(input_file):
    result = _probe(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate,r_frame_rate",
         "-of", "csv=p=0", input_file])
    if result is None or result.returncode != 0:
        return None
    for token in result.stdout.decode(errors="replace").strip().split(","):
        token = token.strip()
        if "/" in token:
            num, _, den = token.partition("/")
            try:
                num, den = float(num), float(den)
                if num > 0 and den > 0:
                    return num / den
            except ValueError:
                pass
        elif token:
            try:
                v = float(token)
                if v > 0:
                    return v
            except ValueError:
                pass
    return None


def probe_audio_streams(input_file):
    result = _probe(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a", input_file])
    if result is None or result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return [{"index": s.get("index", 0),
             "codec_name": s.get("codec_name", "aac"),
             "channels": s.get("channels", 2),
             "sample_rate": int(s["sample_rate"]) if str(s.get("sample_rate", "")).isdigit() else None,
             "lang": ((s.get("tags") or {}).get("language") or "und").lower()}
            for s in data.get("streams", [])]


def probe_sample_rate(input_file):
    """Sample rate (Hz) of the first audio stream, or None."""
    r = _probe(["ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate", "-of", "csv=p=0", input_file])
    if r is None:
        return None
    v = r.stdout.decode(errors="replace").strip().split(",")[0].strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


def audio_track_for_lang(input_file, lang):
    """Index (within the audio streams, i.e. 0:a:N) of the first audio track
    whose language matches `lang`; None if there isn't one. Used so detection
    can fingerprint a chosen language track instead of the default."""
    if not lang:
        return None
    for i, s in enumerate(probe_audio_streams(input_file)):
        if s.get("lang") == lang.lower():
            return i
    return None


def video_kind(input_file):
    """'video' if the file has a real video stream, 'cover' if its only
    'video' streams are attached cover art (an MP3/M4A/FLAC with a picture),
    else None (also when ffprobe can't read it)."""
    r = _probe(["ffprobe", "-v", "error", "-select_streams", "v",
                "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
                "-of", "json", input_file], timeout=60)
    if r is None or r.returncode != 0:
        return None
    try:
        streams = json.loads(r.stdout or b"{}").get("streams", [])
    except ValueError:
        return None
    if not streams:
        return None
    if any(not (s.get("disposition") or {}).get("attached_pic") for s in streams):
        return "video"
    return "cover"


def has_video_stream(input_file):
    """True if the file has a REAL video stream - attached cover art (an
    attached_pic stream, e.g. in an MP3) doesn't count."""
    return video_kind(input_file) == "video"


def _channels_label(ch, layout=""):
    layout = (layout or "").split("(")[0]
    if layout in ("5.1", "7.1", "6.1", "5.0", "7.0", "quad", "mono", "stereo"):
        return {"mono": "1.0", "stereo": "2.0"}.get(layout, layout)
    return {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch" if ch else "?ch")


def probe_audio_tracks(input_file):
    """The audio tracks of a file for a track picker: a list of dicts with
    'track' (N in 0:a:N), 'index' (absolute stream index), 'lang', 'codec',
    'channels', 'layout' ('5.1', '2.0' ...), 'title' and a ready 'label' like
    "#1 eng AC3 5.1 'Main'". Empty list if none / unreadable."""
    r = _probe(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
                "-select_streams", "a", input_file])
    if r is None or r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or b"{}")
    except ValueError:
        return []
    out = []
    for n, s in enumerate(data.get("streams", [])):
        tags = s.get("tags") or {}
        tags = {k.lower(): v for k, v in tags.items()}
        lang = (tags.get("language") or "und").lower()
        ch = s.get("channels") or 0
        layout = _channels_label(ch, s.get("channel_layout"))
        codec = (s.get("codec_name") or "?").upper()
        title = (tags.get("title") or "").strip()
        label = f"#{n + 1} {lang} {codec} {layout}" + (f" '{title}'" if title else "")
        out.append({"track": n, "index": s.get("index", n), "lang": lang,
                    "codec": codec, "channels": ch, "layout": layout,
                    "title": title, "label": label})
    return out


def probe_streams(input_file):
    """Inventory a file's streams for the Compare tab. Returns a dict:
    {duration, size, video:[...], audio:[...], subtitle:[...]} or None."""
    r = _probe(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                "-print_format", "json", input_file])
    if r is None:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    fmt = data.get("format", {}) or {}
    info = {"duration": None, "size": None, "video": [], "audio": [], "subtitle": []}
    try:
        info["duration"] = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    try:
        info["size"] = int(fmt.get("size"))
    except (TypeError, ValueError):
        pass
    for st in data.get("streams", []):
        ct = st.get("codec_type")
        lang = ((st.get("tags") or {}).get("language") or "und")
        if ct == "video":
            info["video"].append({"codec": st.get("codec_name", "?"),
                                  "w": st.get("width"), "h": st.get("height")})
        elif ct == "audio":
            info["audio"].append({"codec": st.get("codec_name", "?"), "lang": lang,
                                  "channels": st.get("channels")})
        elif ct == "subtitle":
            tags = st.get("tags") or {}
            disp = st.get("disposition") or {}
            info["subtitle"].append({
                "codec": st.get("codec_name", "?"), "lang": lang,
                "title": tags.get("title", ""),
                "forced": bool(disp.get("forced")),
                "default": bool(disp.get("default"))})
    return info


def probe_subtitle_inventory(files, sample=None, progress=None, stop_event=None):
    """Scan the given files and return the distinct subtitle LANGUAGES present,
    each as {"lang","title","forced","count"} where count is how many files
    carry that language. sample=None scans ALL files (default); an int caps it.
    progress(done, total) is called as it goes; stop_event aborts early. Lets
    the UI show what subtitle tracks actually exist (tags are unreliable, so
    und/unknown shows up too) so the user can pick which to keep."""
    seen = {}
    total = len(files) if sample is None else min(sample, len(files))
    for i, f in enumerate(files):
        if sample is not None and i >= sample:
            break
        if stop_event is not None and stop_event.is_set():
            break
        if progress:
            progress(i, total)
        info = probe_streams(f)
        if not info:
            continue
        here = set()
        for s in info["subtitle"]:
            lang = (s.get("lang") or "und").lower()
            d = seen.setdefault(lang, {"lang": lang, "title": s.get("title", ""),
                                       "forced": s.get("forced", False), "count": 0})
            if s.get("title") and not d["title"]:
                d["title"] = s["title"]
            if s.get("forced"):
                d["forced"] = True
            here.add(lang)
        for lang in here:
            seen[lang]["count"] += 1
    if progress:
        progress(total, total)
    return sorted(seen.values(), key=lambda d: (-d["count"], d["lang"]))


def quick_check(input_file):
    """Fast integrity check - does ffprobe open the file with valid streams and a
    duration? Catches truncated/corrupt headers, zero-byte files, wrong container.
    Returns (ok, detail)."""
    if not os.path.isfile(input_file) or os.path.getsize(input_file) == 0:
        return False, "missing or zero-byte file"
    info = probe_streams(input_file)
    if info is None:
        return False, "ffprobe could not read it (corrupt header / not media)"
    ns = len(info["video"]) + len(info["audio"]) + len(info["subtitle"])
    if ns == 0:
        return False, "no readable streams"
    if not info["duration"] or info["duration"] <= 0:
        return False, "no valid duration"
    return True, (f"{info['duration']:.0f}s, {len(info['video'])}v/"
                  f"{len(info['audio'])}a/{len(info['subtitle'])}s")


def full_check(input_file, stop_event=None, timeout=1800):
    """Thorough integrity check - decode the ENTIRE file and report any decode
    errors (catches corruption anywhere, not just the header). Slow: reads the
    whole file. Returns (ok, detail); ok is None if stopped.

    stderr is drained in a background thread so a chatty file (e.g. one with a
    broken/missing index, which makes ffmpeg emit lots of warnings) can't fill
    the pipe buffer and deadlock the process - that was making the tool hang.
    A wall-clock *timeout* is a final safety net for a file that never finishes.
    """
    # Decode only the video + audio streams. Mapping subtitle/data streams
    # (-map 0) makes the null muxer try to *encode* the subtitle track, which it
    # can't, so it aborts with "encoder selection failed for format null" - a
    # false alarm that has nothing to do with corruption. -sn -dn drop subs/data;
    # the "?" makes v/a optional so audio-only or video-only files still work.
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", input_file,
         "-map", "0:v?", "-map", "0:a?", "-sn", "-dn", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)

    lines = []

    def _drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                lines.append(raw.decode(errors="replace"))
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    def _kill():
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except OSError:
            pass

    t0 = time.time()
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            _kill()
            return None, "stopped"
        if timeout and (time.time() - t0) > timeout:
            _kill()
            return False, (f"timed out after {int(timeout)}s - file may have a "
                           "broken index or be unreadable")
        time.sleep(0.1)
    reader.join(timeout=2)

    err = "".join(lines)
    # the null muxer emits benign "non monotonically increasing dts" noise even
    # for good files - ignore it; real corruption shows other decode/demux errors.
    _benign = ("monotonically increasing dts", "Last message repeated",
               "Automatic encoder selection failed", "for format null",
               "probably disabled")
    real = [ln.strip() for ln in err.splitlines()
            if ln.strip() and not any(b in ln for b in _benign)]
    if real:
        extra = f"  (+{len(real) - 1} more)" if len(real) > 1 else ""
        return False, real[0][:140] + extra
    if proc.returncode not in (0, None):
        return False, f"ffmpeg exited with code {proc.returncode}"
    return True, "decoded clean, no errors"



def _video_packet_times(path, intervals=None):
    """(pts, end) of the first video stream's packets, all of them or only
    those in `intervals` (ONE ffprobe -read_intervals range)."""
    r = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0"]
               + (["-read_intervals", intervals] if intervals else [])
               + ["-show_entries", "packet=pts_time,duration_time",
                  "-of", "csv=p=0", path])
    out = []
    if r is None or r.returncode != 0:
        return out
    for ln in r.stdout.decode(errors="replace").split():
        a = ln.split(",")
        try:
            pts = float(a[0])
        except (ValueError, IndexError):
            continue
        try:
            dur = float(a[1]) if len(a) > 1 else 0.0
        except ValueError:
            dur = 0.0
        out.append((pts, pts + dur))
    return out


def probe_first_frame_at(path, t, tries=3):
    """Timestamp of the first video frame at/after t (in the file's own
    timeline, as ffmpeg -copyts sees it) - the frame a frame-accurate cut
    starting at t begins with. None if unknown.
    ffprobe's -read_intervals now and then returns a truncated packet list
    (exit code 0), so only a read that covers the whole window counts."""
    lo, hi = max(0.0, t - 3.0), t + 3.0
    end = None
    for _ in range(tries):
        pts = [p for p, _ in _video_packet_times(path, f"{lo:.3f}%{hi:.3f}")]
        after = [p for p in pts if p >= t - 6e-4]       # encode.CUT_EPS: ms-rounded t
        if not after:
            continue
        need = t + 1.0                     # complete = reaches well past t
        if max(pts) < need:                # ... or the file ends there
            if end is None:
                end = probe_duration(path) or 0.0
            need = min(need, end - 0.5)
        if max(pts) >= need:
            return min(after)
    return None


def count_frames_between(path, t0, t1, tries=3):
    """How many video frames have t0 <= pts < t1 (the file's own timeline,
    as ffmpeg -copyts sees it) - from the packets, no decoding. None when
    the packet list can't be read completely."""
    end = None
    for _ in range(tries):
        pts = [p for p, _ in _video_packet_times(path, f"{max(0.0, t0 - 2.0):.3f}%{t1 + 2.0:.3f}")]
        if not pts:
            continue
        need = t1 + 0.5                    # complete = reaches past t1 ...
        if max(pts) < need:                # ... or the file ends there
            if end is None:
                end = probe_duration(path) or 0.0
            need = min(need, end - 0.5)
        if max(pts) >= need and (t0 < 2.0 or min(pts) <= t0 - 0.2):
            return sum(1 for p in pts if t0 <= p < t1)
    return None


def probe_piece_timing(path, tries=3):
    """(container start, first video frame, video end) of an encoded cut piece,
    in seconds, or None. The concat demuxer places a file's packets at
    (its offset + ts - container start), so these give the exact span the
    piece's video covers on the joined timeline. Reads every video packet
    (a piece is short; no seeking, which ffprobe sometimes truncates)."""
    data = _probe_json(path, ["-show_format"])
    try:
        cstart = float((data.get("format") or {}).get("start_time"))
    except (TypeError, ValueError):
        cstart = 0.0
    hint = probe_video_duration(path)
    best = None
    for _ in range(tries):
        times = _video_packet_times(path)
        if not times:
            continue
        cand = (cstart, min(p for p, _ in times), max(e for _, e in times))
        if best is None or cand[2] > best[2]:
            best = cand
        if not hint or cand[2] >= hint - 0.5:
            break
    return best
