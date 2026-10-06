"""Two extra tools:

  ThemeAudioTab - cut the intro's AUDIO to a theme file (theme.mp3) for Plex.
  AudioGainTab  - analyse loudness and adjust the gain / normalise a file.
"""
import os
import re as _re
import glob
import statistics
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import OUTPUT_DIR, AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT, AUDIO_OUT_CHOICES
from .media import (fmt_time, probe_volume, has_video_stream,
                    export_audio_clip, change_gain, normalize_loudness, measure_lufs, peak_normalize)
from .player import VideoPlayer, MARKER_COLORS
from .widgets import (TimeEntry, add_tooltip, enable_file_drop, enable_file_drop_deep,
                     ScrollFrame, build_log_tab)
from . import applog

_MEDIA_TYPES = [("Video / audio", "*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.aac *.flac *.wav *.ogg *.opus"),
                ("All files", "*.*")]


def _first_number(s):
    """Pull the first number out of a string like '-16 LUFS (streaming)' or '3'."""
    m = _re.search(r"-?\d+(?:\.\d+)?", str(s))
    if not m:
        raise ValueError("no number found")
    return float(m.group(0))


def _same_file(a, b):
    """True if paths a and b point at the same file (case-insensitive on
    Windows, symlinks resolved) - used so an output never overwrites its input."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


def _mtime(path):
    """mtime_ns of path, or None if it doesn't exist."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _discard_partial(out, before, log):
    """Remove a stopped/failed output so a truncated file isn't left under the
    final name. *before* is _mtime(out) taken before the job: a file that was
    already there and was never touched (stopped before writing) is kept."""
    if before is not None and _mtime(out) == before:
        return
    try:
        if os.path.exists(out):
            os.remove(out)
            log(f"  removed incomplete output: {os.path.basename(out)}")
    except OSError as e:
        log(f"  could not remove incomplete output {out}: {e}")


# ======================= Theme Audio (intro -> theme.mp3) =======================
class ThemeAudioTab(ttk.Frame):
    FORMATS = {
        "MP3  (theme.mp3 for Plex)": ("libmp3lame", ["-q:a", "2"], ".mp3"),
        "M4A / AAC": ("aac", ["-b:a", "256k"], ".m4a"),
        "FLAC (lossless)": ("flac", [], ".flac"),
        "WAV": ("pcm_s16le", [], ".wav"),
    }

    def __init__(self, master, saved=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        # sub-tab in its own ScrollFrame (like the other tools) so short content
        # fills the window seamlessly and scrolls if it's ever too tall
        main_outer = ttk.Frame(nb)
        nb.add(main_outer, text="  Theme audio  ")
        main_sc = ScrollFrame(main_outer)
        main_sc.pack(fill="both", expand=True)
        main = main_sc.interior
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(top, text="Video / audio file:").pack(side="left", padx=(0, 6))
        self.file_var = tk.StringVar(value=saved.get("last_video", ""))
        ent = ttk.Entry(top, textvariable=self.file_var)
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left", padx=6)
        ttk.Button(top, text="Load", command=self.load_from_entry).pack(side="left")

        body = ttk.Frame(main)
        body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # right: options
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        ttk.Label(right, text="Intro from:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        frow = ttk.Frame(right)
        frow.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        self.f_from = TimeEntry(frow)
        self.f_from.pack(side="left")
        bff = ttk.Button(frow, text="Set", width=5, command=lambda: self._mark(self.f_from))
        bff.pack(side="left", padx=(4, 0))
        add_tooltip(bff, "Set the intro START to the frame shown in the player")
        gff = ttk.Button(frow, text="Go", width=4, command=lambda: self._goto(self.f_from))
        gff.pack(side="left", padx=(4, 0))
        add_tooltip(gff, "Jump the player to the time typed in this box")

        ttk.Label(right, text="Intro to:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        trow = ttk.Frame(right)
        trow.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        self.f_to = TimeEntry(trow)
        self.f_to.pack(side="left")
        btf = ttk.Button(trow, text="Set", width=5, command=lambda: self._mark(self.f_to))
        btf.pack(side="left", padx=(4, 0))
        add_tooltip(btf, "Set the intro END to the frame shown in the player")
        gtf = ttk.Button(trow, text="Go", width=4, command=lambda: self._goto(self.f_to))
        gtf.pack(side="left", padx=(4, 0))
        add_tooltip(gtf, "Jump the player to the time typed in this box")

        for v in self.f_from.vars + self.f_to.vars:
            v.trace_add("write", lambda *a: self._refresh_markers())

        ttk.Label(right, text="Format:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        _fmt = saved.get("theme_fmt")
        self.fmt_var = tk.StringVar(value=_fmt if _fmt in self.FORMATS else list(self.FORMATS)[0])
        ttk.Combobox(right, textvariable=self.fmt_var, values=list(self.FORMATS),
                     state="readonly", width=26).grid(row=2, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(right, text="Output folder:").grid(row=3, column=0, sticky="e", padx=4, pady=3)
        of = ttk.Frame(right)
        of.grid(row=3, column=1, sticky="we", padx=4, pady=3)
        of.columnconfigure(0, weight=1)
        self.outdir_var = tk.StringVar(value=saved.get("theme_out", OUTPUT_DIR))
        ttk.Entry(of, textvariable=self.outdir_var).grid(row=0, column=0, sticky="we")
        ttk.Button(of, text="...", width=3,
                   command=self._pick_out).grid(row=0, column=1, padx=(4, 0))

        ttk.Label(right, text="File name:").grid(row=4, column=0, sticky="e", padx=4, pady=3)
        self.name_var = tk.StringVar(value=saved.get("theme_name", "theme"))
        ttk.Entry(right, textvariable=self.name_var, width=20).grid(row=4, column=1, sticky="w", padx=4, pady=3)

        fr = ttk.Frame(right)
        fr.grid(row=5, column=0, columnspan=2, sticky="w", padx=4, pady=3)
        ttk.Label(fr, text="Fade in (s):").pack(side="left")
        self.fin_var = tk.StringVar(value=saved.get("theme_fin", "0"))
        ttk.Entry(fr, textvariable=self.fin_var, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(fr, text="Fade out (s):").pack(side="left")
        self.fout_var = tk.StringVar(value=saved.get("theme_fout", "0"))
        ttk.Entry(fr, textvariable=self.fout_var, width=5).pack(side="left", padx=4)

        self.go_btn = ttk.Button(right, text="Export theme audio", command=self.start_export)
        self.go_btn.grid(row=6, column=0, columnspan=2, sticky="we", padx=4, pady=(8, 2))
        add_tooltip(self.go_btn, "Cut the intro's audio to the chosen file")
        ttk.Label(right, text="Tip: for Plex, name it 'theme', pick MP3, and put the file in the "
                             "show's folder (Plex plays theme.mp3 on the series page).",
                  style="Hint.TLabel", wraplength=320, justify="left").grid(
                      row=7, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 0))

        # left: player (Set / Go buttons live next to the time boxes on the right,
        # like the other tools)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        self.player = VideoPlayer(left, width=480, height=270, log_fn=self.log)
        self.player.pack()
        self.player.enable_tab_shortcuts()   # arrows/space work anywhere on the tab

        self.status_var = tk.StringVar(value="")
        ttk.Label(main, textvariable=self.status_var, style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))
        prog = ttk.Frame(main)
        prog.grid(row=3, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.stop_btn = ttk.Button(prog, text="Stop", command=self.stop, state="disabled", width=8)
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        add_tooltip(self.stop_btn, "Stop the export")
        self.logbox = build_log_tab(nb)

        enable_file_drop_deep(self.player, self._drop_load)       # drop anywhere in the player
        enable_file_drop(ent, self._drop_load)

    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        s, _ = self.f_from.get_seconds()
        e, _ = self.f_to.get_seconds()
        dur = self.player.timeline.duration
        end = e if e is not None else dur
        marks = [(s, end, MARKER_COLORS["intro"])] if (s is not None and end and end > s) else []
        self.player.set_markers(marks)

    def _mark(self, entry):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo("No video", "Load a file first, then scrub to the point.")
            return
        entry.set_seconds(sec)
        entry.flash()
        self._refresh_markers()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _goto(self, entry):
        if not self.player.has_video():
            messagebox.showinfo("No video", "Load a file first.")
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set("Type a valid time in the box first, then Go.")
            return
        self.player.seek_seconds(sec)
        entry.flash()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _pick_out(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.outdir_var.set(d)

    def _drop_load(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def browse(self):
        path = filedialog.askopenfilename(title="Select file", filetypes=_MEDIA_TYPES)
        if path:
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def load_from_entry(self):
        path = self.file_var.get().strip().strip('"')
        if path and os.path.isfile(path):
            self.player.load(path)
            self._refresh_markers()
        else:
            messagebox.showerror("Error", "Type or browse to a valid file first.")

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def start_export(self):
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror("Error", "Please select a valid file.")
            return
        s, s_ok = self.f_from.get_seconds()
        e, e_ok = self.f_to.get_seconds()
        if not s_ok or not e_ok or s is None or e is None or e <= s:
            messagebox.showerror("Error", "Set a valid Intro From and To (To after From).")
            return
        try:
            fin = float(self.fin_var.get() or 0)
            fout = float(self.fout_var.get() or 0)
            assert fin >= 0 and fout >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("Error", "Fade in/out must be 0 or a positive number.")
            return
        name = (self.name_var.get().strip() or "theme")
        codec, extra, ext = self.FORMATS[self.fmt_var.get()]
        outdir = self.outdir_var.get().strip() or OUTPUT_DIR
        out = os.path.join(outdir, name + ext)
        if _same_file(out, video):
            messagebox.showerror("Error", "The output would overwrite the input file.\n"
                                 "Change the name or the output folder.")
            return
        self.stop_event.clear()
        self._set_running(True)
        self.status_var.set("Exporting...")
        threading.Thread(target=self._worker,
                         args=(video, out, s, e, codec, extra, fin, fout), daemon=True).start()

    def _set_running(self, on):
        self.go_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")

    def stop(self):
        self.stop_event.set()
        self.log("Stopping export...")

    def snapshot(self):
        """Theme-export settings to persist between sessions."""
        return {
            "theme_out": self.outdir_var.get().strip(),
            "theme_fmt": self.fmt_var.get(),
            "theme_name": self.name_var.get(),
            "theme_fin": self.fin_var.get(),
            "theme_fout": self.fout_var.get(),
        }

    def _progress(self, frac, _text=""):
        v = int(max(0.0, min(1.0, frac)) * 1000)
        self.after(0, lambda: self.bar.configure(value=v))

    def _worker(self, video, out, s, e, codec, extra, fin, fout):
        done = "Failed - see log."
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            self.log(f"[THEME] {fmt_time(s)} -> {fmt_time(e)}  ->  {out}")
            before = _mtime(out)
            rc, _err = export_audio_clip(video, out, s, e, codec, extra, fin, fout,
                                         stop_event=self.stop_event,
                                         on_progress=self._progress, on_log=self.log)
            if self.stop_event.is_set() or rc == -1:
                self.log("  [STOPPED]")
                done = "Stopped."
                _discard_partial(out, before, self.log)
            elif rc == 0:
                self.log("  [OK] saved")
                done = "Done."
            else:
                self.log("  [FAIL] export failed")
                _discard_partial(out, before, self.log)
        except Exception as ex:
            self.log(f"  [FAIL] {ex}")
        finally:
            def _f():
                self.status_var.set(done)
                self._set_running(False)
                self.bar.configure(value=0)
            self.after(0, _f)


# ======================= Audio Gain (volume / normalise) =======================
class AudioGainTab(ttk.Frame):
    LUFS_PRESETS = [
        "-12 LUFS  (very loud)",
        "-13 LUFS",
        "-14 LUFS  (loud - Spotify / YouTube)",
        "-15 LUFS",
        "-16 LUFS  (streaming standard)",
        "-17 LUFS",
        "-18 LUFS  (moderate - a bit quieter)",
        "-19 LUFS",
        "-20 LUFS  (quiet)",
        "-21 LUFS",
        "-22 LUFS",
        "-23 LUFS  (broadcast / TV standard)",
        "-24 LUFS",
        "-25 LUFS",
        "-26 LUFS  (quite quiet)",
        "-27 LUFS",
        "-28 LUFS  (very quiet)",
    ]
    DEFAULT_LUFS = "-16 LUFS  (streaming standard)"   # stable default (list order may change)
    _MEDIA_GLOBS = ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.webm",
                    "*.mp3", "*.m4a", "*.aac", "*.flac", "*.wav", "*.ogg", "*.opus")

    def __init__(self, master, saved=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self._meas = {}   # path -> (lufs, peak_db) from Analyze all
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        single = ttk.Frame(nb, padding=8)
        batch = ttk.Frame(nb, padding=8)
        nb.add(single, text="  Single file  ")
        nb.add(batch, text="  Batch (season)  ")

        # ===================== Single file =====================
        single.columnconfigure(1, weight=1)
        ttk.Label(single, text="File:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.file_var = tk.StringVar()
        sent = ttk.Entry(single, textvariable=self.file_var)
        sent.grid(row=0, column=1, sticky="we", padx=4, pady=4)
        ttk.Button(single, text="Browse...", command=self.browse).grid(row=0, column=2, padx=4)

        anrow = ttk.Frame(single)
        anrow.grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 6))
        ttk.Button(anrow, text="Analyze loudness", command=self.analyze).pack(side="left")
        self.analysis_var = tk.StringVar(value="Load a file and click Analyze to see its levels.")
        ttk.Label(anrow, textvariable=self.analysis_var, style="Hint.TLabel").pack(side="left", padx=(10, 0))

        mode = ttk.LabelFrame(single, text=" Adjustment ", padding=(8, 6))
        mode.grid(row=2, column=0, columnspan=3, sticky="we", padx=4, pady=4)
        self.mode_var = tk.StringVar(value="norm")
        ttk.Radiobutton(mode, text="Change gain by", variable=self.mode_var,
                        value="gain").grid(row=0, column=0, sticky="w", pady=2)
        self.gain_var = tk.StringVar(value="0")
        ttk.Spinbox(mode, from_=-40, to=40, increment=0.5, textvariable=self.gain_var,
                    width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(mode, text="dB   (+ louder, - quieter)").grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(mode, text="Normalize to", variable=self.mode_var,
                        value="norm").grid(row=1, column=0, sticky="w", pady=2)
        self.norm_var = tk.StringVar(value=saved.get("ag_single_lufs", self.DEFAULT_LUFS))
        ttk.Combobox(mode, textvariable=self.norm_var, values=self.LUFS_PRESETS,
                     width=30).grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Radiobutton(mode, text="Set peak to", variable=self.mode_var,
                        value="peak").grid(row=2, column=0, sticky="w", pady=2)
        self.speak_var = tk.StringVar(value="-1")
        ttk.Combobox(mode, textvariable=self.speak_var, values=["-0.5", "-1", "-2", "-3", "-6"],
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(mode, text="dB peak").grid(row=2, column=2, sticky="w")
        ttk.Label(mode, text="Output audio:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.aout_var = tk.StringVar(value=list(AUDIO_OUT_CHOICES)[0])
        ttk.Combobox(mode, textvariable=self.aout_var, values=list(AUDIO_OUT_CHOICES),
                     state="readonly", width=30).grid(row=3, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))

        outrow = ttk.Frame(single)
        outrow.grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=4)
        ttk.Label(outrow, text="Extra name tag (optional):").pack(side="left")
        self.suffix_var = tk.StringVar(value="")
        ttk.Entry(outrow, textvariable=self.suffix_var, width=12).pack(side="left", padx=(4, 0))
        ttk.Label(outrow, text="(the target is auto-added to the name; saved next to the input)",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))

        self.go_btn = ttk.Button(single, text="Apply to this file", command=self.start_apply)
        self.go_btn.grid(row=4, column=0, columnspan=3, sticky="we", padx=4, pady=(6, 4))
        add_tooltip(self.go_btn, "Write a new file with the volume adjusted / normalized")
        enable_file_drop(sent, self._drop_load)

        # ===================== Batch (season): 2 columns =====================
        batch.columnconfigure(1, weight=1)
        batch.rowconfigure(0, weight=1)
        bleft_sc = ScrollFrame(batch, canvas_width=300)
        bleft_sc.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        bleft = bleft_sc.interior
        bleft.columnconfigure(0, weight=1)
        bright = ttk.Frame(batch)
        bright.grid(row=0, column=1, sticky="nsew")
        bright.columnconfigure(0, weight=1)
        bright.rowconfigure(1, weight=1)

        ttk.Label(bleft, text="Input folder:").grid(row=0, column=0, sticky="w", pady=(0, 2))
        ir = ttk.Frame(bleft)
        ir.grid(row=1, column=0, sticky="we", pady=(0, 6))
        ir.columnconfigure(0, weight=1)
        self.bin_var = tk.StringVar(value=saved.get("ag_in", AUDIOGAIN_INPUT))
        bin_ent = ttk.Entry(ir, textvariable=self.bin_var, width=28)
        bin_ent.grid(row=0, column=0, sticky="we")
        enable_file_drop(bin_ent, self._drop_bin)
        ttk.Button(ir, text="...", width=3, command=lambda: self._pick(self.bin_var)).grid(row=0, column=1, padx=(4, 0))

        ttk.Label(bleft, text="Output folder:").grid(row=2, column=0, sticky="w", pady=(0, 2))
        orf = ttk.Frame(bleft)
        orf.grid(row=3, column=0, sticky="we", pady=(0, 6))
        orf.columnconfigure(0, weight=1)
        self.bout_var = tk.StringVar(value=saved.get("ag_out", AUDIOGAIN_OUTPUT))
        bout_ent = ttk.Entry(orf, textvariable=self.bout_var, width=28)
        bout_ent.grid(row=0, column=0, sticky="we")
        enable_file_drop(bout_ent, self._drop_bout)
        ttk.Button(orf, text="...", width=3, command=lambda: self._pick(self.bout_var)).grid(row=0, column=1, padx=(4, 0))

        ttk.Label(bleft, text="Match every file by:").grid(row=4, column=0, sticky="w")
        mf = ttk.Frame(bleft)
        mf.grid(row=5, column=0, sticky="we", pady=(2, 10))
        mf.columnconfigure(1, weight=1)
        self.bmatch_var = tk.StringVar(value=saved.get("ag_match", "lufs"))
        rl = ttk.Radiobutton(mf, text="Loudness (LUFS)", variable=self.bmatch_var, value="lufs")
        rl.grid(row=0, column=0, sticky="w")
        add_tooltip(rl, "Best for matching a season: brings every file to the same perceived "
                    "loudness. After Analyze all, the Gain column shows the exact +/- dB each "
                    "file will get - so you can see how much each one moves.")
        self.bnorm_var = tk.StringVar(value=saved.get("ag_lufs", self.DEFAULT_LUFS))
        ttk.Combobox(mf, textvariable=self.bnorm_var, values=self.LUFS_PRESETS,
                     width=18).grid(row=0, column=1, sticky="we", padx=(6, 0))
        rp = ttk.Radiobutton(mf, text="Peak (dB)", variable=self.bmatch_var, value="peak")
        rp.grid(row=1, column=0, sticky="w", pady=(4, 0))
        add_tooltip(rp, "Sets each file's loudest sample to the same peak. The Gain column shows "
                    "the +/- dB each file gets. (Loudness/LUFS usually matches better by ear.)")
        self.bpeak_var = tk.StringVar(value=saved.get("ag_peak", "-1"))
        ttk.Combobox(mf, textvariable=self.bpeak_var, values=["-0.5", "-1", "-2", "-3", "-6"],
                     state="readonly", width=8).grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(4, 0))
        rg = ttk.Radiobutton(mf, text="Gain by (dB)", variable=self.bmatch_var, value="gain")
        rg.grid(row=2, column=0, sticky="w", pady=(4, 0))
        add_tooltip(rg, "Applies the SAME +/- dB to every file (a manual flat shift, no matching). "
                    "To match by hand, read the 'Δ median' column after Analyze all and set this "
                    "to the opposite (e.g. a file at +3.0 needs -3.0). Only works if every file "
                    "needs the same shift; otherwise use Loudness (LUFS).")
        self.bgain_var = tk.StringVar(value=saved.get("ag_gain", "0"))
        ttk.Spinbox(mf, from_=-40, to=40, increment=0.5, textvariable=self.bgain_var,
                    width=8).grid(row=2, column=1, sticky="we", padx=(6, 0), pady=(4, 0))

        ttk.Label(bleft, text="Output audio:").grid(row=6, column=0, sticky="w", pady=(0, 2))
        _aout = saved.get("ag_aout")
        self.baout_var = tk.StringVar(value=_aout if _aout in AUDIO_OUT_CHOICES else list(AUDIO_OUT_CHOICES)[0])
        ttk.Combobox(bleft, textvariable=self.baout_var, values=list(AUDIO_OUT_CHOICES),
                     state="readonly").grid(row=7, column=0, sticky="we", pady=(0, 8))
        self.refresh_btn = ttk.Button(bleft, text="Refresh file list", command=self.refresh_list)
        self.refresh_btn.grid(row=8, column=0, sticky="we", pady=2)
        add_tooltip(self.refresh_btn, "List the media files in the input folder")
        self.analyze_all_btn = ttk.Button(bleft, text="Analyze all (loudness)", command=self.analyze_all)
        self.analyze_all_btn.grid(row=9, column=0, sticky="we", pady=2)
        add_tooltip(self.analyze_all_btn, "Measure each file's loudness, then flag any episode that "
                    "stands out from the season so you can see if they already match")
        tolf = ttk.Frame(bleft)
        tolf.grid(row=10, column=0, sticky="we", pady=(2, 0))
        ttk.Label(tolf, text="Flag if off by more than").pack(side="left")
        self.tol_var = tk.StringVar(value=saved.get("ag_tol", "1.0"))
        ttk.Spinbox(tolf, from_=0.2, to=6.0, increment=0.5, textvariable=self.tol_var,
                    width=5).pack(side="left", padx=(4, 4))
        ttk.Label(tolf, text="LUFS").pack(side="left")
        self.batch_btn = ttk.Button(bleft, text="Normalize folder", command=self.start_batch)
        self.batch_btn.grid(row=11, column=0, sticky="we", pady=(10, 2))
        add_tooltip(self.batch_btn, "Two-pass normalize every file to the target so the season matches")

        hdr = ttk.Frame(bright)
        hdr.grid(row=0, column=0, sticky="we", pady=(0, 2))
        hdr.columnconfigure(0, weight=1)
        ttk.Label(hdr, text="Files in the input folder - tick the ones to include "
                          "(loudness before normalizing; click the checkmark header for all):",
                  style="Hint.TLabel", wraplength=560, justify="left").grid(row=0, column=0, sticky="w")
        rm_btn = ttk.Button(hdr, text="Remove selected", command=self._remove_selected)
        rm_btn.grid(row=0, column=1, sticky="e", padx=(6, 0))
        add_tooltip(rm_btn, "Remove the highlighted rows from the list (or press Delete) so they "
                    "aren't processed. 'Refresh file list' re-adds everything from the folder.")
        tv = ttk.Frame(bright)
        tv.grid(row=1, column=0, sticky="nsew")
        tv.rowconfigure(0, weight=1)
        tv.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tv, columns=("sel", "file", "lufs", "peak", "mean", "dmed", "gain"),
                                 show="headings", height=12)
        self.tree.heading("sel", text="\u2713", command=self._toggle_all)
        self.tree.heading("file", text="File")
        self.tree.heading("lufs", text="LUFS")
        self.tree.heading("peak", text="Peak dB")
        self.tree.heading("mean", text="Mean dB")
        self.tree.heading("dmed", text="\u0394 median")
        self.tree.heading("gain", text="Gain (dB)")
        self.tree.column("sel", width=30, anchor="center", stretch=False)
        self.tree.column("file", width=200, anchor="w")
        for col in ("lufs", "peak", "mean", "dmed", "gain"):
            self.tree.column(col, width=68, anchor="center", stretch=False)
        # episodes that stand out from the season are highlighted amber
        self.tree.tag_configure("outlier", foreground="#e0902e")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        for v in (self.bmatch_var, self.bnorm_var, self.bpeak_var, self.bgain_var):
            v.trace_add("write", lambda *a: self._update_gain_col())
        self.tol_var.trace_add("write", lambda *a: self._flag_outliers(log=False))
        enable_file_drop(self.tree, self._drop_bin)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        ttk.Label(bright, text="Tip: after normalizing, set Input to the output folder and Analyze all "
                             "again to confirm they now match.", style="Hint.TLabel",
                  wraplength=360, justify="left").grid(row=2, column=0, sticky="w", pady=(4, 0))

        # ===================== shared status + progress + log =====================
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        prog = ttk.Frame(self)
        prog.grid(row=2, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=12).grid(row=0, column=1, sticky="w", padx=(6, 0))
        # single Stop in the right corner beside the progress bar, like the other
        # tools - drives whichever job (single or batch) is running
        self.stop_btn = ttk.Button(prog, text="Stop", command=self.stop, state="disabled", width=8)
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, "Stop now - the file being processed is abandoned "
                                   "(its incomplete output is removed)")
        self.logbox = build_log_tab(nb)

        self.refresh_list()

    # ---- small helpers ----
    def _pick(self, var):
        d = filedialog.askdirectory(title="Select folder")
        if d:
            var.set(d)
            if var is self.bin_var:
                self.refresh_list()

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

    def _drop_load(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)

    def browse(self):
        path = filedialog.askopenfilename(title="Select file", filetypes=_MEDIA_TYPES)
        if path:
            self.file_var.set(path)

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
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
    def analyze(self):
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror("Error", "Select a valid file first.")
            return
        self.analysis_var.set("Analyzing...")
        threading.Thread(target=self._analyze_worker, args=(f,), daemon=True).start()

    def _analyze_worker(self, f):
        txt = "Analysis failed - see log."
        try:
            mean, mx = probe_volume(f)
            lufs = measure_lufs(f)
            parts = []
            if lufs is not None:
                parts.append(f"{lufs:.1f} LUFS")
            if mean is not None:
                parts.append(f"mean {mean:.1f} dB")
            if mx is not None:
                parts.append(f"max {mx:.1f} dB")
            txt = ", ".join(parts) if parts else "Could not read levels (no audio?)."
        except Exception as e:
            self.log(f"[FAIL] analyze {os.path.basename(f)}: {e}")
        finally:
            self.after(0, lambda: self.analysis_var.set(txt))

    # ---- single-file apply ----
    @staticmethod
    def _spec_and_tag(mode, gain_s, peak_s, lufs_s):
        """Return ((mode, target), filename_tag). Raises ValueError on bad input."""
        try:
            if mode == "gain":
                g = _first_number(gain_s)
                return ("gain", g), f"{g:+g}dB"
            if mode == "peak":
                pk = _first_number(peak_s)
                return ("peak", pk), f"{pk:g}dBpeak"
            lu = _first_number(lufs_s)
            return ("lufs", f"{lu:g}"), ""   # LUFS mode adds no tag to the name
        except ValueError:
            raise ValueError("Enter a numeric value (dB / LUFS) for the selected mode.")

    def start_apply(self):
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror("Error", "Select a valid file first.")
            return
        try:
            spec, tag = self._spec_and_tag(self.mode_var.get(), self.gain_var.get(),
                                           self.speak_var.get(), self.norm_var.get())
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        base, ext = os.path.splitext(f)
        extra = self.suffix_var.get().strip()
        out = base + extra + (("_" + tag) if tag else "") + ext
        if _same_file(out, f):
            out = base + extra + "_norm" + ext
        if _same_file(out, f):
            messagebox.showerror("Error", "The output would overwrite the input file.")
            return
        self.stop_event.clear()
        self._brunning(True)          # enables the Stop button
        self.status_var.set("Processing...")
        audio_out = AUDIO_OUT_CHOICES[self.aout_var.get()]
        threading.Thread(target=self._one_worker, args=(f, out, spec, audio_out), daemon=True).start()

    def _one_worker(self, f, out, spec, audio_out):
        msg = "Failed - see log."
        try:
            mode, target = spec
            keep = has_video_stream(f)
            tag = "video kept" if keep else "audio only"
            cb = dict(stop_event=self.stop_event, on_progress=self.progress, on_log=self.log)
            before = _mtime(out)
            if mode == "lufs":
                self.log(f"[NORM] {os.path.basename(f)} -> {target} LUFS ({tag})")
                rc, err = normalize_loudness(f, out, target, keep, audio_out=audio_out, **cb)
            elif mode == "peak":
                self.log(f"[PEAK] {os.path.basename(f)} -> {target} dB peak ({tag})")
                rc, err = peak_normalize(f, out, target, keep, audio_out=audio_out, **cb)
            else:
                self.log(f"[GAIN] {os.path.basename(f)} volume={target}dB ({tag})")
                rc, err = change_gain(f, out, f"volume={target}dB", keep, audio_out=audio_out, **cb)
            if self.stop_event.is_set() or rc == -1:
                self.log("  [STOPPED]")
                msg = "Stopped."
                _discard_partial(out, before, self.log)
            elif rc == 0:
                self.log(f"  [OK] {out}")
                msg = "Done."
            else:
                for ln in err.strip().splitlines()[-4:]:
                    self.log(f"    | {ln}")
                self.log("  [FAIL]")
                _discard_partial(out, before, self.log)
        except Exception as e:
            self.log(f"  [FAIL] {e}")
        finally:
            def _f():
                self._brunning(False)
                self.status_var.set(msg)
                self.bar.configure(value=0)
            self.after(0, _f)

    # ---- batch file list + analyze all ----
    def _list_files(self):
        indir = self.bin_var.get().strip()
        if not indir or not os.path.isdir(indir):
            return []
        files = []
        for pat in self._MEDIA_GLOBS:
            files += glob.glob(os.path.join(indir, pat))
        return sorted(set(files))

    def refresh_list(self):
        if self.stop_btn.instate(["!disabled"]):   # a job is running - don't wipe its rows
            self.status_var.set("Busy - Stop the current job before refreshing the list.")
            return
        self.tree.delete(*self.tree.get_children())
        self._meas = {}
        for f in self._list_files():
            self.tree.insert("", "end", iid=f,
                             values=("\u2611", os.path.basename(f), "-", "-", "-", "-", "-"))

    def _on_tree_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) != "#1":
            return
        row = self.tree.identify_row(e.y)
        if row:
            cur = self.tree.set(row, "sel")
            self.tree.set(row, "sel", "\u2610" if cur == "\u2611" else "\u2611")

    def _toggle_all(self):
        rows = self.tree.get_children()
        val = "\u2611" if any(self.tree.set(r, "sel") == "\u2610" for r in rows) else "\u2610"
        for r in rows:
            self.tree.set(r, "sel", val)

    def _remove_selected(self):
        """Drop the highlighted rows from the list so they aren't processed.
        (Refresh file list re-adds everything from the folder.)"""
        if self.stop_btn.instate(["!disabled"]):
            self.status_var.set("Busy - Stop the current job before removing rows.")
            return
        sel = self.tree.selection()
        if not sel:
            self.status_var.set("Select one or more rows first (Ctrl/Shift-click for several).")
            return
        for iid in sel:
            self._meas.pop(iid, None)
            self.tree.delete(iid)

    def _update_gain_col(self):
        """Show the dB gain each analyzed file would get for the current mode/target."""
        if not hasattr(self, "tree"):
            return
        mode = self.bmatch_var.get()
        for r in self.tree.get_children():
            lufs, peak = self._meas.get(r, (None, None))
            g = None
            try:
                if mode == "gain":
                    g = _first_number(self.bgain_var.get())
                elif mode == "lufs" and lufs is not None:
                    g = _first_number(self.bnorm_var.get()) - lufs
                elif mode == "peak" and peak is not None:
                    g = _first_number(self.bpeak_var.get()) - peak
            except ValueError:
                g = None
            self.tree.set(r, "gain", f"{g:+.1f}" if g is not None else "-")

    def analyze_all(self):
        self.refresh_list()
        files = list(self.tree.get_children())
        if not files:
            self.log("No media files found in the input folder.")
            return
        self.stop_event.clear()
        self._brunning(True)
        threading.Thread(target=self._analyze_all_worker, args=(files,), daemon=True).start()

    def _analyze_all_worker(self, files):
        try:
            n = len(files)
            for i, path in enumerate(files, 1):
                if self.stop_event.is_set():
                    self.log("Analysis stopped.")
                    break
                self.status(f"Analyzing {i}/{n}: {os.path.basename(path)}")
                self.progress((i - 1) / n, f"{i - 1}/{n}")
                lufs = measure_lufs(path)
                mean, mx = probe_volume(path)
                self._meas[path] = (lufs, mx)
                vl = f"{lufs:.1f}" if lufs is not None else "?"
                vp = f"{mx:.1f}" if mx is not None else "?"
                vm = f"{mean:.1f}" if mean is not None else "?"

                def _set(p=path, a=vl, b=vp, c=vm):
                    if not self.tree.exists(p):   # row removed meanwhile
                        return
                    self.tree.set(p, "lufs", a)
                    self.tree.set(p, "peak", b)
                    self.tree.set(p, "mean", c)
                self.after(0, _set)
                self.progress(i / n, f"{i}/{n}")
        except Exception as e:
            self.log(f"[FAIL] analysis: {e}")
        finally:
            self.after(0, lambda: (self._brunning(False), self._update_gain_col(),
                                   self._flag_outliers(log=True)))

    def _flag_outliers(self, log=False):
        """Compare each analyzed file to the season's median loudness, fill the
        'Δ median' column, colour the ones that stand out, and (optionally) log
        a plain verdict on whether the season is consistent."""
        try:
            tol = _first_number(self.tol_var.get())
        except ValueError:
            tol = 1.0
        vals = {r: self._meas[r][0] for r in self.tree.get_children()
                if self._meas.get(r) and self._meas[r][0] is not None}
        if len(vals) < 1:
            return
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
        if not log:
            return
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

    # ---- batch normalize ----
    def start_batch(self):
        indir = self.bin_var.get().strip()
        outdir = self.bout_var.get().strip()
        if not indir or not os.path.isdir(indir):
            messagebox.showerror("Error", "Pick a valid input folder.")
            return
        if not outdir:
            messagebox.showerror("Error", "Pick an output folder.")
            return
        try:
            spec, tag = self._spec_and_tag(self.bmatch_var.get(), self.bgain_var.get(),
                                           self.bpeak_var.get(), self.bnorm_var.get())
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        rows = self.tree.get_children()
        if not rows:
            self.refresh_list()
            rows = self.tree.get_children()
        if not rows:
            messagebox.showerror("Error", "No media files in the input folder.")
            return
        files = [r for r in rows if self.tree.set(r, "sel") == "\u2611"]
        if not files:
            messagebox.showerror("Error", "No files ticked - tick at least one (or click the checkmark header).")
            return
        audio_out = AUDIO_OUT_CHOICES[self.baout_var.get()]
        self.stop_event.clear()
        self._brunning(True)
        threading.Thread(target=self._batch_worker, args=(files, outdir, spec, tag, audio_out), daemon=True).start()

    def _batch_worker(self, files, outdir, spec, tag, audio_out):
        try:
            self._batch_run(files, outdir, spec, tag, audio_out)
        except Exception as e:
            self.log(f"[BATCH] [FAIL] {e}")
        finally:
            self.after(0, lambda: self._brunning(False))

    def _batch_run(self, files, outdir, spec, tag, audio_out):
        mode, target = spec
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            pass
        n = len(files)
        ok = 0
        if mode == "gain":
            self.log(f"[BATCH] {n} file(s) -> apply {target:+g} dB gain to each")
        else:
            unit = "LUFS" if mode == "lufs" else "dB peak"
            self.log(f"[BATCH] {n} file(s) -> match each to {target} {unit}")
        for i, f in enumerate(files, 1):
            if self.stop_event.is_set():
                self.log("[BATCH] stopped.")
                break
            name = os.path.basename(f)
            self.status(f"[{i}/{n}] {name}")
            self.progress((i - 1) / n, f"{i - 1}/{n}")
            base, ext = os.path.splitext(name)
            out = os.path.join(outdir, base + (("_" + tag) if tag else "") + ext)
            if _same_file(out, f):
                out = os.path.join(outdir, base + "_norm" + ext)
            if _same_file(out, f):
                self.log(f"  [{i}/{n}] {name}  [FAIL] output would overwrite the input - skipped")
                continue
            keep = has_video_stream(f)
            before = _mtime(out)
            self.log(f"  [{i}/{n}] {name}  ({'video' if keep else 'audio'})")
            # per-file progress folded into the overall [i/n] bar
            base_frac, span = (i - 1) / n, 1.0 / n

            def _pcb(frac, _text="", _b=base_frac, _s=span, _i=i, _n=n):
                self.progress(_b + max(0.0, min(1.0, frac)) * _s, f"{_i}/{_n}")

            if mode == "lufs":
                rc, err = normalize_loudness(f, out, target, keep, audio_out=audio_out,
                                             stop_event=self.stop_event, on_progress=_pcb, on_log=self.log)
            elif mode == "peak":
                rc, err = peak_normalize(f, out, target, keep, audio_out=audio_out,
                                         stop_event=self.stop_event, on_progress=_pcb, on_log=self.log)
            else:
                rc, err = change_gain(f, out, f"volume={target}dB", keep, audio_out=audio_out,
                                      stop_event=self.stop_event, on_progress=_pcb, on_log=self.log)
            if self.stop_event.is_set() or rc == -1:
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
        self.log(f"[BATCH] finished: {ok}/{n} ok  ->  {outdir}")

    def _brunning(self, on):
        for b in (self.batch_btn, self.go_btn, self.refresh_btn, self.analyze_all_btn):
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set("Idle")

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
            "ag_single_lufs": self.norm_var.get(),
        }
