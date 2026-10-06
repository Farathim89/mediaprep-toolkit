"""Audio -> Audio Gain -> Batch (season) sub-tab (BatchGainMixin)."""
import os
import statistics
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT, AUDIO_OUT_CHOICES
from ..engine.loudness import (change_gain, measure_loudness, normalize_loudness,
                               peak_normalize, probe_volume)
from ..engine.probe import probe_audio_tracks
from ..i18n import N_, ntr, tr
from ..ui.widgets import (info_icon, KeyedCombobox, ScrollFrame, Tooltip, add_tooltip,
                          bind_status_colors, enable_file_drop)
from .audio_common import (DEFAULT_TP, GAIN_RANGE, TRACK_SCOPES, _discard_partial,
                           _first_number, _job_tracks, _mtime, _track_by_lang)
from .. import jobs
from ..ui import icons


_ANY_LANG = N_("Any (first track)")     # key; shown as tr(_ANY_LANG)


def _skip_reason(mode, target, value, tol):
    """'Only fix outliers': why a file can be left alone, or None to process it.
    lufs/peak: value is the file's measured LUFS / sample peak (None = unknown
    -> process); gain: skip when the flat gain itself is below the tolerance."""
    try:
        target = float(target)
    except (TypeError, ValueError):
        return None
    if mode == "gain":
        if abs(target) < tol:
            return f"gain {target:+.1f} dB is within ±{tol:.1f}"
        return None
    if value is None:
        return None
    if abs(value - target) <= tol:
        unit = "LUFS" if mode == "lufs" else "dB peak"
        return f"already at {value:.1f} {unit} (within ±{tol:.1f})"
    return None


class BatchGainMixin:
    """Audio Gain -> Batch (season) sub-tab: a folder of files, per-file
    loudness / peak analysis, outlier flags and the batch run."""

    def _build_batch_tab(self, batch, saved):
        """Batch (season) sub-tab: input/output folders, settings and the file list."""
        batch.columnconfigure(1, weight=1)
        batch.rowconfigure(0, weight=1)
        bleft_sc = ScrollFrame(batch, canvas_width=320)
        bleft_sc.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        bleft = bleft_sc.interior
        bleft.columnconfigure(0, weight=1)
        bright = ttk.Frame(batch)
        bright.grid(row=0, column=1, sticky="nsew")
        bright.columnconfigure(0, weight=1)
        bright.rowconfigure(1, weight=1)

        ttk.Label(bleft, text=tr("Input folder:")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        ir = ttk.Frame(bleft)
        ir.grid(row=1, column=0, sticky="we", pady=(0, 6))
        ir.columnconfigure(0, weight=1)
        self.bin_var = tk.StringVar(value=saved.get("ag_in", AUDIOGAIN_INPUT))
        bin_ent = ttk.Entry(ir, textvariable=self.bin_var, width=28)
        bin_ent.grid(row=0, column=0, sticky="we")
        enable_file_drop(bin_ent, self._drop_bin)
        icons.decorate(ttk.Button(ir, text="...", width=3, command=lambda: self._pick(self.bin_var)), "folder").grid(row=0, column=1, padx=(4, 0))

        ttk.Label(bleft, text=tr("Output folder:")).grid(row=2, column=0, sticky="w", pady=(0, 2))
        orf = ttk.Frame(bleft)
        orf.grid(row=3, column=0, sticky="we", pady=(0, 6))
        orf.columnconfigure(0, weight=1)
        self.bout_var = tk.StringVar(value=saved.get("ag_out", AUDIOGAIN_OUTPUT))
        bout_ent = ttk.Entry(orf, textvariable=self.bout_var, width=28)
        bout_ent.grid(row=0, column=0, sticky="we")
        enable_file_drop(bout_ent, self._drop_bout)
        icons.decorate(ttk.Button(orf, text="...", width=3, command=lambda: self._pick(self.bout_var)), "folder").grid(row=0, column=1, padx=(4, 0))

        ttk.Label(bleft, text=tr("Match every file by:")).grid(row=4, column=0, sticky="w")
        mf = ttk.Frame(bleft)
        mf.grid(row=5, column=0, sticky="we", pady=(2, 10))
        mf.columnconfigure(1, weight=1)
        self.bmatch_var = tk.StringVar(value=saved.get("ag_match", "lufs"))
        rl = ttk.Radiobutton(mf, text=tr("Loudness (LUFS)"), variable=self.bmatch_var, value="lufs")
        rl.grid(row=0, column=0, sticky="w")
        add_tooltip(rl, tr("Best for matching a season: brings every file to the same perceived "
                       "loudness. After Analyze all, the Gain column shows the exact +/- dB each "
                       "file will get - so you can see how much each one moves."))
        self.bnorm_var = tk.StringVar(value=saved.get("ag_lufs", self.DEFAULT_LUFS))
        # own row, full width, so the preset labels ('-14 LUFS (loud - ...)') fit
        KeyedCombobox(mf, textvariable=self.bnorm_var, values=self.LUFS_PRESETS,
                      width=34).grid(row=1, column=0, columnspan=2, sticky="we", padx=(18, 0))
        tpf = ttk.Frame(mf)
        tpf.grid(row=2, column=0, columnspan=2, sticky="w", padx=(18, 0), pady=(2, 0))
        ttk.Label(tpf, text=tr("True-peak ceiling:")).pack(side="left")
        btpe = ttk.Entry(tpf, textvariable=self.tp_var, width=6)
        btpe.pack(side="left", padx=4)
        ttk.Label(tpf, text="dBTP").pack(side="left")
        add_tooltip(btpe, tr("The highest true peak loudnorm may produce (-9 to 0, default -1.5). "
                             "A Gain value marked * would push peaks above it, so loudnorm "
                             "compresses that file instead of applying a clean gain."))
        rp = ttk.Radiobutton(mf, text=tr("Peak (dB)"), variable=self.bmatch_var, value="peak")
        rp.grid(row=3, column=0, sticky="w", pady=(4, 0))
        add_tooltip(rp, tr("Sets each file's loudest sample to the same peak. The Gain column shows "
                       "the +/- dB each file gets. (Loudness/LUFS usually matches better by ear.)"))
        self.bpeak_var = tk.StringVar(value=saved.get("ag_peak", "-1"))
        ttk.Combobox(mf, textvariable=self.bpeak_var, values=["-0.5", "-1", "-2", "-3", "-6"],
                     state="readonly", width=8).grid(row=3, column=1, sticky="we", padx=(6, 0), pady=(4, 0))
        rg = ttk.Radiobutton(mf, text=tr("Gain by (dB)"), variable=self.bmatch_var, value="gain")
        rg.grid(row=4, column=0, sticky="w", pady=(4, 0))
        add_tooltip(rg, tr("Applies the SAME +/- dB to every file (a manual flat shift, no matching). "
                       "To match by hand, read the 'Δ median' column after Analyze all and set "
                       "this to the opposite (e.g. a file at +3.0 needs -3.0). Only works if every "
                       "file needs the same shift; otherwise use Loudness (LUFS)."))
        self.bgain_var = tk.StringVar(value=saved.get("ag_gain", "0"))
        ttk.Spinbox(mf, from_=GAIN_RANGE[0], to=GAIN_RANGE[1], increment=0.5,
                    textvariable=self.bgain_var,
                    width=8).grid(row=4, column=1, sticky="we", padx=(6, 0), pady=(4, 0))

        ttk.Label(bleft, text=tr("Output audio:")).grid(row=6, column=0, sticky="w", pady=(0, 2))
        _aout = saved.get("ag_aout")
        self.baout_var = tk.StringVar(value=_aout if _aout in AUDIO_OUT_CHOICES else list(AUDIO_OUT_CHOICES)[0])
        KeyedCombobox(bleft, textvariable=self.baout_var, values=list(AUDIO_OUT_CHOICES),
                      state="readonly").grid(row=7, column=0, sticky="we", pady=(0, 8))
        lf = ttk.Frame(bleft)
        lf.grid(row=8, column=0, sticky="we", pady=(0, 4))
        ttk.Label(lf, text=tr("Track language:")).pack(side="left")
        self.blang_var = tk.StringVar(value=saved.get("ag_batch_lang") or _ANY_LANG)
        self.blang_cb = KeyedCombobox(lf, textvariable=self.blang_var, values=[_ANY_LANG],
                                      state="readonly", width=18)
        self.blang_cb.pack(side="left", padx=(4, 0), fill="x", expand=True)
        add_tooltip(self.blang_cb, tr("Which audio track of each file the table measures (and, with "
                                       "'Selected track only', the one that is changed): the first "
                                       "track in this language, else the first track. Languages are "
                                       "collected from the folder; (n/m) = in how many listed files "
                                       "it exists."))
        sf = ttk.Frame(bleft)
        sf.grid(row=9, column=0, sticky="we", pady=(0, 8))
        ttk.Label(sf, text=tr("Apply to:")).pack(side="left")
        KeyedCombobox(sf, textvariable=self.scope_var, values=list(TRACK_SCOPES),
                      state="readonly", width=24).pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.refresh_btn = icons.decorate(ttk.Button(bleft, text=tr("Refresh file list"), command=self.refresh_list), "refresh")
        self.refresh_btn.grid(row=10, column=0, sticky="we", pady=2)
        add_tooltip(self.refresh_btn, tr("List the media files in the input folder"))
        self.analyze_all_btn = icons.decorate(ttk.Button(bleft, text=tr("Analyze all (loudness)"), command=self.analyze_all), "audio")
        self.analyze_all_btn.grid(row=11, column=0, sticky="we", pady=2)
        add_tooltip(self.analyze_all_btn, tr("Measure the loudness of the TICKED files in the list "
                                              "(the list is not reloaded - removed rows stay "
                                              "removed), then flag any episode that stands out "
                                              "from the season so you can see if they already "
                                              "match"))
        tolf = ttk.Frame(bleft)
        tolf.grid(row=12, column=0, sticky="we", pady=(2, 0))
        ttk.Label(tolf, text=tr("Flag if off by more than")).pack(side="left")
        self.tol_var = tk.StringVar(value=saved.get("ag_tol", "1.0"))
        ttk.Spinbox(tolf, from_=0.2, to=6.0, increment=0.5, textvariable=self.tol_var,
                    width=5).pack(side="left", padx=(4, 4))
        ttk.Label(tolf, text="LUFS").pack(side="left")
        self.bskip_var = tk.BooleanVar(value=bool(saved.get("ag_skip_ok", False)))
        skc = ttk.Checkbutton(bleft, text=tr("Only fix outliers: skip files already within "
                                             "±tolerance of the target"), variable=self.bskip_var)
        skc.grid(row=13, column=0, sticky="w", pady=(4, 0))
        add_tooltip(skc, tr("Files whose measured loudness (LUFS mode) or peak (Peak mode) is "
                        "already within the tolerance above of the target are left alone "
                        "(Gain mode: skipped when the gain itself is smaller than the tolerance). "
                        "Shown as 'skip' in the Gain column; files not analysed yet are "
                        "measured first."))
        bbrow = ttk.Frame(bleft)
        bbrow.grid(row=14, column=0, sticky="we", pady=(10, 2))
        bbrow.columnconfigure(0, weight=1)
        self.batch_btn = icons.decorate(ttk.Button(bbrow, style="Accent.TButton", text=tr("Normalize folder"), command=self.start_batch), "audio")
        self.batch_btn.grid(row=0, column=0, sticky="we")
        add_tooltip(self.batch_btn, tr("Two-pass normalize every file to the target so the season "
                                       "matches"))
        self.queue_btn = icons.decorate(ttk.Button(bbrow, text=tr("Add to queue"), command=self.queue_batch), "queue")
        self.queue_btn.grid(row=0, column=1, padx=(4, 0))
        add_tooltip(self.queue_btn, tr("Queue this normalize (the ticked files and current settings) "
                                       "to run after the jobs already running / queued - see "
                                       "Queue... in the status bar"))

        hdr = ttk.Frame(bright)
        hdr.grid(row=0, column=0, sticky="we", pady=(0, 2))
        hdr.columnconfigure(0, weight=1)
        info_icon(hdr, tr("Files in the input folder - tick the ones to include "
                          "(loudness before normalizing; click the checkmark "
                          "header for all):") + "\n\n" + tr(
            "Gain marked * = in LUFS mode loudnorm will compress "
            "that file (its peaks would pass the true-peak "
            "ceiling) - not a clean gain.\nTip: after "
            "normalizing, set Input to the output folder and "
            "Analyze all again to confirm they now match.")).grid(row=0, column=0, sticky="w")
        rm_btn = icons.decorate(ttk.Button(hdr, text=tr("Remove selected"), command=self._remove_selected), "remove")
        rm_btn.grid(row=0, column=1, sticky="e", padx=(6, 0))
        add_tooltip(rm_btn, tr("Remove the highlighted rows from the list (or press Delete) so they "
                           "aren't processed. 'Refresh file list' re-adds everything from the "
                           "folder."))
        tv = ttk.Frame(bright)
        tv.grid(row=1, column=0, sticky="nsew")
        tv.rowconfigure(0, weight=1)
        tv.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tv, columns=("sel", "file", "lufs", "peak", "mean", "dmed", "gain"),
                                 show="headings", height=8)
        self.tree.heading("sel", text="\u2713", command=self._toggle_all)
        self.tree.heading("file", text=tr("File"))
        self.tree.heading("lufs", text="LUFS")
        self.tree.heading("peak", text=tr("Peak dB"))
        self.tree.heading("mean", text=tr("Mean dB"))
        self.tree.heading("dmed", text=tr("\u0394 median"))
        self.tree.heading("gain", text=tr("Gain (dB)"))
        self.tree.column("sel", width=30, anchor="center", stretch=False)
        self.tree.column("file", width=200, minwidth=120, anchor="w")
        for col in ("lufs", "peak", "mean", "dmed", "gain"):
            self.tree.column(col, width=68, minwidth=56, anchor="center", stretch=True)
        # episodes that stand out from the season are highlighted (warning
        # colour readable on the light and the dark themes)
        bind_status_colors(self.tree, {"outlier": "warn", "skipped": "good"})
        # per-cell tooltip for Gain values marked * (loudnorm will compress)
        self._gain_tip = Tooltip(self.tree, "", follow=True)
        self.tree.bind("<Motion>", self._tree_motion, add="+")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        for v in (self.bmatch_var, self.bnorm_var, self.bpeak_var, self.bgain_var, self.tp_var,
                  self.bskip_var, self.tol_var):
            v.trace_add("write", lambda *a: self._update_gain_col())
        self.tol_var.trace_add("write", lambda *a: self._flag_outliers(log=False))
        self.blang_var.trace_add("write", lambda *a: self._render_meas())
        enable_file_drop(self.tree, self._drop_bin)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

    def _drop_bin(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            path = os.path.dirname(path)   # dropped a media file -> use its folder
        if path and os.path.isdir(path):
            self.bin_var.set(path)
            self.refresh_list()

    def _drop_bout(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            path = os.path.dirname(path)   # dropped a file -> use its folder
        if path and os.path.isdir(path):
            self.bout_var.set(path)

    # ---- batch file list + analyze all ----
    def _list_files(self):
        indir = self.bin_var.get().strip()
        if not indir or not os.path.isdir(indir):
            return []
        # listdir, not glob: folder names with [brackets] break glob patterns
        try:
            names = os.listdir(indir)
        except OSError:
            return []
        return sorted(os.path.join(indir, n) for n in names
                      if n.lower().endswith(self._MEDIA_EXTS)
                      and os.path.isfile(os.path.join(indir, n)))

    def refresh_list(self):
        if self.stop_btn.instate(["!disabled"]):   # a job is running - don't wipe its rows
            self.status_var.set(tr("Busy - Stop the current job before refreshing the list."))
            return
        self.tree.delete(*self.tree.get_children())
        self._meas = {}
        self._ftracks = {}
        files = self._list_files()
        for f in files:
            self.tree.insert("", "end", iid=f,
                             values=("☑", os.path.basename(f), "-", "-", "-", "-", "-"))
        # collect the audio-track languages of the folder in the background
        self._list_token += 1
        tok = self._list_token
        self._set_lang_values()
        if files:
            threading.Thread(target=self._collect_tracks, args=(files, tok), daemon=True).start()

    def _collect_tracks(self, files, tok):
        for f in files:
            if tok != self._list_token:
                return
            if f not in self._ftracks:
                try:
                    self._ftracks[f] = probe_audio_tracks(f)
                except Exception:
                    self._ftracks[f] = []
        self.after(0, lambda: self._langs_ready(tok))

    def _langs_ready(self, tok):
        if tok != self._list_token:
            return
        self._set_lang_values()
        self._render_meas()

    def _batch_lang(self):
        """The chosen track language code ('' = Any / first track)."""
        v = self.blang_var.get().strip()
        if not v or v == _ANY_LANG:
            return ""
        return v.split()[0].lower()

    def _set_lang_values(self):
        """Fill the Track language list from the listed files' tracks."""
        rows = self.tree.get_children()
        counts = {}
        for r in rows:
            for lang in {t["lang"] for t in self._ftracks.get(r, [])}:
                counts[lang] = counts.get(lang, 0) + 1
        cur = self._batch_lang()
        if cur and cur not in counts:
            counts.setdefault(cur, 0)
        n = len(rows)
        labels = {lang: f"{lang}  ({c}/{n})" for lang, c in counts.items()}
        # KeyedCombobox shows tr(_ANY_LANG); the language labels stay as they are
        self.blang_cb.configure(values=[_ANY_LANG] + [labels[k] for k in sorted(labels)])
        if cur and self.blang_var.get() != labels[cur]:
            self.blang_var.set(labels[cur])

    def _row_track(self, path, lang=None, probe=False):
        """(track, found) for a file: the first track in the chosen language,
        else (0, False). probe=True (workers only) probes an unlisted file."""
        lang = self._batch_lang() if lang is None else lang
        if not lang:
            return 0, True
        tracks = self._ftracks.get(path)
        if tracks is None and probe:
            try:
                tracks = self._ftracks[path] = probe_audio_tracks(path)
            except Exception:
                tracks = []
        t = _track_by_lang(tracks or [], lang)
        return (t, True) if t is not None else (0, False)

    def _cur_meas(self, r):
        """(lufs, peak, true_peak, mean) of the row's current track, or None."""
        return self._meas.get((r, self._row_track(r)[0]))

    def _render_meas(self):
        """Show the measurements of each row's current-language track."""
        if not hasattr(self, "tree"):
            return
        for r in self.tree.get_children():
            m = self._cur_meas(r)
            if m is None:
                vals = ("-", "-", "-")
            else:
                vals = tuple(f"{v:.1f}" if v is not None else "?" for v in (m[0], m[1], m[3]))
            self.tree.set(r, "lufs", vals[0])
            self.tree.set(r, "peak", vals[1])
            self.tree.set(r, "mean", vals[2])
        self._update_gain_col()
        self._flag_outliers(log=False)

    def _on_tree_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) != "#1":
            return
        row = self.tree.identify_row(e.y)
        if row:
            cur = self.tree.set(row, "sel")
            self.tree.set(row, "sel", "☐" if cur == "☑" else "☑")

    def _toggle_all(self):
        rows = self.tree.get_children()
        val = "☑" if any(self.tree.set(r, "sel") == "☐" for r in rows) else "☐"
        for r in rows:
            self.tree.set(r, "sel", val)

    def _remove_selected(self):
        """Drop the highlighted rows from the list so they aren't processed.
        (Refresh file list re-adds everything from the folder.)"""
        if self.stop_btn.instate(["!disabled"]):
            self.status_var.set(tr("Busy - Stop the current job before removing rows."))
            return
        sel = self.tree.selection()
        if not sel:
            self.status_var.set(tr("Select one or more rows first (Ctrl/Shift-click for several)."))
            return
        for iid in sel:
            for k in [k for k in self._meas if k[0] == iid]:
                self._meas.pop(k, None)
            self.tree.delete(iid)

    def _tol(self):
        try:
            return abs(_first_number(self.tol_var.get()))
        except ValueError:
            return 1.0

    def _update_gain_col(self):
        """Show the dB gain each analyzed file would get for the current mode/target.
        In LUFS mode a gain that would push the true peak above the ceiling is
        marked * - loudnorm then compresses instead of applying a clean gain.
        With 'Only fix outliers' on, files that would be left alone show 'skip'."""
        if not hasattr(self, "tree") or not hasattr(self, "bskip_var"):
            return
        mode = self.bmatch_var.get()
        tp_ceiling = float(self._tp_value())
        skip, tol = self.bskip_var.get(), self._tol()
        self._compress_rows = set()
        for r in self.tree.get_children():
            lufs, peak, tp, _mean = self._cur_meas(r) or (None, None, None, None)
            g = target = None
            try:
                if mode == "gain":
                    g = target = _first_number(self.bgain_var.get())
                elif mode == "lufs" and lufs is not None:
                    target = _first_number(self.bnorm_var.get())
                    g = target - lufs
                elif mode == "peak" and peak is not None:
                    target = _first_number(self.bpeak_var.get())
                    g = target - peak
            except ValueError:
                g = None
            txt = f"{g:+.1f}" if g is not None else "-"
            if g is not None and skip and _skip_reason(
                    mode, target, lufs if mode == "lufs" else peak, tol):
                txt = tr("skip")
            elif mode == "lufs" and g is not None and tp is not None and tp + g > tp_ceiling:
                txt += "*"
                self._compress_rows.add(r)
            self.tree.set(r, "gain", txt)

    def _tree_motion(self, e):
        """Tooltip on Gain cells marked * only."""
        tip = ""
        if (self.tree.identify_region(e.x, e.y) == "cell"
                and self.tree.identify_column(e.x) == "#7"
                and self.tree.identify_row(e.y) in self._compress_rows):
            tip = tr("loudnorm will compress (not a clean gain)")
        if tip != self._gain_tip.text:
            self._gain_tip._hide()
            self._gain_tip.text = tip
            if tip:
                self._gain_tip._schedule()

    def analyze_all(self):
        if not self.tree.get_children():
            self.refresh_list()          # empty list: load the folder first
        files = [r for r in self.tree.get_children() if self.tree.set(r, "sel") == "☑"]
        if not files:
            if self.tree.get_children():
                messagebox.showerror(tr("Error"), tr("No files ticked - tick at least one "
                                                     "(or click the checkmark header)."))
            else:
                self.log("No media files found in the input folder.")
            return
        self.stop_event.clear()
        self._brunning(True)
        self._jid = jobs.begin(tr("Audio Gain analyze all"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._analyze_all_worker, args=(files, self._batch_lang()),
                         daemon=True).start()

    def _measure_row(self, path, track):
        """Measure one file's track (worker thread): store it and update its
        row if the row still shows that track. Returns the tuple, or None if
        stopped."""
        lufs, tp = measure_loudness(path, track, self.stop_event)
        if self.stop_event.is_set():
            return None
        mean, mx = probe_volume(path, track)
        m = self._meas[(path, track)] = (lufs, mx, tp, mean)
        vals = tuple(f"{v:.1f}" if v is not None else "?" for v in (lufs, mx, mean))

        def _set(p=path, v=vals, t=track):
            if not self.tree.exists(p) or self._row_track(p)[0] != t:
                return                   # row removed / language changed meanwhile
            self.tree.set(p, "lufs", v[0])
            self.tree.set(p, "peak", v[1])
            self.tree.set(p, "mean", v[2])
        self.after(0, _set)
        return m

    def _analyze_all_worker(self, files, lang=""):
        n = len(files)
        done = 0
        stopped = False
        jid = self._jid
        try:
            if lang:
                self.log(f"Analyzing the '{lang}' track of {n} file(s) "
                         "(first track where there is none).")
            for i, path in enumerate(files, 1):
                if self.stop_event.is_set():
                    stopped = True
                    self.log("Analysis stopped.")
                    break
                self.status(tr("Analyzing {i}/{n}: {name}", i=i, n=n, name=os.path.basename(path)))
                self.progress((i - 1) / n, f"{i - 1}/{n}")
                track, found = self._row_track(path, lang, probe=True)
                if not found:
                    self.log(f"  {os.path.basename(path)}: no '{lang}' track - measured track #1")
                if self._measure_row(path, track) is None:
                    stopped = True
                    self.log("Analysis stopped.")
                    break
                done += 1
                self.progress(i / n, f"{i}/{n}")
        except Exception as e:
            self.log(f"[FAIL] analysis: {e}")
        finally:
            self.after(0, lambda: self._finish_analyze(done, n, stopped, jid))

    def _finish_analyze(self, done, n, stopped, jid=None):
        # the summary needs the Tk-side outlier verdict, so the job ends here
        # (scheduled from the worker's finally)
        head = (tr("Stopped - analyzed {done}/{n}", done=done, n=n) if stopped
                else ntr("Analyzed {n} file", "Analyzed {n} files", done))
        text = head + "."
        try:
            self._render_meas()
            verdict = self._flag_outliers(log=True)
            text = head + (" - " + verdict if verdict else ".")
            self._brunning(False, text)
        finally:
            jobs.end(jid, ok=True, summary=text)

    def _flag_outliers(self, log=False):
        """Compare each analyzed file to the season's median loudness, fill the
        'Δ median' column, colour the ones that stand out, and (optionally) log
        a plain verdict on whether the season is consistent."""
        tol = self._tol()
        vals = {}
        for r in self.tree.get_children():
            m = self._cur_meas(r)
            if m and m[0] is not None:
                vals[r] = m[0]
        if len(vals) < 1:
            return None
        med = statistics.median(vals.values())
        spread = max(vals.values()) - min(vals.values())
        outliers = []
        for r in self.tree.get_children():
            lufs = vals.get(r)
            if lufs is None:
                self.tree.set(r, "dmed", "-")
                self.tree.item(r, tags=())
                continue
            d = lufs - med
            self.tree.set(r, "dmed", f"{d:+.1f}")
            if abs(d) > tol:
                self.tree.item(r, tags=("outlier",))
                outliers.append((os.path.basename(r), d))
            else:
                self.tree.item(r, tags=())
        summary = tr("median {med:.1f} LUFS, spread {spread:.1f}; {verdict}", med=med,
                     spread=spread,
                     verdict=(ntr("{n} stands out by > {tol:g} LUFS",
                                  "{n} stand out by > {tol:g} LUFS", len(outliers), tol=tol)
                              if outliers else tr("all consistent")))
        if not log:
            return summary
        self.log(f"Season median {med:.1f} LUFS, spread {spread:.1f} LUFS across "
                 f"{len(vals)} file(s).")
        if not outliers:
            self.log(f"  [CONSISTENT] every episode is within {tol:g} LUFS of the median "
                     "- no normalizing needed.")
        else:
            outliers.sort(key=lambda t: -abs(t[1]))
            self.log(f"  [CHECK] {len(outliers)} episode(s) stand out by more than {tol:g} LUFS:")
            for name, d in outliers[:8]:
                self.log(f"     {d:+.1f} LUFS   {name}")
            if len(outliers) > 8:
                self.log(f"     ...and {len(outliers) - 8} more")
            self.log("  Tip: 'Normalize folder' to a LUFS target evens them all out.")
        return summary

    # ---- batch normalize ----
    def start_batch(self):
        params = self._batch_params()
        if params is not None:
            self._launch_batch(params)

    def queue_batch(self):
        """Add to queue: validate and freeze the ticked files + settings now;
        the queue starts the normalize when nothing else is running."""
        params = self._batch_params()
        if params is None:
            return
        files, outdir = params[0], params[1]
        dest = os.path.basename(os.path.normpath(outdir)) or outdir
        name = ntr("Audio Gain normalize - {n} file -> {dest}",
                   "Audio Gain normalize - {n} files -> {dest}", len(files), dest=dest)
        jobs.enqueue(name, lambda: self._launch_batch(params))
        self.log(f"[QUEUE] added: Audio Gain normalize - {len(files)} file(s) -> {dest}")

    def _launch_batch(self, params):
        """Start a validated normalize (Tk thread). Returns the job id, or None
        when the tab is busy (the queue then drops the entry)."""
        if self.stop_btn.instate(["!disabled"]):
            self.log("[QUEUE] Audio Gain is busy - normalize not started.")
            return None
        files, outdir, spec, tag, audio_out, tp, opts = params
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            self.log("[BATCH] none of the files exist any more - nothing to do.")
            return None
        self.stop_event.clear()
        self._brunning(True)
        self._jid = jobs.begin(tr("Audio Gain normalize folder"), stop_event=self.stop_event,
                               tab=self)
        threading.Thread(target=self._batch_worker,
                         args=(files, outdir, spec, tag, audio_out, tp, opts),
                         daemon=True).start()
        return self._jid

    def _batch_params(self):
        """Validate the batch settings (errors shown in a dialog). Returns
        (files, outdir, spec, tag, audio_out, tp, opts) or None."""
        indir = self.bin_var.get().strip()
        outdir = self.bout_var.get().strip()
        if not indir or not os.path.isdir(indir):
            messagebox.showerror(tr("Error"), tr("Pick a valid input folder."))
            return None
        if not outdir:
            messagebox.showerror(tr("Error"), tr("Pick an output folder."))
            return None
        try:
            spec, tag = self._spec_and_tag(self.bmatch_var.get(), self.bgain_var.get(),
                                           self.bpeak_var.get(), self.bnorm_var.get())
            tp = self._tp_value(strict=spec[0] == "lufs")
        except ValueError as e:
            messagebox.showerror(tr("Error"), str(e))
            return None
        skip_tol = None
        if self.bskip_var.get():
            try:
                skip_tol = abs(_first_number(self.tol_var.get()))
            except ValueError:
                messagebox.showerror(tr("Error"), tr("Only fix outliers needs a numeric "
                                                     "tolerance ('Flag if off by more than')."))
                return None
        if not self._confirm_gain(spec):
            return None
        rows = self.tree.get_children()
        if not rows:
            self.refresh_list()
            rows = self.tree.get_children()
        if not rows:
            messagebox.showerror(tr("Error"), tr("No media files in the input folder."))
            return None
        files = [r for r in rows if self.tree.set(r, "sel") == "☑"]
        if not files:
            messagebox.showerror(tr("Error"), tr("No files ticked - tick at least one "
                                                 "(or click the checkmark header)."))
            return None
        audio_out = AUDIO_OUT_CHOICES[self.baout_var.get()]
        opts = {"lang": self._batch_lang(), "scope": TRACK_SCOPES.get(self.scope_var.get(), "all"),
                "skip_tol": skip_tol}
        return files, outdir, spec, tag, audio_out, tp, opts

    def _batch_worker(self, files, outdir, spec, tag, audio_out, tp, opts=None):
        summary = tr("Batch failed - see log.")
        failed = True
        jid = self._jid
        try:
            summary = self._batch_run(files, outdir, spec, tag, audio_out, tp, opts)
            failed = False
        except Exception as e:
            self.log(f"[BATCH] [FAIL] {e}")
        finally:
            jobs.end(jid, ok=not failed, summary=summary)
            self.after(0, lambda: self._brunning(False, summary))

    def _skip_check(self, f, mode, target, track, tol):
        """'Only fix outliers' for one file (worker thread). Measures the file
        first if it hasn't been analysed. Returns (reason or None, stopped)."""
        if mode == "gain":
            return _skip_reason(mode, target, None, tol), False
        m = self._meas.get((f, track))
        idx = 0 if mode == "lufs" else 1
        if m is None or m[idx] is None:
            self.log("       measuring first (not analysed yet)...")
            m = self._measure_row(f, track)
            if m is None:
                return None, True
        return _skip_reason(mode, target, m[idx], tol), False

    def _mark_skipped(self, f):
        def _a():
            if self.tree.exists(f):
                self.tree.set(f, "gain", tr("skip"))
                self.tree.item(f, tags=("skipped",))
        self.after(0, _a)

    def _batch_run(self, files, outdir, spec, tag, audio_out, tp=DEFAULT_TP, opts=None):
        opts = opts or {}
        lang, scope, skip_tol = opts.get("lang", ""), opts.get("scope", "all"), opts.get("skip_tol")
        mode, target = spec
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            pass
        n = len(files)
        ok = skipped = 0
        stopped = False
        if mode == "gain":
            self.log(f"[BATCH] {n} file(s) -> apply {target:+g} dB gain to each")
        else:
            unit = f"LUFS (true-peak ceiling {tp} dBTP)" if mode == "lufs" else "dB peak"
            self.log(f"[BATCH] {n} file(s) -> match each to {target} {unit}")
        self.log("        audio: " + ("only the " + (f"'{lang}'" if lang else "first")
                                      + " track (others copied)" if scope == "sel"
                                      else "every track, each measured on its own")
                 + (f"; skipping files within ±{skip_tol:g} of the target"
                    if skip_tol is not None else ""))
        for i, f in enumerate(files, 1):
            if self.stop_event.is_set():
                stopped = True
                self.log("[BATCH] stopped.")
                break
            name = os.path.basename(f)
            self.status(f"[{i}/{n}] {name}")
            self.progress((i - 1) / n, f"{i - 1}/{n}")
            track, found = self._row_track(f, lang, probe=True)
            if skip_tol is not None:
                reason, st = self._skip_check(f, mode, target, track, skip_tol)
                if st or self.stop_event.is_set():
                    stopped = True
                    self.log("[BATCH] stopped.")
                    break
                if reason:
                    skipped += 1
                    self.log(f"  [{i}/{n}] {name}  [SKIP] {reason}")
                    self._mark_skipped(f)
                    self.progress(i / n, f"{i}/{n}")
                    continue
            out, keep, label = self._plan_output(f, outdir, "", tag, audio_out)
            if out is None:
                self.log(f"  [{i}/{n}] {name}  [FAIL] output would overwrite the input - skipped")
                continue
            tracks = _job_tracks(scope, keep, track)
            before = _mtime(out)
            self.log(f"  [{i}/{n}] {name}  ({label})  ->  {os.path.basename(out)}")
            if lang and not found:
                self.log(f"       no '{lang}' track - using track #1")
            if tracks is not None:
                self.log(f"       changing audio track #{track + 1} only")
            # per-file progress folded into the overall [i/n] bar
            base_frac, span = (i - 1) / n, 1.0 / n

            def _pcb(frac, _text="", _b=base_frac, _s=span, _i=i, _n=n):
                self.progress(_b + max(0.0, min(1.0, frac)) * _s, f"{_i}/{_n}")

            cb = dict(stop_event=self.stop_event, on_progress=_pcb, on_log=self.log,
                      tracks=tracks)
            if mode == "lufs":
                rc, err = normalize_loudness(f, out, target, keep, tp=tp, audio_out=audio_out, **cb)
            elif mode == "peak":
                rc, err = peak_normalize(f, out, target, keep, audio_out=audio_out, **cb)
            else:
                rc, err = change_gain(f, out, f"volume={target}dB", keep, audio_out=audio_out, **cb)
            if self.stop_event.is_set() or rc == -1:
                stopped = True
                self.log("       [STOPPED]")
                _discard_partial(out, before, self.log)
                break
            if rc == 0:
                ok += 1
                self.log("       [OK]")
            else:
                for ln in err.strip().splitlines()[-3:]:
                    self.log(f"         | {ln}")
                self.log("       [FAIL]")
                _discard_partial(out, before, self.log)
            self.progress(i / n, f"{i}/{n}")
        sk = f", {skipped} skipped (already on target)" if skipped else ""
        self.log(f"[BATCH] finished: {ok}/{n} ok{sk}  ->  {outdir}")
        return ((tr("Stopped - {ok}/{n} done", ok=ok, n=n) if stopped
                 else tr("Done - {ok}/{n} OK", ok=ok, n=n))
                + (ntr(", {n} skipped", ", {n} skipped", skipped) if skipped else "")
                + f"  ->  {outdir}")

    def _brunning(self, on, done_text=None):
        """Enable/disable the job buttons. When a job ends the status line shows
        its summary (done_text) instead of a bare 'Idle'."""
        for b in (self.batch_btn, self.go_btn, self.refresh_btn, self.analyze_all_btn):
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set(tr("Idle") if done_text is None else done_text)
