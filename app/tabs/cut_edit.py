"""Cut / Edit tab: Auto-detect | Manual cut | Multi cut | Log.

RemoverTab is the shell: the per-show preset row, shared encoding /
subtitle settings, Snap and ▾ candidate buttons, the progress bar + Stop
and the job plumbing. Each sub-tab is a mixin: cut_auto.py, cut_manual.py,
cut_multi.py."""
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import (AUDIO_LANG_CHOICES, BIT_DEPTHS, CODECS, LEGACY_CODEC_LABELS,
                      VALID_PRESETS)
from ..engine import detect as detect_engine
from ..engine.formatting import fmt_time
from ..engine.probe import probe_subtitle_inventory
from ..i18n import ntr, tr
from ..ui.dialogs import ask_string, place_dialog
from ..ui.player import VideoPlayer
from ..ui.widgets import ScrollFrame, add_tooltip, build_log_tab, trim_text_lines
from .common import _KIND_NAMES, _same_dir, _same_file, _snap_in_thread, mark_edges
from .cut_auto import AutoCutMixin
from .cut_common import _cand_label, _is_plex, norm_margin, norm_tpl_margin, tr_key
from .cut_manual import ManualCutMixin
from .cut_multi import MultiCutMixin
from .cut_scan import ScanMixin
from .templates import TemplateTab
from .. import applog, presets
from ..ui import icons


# ======================= Tab 2 - Cut / Edit =======================
class RemoverTab(AutoCutMixin, ManualCutMixin, MultiCutMixin, ScanMixin, ttk.Frame):
    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.save_hook = None
        self.unload_players_hook = None   # set by app.py: frees files locked by players
        self._busy = False                # a Cut / Edit job is running
        self._jid = None                  # its job-registry id
        self._manual_cands = {}           # Manual cut: last detect_segments result
        self._manual_cands_path = ""      # ... and the video it belongs to
        self._multi_cands = {}            # Multi cut: path -> detect_segments result
        self._multi_notes = {}            # Multi cut: iid -> "intro weak 0.41; ..."
        self._multi_edges = {}            # Multi cut: iid -> {key: {"start","end"}} edge_src
        self._multi_info = set()          # ... iids whose note is informational (no ⚠)
        self._plex_saved = dict(saved.get("plex_scan") or {})   # last scan options
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- per-show preset row (above the sub-tabs) ----
        self._build_preset_row(saved)

        nb = ttk.Notebook(self)
        nb.grid(row=1, column=0, sticky="nsew")
        self._rnb = nb
        auto_outer = ttk.Frame(nb)
        manual_outer = ttk.Frame(nb)
        multi_outer = ttk.Frame(nb)
        nb.add(auto_outer, text=f"  {tr('Auto-detect')}  ")
        nb.add(manual_outer, text=f"  {tr('Manual cut')}  ")
        nb.add(multi_outer, text=f"  {tr('Multi cut')}  ")
        self._multi_page = multi_outer
        auto_sc = ScrollFrame(auto_outer)
        auto_sc.pack(fill="both", expand=True)
        auto = auto_sc.interior
        man_sc = ScrollFrame(manual_outer)
        man_sc.pack(fill="both", expand=True)
        manual = man_sc.interior
        multi_sc = ScrollFrame(multi_outer)
        multi_sc.pack(fill="both", expand=True)
        multi = multi_sc.interior

        # ===================== Auto-detect: grouped settings =====================
        self._build_auto_tab(auto, saved)

        # ===================== Manual cut: player + points =====================
        self._build_manual_tab(manual, saved)

        # ===================== Multi cut: many files, per-file times =============
        self._build_multi_tab(multi, saved)

        # ===================== shared status + progress + log =====================
        # anchored to the window bottom (non-scrolling) when `bottom` is given
        self.status_var = tk.StringVar(value=tr("Idle"))
        host = bottom if bottom is not None else self
        if bottom is not None:
            ttk.Label(host, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prog = ttk.Frame(host)
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(host, textvariable=self.status_var, style="Hint.TLabel").grid(
                row=2, column=0, sticky="w", pady=(6, 0))
            prog = ttk.Frame(host)
            prog.grid(row=3, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=44).grid(row=0, column=1, sticky="w", padx=(6, 0))
        # single Stop in the right corner, beside the progress bar - it's always
        # visible and drives whichever tool (Auto-detect / Manual / Multi) is running
        self.stop_btn = icons.decorate(ttk.Button(prog, text=tr("Stop"), command=self.stop, state="disabled",
                                   width=-8), "stop")
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, tr("Stop now: kills the running ffmpeg step immediately and "
                                      "discards its partial output (finished files are kept)"))
        self.logbox = build_log_tab(nb)

    def _upd_enc_summary(self):
        codec = tr_key(self.codec_var.get()).split(" (")[0]
        if _is_plex(self.codec_var.get()):
            codec += tr(" (MKV, Plex-friendly audio, bitrate capped below the source - "
                        "files never grow)")
        out = self.dirs["Output folder:"].get().strip() or tr("(not set)")
        self.enc_summary.set(tr(
            "Encoding in use: {codec}, CRF {crf} (H.265/AV1 {crf265}), preset {preset}, "
            "bit depth {depth} -> {out}. Change these on Cut / Edit → Auto-detect. "
            "Manual / Multi always CUT - the run mode there applies to Auto-detect only.",
            codec=codec, crf=self.crf_var.get(), crf265=self.crf265_var.get(),
            preset=self.preset_var.get(), depth=tr_key(self.depth_var.get()), out=out))

    @staticmethod
    def _out_is_source(out_dir, sources):
        """The first source whose folder is the output folder, else None (its
        output would overwrite the source)."""
        for src in sources:
            if _same_dir(out_dir, os.path.dirname(src) or "."):
                return src
        return None

    def _refuse_same_folder(self, out_dir, sources):
        src = self._out_is_source(out_dir, sources)
        if src is None:
            return False
        messagebox.showerror(
            tr("Output folder = source folder"),
            tr("The Output folder is the same folder as the source video(s):\n\n"
               "{folder}\n\nThe cut would overwrite the original. "
               "Pick a different Output folder on Cut / Edit → Auto-detect.",
               folder=os.path.dirname(src) or "."))
        return True

    # ---------------- subtitle language filter ----------------
    def _upd_subs_label(self):
        if not self.subs_filter_var.get():
            txt = tr("(keeping all subtitle tracks)")
        elif self.subs_langs:
            txt = tr("keeping: {langs}", langs=", ".join(sorted(self.subs_langs)))
        else:
            txt = tr("none chosen - click 'Choose languages...'")
        self.subs_lbl.set(txt)
        if hasattr(self, "multi_subs_lbl"):      # mirror onto the Multi cut tab
            self.multi_subs_lbl.set(txt)
        if hasattr(self, "manual_subs_lbl"):     # ... and the Manual cut tab
            self.manual_subs_lbl.set(txt)

    def _centre_dialog(self, dlg, modal=False):
        """Show / re-centre a dialog on the main window (themed, focused;
        modal = grab the input once it is on screen)."""
        place_dialog(dlg, self, modal=modal)

    def _open_subs_dialog(self, files):
        # open the dialog right away with a progress bar; it fills in when done
        dlg = tk.Toplevel(self)
        dlg.title(tr("Choose subtitle languages to keep"))
        dlg.transient(self.winfo_toplevel())
        body = ttk.Frame(dlg, padding=10)
        body.pack(fill="both", expand=True)
        scan_stop = threading.Event()
        status = tk.StringVar(value=ntr("Scanning {n} file for subtitle tracks...",
                                        "Scanning {n} files for subtitle tracks...", len(files)))
        ttk.Label(body, textvariable=status).pack(anchor="w")
        bar = ttk.Progressbar(body, mode="determinate", maximum=len(files), length=400)
        bar.pack(fill="x", pady=(8, 6))
        _cancel = lambda: (scan_stop.set(), dlg.destroy())
        ttk.Button(body, text=tr("Cancel"), command=_cancel).pack(anchor="e")
        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        dlg.bind("<Escape>", lambda e: _cancel())
        self._centre_dialog(dlg, modal=True)

        def prog(done, total):
            def _u():
                if dlg.winfo_exists():
                    bar.configure(value=done)
                    status.set(tr("Scanning subtitles {done}/{total}...", done=done, total=total))
            self.after(0, _u)

        def _work():
            inv = probe_subtitle_inventory(files, progress=prog, stop_event=scan_stop)
            self.after(0, lambda: self._populate_subs_dialog(dlg, body, inv, scan_stop))

        threading.Thread(target=_work, daemon=True).start()

    def _populate_subs_dialog(self, dlg, body, inv, scan_stop):
        if scan_stop.is_set() or not dlg.winfo_exists():
            return
        for w in body.winfo_children():      # clear the progress UI
            w.destroy()
        if not inv:
            ttk.Label(body, text=tr("No subtitle tracks found in the files.")).pack(anchor="w")
            ttk.Button(body, text=tr("Close"), command=dlg.destroy).pack(anchor="e", pady=(8, 0))
            self._centre_dialog(dlg)
            return

        ttk.Label(body, text=tr("Tick the subtitle languages to KEEP (others are dropped). "
                                "'und' = untagged/unknown - often English or forced."),
                  wraplength=420, justify="left").pack(anchor="w", pady=(0, 6))
        vars_by_lang = {}
        for d in inv:
            v = tk.BooleanVar(value=(d["lang"] in self.subs_langs) or not self.subs_langs)
            vars_by_lang[d["lang"]] = v
            bits = [d["lang"]]
            if d.get("title"):
                bits.append(f"“{d['title']}”")
            if d.get("forced"):
                bits.append(tr("[forced]"))
            bits.append(ntr("- in {n} file", "- in {n} files", d["count"]))
            ttk.Checkbutton(body, text="  ".join(bits), variable=v).pack(anchor="w", padx=6, pady=1)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        def _ok():
            self.subs_langs = {lang for lang, v in vars_by_lang.items() if v.get()}
            self.subs_filter_var.set(True)
            self._upd_subs_label()
            dlg.destroy()
        ttk.Button(btns, style="Accent.TButton", text=tr("OK"), command=_ok).pack(side="right")
        ttk.Button(btns, text=tr("Cancel"), command=dlg.destroy).pack(side="right", padx=(0, 6))
        self._centre_dialog(dlg)

    # ---------------- per-show presets ----------------
    # what a preset stores: these RemoverTab.snapshot() keys ...
    PRESET_KEYS = ("video_dir", "intro_dir", "credits_dir", "preintro_dir",
                   "aftercredits_dir", "output_dir", "codec", "crf", "crf_h265", "preset",
                   "bit_depth", "kf_interval", "confidence", "mode", "move_done",
                   "skip_incomplete", "trim_to_match", "anchor_cut", "anchor_secs",
                   "intro_from_start", "credits_to_end", "match_lang", "subs_filter",
                   "subs_langs", "use_preintro", "use_intro", "use_credits",
                   "use_aftercredits", "detect_mode", "review_method", "plex_scan",
                   "margin_frames", "template_margin")
    # ... plus these TemplateTab.snapshot() keys (its Auto-detect settings)
    PRESET_TPL_KEYS = ("det_window", "det_minlen_intro", "det_minlen_credits",
                       "det_minlen_pa", "det_sens", "det_eplen", "det_lang", "det_mode",
                       "det_margin", "det_tpl_margin")
    _DIR_KEYS = {"video_dir": "Videos folder:", "intro_dir": "Intro templates:",
                 "credits_dir": "Credits templates:", "preintro_dir": "Pre-intro templates:",
                 "aftercredits_dir": "After-credits templates:",
                 "output_dir": "Output folder:"}

    def _build_preset_row(self, saved):
        row = ttk.Frame(self)
        row.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(row, text=tr("Show preset:")).pack(side="left", padx=(0, 6))
        last = str(saved.get("last_preset") or "")
        self.preset_name_var = tk.StringVar(value=last if presets.exists(last) else "")
        self.preset_cb = ttk.Combobox(row, textvariable=self.preset_name_var, state="readonly",
                                      width=30, postcommand=self._preset_refresh_list)
        self.preset_cb.pack(side="left")
        add_tooltip(self.preset_cb, tr("Saved settings per show: folders, encoding, detection & "
                                       "mode options and the Templates tab's Auto-detect "
                                       "settings. Pick one, then Load."))
        self.preset_cb.bind("<<ComboboxSelected>>",
                            lambda e: self.preset_status.set(tr("Press Load to apply it.")))
        for txt, cmd, tip in (
                (tr("Save as..."), self._preset_save, tr(
                    "Save the current settings as a preset (folders, codec, CRF, preset, bit "
                    "depth, keyframes, mode, every detection / mode option, subtitle filter, "
                    "segment ticks, Templates tab Auto-detect settings)")),
                (tr("Load"), self._preset_load, tr(
                    "Apply the chosen preset to the Cut / Edit tab (and the Templates tab's "
                    "Auto-detect settings)")),
                (tr("Delete"), self._preset_delete, tr(
                    "Move the chosen preset to Data\\temp\\trash (recoverable - nothing is "
                    "deleted)"))):
            b = ttk.Button(row, text=txt, command=cmd)
            b.pack(side="left", padx=(6, 0))
            add_tooltip(b, tip)
        self.preset_status = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.preset_status, style="Hint.TLabel").pack(
            side="left", padx=(10, 0))
        self._preset_refresh_list()

    def _preset_refresh_list(self):
        try:
            self.preset_cb.configure(values=presets.names())
        except tk.TclError:
            pass

    def _find_template_tab(self):
        """The app's TemplateTab (for its detect settings), or None."""
        tt = getattr(self, "template_tab", None)
        if tt is not None:
            return tt
        stack = [self.winfo_toplevel()]
        while stack:
            w = stack.pop()
            if isinstance(w, TemplateTab):
                self.template_tab = w
                return w
            try:
                stack.extend(w.winfo_children())
            except tk.TclError:
                pass
        return None

    def _preset_collect(self):
        snap = self.snapshot()
        d = {k: snap[k] for k in self.PRESET_KEYS if k in snap}
        tt = self._find_template_tab()
        if tt is not None:
            try:
                ts = tt.snapshot()
                d["template"] = {k: ts[k] for k in self.PRESET_TPL_KEYS if k in ts}
            except Exception:
                pass
        return d

    def _preset_apply(self, d):
        """Set the widgets from a preset dict (unknown / invalid values are
        skipped, so an older or hand-edited preset can't break the tab)."""
        for key, label in self._DIR_KEYS.items():
            if isinstance(d.get(key), str):
                self.dirs[label].set(d[key])
        codec = d.get("codec")
        if isinstance(codec, str):
            codec = LEGACY_CODEC_LABELS.get(codec, codec)
            if codec in CODECS:
                self.codec_var.set(codec)
            else:
                self.log(f"[PRESET] codec '{codec}' isn't available here - kept "
                         f"'{self.codec_var.get()}'")
        for key, var in (("crf", self.crf_var), ("crf_h265", self.crf265_var),
                         ("kf_interval", self.kf_var), ("confidence", self.conf_var),
                         ("anchor_secs", self.anchor_secs_var)):
            if isinstance(d.get(key), (str, int, float)) and not isinstance(d.get(key), bool):
                var.set(str(d[key]))
        if d.get("preset") in VALID_PRESETS:
            self.preset_var.set(d["preset"])
        if d.get("bit_depth") in BIT_DEPTHS:
            self.depth_var.set(d["bit_depth"])
        if d.get("mode") in ("cut", "inject", "chapters"):
            self.mode_var.set(d["mode"])
        for key, var in (("move_done", self.move_done_var),
                         ("skip_incomplete", self.skip_incomplete_var),
                         ("trim_to_match", self.trim_match_var),
                         ("anchor_cut", self.anchor_var),
                         ("intro_from_start", self.intro_from_start_var),
                         ("credits_to_end", self.credits_to_end_var),
                         ("subs_filter", self.subs_filter_var)):
            if key in d:
                var.set(bool(d[key]))
        for key, var in self.use_seg.items():
            if f"use_{key}" in d:
                var.set(bool(d[f"use_{key}"]))
        if "match_lang" in d:
            lbl = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == d["match_lang"]), None)
            if lbl:
                self.match_lang_var.set(lbl)
        if d.get("detect_mode") in ("audio", "visual", "both"):
            self.detect_mode_var.set(d["detect_mode"])
        if "margin_frames" in d:
            self.margin_frames_var.set(str(norm_margin(d["margin_frames"])))
        if "template_margin" in d:
            self.template_margin_var.set(norm_tpl_margin(d["template_margin"]))
        if d.get("review_method") in ("templates", "plex"):
            self.review_method_var.set(d["review_method"])
        if isinstance(d.get("plex_scan"), dict):
            self._plex_saved = dict(d["plex_scan"])
        if isinstance(d.get("subs_langs"), list):
            self.subs_langs = {str(x) for x in d["subs_langs"]}
        self._upd_trim_state()
        self._upd_subs_label()
        tpl = d.get("template")
        tt = self._find_template_tab() if isinstance(tpl, dict) else None
        if tt is not None:
            for key in ("det_window", "det_minlen_intro", "det_minlen_credits",
                        "det_minlen_pa", "det_sens", "det_eplen"):
                var = getattr(tt, key, None)
                if var is not None and isinstance(tpl.get(key), (str, int, float)):
                    var.set(str(tpl[key]))
            if tpl.get("det_mode") in ("audio", "visual", "both") and hasattr(tt, "det_mode_var"):
                tt.det_mode_var.set(tpl["det_mode"])
            if "det_margin" in tpl and hasattr(tt, "det_margin_var"):
                tt.det_margin_var.set(str(norm_margin(tpl["det_margin"])))
            if "det_tpl_margin" in tpl and hasattr(tt, "det_tpl_margin_var"):
                tt.det_tpl_margin_var.set(norm_tpl_margin(tpl["det_tpl_margin"]))
            if "det_lang" in tpl and hasattr(tt, "det_lang_var"):
                lbl = next((k for k, v in AUDIO_LANG_CHOICES.items()
                            if v == tpl["det_lang"]), None)
                if lbl:
                    tt.det_lang_var.set(lbl)

    def _preset_save(self):
        cur = self.preset_name_var.get().strip()
        if not cur:
            cur = os.path.basename(os.path.normpath(
                self.dirs["Videos folder:"].get().strip() or "")) or ""
        name = ask_string(self, tr("Save preset"), tr("Preset name (e.g. the show):"),
                          initialvalue=cur)
        if name is None:
            return
        clean = presets.sanitize(name)
        if not clean:
            messagebox.showerror(tr("Save preset"),
                                 tr("Give the preset a name (letters / numbers)."))
            return
        if presets.exists(clean) and not messagebox.askyesno(
                tr("Save preset"),
                tr("A preset called '{name}' already exists. Overwrite it?", name=clean)):
            return
        try:
            saved = presets.save(clean, self._preset_collect())
        except (OSError, ValueError) as exc:
            messagebox.showerror(tr("Save preset"),
                                 tr("Could not save the preset:\n{error}", error=exc))
            return
        self._preset_refresh_list()
        self.preset_name_var.set(saved)
        self.preset_status.set(tr("Saved '{name}'.", name=saved))
        self.log(f"[PRESET] saved '{saved}'")
        if callable(self.save_hook):
            self.save_hook()            # remember it as the last used preset

    def _preset_load(self):
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showinfo(tr("Load preset"), tr("Pick a preset in the list first."))
            return
        if self._busy:
            messagebox.showinfo(tr("Load preset"), tr("Wait for the running job to finish (or "
                                                      "Stop it) before switching presets."))
            return
        d = presets.load(name)
        if d is None:
            messagebox.showerror(tr("Load preset"),
                                 tr("Could not read the preset '{name}'.", name=name))
            self._preset_refresh_list()
            return
        self._preset_apply(d)
        self.preset_status.set(tr("Loaded '{name}'.", name=name))
        self.log(f"[PRESET] loaded '{name}'")
        if callable(self.save_hook):
            self.save_hook()

    def _preset_delete(self):
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showinfo(tr("Delete preset"), tr("Pick a preset in the list first."))
            return
        if not messagebox.askyesno(tr("Delete preset"), tr(
                "Move the preset '{name}' to Data\\temp\\trash? (It can be recovered from "
                "there.)", name=name)):
            return
        try:
            dest = presets.trash(name)
        except OSError as exc:
            messagebox.showerror(tr("Delete preset"),
                                 tr("Could not move the preset:\n{error}", error=exc))
            return
        self.preset_name_var.set("")
        self._preset_refresh_list()
        self.preset_status.set(tr("'{name}' moved to the trash.", name=name) if dest else
                               tr("'{name}' was already gone.", name=name))
        self.log(f"[PRESET] '{name}' -> {dest}" if dest else f"[PRESET] '{name}' not found")
        if callable(self.save_hook):
            self.save_hook()

    # ---------------- snap (Manual + Multi) ----------------
    def _snap_box(self, player, entry, key, which, btn):
        """Snap one From/To box to the nearest silence / black boundary of
        the video loaded in `player` (same engine as the Templates tab)."""
        video = getattr(player, "_path", None)
        if not player.has_video() or not video or not os.path.isfile(video):
            messagebox.showinfo(tr("No video"), tr("Load a video in the player first."))
            return
        # engine values: a To box gives / takes the exclusive end (the frame
        # after the one it shows); the snap engine returns such a boundary too
        t, ok = entry.get_value()
        if not ok:
            self.status_var.set(tr("That time box is invalid - fix it or clear it to snap "
                                   "from the player position."))
            return
        if t is None:
            t = entry.engine(player.current_seconds())
        if t is None:
            return
        btn.configure(state="disabled")
        seg = tr_key(_KIND_NAMES.get(key, key))
        box = tr("From") if which == "from" else tr("To")
        self.status_var.set(tr("Snapping {seg} {box} near {time}...", seg=seg, box=box,
                               time=fmt_time(entry.shown(t))))

        def done(new_t, reason):
            try:
                btn.configure(state="normal")
            except tk.TclError:
                return
            tag = f"[SNAP] {key} {which}:"
            if new_t is None:
                self.log(f"{tag} {reason}")
                self.status_var.set(tr("Snap: {reason}", reason=reason))
                return
            # the Multi cut boxes belong to whichever file is selected - drop
            # the result if the user moved to another file meanwhile
            if not _same_file(getattr(player, "_path", None), video):
                self.log(f"{tag} discarded - another file is loaded now")
                return
            entry.set_value(new_t)
            entry.flash()
            if player.has_video():
                player.seek_seconds(entry.shown(new_t), play=False)
                player.canvas.focus_set()
            old_s, new_s = fmt_time(entry.shown(t)), fmt_time(entry.shown(new_t))
            msg = f"snapped {old_s} → {new_s} ({reason})"
            self.log(f"{tag} {msg}")
            self.status_var.set(tr("{seg} {box} snapped {old} → {new} ({reason})", seg=seg,
                                   box=box, old=old_s, new=new_s, reason=reason))

        _snap_in_thread(self, video, t, "start" if which == "from" else "end",
                        AUDIO_LANG_CHOICES.get(self.match_lang_var.get()), done)

    # ---------------- detection candidates (Manual + Multi) ----------------
    def _cand_button(self, parent, key, where):
        """Small '▾' menu listing every detected candidate for `key` (best
        first, near-misses marked 'weak'); picking one fills that row."""
        mb = ttk.Menubutton(parent, text="▾", width=2)
        menu = tk.Menu(mb, tearoff=False)
        menu.configure(postcommand=lambda: self._cand_fill_menu(menu, key, where))
        mb["menu"] = menu
        add_tooltip(mb, tr("Candidates: every {seg} match the last Auto-detect / Review found "
                           "for this file (best first; 'weak' = below Min confidence). Pick one "
                           "to fill this row.", seg=tr_key(_KIND_NAMES[key])))
        return mb

    def _cands_for(self, where):
        if where == "manual":
            video = self.sel_var.get().strip().strip('"')
            if self._manual_cands and _same_file(video, self._manual_cands_path):
                return self._manual_cands
            return {}
        path = self._multi_paths.get(self._multi_sel) if self._multi_sel else None
        return self._multi_cands.get(path) or {}

    def _cand_fill_menu(self, menu, key, where):
        menu.delete(0, "end")
        cands = self._cands_for(where).get(key) or []
        if not cands:
            hint = (tr("run Auto-detect first") if where == "manual"
                    else tr("run 'Review first' on Cut / Edit → Auto-detect"))
            menu.add_command(label=tr("(no {seg} candidates - {hint})",
                                      seg=tr_key(_KIND_NAMES[key]), hint=hint),
                             state="disabled")
            return
        for i, c in enumerate(cands, 1):
            menu.add_command(label=_cand_label(key, i, c, ui=True),
                             command=lambda c=c, i=i: self._cand_pick(key, c, where, i))

    def _cand_pick(self, key, c, where, idx=1):
        try:
            a, b = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            return
        if where == "manual":
            ef, et = self._manual[key]
            player = self.player
        else:
            if not self._multi_sel:
                return
            ef, et = self._multi_entries[key]
            player = self.multi_player
        ef.set_value(a)
        et.set_value(b)
        es = c.get("edge_src") if isinstance(c.get("edge_src"), dict) else None
        if where != "manual" and self._multi_sel:
            if es:
                self._multi_edges.setdefault(self._multi_sel, {})[key] = dict(es)
            else:
                self._multi_edges.get(self._multi_sel, {}).pop(key, None)
        mark_edges(ef, et, es)         # ⚠ on an edge placed by sound / a fade
        ef.flash()
        et.flash()
        if where == "manual":
            self._refresh_markers()
        if player.has_video():
            player.seek_seconds(a, play=False)
            player.canvas.focus_set()
        self.log(f"[DETECT] {_cand_label(key, idx, c)} -> {_KIND_NAMES[key]} row")

    # ---------------- Manual cut: auto-detect the previewed video ----------------
    def _engine_ready(self):
        if (hasattr(detect_engine, "detect_segments")
                and hasattr(detect_engine, "load_templates_for")):
            return True
        messagebox.showerror(tr("Not available"), tr("This build's detection engine has no "
                                                     "detect_segments() yet - update the "
                                                     "toolkit."))
        return False

    # --- ui callbacks (thread-safe) ---
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

    def progress(self, frac, text):
        def _a():
            self.bar["value"] = int(frac * 1000)
            self.pct_var.set(text)
        self.after(0, _a)

    def _running(self, on, locked=None):
        # while a cut that moves its sources runs, the preview players refuse
        # to reopen those files (an open player locks them on Windows)
        if on:
            VideoPlayer.lock_paths(locked or ())
        else:
            VideoPlayer.unlock_all()
        self._busy = bool(on)
        st = "disabled" if on else "normal"
        # 'Add to queue' stays usable while a job runs - that's what it's for
        for name in ("start_btn", "cut_sel_btn", "multi_cut_btn", "review_btn",
                     "man_detect_btn", "multi_detect_mb", "multi_plex_btn"):
            w = getattr(self, name, None)
            if w is not None:
                w.configure(state=st)
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set(tr("Idle"))

    def stop(self):
        self.stop_event.set()
        self.log("Stopping - the running ffmpeg step is killed now and its partial output "
                 "discarded...")

    def snapshot(self):
        return {
            "video_dir": self.dirs["Videos folder:"].get(),
            "intro_dir": self.dirs["Intro templates:"].get(),
            "credits_dir": self.dirs["Credits templates:"].get(),
            "preintro_dir": self.dirs["Pre-intro templates:"].get(),
            "aftercredits_dir": self.dirs["After-credits templates:"].get(),
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": self.crf_var.get(),
            "crf_h265": self.crf265_var.get(),
            "preset": self.preset_var.get(),
            "codec": self.codec_var.get(),
            "bit_depth": self.depth_var.get(),
            "kf_interval": self.kf_var.get(),
            "confidence": self.conf_var.get(),
            "mode": self.mode_var.get(),
            "input_folders_v1": True,
            "move_done": self.move_done_var.get(),
            "skip_incomplete": self.skip_incomplete_var.get(),
            "trim_to_match": self.trim_match_var.get(),
            "anchor_cut": self.anchor_var.get(),
            "anchor_secs": self.anchor_secs_var.get(),
            "intro_from_start": self.intro_from_start_var.get(),
            "credits_to_end": self.credits_to_end_var.get(),
            "match_lang": AUDIO_LANG_CHOICES.get(self.match_lang_var.get()),
            "detect_mode": self.detect_mode_var.get(),
            "margin_frames": norm_margin(self.margin_frames_var.get()),
            "template_margin": norm_tpl_margin(self.template_margin_var.get()),
            "review_method": self.review_method_var.get(),
            "plex_scan": dict(self._plex_saved or {}),
            "subs_filter": self.subs_filter_var.get(),
            "subs_langs": sorted(self.subs_langs),
            "multi_keep_pos": self._multi_keep_pos_var.get(),
            "multi_files": self._multi_snapshot(),
            "manual_video": self.sel_var.get().strip(),
            "last_preset": self.preset_name_var.get().strip(),
            **{f"use_{k}": v.get() for k, v in self.use_seg.items()},
        }
