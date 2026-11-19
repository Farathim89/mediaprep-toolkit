"""Join engine (engine/join.py): the subtitle tracks of a re-encoded join.

Text subtitles of every part are merged into one track on the joined
timeline - ASS (header + styles of the first part, styles the others add,
every event moved and clipped to its part) or SRT (cues moved, numbered
again). Picture subtitles (PGS / VobSub / DVB) are stream-copied: each
part's track is copied out and the pieces are put one after another with
the concat demuxer, each part as long as it lasts in the joined file; a
part without the track simply has no subtitles there. No Tk."""
import os
import re

from .probe import _probe, _probe_json
from .process import _run_capture_stoppable

# ======================= subtitles =======================
_ASS_TIME = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})$")
_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _ass_t(text):
    m = _ASS_TIME.match(text.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac) / (10 ** len(frac))


def _ass_fmt(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _sections(text):
    """ASS text -> [(section name, [lines])] in file order."""
    out, cur = [], None
    for ln in text.splitlines():
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            cur = (st[1:-1].strip(), [])
            out.append(cur)
        elif cur is not None:
            cur[1].append(ln)
        elif st:
            out.append(("", [ln]))
    return out


def _fields(fmt_line):
    return [f.strip().lower() for f in fmt_line.split(":", 1)[1].split(",")]


def merge_ass(texts, offsets, spans):
    """ASS scripts -> one: the first one's header and styles (+ styles the
    others add under new names), every event moved by its part's offset and
    clipped to its part."""
    first = _sections(texts[0])
    style_fmt, style_names, styles_at = None, set(), None
    for k, (name, lines) in enumerate(first):
        if name.lower() in ("v4+ styles", "v4 styles"):
            styles_at = k
            for ln in lines:
                if ln.lower().startswith("format:"):
                    style_fmt = _fields(ln)
                elif ln.lower().startswith("style:"):
                    style_names.add(ln.split(":", 1)[1].split(",")[0].strip())
    ev_line = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ev_fmt = _fields(ev_line)
    events, extra_styles = [], []
    for k, (text, off, span) in enumerate(zip(texts, offsets, spans)):
        fmt = None
        for name, lines in _sections(text):
            low = name.lower()
            if low in ("v4+ styles", "v4 styles") and k:
                sfmt = None
                for ln in lines:
                    if ln.lower().startswith("format:"):
                        sfmt = _fields(ln)
                    elif ln.lower().startswith("style:") and sfmt == style_fmt:
                        nm = ln.split(":", 1)[1].split(",")[0].strip()
                        if nm not in style_names:
                            style_names.add(nm)
                            extra_styles.append(ln)
            elif low == "events":
                for ln in lines:
                    head, _, rest = ln.partition(":")
                    kind = head.strip().lower()
                    if kind == "format":
                        fmt = _fields(ln)
                        if k == 0:
                            ev_fmt, ev_line = fmt, ln.strip()
                        continue
                    if kind not in ("dialogue", "comment"):
                        continue
                    f = fmt or ev_fmt
                    vals = rest.lstrip().split(",", len(f) - 1)
                    if len(vals) != len(f):
                        continue
                    d = dict(zip(f, vals))
                    a, b = _ass_t(d.get("start", "")), _ass_t(d.get("end", ""))
                    if a is None or b is None or a >= span:
                        continue
                    d["start"], d["end"] = _ass_fmt(off + a), _ass_fmt(off + min(b, span))
                    row = [d.get(x, "") for x in ev_fmt]
                    events.append((off + a, f"{head.strip()}: {','.join(row)}"))
    out = []
    for k, (name, lines) in enumerate(first):
        if name.lower() == "events":
            continue
        if name:
            out.append(f"[{name}]")
        out += [ln for ln in lines if ln.strip()] + ([] if k == styles_at else [""])
        if k == styles_at:
            out += extra_styles + [""]
    out += ["[Events]", ev_line]
    out += [e for _t, e in sorted(events, key=lambda x: x[0])]
    return "\n".join(out) + "\n"


def _srt_fmt(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_srt(texts, offsets, spans):
    """SRT files -> one, every cue moved by its part's offset and clipped
    to its part, numbered again."""
    cues = []
    for text, off, span in zip(texts, offsets, spans):
        for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
            lines = block.split("\n")
            for j, ln in enumerate(lines):
                m = _SRT_TIME.search(ln)
                if not m:
                    continue
                g = [int(x) for x in m.groups()]
                a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 10 ** len(m.group(4))
                b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 10 ** len(m.group(8))
                body = [x for x in lines[j + 1:]]
                if a < span and body:
                    cues.append((off + a, off + min(b, span), body))
                break
    cues.sort(key=lambda c: c[0])
    out = []
    for n, (a, b, body) in enumerate(cues, 1):
        out += [str(n), f"{_srt_fmt(a)} --> {_srt_fmt(b)}"] + body + [""]
    return "\n".join(out) + "\n"


def merge_text_track(infos, i, offsets, spans, tmp, log, stop_event):
    """Text subtitle track i of every file merged onto the joined timeline
    (ASS when every part is ASS / SSA - styles of all parts kept -, else
    SRT). Returns (merged file, 'ass' / 'srt') or None (logged)."""
    tracks = [fi["subs"][i] for fi in infos]
    ass = all(t["codec"] in ("ass", "ssa") for t in tracks)
    ext = "ass" if ass else "srt"
    texts = []
    for k, (fi, t) in enumerate(zip(infos, tracks)):
        if stop_event.is_set():
            return None
        dst = os.path.join(tmp, f"sub{i}_{k}.{ext}")
        codec = "copy" if t["codec"] == ("ass" if ass else "subrip") else ext
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-v", "error", "-i", fi["path"],
               "-map", f"0:{t['idx']}", "-c:s", codec, dst]
        rc, err = _run_capture_stoppable(cmd, stop_event)
        if rc != 0 or not os.path.isfile(dst):
            if rc != -1:
                log(f"   [WARN] subtitle track {i + 1}: could not read it from part {k + 1}: "
                    f"{(err or '').strip()[:120]}")
            return None
        with open(dst, "r", encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
    merged = os.path.join(tmp, f"merged{i}.{ext}")
    with open(merged, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(merge_ass(texts, offsets, spans) if ass else merge_srt(texts, offsets, spans))
    if not ass and any(t["codec"] in ("ass", "ssa") for t in tracks):
        log(f"   subtitle track {i + 1}: mixed formats -> SRT (styles are lost)")
    return merged, ext


def _packet_count(path):
    """Number of packets in a (one-stream) file, -1 if it can't be read."""
    r = _probe(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                path])
    if r is None or r.returncode != 0:
        return -1
    return len([ln for ln in r.stdout.decode(errors="replace").split() if ln.strip()])


def _start_time(path):
    """A file's start time as ffmpeg reads it (0 when it has none)."""
    v = (_probe_json(path, ["-show_entries", "format=start_time"]).get("format") or {}).get(
        "start_time")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _quote(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


def concat_picture_track(infos, i, offsets, spans, tmp, log, stop_event):
    """Picture subtitle track i (PGS / VobSub / DVB) of the parts, stream-
    copied onto the joined timeline (part k at offsets[k], spans[k] long).
    Returns (.mks file, its -itsoffset in the final mux, track of the first
    part that has it), or None (logged)."""
    tracks = [fi["subs"][i] if i < len(fi["subs"]) else None for fi in infos]
    present = [(k, t) for k, t in enumerate(tracks) if t is not None]
    codecs = sorted({t["codec"] for _k, t in present})
    if len(codecs) != 1:
        log(f"   [WARN] subtitle track {i + 1} dropped - the parts have different picture "
            f"subtitle formats ({', '.join(codecs)})")
        return None
    ref = present[0][1]
    sizes = {t.get("size") for _k, t in present if t.get("size")}
    if len(sizes) > 1:
        log(f"   [WARN] subtitle track {i + 1}: the parts' subtitle pictures have different "
            "sizes - players show them all at the first part's size")
    parts, total = {}, 0
    for k, t in present:
        if stop_event.is_set():
            return None
        dst = os.path.join(tmp, f"pic{i}_{k}.mks")
        rc, err = _run_capture_stoppable(
            ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-v", "error", "-i", infos[k]["path"],
             "-map", f"0:{t['idx']}", "-c", "copy", "-f", "matroska", dst], stop_event)
        if rc == -1:
            return None
        n = _packet_count(dst) if rc == 0 and os.path.isfile(dst) else -1
        if n < 0:
            log(f"   [WARN] subtitle track {i + 1} dropped - could not copy it from part "
                f"{k + 1}: {(err or '').strip()[:120]}")
            return None
        if n:                       # (an empty track: nothing to place)
            parts[k] = dst
            total += n
    if not parts:
        log(f"   [WARN] subtitle track {i + 1} dropped - it has no subtitles in any part")
        return None
    missing = [k + 1 for k, t in enumerate(tracks) if t is None]
    if missing:
        log(f"   subtitle track {i + 1}: part(s) {', '.join(map(str, missing))} have no such "
            "track - no subtitles there")
    # one entry per part that has cues, lasting until the next such part
    # starts (a part without the track is a gap); inpoint 0 keeps each
    # piece's own times (its first cue isn't moved to the entry's start)
    ks = sorted(parts)
    end_all = offsets[-1] + spans[-1]
    lines = ["ffconcat version 1.0"]
    for j, k in enumerate(ks):
        end = offsets[ks[j + 1]] if j + 1 < len(ks) else end_all
        lines += [f"file {_quote(os.path.abspath(parts[k]))}", "inpoint 0",
                  f"duration {max(0.001, end - offsets[k]):.6f}"]
    lst = os.path.join(tmp, f"pic{i}.txt")
    with open(lst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    out = os.path.join(tmp, f"pic{i}.mks")
    rc, err = _run_capture_stoppable(
        ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-v", "error", "-copyts", "-f", "concat",
         "-safe", "0", "-i", lst, "-map", "0:0", "-c", "copy", "-f", "matroska", out],
        stop_event)
    if rc == -1:
        return None
    # the concat demuxer stops quietly at a piece it can't read - count
    got = _packet_count(out) if rc == 0 and os.path.isfile(out) else -1
    if got != total:
        log(f"   [WARN] subtitle track {i + 1} dropped - joining its pieces failed "
            f"({got} of {total} subtitle packets) {(err or '').strip()[:120]}")
        return None
    # the pieces start at the first part with cues; ffmpeg moves an input to
    # start at 0 - -itsoffset puts it back where it belongs
    return out, offsets[ks[0]] + _start_time(out), ref


def merge_subtitle_tracks(infos, offsets, spans, tmp, log, stop_event, mp4=False):
    """The subtitle tracks of a re-encoded join, in track order:
    [{"path", "codec" (for -c:s), "track" (of the first part that has it),
    "itsoffset" (None for text)}].
    Text: kept when EVERY file has the track (merged + re-timed), else
    dropped. Pictures: stream-copied, a file without the track has no
    subtitles there; MP4 can't hold them (dropped). Each drop is logged."""
    out = []
    for i in range(max(len(fi["subs"]) for fi in infos)):
        tracks = [fi["subs"][i] if i < len(fi["subs"]) else None for fi in infos]
        here = [t for t in tracks if t is not None]
        ref = here[0]
        kinds = {t["text"] for t in here}
        if kinds == {False}:
            if mp4:
                log(f"   [WARN] subtitle track {i + 1} dropped - MP4 can't hold picture "
                    f"subtitles ({ref['codec']})")
                continue
            got = concat_picture_track(infos, i, offsets, spans, tmp, log, stop_event)
            if got:
                path, shift, first = got
                out.append({"path": path, "codec": "copy", "track": first, "itsoffset": shift})
                log(f"   subtitle track {i + 1}: {first['codec']} pictures copied as they are")
            elif stop_event.is_set():
                break
            continue
        if len(kinds) > 1:
            log(f"   [WARN] subtitle track {i + 1} dropped - text and picture subtitles "
                "can't be joined into one track")
            continue
        if len(here) < len(tracks):
            log(f"   [WARN] subtitle track {i + 1} dropped - not every file has it")
            continue
        got = merge_text_track(infos, i, offsets, spans, tmp, log, stop_event)
        if got:
            out.append({"path": got[0], "codec": "mov_text" if mp4 else got[1],
                        "track": tracks[0], "itsoffset": None})
        elif stop_event.is_set():
            break
        else:
            log(f"   [WARN] subtitle track {i + 1} dropped")
    return out
