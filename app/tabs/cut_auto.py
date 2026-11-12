"""Cut / Edit -> Auto-detect sub-tab (AutoCutMixin): Start batch, Review
first and Add to queue with the settings of the Settings sub-tab
(cut_settings.py)."""
import os
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import AUDIO_LANG_CHOICES, BIT_DEPTHS, CODECS
from ..engine.cut import run_batch
from ..i18n import ntr, tr
from ..ui.helpwin import help_button
from ..ui.tooltips import add_tooltip
from ..ui.widgets import auto_wrap
from .common import _list_media, _same_dir
from .cut_common import _ask_choice, norm_margin, norm_tpl_margin
from .. import jobs as jobreg
from ..ui import icons


def _batch_fails(res):
    """How many files failed, from run_batch's per-file summary
    [(source, status, output or None)] (status ok / failed / skipped /
    stopped). None: the engine returns no summary (an older build)."""
    if not isinstance(res, (list, tuple)):
        return None
    n = 0
    for item in res:
        status = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None
        if isinstance(item, dict):
            status = "failed" if item.get("ok") is False else item.get("status")
        if status == "failed":
            n += 1
    return n


class AutoCutMixin:
    """Cut / Edit -> Auto-detect sub-tab: batch run / Review first / Add to
    queue (the folders, encoding and detection settings they use are on the
    Settings sub-tab - CutSettingsMixin)."""

    def _build_auto_tab(self, auto, saved):
        """Auto-detect sub-tab: intro, the settings in use (they are set on
        the Settings sub-tab), Start batch / Review first / Add to queue."""
        auto.columnconfigure(0, weight=1)
        ahead = ttk.Frame(auto)
        ahead.grid(row=0, column=0, sticky="we", padx=6, pady=(6, 0))
        help_button(ahead, "cut_auto").pack(side="right")
        auto_wrap(ttk.Label(ahead, style="Hint.TLabel", justify="left", text=tr(
            "Match your templates in every video of the Videos folder and cut them out "
            "- or review the detections first.")), width=860).pack(side="left", anchor="w")

        # what Start batch / Review first will use - changed on the Settings sub-tab
        used = ttk.LabelFrame(auto, text=tr("Settings in use"))
        used.grid(row=1, column=0, sticky="we", padx=6, pady=(10, 0))
        row, self.auto_change_btn = self._settings_summary_row(used, self.auto_summary,
                                                               wrap=860, below=True)
        row.pack(fill="x", padx=4, pady=(2, 2))

        runrow = ttk.Frame(auto)
        runrow.grid(row=2, column=0, sticky="we", padx=6, pady=(14, 6))
        self.start_btn = icons.decorate(ttk.Button(runrow, style="Accent.TButton", text=tr("Start batch (auto-detect)"),
                                    command=self.start), "run")
        self.start_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.start_btn, tr("Auto-detect intro, credits, and (if you have templates) "
                                       "pre-intro and after-credits in every video, then cut "
                                       "them out"))
        self.review_btn = icons.decorate(ttk.Button(runrow, text=tr("Review first (detect only)"),
                                     command=self.review_first), "detect")
        self.review_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.review_btn, tr(
            "Detect only - nothing is cut. Every video in the Videos "
            "folder is matched against the templates and loaded into the Multi cut "
            "list with its detected times; files where a segment wasn't found or only "
            "matched weakly get a ⚠ and a note. Fix those rows there (player, Set, "
            "Snap, ▾ candidates), then 'Cut all files'."))
        self.queue_btn = icons.decorate(ttk.Button(runrow, text=tr("Add to queue"), command=self._enqueue_batch), "queue")
        self.queue_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.queue_btn, tr(
            "Queue this batch with the CURRENT folders and settings - "
            "it starts when nothing else is running, so you can line up several "
            "seasons (change the folders, Add to queue again). See Queue... in the "
            "status bar."))
        # Stop lives beside the progress bar at the bottom (shared by all tools)
        rmrow = ttk.Frame(auto)
        rmrow.grid(row=3, column=0, sticky="we", padx=6, pady=(0, 6))
        ttk.Label(rmrow, text=tr("Review first:")).pack(side="left")
        rm = saved.get("review_method")
        self.review_method_var = tk.StringVar(value=rm if rm in ("templates", "plex")
                                              else "templates")
        r1 = ttk.Radiobutton(rmrow, text=tr("Match templates"), value="templates",
                             variable=self.review_method_var)
        r1.pack(side="left", padx=(6, 0))
        add_tooltip(r1, tr("Review first matches the templates in the template folders "
                           "(like Start batch)"))
        r2 = ttk.Radiobutton(rmrow, text=tr("Plex-style scan (no templates)"), value="plex",
                             variable=self.review_method_var)
        r2.pack(side="left", padx=(10, 0))
        add_tooltip(r2, tr("Review first finds the intro and credits WITHOUT templates, like "
                           "Plex: the opening that recurs across the episodes of the Videos "
                           "folder and the credits near the end. Opens the scan options "
                           "first."))

    # ---------------- Review first (detect only -> Multi cut) ----------------
    def _review_list_mode(self):
        """'replace' / 'append' for loading review results into Multi cut
        (asked when the list isn't empty), None = cancelled."""
        if not self._multi_paths:
            return "replace"
        choice = _ask_choice(
            self, tr("Multi cut list isn't empty"),
            ntr("The Multi cut list already holds {n} file.",
                "The Multi cut list already holds {n} files.", len(self._multi_paths))
            + "\n\n" + tr("Replace it with the review results, Append them (files already "
                          "in the list get their times updated), or Cancel?"),
            [("replace", tr("Replace")), ("append", tr("Append")),
             ("cancel", tr("Cancel"))])
        return choice if choice in ("replace", "append") else None

    def review_first(self):
        if self._busy:
            return
        if self.review_method_var.get() == "plex":
            files = _list_media(self.dirs["Videos folder:"].get().strip().strip('"'))
            if not files:
                messagebox.showerror(tr("Error"), tr("No videos found in the Videos folder."))
                return
            self._plex_review(files)
            return
        if not self._engine_ready():
            return
        cfg = self._batch_cfg(check_output=False)
        if cfg is None:
            return
        files = _list_media(cfg["video_dir"])
        if not files:
            messagebox.showerror(tr("Error"), tr("No videos found in the Videos folder."))
            return
        mode = self._review_list_mode()
        if mode is None:
            return
        # re-checked after the dialog: a queued job may have started meanwhile
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        if self.save_hook:
            self.save_hook()
        self._running(True)
        jid = self._jid = jobreg.begin(tr("Cut / Edit review (detect only)"),
                                       stop_event=self.stop_event, tab=self)
        self._spawn(self._review_worker, files, cfg, mode, jid)

    def _review_worker(self, files, cfg, mode, jid=None):
        stop, seq = self._run_ctx()
        results = {}
        crashed = False
        n = len(files)
        n_warn = 0
        have = None         # kinds that have templates at all
        try:
            self.log(f"[REVIEW] detecting in {n} video(s) - nothing is cut")
            results, have, n_warn = self._detect_loop(files, cfg, "REVIEW", review=True,
                                                      stop=stop)
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] review crashed: {e!r}")
        finally:
            stopped = stop.is_set()
            jobreg.end(jid, ok=not crashed and not stopped,
                       summary=tr("{done}/{n} reviewed, {warn} need a look",
                                  done=len(results), n=n, warn=n_warn))
            self._finish(seq, lambda: self._review_done(files, cfg, mode, results, stopped,
                                                        have))

    def _review_done(self, files, cfg, mode, results, stopped, have=None):
        self._running(False)
        self.bar["value"] = 0
        if not results:
            self.status_var.set(tr("Review: nothing to load.") if not stopped else
                                tr("Review stopped before any file finished."))
            self.log("[REVIEW] nothing loaded into Multi cut.")
            return
        if mode == "replace":
            self._multi_clear()
        entries = []
        for f in files:
            if f not in results:
                continue
            entries.append(self._multi_detect_entry(f, results[f], cfg, have,
                                                    ranges_use_have=False))
        _new, n_upd, first, first_warn = self._multi_apply_results(entries)
        n_warn = sum(1 for e in entries if e[3])
        n_ok = len(entries) - n_warn
        self._multi_sel = None              # open the first row that needs a look
        self._multi_reselect(first_warn or first)
        summary = (f"{len(results)} file(s) loaded into Multi cut: {n_ok} complete, "
                   f"{n_warn} need a look (⚠)" + (f", {n_upd} updated" if n_upd else ""))
        shown = ntr("{n} file loaded into Multi cut: {ok} complete, {warn} need a look (⚠)",
                    "{n} files loaded into Multi cut: {ok} complete, {warn} need a look (⚠)",
                    len(results), ok=n_ok, warn=n_warn)
        if n_upd:
            shown += tr(", {n} updated", n=n_upd)
        if stopped:
            summary += f" - stopped, {len(files) - len(results)} file(s) not reviewed"
            shown += ntr(" - stopped, {n} file not reviewed", " - stopped, {n} files not reviewed",
                         len(files) - len(results))
        self.log(f"[REVIEW] {summary}. Fix the ⚠ rows (Set / Snap / ▾), then 'Cut all files'.")
        if cfg.get("mode") not in ("cut", "smartcut"):
            self.log(f"[REVIEW] note: Multi cut always CUTS - the '{cfg.get('mode')}' run mode "
                     "applies to Start batch only.")
        self.status_var.set(shown)

    def _batch_cfg(self, check_output=True):
        """The run_batch cfg from the Settings sub-tab, or None after an
        error box. check_output=False skips the 'Output = Videos folder' check
        (detect-only runs write nothing)."""
        try:
            crf = int(self.crf_var.get())
            crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get())
            assert kf >= 0
            conf = float(self.conf_var.get())
            assert 0 <= conf <= 1
            anchor_secs = float(self.anchor_secs_var.get())
            assert 5 <= anchor_secs <= 40
        except (ValueError, AssertionError):
            messagebox.showerror(tr("Error"), tr("Check the CRF values (0-51), keyframe interval "
                                                 "(>=0), Min confidence (0-1) and anchor seconds "
                                                 "(5-40)."))
            return None
        if check_output and _same_dir(self.dirs["Output folder:"].get(),
                                      self.dirs["Videos folder:"].get()):
            messagebox.showerror(
                tr("Output folder = source folder"),
                tr("The Output folder is the same as the Videos folder - the cut files would "
                   "overwrite the originals. Pick a different Output folder."))
            return None
        cfg = {
            "video_dir": self.dirs["Videos folder:"].get(),
            "intro_dir": self.dirs["Intro templates:"].get(),
            "credits_dir": self.dirs["Credits templates:"].get(),
            "preintro_dir": self.dirs["Pre-intro templates:"].get(),
            "aftercredits_dir": self.dirs["After-credits templates:"].get(),
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": crf, "crf_h265": crf265, "preset": self.preset_var.get(),
            "encoder": CODECS.get(self.codec_var.get(), "libx264"),
            "bit_depth": BIT_DEPTHS.get(self.depth_var.get(), "auto"),
            "kf_interval": kf, "confidence": conf,
            # run mode Cut + Cut method Smart cut -> the engine's "smartcut"
            "mode": ("smartcut" if self.mode_var.get() == "cut" and self.smart_cut_on()
                     else self.mode_var.get()),
            "smart_cut": self.smart_cut_on(),
            "use": {k: v.get() for k, v in self.use_seg.items()},
            "move_done": self.move_done_var.get(),
            "skip_incomplete": self.skip_incomplete_var.get(),
            "trim_to_match": self.trim_match_var.get(),
            "anchor_cut": self.anchor_var.get(),
            "anchor_secs": anchor_secs,
            "intro_from_start": self.intro_from_start_var.get(),
            "credits_to_end": self.credits_to_end_var.get(),
            "match_lang": AUDIO_LANG_CHOICES.get(self.match_lang_var.get()),
            "detect_mode": self.detect_mode_var.get(),
            "margin_frames": norm_margin(self.margin_frames_var.get()),
            "template_margin": norm_tpl_margin(self.template_margin_var.get()),
            "subs_langs": (sorted(self.subs_langs)
                           if self.subs_filter_var.get() and self.subs_langs else None),
        }
        if not any(cfg["use"].values()):
            messagebox.showerror(tr("Error"), tr("Tick at least one segment type to process."))
            return None
        return cfg

    def start(self, cfg=None):
        """Start the auto-detect batch (cfg=None: from the current settings;
        the queue passes the cfg captured when it was queued). Returns the
        job id, or None if nothing started."""
        if self._busy:
            return None
        if cfg is None:
            cfg = self._batch_cfg()
            if cfg is None:
                return None
        # moving finished sources needs no player holding the file open (Windows
        # locks it) - release every preview player before the run starts
        if cfg["move_done"] and callable(self.unload_players_hook):
            self.unload_players_hook()
        if self.save_hook:
            self.save_hook()
        self._running(True, _list_media(cfg["video_dir"]) if cfg["move_done"] else None)
        folder = os.path.basename(os.path.normpath(cfg["video_dir"])) or cfg["video_dir"]
        jid = self._jid = jobreg.begin(tr("Cut / Edit batch - {folder}", folder=folder),
                                       stop_event=self.stop_event, tab=self)
        self._spawn(self._worker, cfg, jid)
        return jid

    def _enqueue_batch(self):
        """Queue a batch with the folders / settings as they are now."""
        cfg = self._batch_cfg()
        if cfg is None:
            return
        folder = os.path.basename(os.path.normpath(cfg["video_dir"])) or cfg["video_dir"]
        name = tr("Cut / Edit batch - {folder}", folder=folder)
        jobreg.enqueue(name, lambda c=dict(cfg): self.start(cfg=c))
        self.log(f"[QUEUE] added: Cut / Edit batch - {folder} "
                 f"({len(_list_media(cfg['video_dir']))} video(s) in the folder now)")
        self.status_var.set(tr("Queued: {name}", name=name))

    def _worker(self, cfg, jid=None):
        stop, seq = self._run_ctx()
        crashed = False
        fails = None
        try:
            fails = _batch_fails(run_batch(cfg, self, stop))
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            jobreg.end(jid, ok=not crashed and not fails,
                       summary=tr("videos: {src} -> {out}", src=cfg.get("video_dir"),
                                  out=cfg.get("output_dir")))
            self._finish(seq, lambda: self._running(False))
