"""Template matching: audio fingerprints (MFCC), template loading and the
matcher shared by the batch and the review / Auto-detect buttons
(_detect_core, detect_segments)."""
import os
import glob
import subprocess
import threading
import time

from ..config import CREDITS_SEARCH_WINDOW, INTRO_SEARCH_WINDOW, limit_cmd, popen_flags, TEMP_DIR
from .probe import (audio_track_for_lang, probe_audio_streams, probe_duration,
                    probe_video_duration)


# ======================= detection (needs librosa) =======================
def get_mfcc_match(y_main, y_temp, sr, librosa, np, fftconvolve):
    """Find where `y_temp` best lines up inside `y_main` and how well it fits.
    Returns (time_sec, score).

    The score's SCALE is deliberately compressed: even a near-perfect match
    reads ~0.7 (not 1.0), because the whole search window is z-normalized
    globally. That global normalization is exactly what makes this a strong
    *discriminator* - it crushes wrong matches down to ~0.05-0.15, leaving a
    huge gap between a real match (~0.5-0.7) and a coincidence. A locally-
    normalized cross-correlation reads a prettier ~1.0 on a perfect match but
    inflates wrong matches to ~0.6 (taking the max over a long window), which
    squeezes that gap and causes mistakes - measured and rejected, see history.
    So the number looks low but the detection is reliable; keep the confidence
    threshold around 0.3-0.4."""
    if len(y_main) < len(y_temp):
        return 0.0, 0.0
    mfcc_main = librosa.feature.mfcc(y=y_main, sr=sr, n_mfcc=13)
    mfcc_temp = librosa.feature.mfcc(y=y_temp, sr=sr, n_mfcc=13)
    mfcc_main = (mfcc_main - np.mean(mfcc_main, axis=1, keepdims=True)) / (np.std(mfcc_main, axis=1, keepdims=True) + 1e-8)
    mfcc_temp = (mfcc_temp - np.mean(mfcc_temp, axis=1, keepdims=True)) / (np.std(mfcc_temp, axis=1, keepdims=True) + 1e-8)
    if mfcc_main.shape[1] < mfcc_temp.shape[1]:
        return 0.0, 0.0
    scores = np.zeros(mfcc_main.shape[1] - mfcc_temp.shape[1] + 1)
    for i in range(13):
        scores += fftconvolve(mfcc_main[i], mfcc_temp[i, ::-1], mode='valid')
    scores = scores / (13 * mfcc_temp.shape[1])
    best_frame = int(np.argmax(scores))
    return librosa.frames_to_time(best_frame, sr=sr), float(scores[best_frame])


def audio_fingerprint(path, sr=22050, hop=512, n_mfcc=13):
    """Mono-audio MFCC (z-normed) for alignment, plus feature-frames-per-second.
    Finer hop than detection (512 -> ~23 ms) for frame-accurate alignment.
    Returns (M, fps) or (None, 0.0) if the audio can't be read."""
    import librosa
    os.makedirs(TEMP_DIR, exist_ok=True)
    dst = os.path.join(TEMP_DIR, f"_align_{abs(hash(path)) % 10**8}.wav")
    try:
        if not extract_wav(path, dst):
            return None, 0.0
        y, _ = librosa.load(dst, sr=sr, mono=True)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    if y is None or len(y) < sr:
        return None, 0.0
    M = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc, hop_length=hop)
    M = (M - M.mean(axis=1, keepdims=True)) / (M.std(axis=1, keepdims=True) + 1e-8)
    return M, sr / hop


def align_window(M_ref, fps, M_new, new_pos, length):
    """Find where the new file's audio at [new_pos, new_pos+length] best lines
    up inside the reference (cross-correlating the precomputed MFCCs). Returns
    (ref_pos_sec, score); score ~1 = confident, low = no good match."""
    import numpy as np
    from scipy.signal import fftconvolve
    a = max(0, int(new_pos * fps))
    b = int((new_pos + length) * fps) if length else M_new.shape[1]
    needle = M_new[:, a:b]
    if needle.shape[1] < 4 or M_ref.shape[1] < needle.shape[1]:
        return None, 0.0
    scores = np.zeros(M_ref.shape[1] - needle.shape[1] + 1)
    for i in range(needle.shape[0]):
        scores += fftconvolve(M_ref[i], needle[i, ::-1], mode="valid")
    scores /= needle.shape[0] * needle.shape[1]
    j = int(np.argmax(scores))
    # parabolic interpolation around the peak for sub-frame (sub-hop) accuracy,
    # so the aligned position lands within a fraction of a video frame
    delta = 0.0
    if 0 < j < len(scores) - 1:
        a, b, c = scores[j - 1], scores[j], scores[j + 1]
        denom = a - 2 * b + c
        if denom != 0:
            delta = max(-0.5, min(0.5, 0.5 * (a - c) / denom))
    return (j + delta) / fps, float(scores[j])


# sample 0 of every decode = the requested start on the app timeline, even
# when the audio track starts later (delayed / gapped audio is padded with
# silence instead of shifting everything early) - the same as engine.refine
_AUDIO_ALIGN = "aresample=async=1:first_pts=0"


def extract_wav(src, dst, track=None, stop_event=None, start=None, duration=None,
                timeout=None):
    """Decode audio to mono 22.05 kHz WAV. track=None uses the default audio
    stream; an int picks that audio stream (0:a:N) so each language track can
    be fingerprinted separately. start / duration (s): only that window
    (sample 0 = file time `start`). stop_event set = ffmpeg is killed,
    False; so is a decode that takes longer than `timeout` seconds (default
    30 min for a whole file, 10 min for a window)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if start and float(start) > 0:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", src]
    if duration:
        cmd += ["-t", f"{float(duration):.3f}"]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-vn", "-sn", "-dn", "-af", _AUDIO_ALIGN, "-acodec", "pcm_s16le", "-ar", "22050",
            "-ac", "1", dst]
    if timeout is None:
        timeout = 600.0 if duration else 1800.0
    try:
        p = subprocess.Popen(limit_cmd(cmd), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=popen_flags())
    except OSError:                      # ffmpeg missing
        return False
    t_end = time.monotonic() + float(timeout)
    while True:
        try:
            return p.wait(timeout=0.2) == 0
        except subprocess.TimeoutExpired:
            if _is_stopped(stop_event) or time.monotonic() > t_end:
                p.kill()
                p.wait()
                return False


def best_match_over(slices, tpl_y, sr, librosa, np, fftconvolve, stop_event=None):
    """Match a template against several audio-track slices (same timeline) and
    return the best (time, score). Lets a template match whichever language
    track it was cut from, so the episode's default track no longer matters."""
    best_t, best_s = 0.0, 0.0
    for y in slices:
        if _is_stopped(stop_event):
            break
        t, s = get_mfcc_match(y, tpl_y, sr, librosa, np, fftconvolve)
        if s > best_s:
            best_t, best_s = t, s
    return best_t, best_s


def anchor_match(slices, tpl_y, sr, anchor_secs, librosa, np, fftconvolve, stop_event=None):
    """Locate a segment by matching only the FIRST and LAST `anchor_secs` of the
    template, independently - so the cut spans the real boundaries in each
    episode even if the segment's length varies. Returns (start, end, score)
    relative to the slice, or None if the template is too short to split or the
    ends don't line up sensibly. score is the weaker of the two ends (both must
    be found)."""
    N = int(anchor_secs * sr)
    if len(tpl_y) < 2 * N + int(1.0 * sr):     # too short to take two ends
        return None
    hs, h_score = best_match_over(slices, tpl_y[:N], sr, librosa, np, fftconvolve, stop_event)
    ts, t_score = best_match_over(slices, tpl_y[-N:], sr, librosa, np, fftconvolve, stop_event)
    start, end = hs, ts + anchor_secs
    tpl_len = len(tpl_y) / sr
    # the found length must be in a sane range of the template (guards against
    # one end mis-locating); otherwise fall back to the full-template method
    if not (0.5 * tpl_len <= (end - start) <= 1.6 * tpl_len):
        return None
    # report the AVERAGE of the two ends (comparable to a full-template score),
    # not the min - the intro's start is often the less distinctive end, and
    # taking the min made a solid match look weak and fall under the threshold.
    # The length check above already ensures both ends landed sensibly.
    return start, end, (h_score + t_score) / 2


def matched_seconds(tracks, start_sec, tpl_y, sr, librosa, np,
                    hop=2048, thresh=0.5, max_gap_s=0.8):
    """How many seconds from `start_sec` the template audio actually keeps
    matching the episode - so a fixed-length template doesn't overcut an
    episode whose segment is genuinely shorter. Compares template vs episode
    frame-by-frame (best of all audio tracks) and finds where the run stops,
    bridging brief quiet dips. Returns the matched length in seconds, or the
    full template length if it can't tell (never trims on a bad read)."""
    def feats(y):
        m = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
        # z-norm each coefficient (kills the coeff-0/energy bias that would make
        # even unrelated audio look similar), then L2-normalize each frame so the
        # per-frame dot product is a clean cosine similarity
        m = (m - m.mean(axis=1, keepdims=True)) / (m.std(axis=1, keepdims=True) + 1e-8)
        return m / (np.linalg.norm(m, axis=0, keepdims=True) + 1e-8)

    tpl_len = len(tpl_y) / sr
    Mt = feats(tpl_y)
    fps = sr / hop
    max_gap = int(max_gap_s * fps)
    a = int(start_sec * sr)
    best_len = 0.0
    for y in tracks:
        reg = y[a:a + len(tpl_y) + int(2 * sr)]
        if len(reg) < sr:
            continue
        Mr = feats(reg)
        L = min(Mt.shape[1], Mr.shape[1])
        if L < 3:
            continue
        sim = np.sum(Mt[:, :L] * Mr[:, :L], axis=0)
        if L >= 5:
            sim = np.convolve(sim, np.ones(5) / 5, mode="same")
        last_good, gap = -1, 0
        for i in range(L):
            if sim[i] >= thresh:
                last_good, gap = i, 0
            else:
                gap += 1
                if gap > max_gap and last_good >= 0:
                    break
        if last_good >= 0:
            best_len = max(best_len, (last_good + 1) / fps)
    # never trim below half the template (guards against a false early cut);
    # if nothing matched at all, keep the full length
    if best_len < 0.5 * tpl_len:
        return tpl_len
    return min(best_len, tpl_len)


# ======================= template matching (shared) =======================
# segment kinds in the order run_batch has always loaded / logged them
_SEG_KINDS = ("intro", "credits", "preintro", "aftercredits")
_SEG_DIR_KEY = {"intro": "intro_dir", "credits": "credits_dir",
                "preintro": "preintro_dir", "aftercredits": "aftercredits_dir"}
_SEG_LOAD_LABEL = {"intro": "Intro", "credits": "Credits",
                   "preintro": "Pre-intro", "aftercredits": "After-credits"}
# (name in the per-template score lines, its column width, [OK] label)
_SEG_LOG = {"intro": ("intro", 30, "INTRO"), "credits": ("credits", 28, "CREDITS"),
            "preintro": ("pre-intro", 26, "PRE-INTRO"),
            "aftercredits": ("after-credits", 22, "AFTER-CREDITS")}
_NEAR_MISS = 0.6        # candidates down to this x confidence are offered
_MAX_CANDS = 5


def _want_seg(cfg, kind):
    """Segment-type switch: cfg['use_<kind>'] if given, else cfg['use'][kind]
    (the batch tab's checkbox dict); missing = on."""
    key = f"use_{kind}"
    if key in cfg:
        return bool(cfg[key])
    return (cfg.get("use") or {}).get(kind, True)


def _is_stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _det_mode(cfg):
    """cfg['detect_mode']: 'audio' (default - the classic matcher), 'visual'
    (template PICTURES, engine.vfp) or 'both'."""
    m = cfg.get("detect_mode")
    return m if m in ("audio", "visual", "both") else "audio"


def _load_template_folder(folder, label, log, stop_event, librosa, visual=False):
    data = []
    os.makedirs(TEMP_DIR, exist_ok=True)
    for i, file in enumerate(sorted(glob.glob(os.path.join(folder, "*.*")))):
        if _is_stopped(stop_event):
            break
        if file.lower().endswith((".txt", ".json", ".ini", ".md")):
            continue  # notes/config files, not templates
        name = os.path.basename(file)
        log(f"Loading {label}: {name}")
        tmp = os.path.join(TEMP_DIR, f"tpl_{os.getpid()}_{threading.get_ident()}"
                                     f"_{label.lower()}_{i}.wav")
        y = sr = None
        try:
            if extract_wav(file, tmp, stop_event=stop_event):
                y, sr = librosa.load(tmp, sr=None)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        vh = None
        if visual and not _is_stopped(stop_event):
            from .vfp import video_hashes
            vh = video_hashes(file, stop_event=stop_event)
            if vh is not None and int(vh.valid.sum()) < 4:
                vh = None
            if vh is None:
                log(f"   [WARN] template {name} has no usable pictures - audio only")
        if y is None or len(y) == 0:
            if vh is None:
                log(f"   [WARN] could not convert template {name}")
                continue
            log(f"   [WARN] template {name} has no audio - matching its pictures only")
            data.append({'name': name, 'path': file, 'y': None, 'y_match': None,
                         'sr': None, 'lead': 0.0, 'duration': len(vh) / vh.fps, 'vh': vh})
            continue
        # A silent/quiet lead-in has no fingerprint, so the matcher would
        # lock onto the first audible note and the cut would start late.
        # Trim the leading silence for MATCHING and remember how much, so
        # the cut can be backed up to the true (silent) start.
        lead = 0.0
        y_match = y
        try:
            _, idx = librosa.effects.trim(y, top_db=45)
            cut = int(idx[0])
            if 0.3 * sr < cut < 0.5 * len(y):     # real lead-in, not the whole clip
                lead = cut / sr
                y_match = y[cut:]
        except Exception:
            pass
        data.append({'name': name, 'path': file, 'y': y, 'y_match': y_match, 'sr': sr,
                     'lead': lead, 'duration': len(y) / sr, 'vh': vh})
    return data


def load_templates_for(cfg, log=None, stop_event=None):
    """Load every template (each file = one variant) of each enabled segment
    folder in cfg. Returns {"intro": [tpl...], "credits": [...],
    "preintro": [...], "aftercredits": [...]} - pass it to detect_segments()
    so a batch doesn't reload them per file. Needs librosa. With
    cfg['detect_mode'] 'visual' / 'both' each template's pictures are
    fingerprinted too (tpl['vh'])."""
    import librosa
    log = log or (lambda m: None)
    visual = _det_mode(cfg) != "audio"
    out = {}
    for kind in _SEG_KINDS:
        folder = cfg.get(_SEG_DIR_KEY[kind])
        out[kind] = (_load_template_folder(folder, _SEG_LOAD_LABEL[kind], log,
                                           stop_event, librosa, visual)
                     if folder and _want_seg(cfg, kind) and not _is_stopped(stop_event)
                     else [])
    return out


_HEAD_PAD = 5.0         # s decoded past the intro window + the longest intro template
_SPLIT_SLACK = 60.0     # head + tail within this of the whole file: decode it whole


class _EpAudio:
    """An episode's decoded audio tracks (mono float32 at sr), one entry per
    track: heads[i] = file time 0 .. ~head_secs, tails[i] = file time
    tail_t0 .. the end (empty when not needed / unreadable); idx[i] = its
    audio stream (0:a:N). full: the whole file was decoded once (a short
    file, or its length is unknown) - heads and tails are the same arrays,
    tail_t0 = 0."""
    __slots__ = ("heads", "tails", "tail_t0", "sr", "idx", "full")

    def __init__(self):
        self.heads, self.tails, self.idx = [], [], []
        self.tail_t0, self.sr, self.full = 0.0, None, True

    def region(self, start_sec, n):
        """Every track's samples from file time start_sec on (at most n),
        taken from the head or the tail that holds start_sec."""
        sr = self.sr
        n = int(n)
        a = int(start_sec * sr)
        if self.full or a + n <= len(self.heads[0]) or start_sec < self.tail_t0:
            return [y[max(0, a):a + n] for y in self.heads]
        b = a - int(round(self.tail_t0 * sr))
        return [y[max(0, b):b + n] for y in self.tails]


def _audio_end(video):
    """End (s) of the longest audio stream of `video` (its stream duration /
    DURATION tag), or None."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
           "stream=duration:stream_tags=DURATION", "-of", "default=nw=1", video]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, creationflags=popen_flags(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    best = None
    for line in r.stdout.decode(errors="replace").splitlines():
        v = line.split("=", 1)[-1].strip()
        try:
            if ":" in v:                                  # HH:MM:SS.mmm tag form
                h, m, s = v.split(":")
                val = int(h) * 3600 + int(m) * 60 + float(s)
            else:
                val = float(v)
        except ValueError:
            continue
        if val > 0 and (best is None or val > best):
            best = val
    return best


def _episode_audio(video, cfg, stop_event, librosa, head_secs=None, need_tail=True,
                   progress=None):
    """Decode the episode's audio for matching: EVERY track (so a template
    matches whichever language track it was cut from - an English template
    scores low against a Japanese default track) or only the cfg['match_lang']
    track if the file has it. Only what the matcher reads is decoded: the
    first head_secs (default: the intro search window) and - need_tail -
    the last CREDITS_SEARCH_WINDOW seconds of each track (a 2-hour film with
    six dubs would take gigabytes otherwise); a file not much longer than
    both together, or of unknown length, is decoded whole once. Returns an
    _EpAudio (no heads = no audio could be read)."""
    import numpy as np
    prog = progress or (lambda f, t="": None)
    n_audio = max(1, len(probe_audio_streams(video)))
    match_lang = cfg.get("match_lang")
    if match_lang:
        mi = audio_track_for_lang(video, match_lang)
        track_indices = [mi] if mi is not None else list(range(n_audio))
    else:
        track_indices = list(range(n_audio))
    head_secs = float(head_secs or (INTRO_SEARCH_WINDOW + _HEAD_PAD))
    ea = _EpAudio()
    dur = _audio_end(video) or probe_duration(video)
    if dur and dur > head_secs + CREDITS_SEARCH_WINDOW + _SPLIT_SLACK:
        ea.full = False
        ea.tail_t0 = round(max(0.0, float(dur) - CREDITS_SEARCH_WINDOW), 3)
    os.makedirs(TEMP_DIR, exist_ok=True)
    temp_audio = os.path.join(TEMP_DIR, f"ep_{os.getpid()}_{threading.get_ident()}_audio.wav")

    def load(ai, **kw):
        if not extract_wav(video, temp_audio, track=ai, stop_event=stop_event, **kw):
            return None
        return librosa.load(temp_audio, sr=None)
    try:
        for i, ai in enumerate(track_indices):
            if _is_stopped(stop_event):
                break
            prog(i / len(track_indices),
                 f"Reading audio track {i + 1}/{len(track_indices)}")
            if ea.full:
                got = load(ai)
                if got is None:
                    continue
                y, ea.sr = got
                ea.heads.append(y)
                ea.tails.append(y)
                ea.idx.append(ai)
                continue
            got = load(ai, duration=head_secs)
            if got is None:
                continue
            yh, ea.sr = got
            yt = None
            if need_tail and not _is_stopped(stop_event):
                gt = load(ai, start=ea.tail_t0)
                if gt is not None and gt[1] == ea.sr:
                    yt = gt[0]
            ea.heads.append(yh)
            ea.tails.append(yt if yt is not None else np.zeros(0, dtype=np.float32))
            ea.idx.append(ai)
    finally:
        try:
            if os.path.exists(temp_audio):
                os.remove(temp_audio)
        except OSError:
            pass
    return ea


def _episode_tracks_idx(video, cfg, stop_event, librosa):
    """Every matched audio track decoded whole: (tracks, sr, stream index
    (0:a:N) of each). Memory grows with the file's length x tracks - the
    matcher itself uses _episode_audio (head / tail only)."""
    ea = _episode_audio(video, cfg, stop_event, librosa, head_secs=float("inf"))
    return ea.heads, ea.sr, ea.idx


def _episode_tracks(video, cfg, stop_event, librosa):
    """_episode_tracks_idx without the stream indices: (tracks, sr)."""
    tracks, sr, _idx = _episode_tracks_idx(video, cfg, stop_event, librosa)
    return tracks, sr


# a release converted 23.976 <-> 25 fps (PAL speed-up / slow-down) plays
# every frame ~4 % faster / slower: a template that doesn't match at its own
# speed is also tried at these (the frames stay 1:1, so the frame-exact
# refinement works as is)
_FILM = 24000 / 1001
SPEEDS = (25.0 / _FILM, _FILM / 25.0)


def _speed_audio(tpl, sp, sr, librosa, np):
    """The template's match audio played `sp` times faster - pitch kept
    (atempo-style conversion) and pitch shifted (plain speed-up) - cached."""
    key = "_y_speed"
    cache = tpl.setdefault(key, {})
    if sp not in cache:
        y = tpl['y_match']
        out = []
        try:
            out.append(librosa.effects.time_stretch(y, rate=sp))
        except Exception:
            pass
        try:
            out.append(librosa.resample(y, orig_sr=int(round(sr * sp)), target_sr=sr))
        except Exception:
            pass
        cache[sp] = out
    return cache[sp]


def _match_kind(kind, tpls, ea, cfg, stop_event, log, np, librosa, fftconvolve):
    """One raw candidate per template (its best position), in template order,
    times in file seconds. ea: the episode's _EpAudio. Logs the
    per-template score lines."""
    sr = ea.sr
    anchor_cut = bool(cfg.get("anchor_cut"))
    anchor_secs = float(cfg.get("anchor_secs", 15.0))
    if kind in ("intro", "preintro"):
        win = min(int(INTRO_SEARCH_WINDOW * sr), len(ea.heads[0]))
        slices, offset = [y[:win] for y in ea.heads], 0.0
    elif ea.full:
        # tail search: slice every track from ONE common absolute start, so
        # a single offset maps a match in any track back to file time
        # (tracks can differ in length; their own -win tails wouldn't align)
        longest = max(len(y) for y in ea.tails)
        a0 = max(0, longest - int(CREDITS_SEARCH_WINDOW * sr))
        slices, offset = [y[a0:] for y in ea.tails], a0 / sr
    else:
        # the decoded tails all start at the same file time
        slices, offset = list(ea.tails), ea.tail_t0
    lname, width, _ = _SEG_LOG[kind]
    cands = []
    for tpl in tpls:
        if _is_stopped(stop_event):        # Stop responds between templates
            break
        if tpl.get('y_match') is None:     # picture-only template
            continue
        am = (anchor_match(slices, tpl['y_match'], sr, anchor_secs, librosa, np, fftconvolve,
                           stop_event)
              if anchor_cut else None)
        if am is not None:
            st, en, score = am
            st -= tpl['lead']                 # back up over the silent lead-in
            log(f"     {lname} {tpl['name']:{width}} -> {score:.3f} (anchored {en - st:.0f}s)")
        else:
            s, score = best_match_over(slices, tpl['y_match'], sr, librosa, np, fftconvolve,
                                       stop_event)
            sp_used = 1.0
            if score < cfg["confidence"] and not _is_stopped(stop_event):
                # maybe a 25 fps (PAL) release of a 23.976 fps template or v.v.
                for sp in SPEEDS:
                    if _is_stopped(stop_event):
                        break
                    for y2 in _speed_audio(tpl, sp, sr, librosa, np):
                        s2, sc2 = best_match_over(slices, y2, sr, librosa, np, fftconvolve,
                                                  stop_event)
                        if sc2 >= cfg["confidence"] and sc2 > 1.25 * score:
                            s, score, sp_used = s2, sc2, sp
            st = s - tpl['lead'] / sp_used
            en = st + tpl['duration'] / sp_used
            log(f"     {lname} {tpl['name']:{width}} -> {score:.3f}"
                + (f" (at {sp_used:.3f}x speed)" if sp_used != 1.0 else ""))
        cands.append({"start": st + offset, "end": en + offset, "score": float(score),
                      "template": tpl['name'], "anchored": am is not None, "_tpl": tpl,
                      "src": "audio", "speed": sp_used if am is None else 1.0})
    return cands


_VIS_MIN = 0.5          # a picture match needs >= this share of matching frames
_AGREE = 2.0            # audio and picture starts this close = the same match


def _match_kind_visual(kind, tpls, total, video, stop_event, log, cache, conf=0.0):
    """Picture candidates: each template's frame hashes slid over the
    episode's start (intro / pre-intro) or end (credits / after-credits)
    region (decoded once per episode, kept in `cache`). One candidate per
    template that has pictures."""
    from .vfp import Hashes, match_template, video_hashes
    side = "start" if kind in ("intro", "preintro") else "end"
    if side not in cache:
        if side == "start":
            cache[side] = video_hashes(video, 0.0, INTRO_SEARCH_WINDOW, stop_event=stop_event)
        else:
            cache[side] = video_hashes(video, max(0.0, total - CREDITS_SEARCH_WINDOW), None,
                                       stop_event=stop_event)
    eh = cache[side]
    lname, width, _ = _SEG_LOG[kind]
    cands = []
    if eh is None:
        return cands
    for tpl in tpls:
        if _is_stopped(stop_event):
            break
        if tpl.get('vh') is None:
            continue
        r = match_template(tpl['vh'], eh)
        if r is None:
            continue
        st, score = r
        sp_used = 1.0
        if score < max(conf, _VIS_MIN):
            # maybe a 25 fps (PAL) release of a 23.976 fps template or v.v.:
            # the template's samples at the episode's pace
            import numpy as np
            h = tpl['vh']
            for sp in SPEEDS:
                idx = np.minimum(np.round(np.arange(int(len(h) / sp)) * sp).astype(int),
                                 len(h) - 1)
                r2 = match_template(Hashes(h.bits[idx], h.valid[idx], h.t0, h.fps), eh)
                if r2 is not None and r2[1] >= max(conf, _VIS_MIN) and r2[1] > 1.25 * score:
                    st, score, sp_used = r2[0], r2[1], sp
        log(f"     {lname} {tpl['name']:{width}} -> {score:.3f} (video"
            + (f", at {sp_used:.3f}x speed)" if sp_used != 1.0 else ")"))
        cands.append({"start": st, "end": st + tpl['duration'] / sp_used, "score": float(score),
                      "template": tpl['name'], "anchored": False, "_tpl": tpl,
                      "src": "visual", "speed": sp_used})
    return cands


def _merge_sources(a_cands, v_cands, mode, conf):
    """One candidate per template from its audio and picture candidates.
    'both': agreeing starts (within 2 s) = confirmed 'audio+visual' (audio's
    finer timing, the better score); disagreeing = a valid audio match is
    kept, else a valid picture match, flagged 'conflict' (the two scores are
    on different scales, so a confident audio match is never overruled);
    one source = that."""
    if mode == "audio":
        return a_cands
    if mode == "visual":
        return v_cands
    by_a = {c["template"]: c for c in a_cands}
    by_v = {c["template"]: c for c in v_cands}
    order = [c["template"] for c in a_cands] + [t for t in by_v if t not in by_a]
    out = []
    for name in order:
        ca, cv = by_a.get(name), by_v.get(name)
        if cv and cv["score"] < max(conf, _VIS_MIN) and ca:
            cv = None                     # a weak picture match neither confirms nor vetoes
        if ca and cv:
            if abs(ca["start"] - cv["start"]) <= _AGREE:
                c = dict(ca, score=max(ca["score"], cv["score"]), src="audio+visual",
                         v_score=cv["score"])
            elif ca["score"] >= conf:
                c = dict(ca, conflict=True, v_score=cv["score"])
            else:
                c = dict(cv, a_score=ca["score"])     # audio was only weak
        else:
            c = ca or cv
        out.append(c)
    return out


def _tpl_video_end(tpl):
    """End time of a template clip's last frame (its video duration), cached
    in the template dict; falls back to the audio length."""
    if "_vend" not in tpl:
        from .probe import probe_video_duration as _pvd
        v = None
        try:
            v = _pvd(tpl["path"])
        except Exception:
            v = None
        tpl["_vend"] = float(v) if v else float(tpl.get("duration") or 0.0)
    return tpl["_vend"]


_TPL_CACHE = 16         # decoded windows kept per template (engine.refine_base.LRUCache)


def _refine_cand(video, c, tracks, rcache, log, lname, cfg=None, stop_event=None):
    """Snap a candidate's start / end to the exact frames (engine.refine),
    each edge on its own, against the template clip: its start = the
    template's first frame; its end = the template's end (exact) - or, when
    trim_to_match cut the end short, the matching spot inside the template.
    cfg['template_margin']: 'follow' (default) = the template's own first /
    last frame mapped into the episode (a template cut with a safety margin
    of a frame or two keeps it); 'exact' = the intro / credits content edge;
    'exact+margin' = the content edge + cfg['margin_frames'] (default 1)
    frames each side. Each edge has a time budget (engine.refine
    BOUNDARY_BUDGET - then it keeps its coarse value, edge_src 'coarse');
    stop_event stops it at once."""
    from .refine import refine_range
    from .refine_base import LRUCache
    from .refine_template import add_margin, norm_margin, norm_template_mode
    cfg = cfg or {}
    tmode = norm_template_mode(cfg.get("template_margin"))
    tpl = c["_tpl"]
    tend = _tpl_video_end(tpl)
    if tend <= 0:
        return
    rel_end = (c["end"] - c["start"]) * float(c.get("speed") or 1.0)   # template time
    trimmed = rel_end < tend - 1.0 and not c.get("anchored")
    ref_rng = (0.0, rel_end if trimmed else tend)
    # the template's decoded windows, kept for the next episodes of a batch -
    # the last few only (follow-up windows differ per episode)
    rc = tpl.get("_rcache")
    if not isinstance(rc, LRUCache):
        rc = tpl["_rcache"] = LRUCache(_TPL_CACHE)
    kinds = []
    s, e, done = refine_range(video, (c["start"], c["end"]), tpl["path"], ref_rng,
                              ref_exact=(True, not trimmed), log=log, label=f"{lname} ",
                              track=tracks, ref_track=None, cache=rcache,
                              ref_cache=rc, template_mode=tmode,
                              src_out=kinds, stop_event=stop_event)
    c["edge_src"] = {"start": kinds[0] if kinds else "coarse",
                     "end": kinds[1] if kinds else "coarse"}
    if tmode == "exact+margin" and not _is_stopped(stop_event):
        s, e = add_margin(video, (s, e), norm_margin(cfg.get("margin_frames")),
                          cache=rcache, edges=done, stop_event=stop_event)
    if done[0]:
        c["start"] = s
        c["match_start"] = s
    if done[1]:
        c["end"] = e
    c["refined"] = done


def _safe_progress(progress):
    """progress(frac, text) that clamps frac to [0, 1] and never raises into
    the detection; a no-op for None."""
    if progress is None:
        return lambda f, t="": None

    def _p(f, t=""):
        try:
            progress(max(0.0, min(1.0, float(f))), t)
        except Exception:
            pass
    return _p


def _detect_core(video, cfg, templates, stop_event, log, progress=None, err_out=None):
    """The matching engine shared by run_batch and detect_segments. Returns
    None if no audio could be read (or the file couldn't be processed: out
    of memory, a file error - logged as [WARN], the reason appended to the
    err_out list), else {"duration", "n_tracks", <kind>: [cand...]}: every
    template's candidate, best first, with ok (validity), trim_to_match,
    intro_from_start and credits_to_end applied. Logs exactly what the batch
    has always logged for the BEST candidate of each kind. progress(frac,
    text): optional, see detect_segments."""
    try:
        return _detect_core_run(video, cfg, templates, stop_event, log,
                                _safe_progress(progress))
    except (MemoryError, OSError) as exc:
        why = ("out of memory" if isinstance(exc, MemoryError)
               else f"file error ({exc})")
        try:
            log(f"   [WARN] {os.path.basename(video)}: detection failed - {why}")
        except Exception:
            pass
        if err_out is not None:
            err_out.append(why)
        return None


def _tpl_secs(tpls):
    """Longest audio template of a list (s), 0 if none."""
    return max([float(t.get("duration") or 0.0) for t in tpls or ()
                if t.get("y_match") is not None] or [0.0])


def _detect_core_run(video, cfg, templates, stop_event, log, prog):
    import numpy as np
    import librosa
    from scipy.signal import fftconvolve
    mode = _det_mode(cfg)
    has_vis = any(t.get('vh') is not None for v in templates.values() for t in (v or ()))
    if mode == "both" and not has_vis:
        mode = "audio"            # no template has usable pictures
    # the audio the matcher reads: the intro search window (+ the longest
    # intro template - trim_to_match reads past a match) and the credits
    # search window at the end
    head_tpl = max(_tpl_secs(templates.get("intro")), _tpl_secs(templates.get("preintro")))
    tail_tpl = max(_tpl_secs(templates.get("credits")),
                   _tpl_secs(templates.get("aftercredits")))
    head_secs = INTRO_SEARCH_WINDOW + head_tpl + _HEAD_PAD if head_tpl else 30.0
    A0, A1 = 0.0, 0.3                                  # progress band of the audio decode
    prog(A0, "Reading audio")
    ea = (_EpAudio() if mode == "visual"
          else _episode_audio(video, cfg, stop_event, librosa, head_secs=head_secs,
                              need_tail=tail_tpl > 0,
                              progress=lambda f, t="": prog(A0 + (A1 - A0) * f, t)))
    tracks, sr, track_idx = ea.heads, ea.sr, ea.idx
    if not tracks and mode == "audio":
        return None
    # the timeline to cut is the VIDEO's, not the first audio track's
    # (audio often ends a little early or runs a little long)
    total = probe_video_duration(video) or probe_duration(video)
    if not total and tracks:
        total = (len(tracks[0]) / sr if ea.full
                 else ea.tail_t0 + max(len(y) for y in ea.tails) / sr)
    if not total:
        return None
    log(f"   Duration: {total:.1f}s"
        + (f"  ({len(tracks)} audio tracks)" if len(tracks) > 1 else ""))
    if mode == "both" and not tracks:
        log("   no readable audio - matching the template pictures only")
    vcache = {}
    rcache = {}               # episode windows decoded by the refinement
    conf = cfg["confidence"]
    anchor_cut = bool(cfg.get("anchor_cut"))
    res = {"duration": float(total), "n_tracks": len(tracks)}
    intro_veto = 0.0          # end of a VALID intro (credits must start after it)
    todo = [k for k in _SEG_KINDS if templates.get(k)]
    for kind in _SEG_KINDS:
        tpls = templates.get(kind) or []
        res[kind] = []
        if not tpls or _is_stopped(stop_event):
            continue
        # progress: each kind an equal share of the rest, half matching, half refining
        j = todo.index(kind)
        k0 = A1 + (1.0 - A1) * j / len(todo)
        k1 = A1 + (1.0 - A1) * (j + 1) / len(todo)
        lname = _SEG_LOG[kind][0]
        prog(k0, f"Matching {lname} templates")
        a_c = (_match_kind(kind, tpls, ea, cfg, stop_event, log,
                           np, librosa, fftconvolve) if tracks else [])
        if mode != "audio":
            prog(k0 + 0.25 * (k1 - k0), f"Matching {lname} pictures")
        v_c = (_match_kind_visual(kind, tpls, total, video, stop_event, log, vcache, conf)
               if mode != "audio" else [])
        cands = _merge_sources(a_c, v_c, mode, conf)
        cands.sort(key=lambda c: -c["score"])          # stable: template order on ties
        n_ref = max(1, sum(1 for r, c in enumerate(cands)
                           if r == 0 or c["score"] >= c.get("thr", conf)))
        i_ref = 0
        for rank, c in enumerate(cands):
            c["match_start"] = c["start"]
            c["thr"] = max(conf, _VIS_MIN) if c.get("src") == "visual" else conf
            c["ok"] = c["score"] >= c["thr"] and not _is_stopped(stop_event)
            # only a VALID intro may veto credits (a weak match's end is noise)
            if kind == "credits":
                c["ok"] = c["ok"] and c["start"] > intro_veto + 10
            if (cfg.get("trim_to_match") and not anchor_cut and not _is_stopped(stop_event)
                    and tracks and c["_tpl"].get('y') is not None
                    and c.get("src") != "visual" and float(c.get("speed") or 1.0) == 1.0
                    and (c["ok"] or (rank > 0 and c["score"] >= _NEAR_MISS * conf))):
                tpl = c["_tpl"]
                reg = ea.region(c["start"], len(tpl['y']) + int(2 * sr))
                ml = matched_seconds(reg, 0.0, tpl['y'], sr, librosa, np)
                if ml < tpl['duration'] - 1.0:
                    if rank == 0:
                        log(f"   {_SEG_LOG[kind][0]} shorter here - trimmed cut"
                            f" {tpl['duration']:.0f}s -> {ml:.0f}s")
                    c["end"] = c["start"] + ml
            # frame-exact edges (engine.refine): the valid candidates and the
            # best one, each edge against the template clip's own edge
            if (cfg.get("refine", True) and not _is_stopped(stop_event)
                    and (c["ok"] or rank == 0) and c["_tpl"].get("path")):
                prog(k0 + (k1 - k0) * (0.5 + 0.5 * min(i_ref, n_ref - 1) / n_ref),
                     f"Refining {lname} edges")
                i_ref += 1
                clog = log if rank == 0 or c["ok"] else (lambda m: None)
                clog(f"   refining {_SEG_LOG[kind][0]} edges ({c['start']:.2f}s - "
                     f"{c['end']:.2f}s)...")
                _refine_cand(video, c, track_idx or None, rcache, clog,
                             _SEG_LOG[kind][0], cfg, stop_event)
        best = cands[0] if cands else None
        if kind == "intro" and best and best["ok"]:
            intro_veto = best["end"]
        how = f" ({best['src']})" if best and mode != "audio" else ""
        if best and best.get("conflict"):
            how += " - audio and pictures disagree"
        if best and best["ok"]:
            log(f"   [OK] {_SEG_LOG[kind][2]} at {best['start']:.2f}s{how}")
        else:
            log(f"   [--] weak {_SEG_LOG[kind][0]} match"
                f" ({max(0.0, best['score'] if best else 0.0):.3f}){how}")
        res[kind] = cands
    # optional: extend the intro cut back to the START of the file (also
    # removes any recap/cold-open before the intro), and the credits cut
    # forward to the END of the file (credits + preview + anything after).
    # Chapters mode keeps the intro's real start (the part before it becomes
    # its own chapter); match_start always holds where the audio matched.
    if not _is_stopped(stop_event):
        for rank, c in enumerate(res.get("intro", [])):
            if (cfg.get("intro_from_start") and c["start"] > 0.05
                    and cfg.get("mode") != "chapters"):
                if rank == 0 and c["ok"]:
                    log(f"   extending intro cut to file start (was {c['start']:.1f}s)")
                c["start"] = 0.0
                c["edge_src"] = dict(c.get("edge_src") or {}, start="file")
        for rank, c in enumerate(res.get("credits", [])):
            if cfg.get("credits_to_end") and c["end"] < total - 0.05:
                if rank == 0 and c["ok"]:
                    log(f"   extending credits cut to file end (was {c['end']:.1f}s)")
                c["end"] = total
                c["edge_src"] = dict(c.get("edge_src") or {}, end="file")
    return res


def _warn_only(m):
    """Default log of detect_segments: drop everything but [WARN] lines,
    which go to the session log."""
    if "[WARN]" in str(m):
        try:
            from .. import applog
            applog.record(str(m).strip())
        except Exception:
            pass


def detect_segments(video, cfg, stop_event=None, log=None, templates=None, progress=None):
    """Match one episode against the templates (all variants in each folder).
    progress: optional callback progress(frac, text) - frac 0.0 .. 1.0 for
    THIS file, text a short English stage ('Loading templates', 'Reading
    audio track 1/2', 'Matching intro templates', 'Matching intro
    pictures', 'Refining intro edges', ... 'Done' / 'Stopped'); called
    from the detecting thread at every stage (a slow file shows it isn't
    hung - the caller marshals it to the UI thread); an exception it raises is
    ignored.
    cfg: the same dict run_batch takes (intro_dir, credits_dir, preintro_dir,
    aftercredits_dir, confidence, match_lang, trim_to_match, anchor_cut,
    anchor_secs, intro_from_start, credits_to_end, use_<kind> or use{},
    template_margin ('follow' default / 'exact' / 'exact+margin'),
    margin_frames (0-5, default 1; only used by 'exact+margin') ...).
    templates: optional preloaded result of load_templates_for(cfg).
    Returns {"duration": float, "preintro": [cand...], "intro": [...],
             "credits": [...], "aftercredits": [...]}
    cand = {"start", "end", "score", "template" (file name), "ok" (score >=
    confidence and run_batch's validity rules), "match_start" (where the
    audio matched, before intro_from_start), "anchored", "src" ('audio' /
    'visual' / 'audio+visual' - cfg['detect_mode']), "conflict" (audio and
    pictures pointed at different places), "refined" (an edge was snapped
    to the exact frame - cfg['refine'], on by default), "edge_src" ({"start",
    "end"}: 'visual' = frame-exact picture match / cut, 'fade' = black
    stretch without a picture cut, 'audio' = placed by the sound, 'coarse'
    = not refined, 'file' = extended to the file start / end - anything but
    'visual' / 'file' is approximate: show a "check this edge" warning)}.
    Each list is best
    first, at most 5, near-misses (score >= 0.6 x confidence) with ok=False.
    Adds "error" if the episode's audio couldn't be read (or the file
    couldn't be processed: "out of memory", "file error (...)") - never
    raises for one bad file, so a batch goes on. Needs librosa.
    Without a log, [WARN] lines (e.g. an edge that ran out of its time
    budget) still go to the session log (applog)."""
    log = log or _warn_only
    prog = _safe_progress(progress)
    if templates is None:
        prog(0.0, "Loading templates")
        templates = load_templates_for(cfg, log, stop_event)
    out = {"duration": 0.0, "preintro": [], "intro": [], "credits": [], "aftercredits": []}
    err = []
    res = _detect_core(video, cfg, templates, stop_event, log, progress=prog, err_out=err)
    if res is None:
        try:
            out["duration"] = float(probe_video_duration(video) or probe_duration(video)
                                    or 0.0)
        except Exception:
            pass
        out["error"] = err[0] if err else "audio extract failed"
        prog(1.0, "Stopped" if _is_stopped(stop_event) else "Done")
        return out
    total = res["duration"]
    out["duration"] = total
    for kind in ("preintro", "intro", "credits", "aftercredits"):
        lst = []
        for c in res.get(kind, []):
            if not (c["ok"] or c["score"] >= _NEAR_MISS * c.get("thr", cfg["confidence"])):
                continue
            lst.append({"start": float(max(0.0, c["start"])),
                        "end": float(min(total, c["end"])),
                        "score": float(c["score"]), "template": c["template"],
                        "ok": bool(c["ok"]),
                        "match_start": float(max(0.0, c["match_start"])),
                        "anchored": bool(c["anchored"]),
                        "src": c.get("src", "audio"),
                        "conflict": bool(c.get("conflict")),
                        "refined": bool(any(c.get("refined") or ())),
                        "edge_src": dict({"start": "coarse", "end": "coarse"},
                                         **(c.get("edge_src") or {}))})
        out[kind] = lst[:_MAX_CANDS]
    prog(1.0, "Stopped" if _is_stopped(stop_event) else "Done")
    return out
