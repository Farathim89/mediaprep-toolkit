"""Convert engine (engine/recode.py): the ffmpeg command of a job -
build_recode_cmd (video / audio / subtitle maps, filters, encoder options)
and which encoders this PC can run (encoder_usable, resolve_encoder). No Tk."""
import subprocess
import threading

from ..config import limit_cmd, NVENC_PRESET_MAP, popen_flags, SVT_PRESET_MAP
from .encode import NVENC_CPU_FALLBACK, nvenc_available
from .recode import (NVENC, _CPU_FALLBACK, _FPS_EXACT, _HDR_TRC, _HQDN3D, _MP4_AUDIO_BAD,
                     _NLMEANS, _TEN_BIT, _TEN_BIT_AUTO, _crop_of, _out_dims, default_bitrate)


_AV1_NV = {}
_AV1_LOCK = threading.Lock()


def encoder_usable(enc):
    """True if this PC can run `enc` (NVENC needs an NVIDIA GPU + driver)."""
    if enc in NVENC_CPU_FALLBACK:
        return nvenc_available(enc)
    if enc != "av1_nvenc":
        return True
    with _AV1_LOCK:
        if "ok" not in _AV1_NV:
            try:
                r = subprocess.run(
                    limit_cmd(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                               "-i", "color=s=256x256:d=0.1", "-frames:v", "1", "-c:v",
                               "av1_nvenc", "-f", "null", "-"]), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=popen_flags(), timeout=30)
                _AV1_NV["ok"] = r.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                _AV1_NV["ok"] = False
        return _AV1_NV["ok"]


def _has_filter(name, _cache={}):
    if name not in _cache:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=popen_flags(), timeout=30)
            names = {ln.split()[1] for ln in r.stdout.decode(errors="replace").splitlines()
                     if len(ln.split()) > 2 and ln.startswith(" ")}
        except (subprocess.TimeoutExpired, OSError, IndexError):
            names = set()
        for n in ("zscale", "libplacebo", "tonemap"):
            _cache[n] = n in names
    return _cache.get(name, False)


# ======================= command builder =======================
def _ff_path(path):
    """A file path as a filter option value inside a filtergraph (two
    escaping levels: option value, then the graph)."""
    p = path.replace("\\", "/")
    for ch in "\\':":
        p = p.replace(ch, "\\" + ch)
    out = ""
    for ch in p:
        out += "\\" + ch if ch in "\\'[],;" else ch
    return out


def resolve_encoder(enc, log=None):
    """The encoder that really runs: NVENC -> CPU fallback without a GPU."""
    if enc in NVENC and not encoder_usable(enc):
        cpu = _CPU_FALLBACK[enc]
        if log:
            log(f"   [WARN] {enc} is not usable here (no NVIDIA GPU/driver?) - "
                f"using {cpu} (CPU) instead")
        return cpu
    return enc


def _out_fps(job):
    s = job["settings"]
    if s["fps"] != "source":
        num = _FPS_EXACT.get(s["fps"])
        if num:
            a, b = num.split("/")
            return float(a) / float(b)
        return float(s["fps"])
    return job["info"]["video"]["fps"]


def _tonemap_chain(v, depth):
    trc = v["transfer"] if v["transfer"] in _HDR_TRC else "smpte2084"
    fmt = "yuv420p10le" if depth >= 10 else "yuv420p"
    if _has_filter("zscale") and _has_filter("tonemap"):
        prim = v["primaries"] or "bt2020"
        mat = v["matrix"] if v["matrix"] in ("bt2020nc", "bt2020c") else "bt2020nc"
        return (f"zscale=tin={trc}:pin={prim}:min={mat}:t=linear:npl=100,format=gbrpf32le,"
                "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv"), "zscale + tonemap (hable)"
    if _has_filter("libplacebo"):
        return (f"libplacebo=tonemapping=hable:colorspace=bt709:color_primaries=bt709:"
                f"color_trc=bt709:range=tv:format={fmt}"), "libplacebo (hable)"
    return None, "no tone-mapping filter in this ffmpeg"


def _overlay_fmt(v):
    """overlay's working format for a bitmap-subtitle burn-in: the source
    picture's chroma layout and depth. Its default (yuv420) is 8-bit, and
    format=auto with the RGB subtitle picture even picks 8-bit RGB - either
    squeezes a 10-bit / HDR picture through 8 bits (banding)."""
    pix = v.get("pix_fmt") or ""
    chroma = "444" if "444" in pix else "422" if "422" in pix else "420"
    return f"yuv{chroma}" + ("p10" if v.get("bit_depth", 8) >= 10 else "")


def build_recode_cmd(job, output, log=None, sample=None):
    """ffmpeg argv for `job` writing `output` (container from its extension).
    sample=(start, length): only that part (Preview). Returns (cmd, plan);
    plan = {"encoder", "fps", "duration", "audio": n, "subs": n, "w", "h",
    "tonemap": bool, "notes": [log lines]}."""
    log = log or (lambda m: None)
    info, s = job["info"], job["settings"]
    v = info["video"]
    mp4 = output.lower().endswith(".mp4")
    notes = []
    plan = {"tonemap": False, "notes": notes}
    enc = s["vcodec"]
    plex = enc in ("plex_hevc", "plex_h264")
    if plex:
        enc = "hevc_nvenc" if enc == "plex_hevc" else "h264_nvenc"
    enc = resolve_encoder(enc, log)
    plan["encoder"] = enc
    copy_v = enc == "copy"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    # a sample: fast seek to 10 s before, then an exact output -ss - an input
    # -ss alone starts COPIED audio at the earlier keyframe (out of sync)
    coarse = max(0.0, sample[0] - 10.0) if sample else 0.0
    if sample and coarse:
        cmd += ["-ss", f"{coarse:.3f}"]
    cmd += ["-i", job["input"]]
    if sample:
        cmd += ["-ss", f"{sample[0] - coarse:.3f}", "-t", f"{sample[1]:.3f}"]

    # ---------------- video ----------------
    depth = 8
    if not copy_v:
        want = s["depth"]
        if want == "auto":
            depth = 10 if v["bit_depth"] >= 10 and enc in _TEN_BIT_AUTO else 8
        else:
            depth = 10 if want == "10" else 8
        if depth == 10 and enc not in _TEN_BIT:
            notes.append(f"{enc} is 8-bit only - 10-bit not possible, using 8-bit")
            depth = 8
    plan["depth"] = depth
    pre, post = [], []          # filters before / after a PGS overlay
    burn = [t for t in job["subs"] if t.get("burn")]
    burn = burn[0] if burn else None
    bsrc = info["subs"][burn["src"]] if burn and burn["src"] < len(info["subs"]) else None
    if not copy_v:
        de = s["deint"]
        if de == "auto":
            det = job.get("detect", {}).get("interlace")
            de = "bwdif" if det and det.get("interlaced") else "off"
            notes.append("deinterlace auto: " + ("interlaced -> bwdif" if de == "bwdif"
                                                 else "progressive - not needed"))
        if de == "bwdif":
            pre.append("bwdif=mode=send_frame:parity=auto:deint=all")
        elif de == "yadif":
            pre.append("yadif=mode=send_frame:parity=auto:deint=all")
        c = _crop_of(job)
        if c:
            w, h = v["w"] - c[0] - c[1], v["h"] - c[2] - c[3]
            post.append(f"crop={w}:{h}:{c[0]}:{c[2]}")
        if s["tonemap"] and v["transfer"] in _HDR_TRC:
            chain, how = _tonemap_chain(v, depth)
            if chain:
                post.append(chain)
                plan["tonemap"] = True
                notes.append(f"HDR -> SDR ({v['transfer']} -> bt709): {how}")
            else:
                notes.append(f"[WARN] HDR source but {how} - kept as HDR")
        if s["denoise"] in _HQDN3D:
            post.append((_NLMEANS if s["denoise_method"] == "nlmeans" else _HQDN3D)[s["denoise"]])
        if s["deblock"]:
            post.append("deblock=filter=weak:block=4")
        ow, oh = _out_dims(job)
        cw, ch = (v["w"] - c[0] - c[1], v["h"] - c[2] - c[3]) if c else (v["w"], v["h"])
        if (ow, oh) == (cw - cw % 2, ch - ch % 2):          # no resize
            if (ow, oh) != (cw, ch):
                # odd width / height: x264 / x265 / NVENC 4:2:0 refuse it -
                # drop the last column / row (no resampling)
                post.append(f"crop={ow}:{oh}:0:0")
                notes.append(f"odd frame size {cw}x{ch} -> {ow}x{oh} (4:2:0 needs even)")
            if s.get("height") and int(s["height"]) > ch and s.get("no_upscale"):
                notes.append(f"resize to {s['height']}p skipped (never upscale; source is {ch}p)")
        else:
            post.append(f"scale={ow}:{oh}:flags=lanczos")
        if bsrc and bsrc["text"]:
            sub_f = (f"subtitles=filename={_ff_path(job['input'])}:si={burn['src']}")
            if coarse:      # the filter reads the file from 0 - shift into its timeline
                sub_f = f"setpts=PTS+{coarse:.3f}/TB,{sub_f},setpts=PTS-{coarse:.3f}/TB"
            post.append(sub_f)
            notes.append(f"burn-in: subtitle #{burn['src'] + 1} ({bsrc['codec']})")
        fps = s["fps"]
        if fps != "source" and s["fps_mode"] == "cfr":
            post.append(f"fps={_FPS_EXACT.get(fps, fps)}")
        pix = ("p010le" if depth >= 10 else "yuv420p") if enc in NVENC else (
            "yuv420p10le" if depth >= 10 else "yuv420p")
        post.append(f"format={pix}")
        plan["w"], plan["h"] = ow, oh
        vin = f"[0:{v['idx']}]"
        if bsrc and not bsrc["text"]:
            graph = (f"{vin}{','.join(pre) or 'null'}[pv];"
                     f"[pv][0:{bsrc['idx']}]overlay=eof_action=pass:format={_overlay_fmt(v)}[ov];"
                     f"[ov]{','.join(post)}[vout]")
            notes.append(f"burn-in: subtitle #{burn['src'] + 1} ({bsrc['codec']}, overlay)")
        else:
            graph = f"{vin}{','.join(pre + post)}[vout]"
        cmd += ["-filter_complex", graph, "-map", "[vout]"]
    else:
        cmd += ["-map", f"0:{v['idx']}"]
        plan["w"], plan["h"] = v["w"], v["h"]
        if burn or _crop_of(job) or s.get("height") or s["deint"] != "off" or \
                s["denoise"] != "off" or s["deblock"]:
            notes.append("[WARN] video is copied - crop / resize / filters / burn-in are ignored")

    # encoder args
    q = int(s["crf"])
    cap = None
    if not copy_v and s["cap"] and v.get("bps"):
        cap = int(v["bps"] * 0.95)
    if copy_v:
        cmd += ["-c:v", "copy"]
    elif enc in NVENC:
        cmd += ["-c:v", enc, "-preset", NVENC_PRESET_MAP.get(s["preset"], "p5"),
                "-tune", "hq", "-rc", "vbr"]
        if plex:
            cmd += ["-multipass", "fullres"]
        if s["rate"] == "bitrate":
            cmd += ["-b:v", f"{int(s['bitrate'])}k"]
        else:
            cmd += ["-cq", str(q), "-b:v", "0"]
        if enc == "h264_nvenc":
            cmd += ["-profile:v", "high"]
    elif enc == "libsvtav1":
        cmd += ["-c:v", enc, "-preset", SVT_PRESET_MAP.get(s["preset"], "8")]
        cmd += (["-b:v", f"{int(s['bitrate'])}k"] if s["rate"] == "bitrate"
                else ["-crf", str(q)])
    else:
        cmd += ["-c:v", enc, "-preset", s["preset"]]
        cmd += (["-b:v", f"{int(s['bitrate'])}k"] if s["rate"] == "bitrate"
                else ["-crf", str(q)])
        if enc == "libx265":
            cmd += ["-x265-params", "log-level=error"]
    if cap and not copy_v:
        if s["rate"] == "bitrate" and int(s["bitrate"]) * 1000 > cap:
            cmd[cmd.index("-b:v") + 1] = str(cap)
        cmd += ["-maxrate:v", str(cap), "-bufsize:v", str(2 * cap)]
        notes.append(f"never bigger: video capped at {cap / 1e6:.1f} Mb/s "
                     f"(source {v['bps'] / 1e6:.1f})")
    elif s["cap"] and not copy_v:
        notes.append("[WARN] never bigger: source video bitrate unknown - no cap")
    if plan["tonemap"]:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if not copy_v and mp4 and enc in ("libx265", "hevc_nvenc"):
        cmd += ["-tag:v", "hvc1"]
    out_fps = _out_fps(job)
    if s["fps"] != "source" and s["fps_mode"] == "vfr" and not copy_v:
        cmd += ["-fpsmax", _FPS_EXACT.get(s["fps"], s["fps"]), "-fps_mode", "vfr"]
        out_fps = None
    elif not copy_v:
        cmd += ["-fps_mode", "vfr" if s["fps_mode"] == "vfr" else "cfr"]
        if s["fps_mode"] == "vfr":
            out_fps = None
    plan["fps"] = out_fps

    # ---------------- audio ----------------
    na = 0
    for t in job["audio"]:
        if not t.get("keep") or t["src"] >= len(info["audio"]):
            continue
        a = info["audio"][t["src"]]
        codec, br, mix = t.get("codec") or "copy", t.get("bitrate") or "", t.get("mix") or "keep"
        ch = a["channels"] or 2
        want_ch = {"stereo": 2, "5.1": 6}.get(mix)
        if want_ch and want_ch >= ch:
            want_ch = None             # never up-mix
        if codec == "copy" and want_ch:
            codec = "aac"
            br = br or default_bitrate("aac", want_ch)
            notes.append(f"audio #{t['src'] + 1}: mixdown needs a re-encode -> AAC")
        if codec == "copy" and mp4 and (a["codec"] in _MP4_AUDIO_BAD
                                        or a["codec"].startswith("pcm_")):
            codec, br = "eac3", "640k"
            notes.append(f"audio #{t['src'] + 1}: MP4 can't hold {a['codec']} -> E-AC3 640k")
        if codec in ("ac3", "eac3") and (want_ch or ch) > 6:
            want_ch = 6
        cmd += ["-map", f"0:{a['idx']}", f"-c:a:{na}", codec]
        if codec not in ("copy", "flac") and br:
            cmd += [f"-b:a:{na}", br]
        if want_ch:
            cmd += [f"-ac:a:{na}", str(want_ch)]
        if codec == "libopus" and (want_ch or ch) == 6:
            cmd += [f"-filter:a:{na}", "aformat=channel_layouts=5.1"]
        cmd += [f"-metadata:s:a:{na}", f"language={a['lang']}",
                f"-metadata:s:a:{na}", f"title={t.get('title') or ''}",
                f"-disposition:a:{na}", "default" if t.get("default") else "0"]
        na += 1
    plan["audio"] = na

    # ---------------- subtitles ----------------
    ns = 0
    dropped = []
    for t in job["subs"]:
        if not t.get("keep") or t.get("burn") or t["src"] >= len(info["subs"]):
            continue
        if sample:          # a preview is about the picture / sound (copied
            continue        # subtitles that began before -ss shift its start)
        x = info["subs"][t["src"]]
        if mp4:
            if not x["text"]:
                dropped.append(f"#{t['src'] + 1} {x['codec']}")
                continue
            codec = "mov_text"
            if x["codec"] != "mov_text":
                notes.append(f"subtitle #{t['src'] + 1}: {x['codec']} -> mov_text (MP4)")
        elif x["codec"] == "mov_text" or (s["subs_srt"] and x["text"] and x["codec"] != "subrip"):
            codec = "srt"
            notes.append(f"subtitle #{t['src'] + 1}: {x['codec']} -> SRT")
        else:
            codec = "copy"
        flags = "+".join(f for f, on in (("default", t.get("default")),
                                         ("forced", t.get("forced"))) if on) or "0"
        cmd += ["-map", f"0:{x['idx']}", f"-c:s:{ns}", codec,
                f"-metadata:s:s:{ns}", f"language={x['lang']}",
                f"-disposition:s:{ns}", flags]
        if x["title"]:
            cmd += [f"-metadata:s:s:{ns}", f"title={x['title']}"]
        ns += 1
    if dropped:
        notes.append(f"[WARN] MP4 can't hold bitmap subtitles - dropped {', '.join(dropped)}"
                     " (use MKV or burn one in)")
    plan["subs"] = ns

    # ---------------- attachments / chapters / metadata ----------------
    if info["attachments"] and not sample:
        if mp4:
            if s["attachments"]:
                notes.append(f"[WARN] MP4 can't hold attachments - {info['attachments']} "
                             "font(s) / attachment(s) dropped")
        elif s["attachments"]:
            cmd += ["-map", "0:t?"]      # last - after the subtitle streams
    if sample:
        cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += ["-map_chapters", "0" if s["chapters"] and not sample else "-1",
            "-map_metadata", "0" if s["metadata"] else "-1"]
    if mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "4096", output]
    plan["duration"] = (sample[1] if sample else info.get("vduration") or info["duration"]) or 0
    return cmd, plan
