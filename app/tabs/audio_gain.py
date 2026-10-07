"""Audio -> Audio Gain: analyse loudness and adjust the gain / normalise a
file (Single file sub-tab). The Batch (season) sub-tab is BatchGainMixin
in audio_gain_batch.py."""
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import AUDIO_OUT_CHOICES
from ..engine.loudness import (change_gain, measure_loudness, normalize_loudness,
                               peak_normalize, probe_volume)
from ..engine.probe import video_kind
from ..i18n import N_, tr
from ..ui.widgets import (info_icon, KeyedCombobox, add_tooltip, auto_wrap, build_log_tab,
                          enable_file_drop, trim_text_lines)
from .audio_common import (DEFAULT_TP, GAIN_RANGE, GAIN_WARN, LUFS_RANGE, PEAK_RANGE,
                           TP_RANGE, TRACK_SCOPES, _TrackPicker,
                           _check_range, _discard_partial, _first_number,
                           _job_tracks, _mtime, _nb_help, _same_file,
                           media_types)
from .audio_gain_batch import BatchGainMixin
from .. import applog, jobs
from ..ui import icons


# audio-only output: the extension follows the chosen codec
_CODEC_EXT = {"ac3": ".ac3", "eac3": ".eac3", "flac": ".flac", "libopus": ".opus",
              "aac": ".m4a", "libmp3lame": ".mp3"}
# video containers: audio codecs they can hold; anything else is written as .mkv
_MP4_LIKE = {"aac", "ac3", "eac3", "libmp3lame"}
_CONTAINER_CODECS = {".mp4": _MP4_LIKE, ".m4v": _MP4_LIKE, ".mov": _MP4_LIKE,
                     ".ts": _MP4_LIKE, ".avi": {"ac3", "libmp3lame"},
                     ".webm": {"libopus", "libvorbis"}}


def _output_ext(src, kind, audio_out):
    """Extension for an Audio Gain output. Match source keeps the input's;
    an audio-only output follows the codec (AC3 -> .ac3, Opus -> .opus ...);
    a video file keeps its container unless that can't hold the codec
    (e.g. Opus into .mp4/.avi), then it becomes .mkv."""
    ext = os.path.splitext(src)[1]
    if audio_out is None:
        return ext
    codec = audio_out[0]
    if kind == "video":
        low = ext.lower()
        if low == ".mkv" or codec in _CONTAINER_CODECS.get(low, ()):
            return ext
        return ".mkv"
    return _CODEC_EXT.get(codec, ext)


def _join_tag(extra):
    """The optional extra name tag, joined with '_' unless it already starts
    with a separator."""
    extra = (extra or "").strip()
    if extra and extra[0] not in "_-. ":
        extra = "_" + extra
    return extra


# ======================= Audio Gain (volume / normalise) =======================
class AudioGainTab(BatchGainMixin, ttk.Frame):
    LUFS_PRESETS = [
        N_("-12 LUFS  (very loud)"),
        "-13 LUFS",
        N_("-14 LUFS  (loud - Spotify / YouTube)"),
        "-15 LUFS",
        N_("-16 LUFS  (streaming standard)"),
        "-17 LUFS",
        N_("-18 LUFS  (moderate - a bit quieter)"),
        "-19 LUFS",
        N_("-20 LUFS  (quiet)"),
        "-21 LUFS",
        "-22 LUFS",
        N_("-23 LUFS  (broadcast / TV standard)"),
        "-24 LUFS",
        "-25 LUFS",
        N_("-26 LUFS  (quite quiet)"),
        "-27 LUFS",
        N_("-28 LUFS  (very quiet)"),
    ]
    DEFAULT_LUFS = N_("-16 LUFS  (streaming standard)")   # stable default (list order may change)
    _MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm",
                   ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus")

    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        # (path, track) -> (lufs, peak_db, true_peak_dbtp, mean_db) from Analyze all
        self._meas = {}
        self._ftracks = {}            # path -> probe_audio_tracks() (batch languages)
        self._list_token = 0          # bumps on every Refresh file list
        self._compress_rows = set()   # rows whose LUFS gain will make loudnorm compress
        self._analyzing = False       # single-file Analyze running
        self._an_token = 0            # bumps when the single file changes
        self._jid = None              # jobs registry id of the running job
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        single = ttk.Frame(nb, padding=8)
        batch = ttk.Frame(nb, padding=8)
        nb.add(single, text="  " + tr("Single file") + "  ")
        nb.add(batch, text="  " + tr("Batch (season)") + "  ")

        # ===================== Single file =====================
        single.columnconfigure(1, weight=1)
        ttk.Label(single, text=tr("File:")).grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.file_var = tk.StringVar()
        sent = ttk.Entry(single, textvariable=self.file_var)
        sent.grid(row=0, column=1, sticky="we", padx=4, pady=4)
        icons.decorate(ttk.Button(single, text=tr("Browse..."), command=self.browse), "folder").grid(row=0, column=2, padx=4)

        ttk.Label(single, text=tr("Audio track:")).grid(row=1, column=0, sticky="e", padx=4, pady=(0, 4))
        trow = ttk.Frame(single)
        trow.grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 4))
        self.track_picker = _TrackPicker(trow, self, saved.get("ag_track_lang", ""), width=40)
        self.track_picker.combo.pack(side="left")
        self.track_picker.combo.bind("<<ComboboxSelected>>", self._track_changed, add="+")
        ttk.Label(trow, text=tr("Apply to:")).pack(side="left", padx=(12, 4))
        _sc = saved.get("ag_track_scope")
        self.scope_var = tk.StringVar(value=_sc if _sc in TRACK_SCOPES else list(TRACK_SCOPES)[0])
        scb = KeyedCombobox(trow, textvariable=self.scope_var, values=list(TRACK_SCOPES),
                            state="readonly", width=40)
        scb.pack(side="left")
        add_tooltip(scb, tr("All tracks: every audio track is measured and adjusted on its own. "
                            "Selected track only: just the chosen track (Batch: the Track language "
                            "one) is changed, every other track is copied untouched. Shared with "
                            "Batch. (Audio-only output always writes the selected track.)"))

        anrow = ttk.Frame(single)
        anrow.grid(row=2, column=0, columnspan=3, sticky="we", padx=4, pady=(0, 6))
        self.an_btn = icons.decorate(ttk.Button(anrow, text=tr("Analyze loudness"), command=self.analyze), "audio")
        self.an_btn.pack(side="left")
        self.analysis_var = tk.StringVar(value=self._an_hint())
        self.file_var.trace_add("write", lambda *a: self._file_changed())
        auto_wrap(ttk.Label(anrow, textvariable=self.analysis_var, style="Hint.TLabel",
                            justify="left")).pack(side="left", fill="x", expand=True, padx=(10, 0))

        mode = ttk.LabelFrame(single, text=tr("Adjustment"))
        mode.grid(row=3, column=0, columnspan=3, sticky="we", padx=4, pady=4)
        _m = saved.get("ag_single_mode")
        self.mode_var = tk.StringVar(value=_m if _m in ("gain", "norm", "peak") else "norm")
        ttk.Radiobutton(mode, text=tr("Change gain by"), variable=self.mode_var,
                        value="gain").grid(row=0, column=0, sticky="w", pady=2)
        self.gain_var = tk.StringVar(value=saved.get("ag_single_gain", "0"))
        ttk.Spinbox(mode, from_=GAIN_RANGE[0], to=GAIN_RANGE[1], increment=0.5,
                    textvariable=self.gain_var,
                    width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(mode, text=tr("dB   (+ louder, - quieter)")).grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(mode, text=tr("Normalize to"), variable=self.mode_var,
                        value="norm").grid(row=1, column=0, sticky="w", pady=2)
        self.norm_var = tk.StringVar(value=saved.get("ag_single_lufs", self.DEFAULT_LUFS))
        KeyedCombobox(mode, textvariable=self.norm_var, values=self.LUFS_PRESETS,
                      width=30).grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Radiobutton(mode, text=tr("Set peak to"), variable=self.mode_var,
                        value="peak").grid(row=2, column=0, sticky="w", pady=2)
        self.speak_var = tk.StringVar(value=saved.get("ag_single_peak", "-1"))
        ttk.Combobox(mode, textvariable=self.speak_var, values=["-0.5", "-1", "-2", "-3", "-6"],
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(mode, text=tr("dB peak")).grid(row=2, column=2, sticky="w")
        self.tp_var = tk.StringVar(value=saved.get("ag_tp", DEFAULT_TP))
        tpl = ttk.Label(mode, text=tr("True-peak ceiling:"))
        tpl.grid(row=3, column=0, sticky="w", pady=(4, 0))
        tpe = ttk.Entry(mode, textvariable=self.tp_var, width=6)
        tpe.grid(row=3, column=1, sticky="w", padx=6, pady=(4, 0))
        ttk.Label(mode, text=tr("dBTP  (LUFS mode only; shared with Batch)")).grid(
            row=3, column=2, sticky="w", pady=(4, 0))
        for w in (tpl, tpe):
            add_tooltip(w, tr("The highest true peak loudnorm may produce (-9 to 0, default -1.5). "
                              "If reaching the LUFS target would push peaks above it, loudnorm "
                              "compresses the loud parts instead of applying a clean gain."))
        ttk.Label(mode, text=tr("Output audio:")).grid(row=4, column=0, sticky="w", pady=(8, 0))
        _sa = saved.get("ag_single_aout")
        self.aout_var = tk.StringVar(value=_sa if _sa in AUDIO_OUT_CHOICES else list(AUDIO_OUT_CHOICES)[0])
        KeyedCombobox(mode, textvariable=self.aout_var, values=list(AUDIO_OUT_CHOICES),
                      state="readonly", width=30).grid(row=4, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))

        outrow = ttk.Frame(single)
        outrow.grid(row=4, column=0, columnspan=3, sticky="we", padx=4, pady=4)
        ttk.Label(outrow, text=tr("Extra name tag (optional):")).pack(side="left")
        self.suffix_var = tk.StringVar(value=saved.get("ag_single_tag", ""))
        ttk.Entry(outrow, textvariable=self.suffix_var, width=12).pack(side="left", padx=(4, 0))
        info_icon(outrow, tr("(the target is auto-added to the name; saved next "
                             "to the input)")).pack(side="left", padx=(4, 0))

        self.go_btn = icons.decorate(ttk.Button(single, style="Accent.TButton", text=tr("Apply to this file"), command=self.start_apply), "check")
        self.go_btn.grid(row=5, column=0, columnspan=3, sticky="we", padx=4, pady=(6, 4))
        add_tooltip(self.go_btn, tr("Write a new file with the volume adjusted / normalized"))
        enable_file_drop(sent, self._drop_load)

        # ===================== Batch (season): 2 columns =====================
        self._build_batch_tab(batch, saved)

        # ===================== shared status + progress + log =====================
        # status + progress + Stop: in the window's fixed footer when `bottom`
        # is given (always visible), else under the content
        self.status_var = tk.StringVar(value="")
        if bottom is not None:
            ttk.Label(bottom, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prog = ttk.Frame(bottom)
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(self, textvariable=self.status_var, style="Hint.TLabel").grid(
                row=1, column=0, sticky="w", pady=(6, 0))
            prog = ttk.Frame(self)
            prog.grid(row=2, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=12).grid(row=0, column=1, sticky="w", padx=(6, 0))
        # single Stop in the right corner beside the progress bar, like the other
        # tools - drives whichever job (single or batch) is running
        self.stop_btn = icons.decorate(ttk.Button(prog, text=tr("Stop"), command=self.stop, state="disabled"), "stop")
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, tr("Stop now - the file being processed is abandoned "
                                      "(its incomplete output is removed)"))
        self.logbox = build_log_tab(nb)
        _nb_help(nb, {0: "gain_single", 1: "gain_batch"}, "gain_batch")

        self.refresh_list()

    # ---- small helpers ----
    def _pick(self, var):
        d = filedialog.askdirectory(title=tr("Select folder"))
        if d:
            var.set(d)
            if var is self.bin_var:
                self.refresh_list()

    def _drop_load(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)

    def browse(self):
        path = filedialog.askopenfilename(title=tr("Select file"), filetypes=media_types())
        if path:
            self.file_var.set(path)

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            trim_text_lines(self.logbox)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def progress(self, frac, text=""):
        def _a():
            self.bar["value"] = int(frac * 1000)
            self.pct_var.set(text)
        self.after(0, _a)

    # ---- single-file analyze ----
    @staticmethod
    def _an_hint():
        return tr("Load a file and click Analyze to see its levels.")

    def _file_changed(self):
        """A new single file: the old analysis no longer applies."""
        self._an_token += 1
        self.analysis_var.set(self._an_hint())
        self.track_picker.load(self.file_var.get())

    def _track_changed(self, _e=None):
        """Another track picked: the shown analysis was of the old one."""
        self._an_token += 1
        self.analysis_var.set(self._an_hint())

    def analyze(self):
        if self._analyzing:
            return
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror(tr("Error"), tr("Select a valid file first."))
            return
        self._analyzing = True
        self.an_btn.configure(state="disabled")
        self.analysis_var.set(tr("Analyzing..."))
        jid = jobs.begin(tr("Audio Gain analyze"), stop_event=None, tab=self)
        threading.Thread(target=self._analyze_worker,
                         args=(f, self._an_token, self.track_picker.track_for(f), jid),
                         daemon=True).start()

    def _analyze_worker(self, f, token, picked=None, jid=None):
        txt = tr("Analysis failed - see log.")
        failed = True
        try:
            track = self.track_picker.resolve_track(f, picked)
            mean, mx = probe_volume(f, track)
            lufs, tp = measure_loudness(f, track)
            parts = []
            if lufs is not None:
                parts.append(f"{lufs:.1f} LUFS")
            if tp is not None:
                parts.append(tr("true peak {tp:.1f} dBTP", tp=tp))
            if mean is not None:
                parts.append(tr("mean {db:.1f} dB", db=mean))
            if mx is not None:
                parts.append(tr("max {db:.1f} dB", db=mx))
            txt = (tr("Track #{n}: {levels}", n=track + 1, levels=", ".join(parts)) if parts
                   else tr("Could not read levels (no audio?)."))
            failed = False
        except Exception as e:
            self.log(f"[FAIL] analyze {os.path.basename(f)}: {e}")
        finally:
            jobs.end(jid, ok=not failed, summary=txt)

            def _done():
                self._analyzing = False
                self.an_btn.configure(state="normal")
                if token == self._an_token:      # file not changed meanwhile
                    self.analysis_var.set(txt)
            self.after(0, _done)

    # ---- single-file apply ----
    @staticmethod
    def _spec_and_tag(mode, gain_s, peak_s, lufs_s):
        """Return ((mode, target), filename_tag). Raises ValueError on bad input."""
        try:
            if mode == "gain":
                g = _first_number(gain_s)
            elif mode == "peak":
                pk = _first_number(peak_s)
            else:
                lu = _first_number(lufs_s)
        except ValueError:
            raise ValueError(tr("Enter a numeric value (dB / LUFS) for the selected mode."))
        if mode == "gain":
            _check_range(g, GAIN_RANGE, tr("Gain (dB)"))
            return ("gain", g), f"{g:+g}dB"
        if mode == "peak":
            _check_range(pk, PEAK_RANGE, tr("Peak (dB)"))
            return ("peak", pk), f"{pk:g}dBpeak"
        _check_range(lu, LUFS_RANGE, tr("Loudness target (LUFS)"))
        return ("lufs", f"{lu:g}"), f"{lu:g}LUFS"     # e.g. _-16LUFS

    def _tp_value(self, strict=False):
        """True-peak ceiling as a string for loudnorm. strict -> ValueError
        when invalid; otherwise falls back to the default."""
        try:
            num = _first_number(self.tp_var.get())
        except ValueError:
            if strict:
                raise ValueError(tr("True-peak ceiling must be a number (e.g. -1.5)."))
            return DEFAULT_TP
        try:
            v = _check_range(num, TP_RANGE, tr("True-peak ceiling (dBTP)"))
        except ValueError:
            if strict:
                raise
            return DEFAULT_TP
        return f"{v:g}"

    @staticmethod
    def _confirm_gain(spec, parent=None):
        """Warn before a big flat boost (likely clipping). True = go ahead."""
        if spec[0] == "gain" and spec[1] > GAIN_WARN:
            return messagebox.askyesno(
                tr("Large gain"),
                tr("+{db:g} dB is a big boost - loud passages will very likely CLIP "
                   "(distort).\nConsider 'Normalize to' (LUFS) instead.\n\n"
                   "Apply it anyway?", db=spec[1]), parent=parent)
        return True

    def _plan_output(self, f, folder, extra, tag, audio_out):
        """Work out the output path and whether to copy the video stream.
        Returns (out | None, keep_video, label)."""
        kind = video_kind(f)
        ext = _output_ext(f, kind, audio_out)
        keep = kind is not None
        label = {"video": "video kept", "cover": "cover art kept"}.get(kind, "audio only")
        if kind == "cover" and ext.lower() != os.path.splitext(f)[1].lower():
            keep = False      # e.g. .ac3 / .opus can't carry a cover picture
            label = f"audio only - cover art dropped ({ext} can't hold it)"
        base = os.path.splitext(f if folder is None
                                else os.path.join(folder, os.path.basename(f)))[0]
        out = base + extra + (("_" + tag) if tag else "") + ext
        if _same_file(out, f):
            out = base + extra + "_norm" + ext
        if _same_file(out, f):
            return None, keep, label
        return out, keep, label

    def start_apply(self):
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror(tr("Error"), tr("Select a valid file first."))
            return
        try:
            spec, tag = self._spec_and_tag(self.mode_var.get(), self.gain_var.get(),
                                           self.speak_var.get(), self.norm_var.get())
            tp = self._tp_value(strict=spec[0] == "lufs")
        except ValueError as e:
            messagebox.showerror(tr("Error"), str(e))
            return
        if not self._confirm_gain(spec):
            return
        extra = _join_tag(self.suffix_var.get())
        self.stop_event.clear()
        self._brunning(True)          # enables the Stop button
        self.status_var.set(tr("Processing..."))
        audio_out = AUDIO_OUT_CHOICES[self.aout_var.get()]
        scope = TRACK_SCOPES.get(self.scope_var.get(), "all")
        self._jid = jobs.begin(tr("Audio Gain apply"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._one_worker,
                         args=(f, extra, tag, spec, audio_out, tp,
                               self.track_picker.track_for(f), scope),
                         daemon=True).start()

    def _one_worker(self, f, extra, ntag, spec, audio_out, tp, picked=None, scope="all"):
        msg = tr("Failed - see log.")
        failed = True
        jid = self._jid
        try:
            mode, target = spec
            out, keep, tag = self._plan_output(f, None, extra, ntag, audio_out)
            if out is None:
                self.log(f"[FAIL] {os.path.basename(f)}: the output would overwrite the input.")
                msg = tr("Failed - the output would overwrite the input.")
                return
            self.log(f"  output: {out}")
            track = self.track_picker.resolve_track(f, picked)
            tracks = _job_tracks(scope, keep, track)
            self.log("  audio: " + (f"track #{track + 1} only" if tracks is not None
                                    else "every track (each measured on its own)")
                     + ("" if keep else " (audio-only output)"))
            cb = dict(stop_event=self.stop_event, on_progress=self.progress, on_log=self.log,
                      tracks=tracks)
            before = _mtime(out)
            if mode == "lufs":
                self.log(f"[NORM] {os.path.basename(f)} -> {target} LUFS, TP {tp} ({tag})")
                rc, err = normalize_loudness(f, out, target, keep, tp=tp,
                                             audio_out=audio_out, **cb)
            elif mode == "peak":
                self.log(f"[PEAK] {os.path.basename(f)} -> {target} dB peak ({tag})")
                rc, err = peak_normalize(f, out, target, keep, audio_out=audio_out, **cb)
            else:
                self.log(f"[GAIN] {os.path.basename(f)} volume={target}dB ({tag})")
                rc, err = change_gain(f, out, f"volume={target}dB", keep, audio_out=audio_out, **cb)
            if self.stop_event.is_set() or rc == -1:
                self.log("  [STOPPED]")
                msg = tr("Stopped.")
                failed = False
                _discard_partial(out, before, self.log)
            elif rc == 0:
                self.log(f"  [OK] {out}")
                msg = tr("Done.")
                failed = False
            else:
                for ln in err.strip().splitlines()[-4:]:
                    self.log(f"    | {ln}")
                self.log("  [FAIL]")
                _discard_partial(out, before, self.log)
        except Exception as e:
            self.log(f"  [FAIL] {e}")
        finally:
            jobs.end(jid, ok=not failed, summary=msg)

            def _f():
                self._brunning(False, msg)
                self.bar.configure(value=0)
            self.after(0, _f)

    def stop(self):
        self.stop_event.set()
        self.log("Stopping (the file in progress is abandoned)...")

    def snapshot(self):
        """Batch + single settings to persist between sessions."""
        return {
            "ag_in": self.bin_var.get(),
            "ag_out": self.bout_var.get(),
            "ag_match": self.bmatch_var.get(),
            "ag_lufs": self.bnorm_var.get(),
            "ag_peak": self.bpeak_var.get(),
            "ag_gain": self.bgain_var.get(),
            "ag_aout": self.baout_var.get(),
            "ag_tol": self.tol_var.get(),
            "ag_skip_ok": bool(self.bskip_var.get()),
            "ag_batch_lang": self._batch_lang(),
            "ag_track_scope": self.scope_var.get(),
            "ag_track_lang": self.track_picker.pref_lang,
            "ag_single_lufs": self.norm_var.get(),
            "ag_single_mode": self.mode_var.get(),
            "ag_single_gain": self.gain_var.get(),
            "ag_single_peak": self.speak_var.get(),
            "ag_single_aout": self.aout_var.get(),
            "ag_single_tag": self.suffix_var.get(),
            "ag_tp": self.tp_var.get(),
        }
