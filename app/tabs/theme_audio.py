"""Audio -> Theme Audio: cut the intro's AUDIO to a theme file (theme.mp3)
for Plex."""
import itertools
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import OUTPUT_DIR, TEMP_DIR
from ..engine.formatting import fmt_time
from ..engine.loudness import export_audio_clip
from ..engine.probe import probe_duration
from ..ui.playback import AudioPlayer
from ..i18n import N_, tr
from ..ui.player import VideoPlayer
from ..ui.widgets import (info_icon, KeyedCombobox, ScrollFrame, TimeEntry, add_tooltip,
                          build_log_tab, enable_file_drop, enable_file_drop_deep,
                          trim_text_lines)
from .audio_common import (LUFS_RANGE, _TrackPicker, _check_range, _discard_partial,
                           _first_number, _mtime, _nb_help, _same_file, media_types)
from .. import applog, jobs
from ..ui import icons


_THEME_EXTS = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus")
_BAD_NAME_CHARS = '<>:"/\\|?*'
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


THEME_NORM_DEFAULT = "-16"
THEME_TP = "-1.5"


_preview_ids = itertools.count(1)


def _remove_file(widget, path, tries=6):
    """Delete a temp file; retry a few times (Windows keeps it locked for a
    moment after the decoder reading it was killed)."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        if tries > 0:
            widget.after(300, lambda: _remove_file(widget, path, tries - 1))


# ======================= Theme Audio (intro -> theme.mp3) =======================
class ThemeAudioTab(ttk.Frame):
    FORMATS = {
        N_("MP3  (theme.mp3 for Plex)"): ("libmp3lame", ["-q:a", "2"], ".mp3"),
        "M4A / AAC": ("aac", ["-b:a", "256k"], ".m4a"),
        N_("FLAC (lossless)"): ("flac", [], ".flac"),
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
        nb.add(main_outer, text="  " + tr("Theme audio") + "  ")
        self._jid = None
        main_sc = ScrollFrame(main_outer)
        main_sc.pack(fill="both", expand=True)
        main = main_sc.interior
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(top, text=tr("Video / audio file:")).pack(side="left", padx=(0, 6))
        self.file_var = tk.StringVar(value=saved.get("last_video", ""))
        ent = ttk.Entry(top, textvariable=self.file_var)
        ent.pack(side="left", fill="x", expand=True)
        icons.decorate(ttk.Button(top, text=tr("Browse..."), command=self.browse), "folder").pack(side="left", padx=6)
        icons.decorate(ttk.Button(top, text=tr("Load"), command=self.load_from_entry), "load").pack(side="left")

        body = ttk.Frame(main)
        body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # right: options
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        ttk.Label(right, text=tr("Intro from:")).grid(row=0, column=0, sticky="e", padx=4, pady=3)
        frow = ttk.Frame(right)
        frow.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        self.f_from = TimeEntry(frow)
        self.f_from.pack(side="left")
        bff = ttk.Button(frow, text=tr("Set"), command=lambda: self._mark(self.f_from))
        bff.pack(side="left", padx=(4, 0))
        add_tooltip(bff, tr("Set the intro START to the frame shown in the player"))
        gff = ttk.Button(frow, text=tr("Go"), command=lambda: self._goto(self.f_from))
        gff.pack(side="left", padx=(4, 0))
        add_tooltip(gff, tr("Jump the player to the time typed in this box"))

        ttk.Label(right, text=tr("Intro to:")).grid(row=1, column=0, sticky="e", padx=4, pady=3)
        trow = ttk.Frame(right)
        trow.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        self.f_to = TimeEntry(trow)
        self.f_to.pack(side="left")
        btf = ttk.Button(trow, text=tr("Set"), command=lambda: self._mark(self.f_to))
        btf.pack(side="left", padx=(4, 0))
        add_tooltip(btf, tr("Set the intro END to the frame shown in the player"))
        gtf = ttk.Button(trow, text=tr("Go"), command=lambda: self._goto(self.f_to))
        gtf.pack(side="left", padx=(4, 0))
        add_tooltip(gtf, tr("Jump the player to the time typed in this box"))

        for v in self.f_from.vars + self.f_to.vars:
            v.trace_add("write", lambda *a: self._refresh_markers())

        ttk.Label(right, text=tr("Audio track:")).grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.track_picker = _TrackPicker(right, self, saved.get("theme_track_lang", ""),
                                         width=34)
        self.track_picker.combo.grid(row=2, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(right, text=tr("Format:")).grid(row=3, column=0, sticky="e", padx=4, pady=3)
        _fmt = saved.get("theme_fmt")
        self.fmt_var = tk.StringVar(value=_fmt if _fmt in self.FORMATS else list(self.FORMATS)[0])
        KeyedCombobox(right, textvariable=self.fmt_var, values=list(self.FORMATS),
                      state="readonly", width=26).grid(row=3, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(right, text=tr("Output folder:")).grid(row=4, column=0, sticky="e", padx=4, pady=3)
        of = ttk.Frame(right)
        of.grid(row=4, column=1, sticky="we", padx=4, pady=3)
        of.columnconfigure(0, weight=1)
        self.outdir_var = tk.StringVar(value=saved.get("theme_out", OUTPUT_DIR))
        ttk.Entry(of, textvariable=self.outdir_var).grid(row=0, column=0, sticky="we")
        icons.decorate(ttk.Button(of, text="...", width=3,
                   command=self._pick_out), "folder").grid(row=0, column=1, padx=(4, 0))

        ttk.Label(right, text=tr("File name:")).grid(row=5, column=0, sticky="e", padx=4, pady=3)
        self.name_var = tk.StringVar(value=saved.get("theme_name", "theme"))
        ttk.Entry(right, textvariable=self.name_var, width=20).grid(row=5, column=1, sticky="w", padx=4, pady=3)

        fr = ttk.Frame(right)
        fr.grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=3)
        ttk.Label(fr, text=tr("Fade in (s):")).pack(side="left")
        self.fin_var = tk.StringVar(value=saved.get("theme_fin", "0"))
        ttk.Entry(fr, textvariable=self.fin_var, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(fr, text=tr("Fade out (s):")).pack(side="left")
        self.fout_var = tk.StringVar(value=saved.get("theme_fout", "0"))
        ttk.Entry(fr, textvariable=self.fout_var, width=5).pack(side="left", padx=4)

        nr = ttk.Frame(right)
        nr.grid(row=7, column=0, columnspan=2, sticky="w", padx=4, pady=3)
        self.tnorm_var = tk.BooleanVar(value=bool(saved.get("theme_norm", False)))
        ncb = ttk.Checkbutton(nr, text=tr("Normalize to"), variable=self.tnorm_var)
        ncb.pack(side="left")
        self.tnorm_target = tk.StringVar(value=saved.get("theme_norm_target", THEME_NORM_DEFAULT))
        nte = ttk.Entry(nr, textvariable=self.tnorm_target, width=5)
        nte.pack(side="left", padx=4)
        ttk.Label(nr, text=tr("LUFS (stereo)")).pack(side="left")
        for w in (ncb, nte):
            add_tooltip(w, tr("Two-pass loudness normalize the clip (after the cut and the "
                              "fades) to this target, downmixed to stereo, true peak <= "
                              "{tp} dBTP. -16 suits Plex themes next to streaming audio.",
                              tp=THEME_TP))

        brow = ttk.Frame(right)
        brow.grid(row=8, column=0, columnspan=2, sticky="we", padx=4, pady=(8, 2))
        brow.columnconfigure(0, weight=1)
        self.go_btn = icons.decorate(ttk.Button(brow, style="Accent.TButton", text=tr("Export theme audio"), command=self.start_export), "export")
        self.go_btn.grid(row=0, column=0, sticky="we")
        add_tooltip(self.go_btn, tr("Cut the intro's audio to the chosen file"))
        self.prev_btn = icons.decorate(ttk.Button(brow, text=tr("Preview"), command=self.start_preview), "play")
        self.prev_btn.grid(row=0, column=1, padx=(6, 0))
        add_tooltip(self.prev_btn, tr("Listen to From -> To exactly as it will be exported "
                                      "(track, fades and normalize applied). Stop ends it."))
        info_icon(brow, tr("Tip: for Plex, name it 'theme', pick MP3, and put "
                           "the file in the show's folder (Plex plays theme.mp3 "
                           "on the series page).")).grid(row=0, column=2, padx=(6, 0))
        self._prev_audio = None      # AudioPlayer while a preview plays
        self._prev_file = None       # its temp WAV
        self._prev_job = None        # after() id that ends the preview

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
        self.stop_btn = icons.decorate(ttk.Button(prog, text=tr("Stop"), command=self.stop, state="disabled"), "stop")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        add_tooltip(self.stop_btn, tr("Stop the export / preview"))
        self.logbox = build_log_tab(nb)
        _nb_help(nb, {}, "theme")

        enable_file_drop_deep(self.player, self._drop_load)       # drop anywhere in the player
        enable_file_drop(ent, self._drop_load)
        # list the audio tracks of whatever file is in the box (typed, browsed,
        # dropped or restored from last session)
        self.file_var.trace_add("write", lambda *a: self.track_picker.load(self.file_var.get()))
        self.track_picker.load(self.file_var.get())

    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        s, _ = self.f_from.get_seconds()
        e, _ = self.f_to.get_seconds()
        dur = self.player.timeline.duration
        end = e if e is not None else dur
        marks = [(s, end, "intro")] if (s is not None and end and end > s) else []
        self.player.set_markers(marks)

    def _mark(self, entry):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo(tr("No video"), tr("Load a file first, then scrub to the point."))
            return
        entry.set_seconds(sec)
        entry.flash()
        self._refresh_markers()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _goto(self, entry):
        if not self.player.has_video():
            messagebox.showinfo(tr("No video"), tr("Load a file first."))
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set(tr("Type a valid time in the box first, then Go."))
            return
        self.player.seek_seconds(sec)
        entry.flash()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _pick_out(self):
        d = filedialog.askdirectory(title=tr("Output folder"))
        if d:
            self.outdir_var.set(d)

    def _drop_load(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def browse(self):
        path = filedialog.askopenfilename(title=tr("Select file"), filetypes=media_types())
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
            messagebox.showerror(tr("Error"), tr("Type or browse to a valid file first."))

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            trim_text_lines(self.logbox)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def _checked_range(self):
        """Validate file, From/To, fades and the normalize target (shared by
        Export and Preview). Returns (video, s, e, fin, fout, norm) or None
        after showing the error; norm is the LUFS target or None."""
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror(tr("Error"), tr("Please select a valid file."))
            return None
        s, s_ok = self.f_from.get_seconds()
        e, e_ok = self.f_to.get_seconds()
        if not s_ok or not e_ok or s is None or e is None or e <= s:
            messagebox.showerror(tr("Error"), tr("Set a valid Intro From and To (To after From)."))
            return None
        try:
            fin = float(self.fin_var.get() or 0)
            fout = float(self.fout_var.get() or 0)
            assert fin >= 0 and fout >= 0
        except (ValueError, AssertionError):
            messagebox.showerror(tr("Error"), tr("Fade in/out must be 0 or a positive number."))
            return None
        norm = None
        if self.tnorm_var.get():
            try:
                num = _first_number(self.tnorm_target.get())
            except ValueError:
                messagebox.showerror(tr("Error"),
                                     tr("Normalize target must be a number (e.g. -16)."))
                return None
            try:
                norm = _check_range(num, LUFS_RANGE, tr("Normalize target (LUFS)"))
            except ValueError as ex:
                messagebox.showerror(tr("Error"), str(ex))
                return None
        # the range must lie inside the file
        dur = None
        if self.player.has_video() and getattr(self.player, "_path", None) \
                and _same_file(self.player._path, video):
            dur = self.player.timeline.duration or None
        if dur is None:
            dur = probe_duration(video)
        if dur and (s >= dur or e > dur + 0.05):
            messagebox.showerror(tr("Error"), tr("The range {start} -> {end} goes past the end "
                                                 "of the file ({dur}).", start=fmt_time(s),
                                                 end=fmt_time(e), dur=fmt_time(dur)))
            return None
        return video, s, e, fin, fout, norm

    def start_export(self):
        chk = self._checked_range()
        if chk is None:
            return
        video, s, e, fin, fout, norm = chk
        name = self.name_var.get().strip() or "theme"
        if name.lower().endswith(_THEME_EXTS):       # 'theme.mp3' -> 'theme'
            name = os.path.splitext(name)[0].strip()
            self.name_var.set(name)
        bad = sorted({c for c in name if c in _BAD_NAME_CHARS or ord(c) < 32})
        if (not name or bad or name.rstrip(". ") != name
                or name.split(".")[0].upper() in _RESERVED_NAMES):
            messagebox.showerror(tr("Error"), (
                tr("Invalid file name - it contains {chars}.", chars=" ".join(bad)) if bad
                else tr("Invalid file name.")) + "\n"
                + tr("Windows names can't use < > : \" / \\ | ? * , end in a dot/space, "
                     "or be CON, NUL, COM1 ..."))
            return
        if e - s < 3.0 and not messagebox.askyesno(
                tr("Very short"),
                tr("The theme would only be {sec:.2f} s long. Export anyway?", sec=e - s)):
            return
        codec, extra, ext = self.FORMATS[self.fmt_var.get()]
        outdir = self.outdir_var.get().strip() or OUTPUT_DIR
        out = os.path.join(outdir, name + ext)
        if _same_file(out, video):
            messagebox.showerror(tr("Error"), tr("The output would overwrite the input file.\n"
                                                 "Change the name or the output folder."))
            return
        self._end_preview(None)
        picked = self.track_picker.track_for(video)
        self.stop_event.clear()
        self._set_running(True)
        self.status_var.set(tr("Exporting..."))
        self._jid = jobs.begin(tr("Theme export"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._worker,
                         args=(video, out, s, e, codec, extra, fin, fout, picked, norm),
                         daemon=True).start()

    def _set_running(self, on):
        self.go_btn.configure(state="disabled" if on else "normal")
        self.prev_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")

    def stop(self):
        self.stop_event.set()
        if self._prev_audio is not None:
            self._end_preview(tr("Preview stopped."))
        else:
            self.log("Stopping...")

    # ---- preview: render the clip to a temp WAV, play it ----
    def start_preview(self):
        chk = self._checked_range()
        if chk is None:
            return
        video, s, e, fin, fout, norm = chk
        self._end_preview(None)
        try:
            self.player.pause()             # don't play the video's sound over it
        except Exception:
            pass
        picked = self.track_picker.track_for(video)
        os.makedirs(TEMP_DIR, exist_ok=True)
        tmp = os.path.join(TEMP_DIR, f"theme_preview_{os.getpid()}_{next(_preview_ids)}.wav")
        self._prev_file = tmp
        self.stop_event.clear()
        self._set_running(True)
        self.status_var.set(tr("Rendering preview (normalizing to {lufs:g} LUFS)...", lufs=norm)
                            if norm is not None else tr("Rendering preview..."))
        self._jid = jobs.begin(tr("Theme preview"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._preview_worker,
                         args=(video, tmp, s, e, fin, fout, picked, norm),
                         daemon=True).start()

    def _preview_worker(self, video, tmp, s, e, fin, fout, picked, norm):
        play, msg = False, tr("Preview failed - see log.")
        stopped = False
        jid = self._jid
        try:
            track = self.track_picker.resolve_track(video, picked)
            self.log(f"[PREVIEW] {fmt_time(s)} -> {fmt_time(e)}, audio track #{track + 1}"
                     + (f", normalize {norm:g} LUFS" if norm is not None else ""))
            rc, _err = export_audio_clip(video, tmp, s, e, "pcm_s16le", ["-ac", "2"],
                                         fade_in=fin, fade_out=fout, track=track,
                                         normalize=norm, tp=THEME_TP,
                                         stop_event=self.stop_event,
                                         on_progress=self._progress, on_log=self.log)
            if self.stop_event.is_set() or rc == -1:
                msg = tr("Preview stopped.")
                stopped = True
            elif rc == 0 and os.path.isfile(tmp):
                play = True
            else:
                self.log("  [FAIL] could not render the preview")
        except Exception as ex:
            self.log(f"  [FAIL] preview: {ex}")
        finally:
            jobs.end(jid, ok=play or stopped,
                     summary=tr("Previewing {sec:.1f} s", sec=e - s) if play else msg)
            if play:
                self.after(0, lambda: self._play_preview(tmp, e - s))
            else:
                self.after(0, lambda: self._end_preview(msg, tmp))

    def _play_preview(self, tmp, dur):
        if self.stop_event.is_set() or tmp != self._prev_file:
            self._end_preview(tr("Preview stopped."), tmp)
            return
        self.bar.configure(value=0)
        ap = AudioPlayer(log_fn=self.log)
        ap.volume = getattr(self.player.audio, "volume", 0.8)
        ap.start(tmp, 0.0)
        if not ap.is_active():
            self.log("  [FAIL] no sound output (sounddevice / audio device missing?) - "
                     f"the rendered preview is {tmp}")
            self._prev_file = None          # keep it so it can be played elsewhere
            self._end_preview(tr("Preview could not play - see log."))
            return
        self._prev_audio = ap
        self.status_var.set(tr("Previewing {sec:.1f} s ... (Stop ends it)", sec=dur))
        self._prev_job = self.after(int(dur * 1000) + 600,
                                    lambda: self._end_preview(tr("Preview finished.")))

    def _end_preview(self, msg, tmp=None):
        """Stop a preview (if any), delete its temp file, re-enable the buttons.
        msg=None: silent cleanup before another job starts."""
        if self._prev_job is not None:
            try:
                self.after_cancel(self._prev_job)
            except (tk.TclError, ValueError):
                pass
            self._prev_job = None
        if self._prev_audio is not None:
            self._prev_audio.stop()
            self._prev_audio = None
        for p in {tmp, self._prev_file} - {None}:
            _remove_file(self, p)
        self._prev_file = None
        if msg is not None:
            self.status_var.set(msg)
            self._set_running(False)
            self.bar.configure(value=0)

    def snapshot(self):
        """Theme-export settings to persist between sessions."""
        return {
            "theme_out": self.outdir_var.get().strip(),
            "theme_fmt": self.fmt_var.get(),
            "theme_name": self.name_var.get(),
            "theme_fin": self.fin_var.get(),
            "theme_fout": self.fout_var.get(),
            "theme_norm": bool(self.tnorm_var.get()),
            "theme_norm_target": self.tnorm_target.get().strip() or THEME_NORM_DEFAULT,
            "theme_track_lang": self.track_picker.pref_lang,
        }

    def _progress(self, frac, _text=""):
        v = int(max(0.0, min(1.0, frac)) * 1000)
        self.after(0, lambda: self.bar.configure(value=v))

    def _worker(self, video, out, s, e, codec, extra, fin, fout, picked=None, norm=None):
        done = tr("Failed - see log.")
        failed = True
        jid = self._jid
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            track = self.track_picker.resolve_track(video, picked)
            self.log(f"[THEME] {fmt_time(s)} -> {fmt_time(e)}  (audio track #{track + 1}"
                     + (f", normalize {norm:g} LUFS stereo" if norm is not None else "")
                     + f")  ->  {out}")
            before = _mtime(out)
            rc, _err = export_audio_clip(video, out, s, e, codec, extra,
                                         fade_in=fin, fade_out=fout, track=track,
                                         normalize=norm, tp=THEME_TP,
                                         stop_event=self.stop_event,
                                         on_progress=self._progress, on_log=self.log)
            if self.stop_event.is_set() or rc == -1:
                self.log("  [STOPPED]")
                done = tr("Stopped.")
                failed = False
                _discard_partial(out, before, self.log)
            elif rc == 0:
                self.log("  [OK] saved")
                done = tr("Done.")
                failed = False
            else:
                self.log("  [FAIL] export failed")
                _discard_partial(out, before, self.log)
        except Exception as ex:
            self.log(f"  [FAIL] {ex}")
        finally:
            jobs.end(jid, ok=not failed, summary=done)

            def _f():
                self.status_var.set(done)
                self._set_running(False)
                self.bar.configure(value=0)
            self.after(0, _f)
