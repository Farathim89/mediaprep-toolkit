"""Cut / Edit -> Auto-detect sub-tab (AutoCutMixin)."""
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import (AFTERCREDITS_DIR, AUDIO_LANG_CHOICES, BIT_DEPTHS, CODECS,
                      CREDITS_DIR, DEFAULT_CODEC_LABEL, INTRO_DIR, LEGACY_CODEC_LABELS,
                      OUTPUT_DIR, PREINTRO_DIR, VALID_PRESETS, VIDEO_DIR)
from ..engine.cut import run_batch
from ..i18n import N_, ntr, tr
from ..ui.widgets import (KeyedCombobox, add_tooltip, enable_file_drop, help_button,
                          info_icon)
from .common import _list_media, _same_dir
from .cut_common import (_ask_choice, _detect_notes, _is_plex, margin_widgets, mode_combobox,
                         norm_margin, norm_tpl_margin, tr_key)
from .. import jobs as jobreg
from ..ui import icons


class AutoCutMixin:
    """Cut / Edit -> Auto-detect sub-tab: folders, encoding, detection
    settings, batch run / Review first / Add to queue."""

    def _build_auto_tab(self, auto, saved):
        """Auto-detect sub-tab: folders, encoding, detection & mode, run buttons."""
        # one-time migration: after the template folders moved under input/, use the
        # new defaults once instead of any old root-level paths a user had saved.
        migrate = not saved.get("input_folders_v1", False)
        def _fd(key, const):
            return const if migrate else saved.get(key, const)

        self.dirs = {}
        ahead = ttk.Frame(auto)
        ahead.pack(fill="x", padx=6, pady=(6, 0))
        help_button(ahead, "cut_auto").pack(side="right")
        info_icon(ahead, tr("Match your templates in every video of the Videos "
                            "folder and cut them out - or review the detections "
                            "first.")).pack(side="right")
        folders = ttk.LabelFrame(auto, text=tr("Folders"))
        folders.pack(fill="x", padx=6, pady=(4, 0))
        folders.columnconfigure(1, weight=1)      # left group entry stretches
        folders.columnconfigure(4, weight=1)      # right group entry stretches
        # self.dirs is keyed by these English labels; the widgets show tr(label)
        folder_specs = [
            (N_("Videos folder:"), _fd("video_dir", VIDEO_DIR)),
            (N_("Intro templates:"), _fd("intro_dir", INTRO_DIR)),
            (N_("Credits templates:"), _fd("credits_dir", CREDITS_DIR)),
            (N_("Pre-intro templates:"), _fd("preintro_dir", PREINTRO_DIR)),
            (N_("After-credits templates:"), _fd("aftercredits_dir", AFTERCREDITS_DIR)),
            (N_("Output folder:"), _fd("output_dir", OUTPUT_DIR)),
        ]
        for i, (label, default) in enumerate(folder_specs):
            col = 0 if i < 3 else 3               # first 3 left, next 3 right
            r = i % 3
            ttk.Label(folders, text=tr_key(label)).grid(row=r, column=col, sticky="w",
                                                padx=(4 if col == 0 else 14, 4), pady=2)
            var = tk.StringVar(value=default)
            self.dirs[label] = var
            fe = ttk.Entry(folders, textvariable=var)
            fe.grid(row=r, column=col + 1, sticky="we", padx=4, pady=2)
            enable_file_drop(fe, lambda p, v=var: self._drop_folder(v, p))
            icons.decorate(ttk.Button(folders, text="...", width=3,
                       command=lambda v=var: self._pick_dir(v)), "folder").grid(row=r, column=col + 2, padx=2)

        enc = ttk.LabelFrame(auto, text=tr("Encoding"))
        enc.pack(fill="x", padx=6, pady=(14, 0))
        enc.columnconfigure(1, weight=1)
        # LEFT column = codec / bit depth; RIGHT column = quality / preset
        eleft = ttk.Frame(enc)
        eleft.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        eright = ttk.Frame(enc)
        eright.grid(row=0, column=1, sticky="nw")

        ttk.Label(eleft, text=tr("Codec:")).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        codec_saved = saved.get("codec", DEFAULT_CODEC_LABEL)
        codec_saved = LEGACY_CODEC_LABELS.get(codec_saved, codec_saved)   # old "Auto" -> "Auto - CPU"
        self.codec_var = tk.StringVar(value=codec_saved if codec_saved in CODECS else DEFAULT_CODEC_LABEL)
        codec_cb = KeyedCombobox(eleft, textvariable=self.codec_var, values=list(CODECS.keys()),
                                 state="readonly", width=34)
        codec_cb.grid(row=0, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        add_tooltip(codec_cb, tr(
            "Auto - CPU picks the encoder that matches each source's codec "
            "family (HEVC source -> libx265, AV1 -> SVT-AV1, else libx264) so "
            "the output doesn't balloon in size. Auto - GPU / NVENC does the same "
            "with the NVIDIA encoders (HEVC/AV1/VP9 -> H.265 NVENC, else H.264 NVENC) - "
            "much faster, slightly bigger files at the same quality; falls back to the "
            "CPU encoder on a PC without an NVIDIA GPU. Plex-ready HEVC / H.264 = "
            "near-lossless HEVC or H.264 with Plex-friendly audio in an MKV, with the "
            "video bitrate capped just below the source's so the cut file never "
            "grows (Plex direct-plays it without transcoding)."))
        ttk.Label(eleft, text=tr("Bit depth:")).grid(row=1, column=0, sticky="w", padx=4, pady=2)
        depth_saved = saved.get("bit_depth", "Auto (match source)")
        self.depth_var = tk.StringVar(value=depth_saved if depth_saved in BIT_DEPTHS else "Auto (match source)")
        KeyedCombobox(eleft, textvariable=self.depth_var, values=list(BIT_DEPTHS.keys()),
                      state="readonly", width=22).grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=2)

        # H.264 CRF on top; H.265/AV1 CRF sits on the Preset row so the two
        # quality fields stack and the row reads evenly
        lbl264 = ttk.Label(eright, text=tr("CRF/CQ H.264:"))
        lbl264.grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.crf_var = tk.StringVar(value=str(saved.get("crf", "18")))
        sp264 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf_var, width=5)
        sp264.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp264, tr("Quality for H.264 outputs (lower = better/bigger). 18-20 is a "
                              "good range."))
        lbl265 = ttk.Label(eright, text="H.265 / AV1:")
        lbl265.grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.crf265_var = tk.StringVar(value=str(saved.get("crf_h265", "22")))
        sp265 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf265_var, width=5)
        sp265.grid(row=1, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp265, tr("Quality for H.265 / AV1 outputs. These codecs match H.264's "
                              "quality at a higher number - H.264 CRF 18 is roughly H.265 CRF "
                              "22-23."))
        ttk.Label(eright, text=tr("Preset:")).grid(row=1, column=2, sticky="e", padx=(14, 4), pady=2)
        preset_saved = saved.get("preset", "slow")
        self.preset_var = tk.StringVar(value=preset_saved if preset_saved in VALID_PRESETS else "slow")
        preset_cb = ttk.Combobox(eright, textvariable=self.preset_var, values=VALID_PRESETS,
                                 state="readonly", width=10)
        preset_cb.grid(row=1, column=3, sticky="w", padx=4, pady=2)
        # size/speed impact vs medium at the same CRF (rough, codec-dependent)
        preset_info = {
            "ultrafast": N_("≈ +60-100% size, ~10x faster"),
            "superfast": N_("≈ +40-70% size, ~8x faster"),
            "veryfast": N_("≈ +20-40% size, ~5x faster"),
            "faster": N_("≈ +10-20% size, ~3x faster"),
            "fast": N_("≈ +5-15% size, ~2x faster"),
            "medium": N_("baseline size & speed"),
            "slow": N_("≈ 5-10% smaller, ~2x slower"),
            "slower": N_("≈ 8-12% smaller, ~4x slower"),
            "veryslow": N_("≈ 10-15% smaller, ~8x slower"),
        }
        self.preset_hint = tk.StringVar()
        ttk.Label(eright, textvariable=self.preset_hint, style="Hint.TLabel").grid(
            row=1, column=4, sticky="w", padx=8, pady=2)

        add_tooltip(preset_cb, tr("Encoder speed vs. size (x264/x265 names). NVENC and SVT-AV1 "
                                  "have fewer speed levels, so neighbouring presets map to the "
                                  "same level there and the size/speed hint is only a rough "
                                  "guide."))

        def _upd_preset_hint(*_a):
            hint = preset_info.get(self.preset_var.get(), "")
            hint = tr_key(hint) if hint else ""
            enc = CODECS.get(self.codec_var.get(), "")
            if _is_plex(self.codec_var.get()):
                plex = tr("Plex-ready: bitrate capped below source")
                hint = f"{hint} - {plex}" if hint else plex
            elif hint and ("nvenc" in enc or enc in ("libsvtav1", "auto_gpu")):
                hint += " " + tr("(x264 scale - fewer levels here)")
            self.preset_hint.set(hint)
        preset_cb.bind("<<ComboboxSelected>>", _upd_preset_hint)
        self.codec_var.trace_add("write", _upd_preset_hint)
        _upd_preset_hint()

        det = ttk.LabelFrame(auto, text=tr("Detection & mode"))
        det.pack(fill="x", padx=6, pady=(14, 0))
        det.columnconfigure(1, weight=1)
        # LEFT column = detection settings; RIGHT column = the run options
        left = ttk.Frame(det)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        right = ttk.Frame(det)
        right.grid(row=0, column=1, sticky="nw")

        # ---------- LEFT: keyframe/confidence, mode, segments, match audio ----------
        krow = ttk.Frame(left)
        krow.grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(krow, text=tr("Keyframe every N s (0=cut pts):")).pack(side="left")
        self.kf_var = tk.StringVar(value=str(saved.get("kf_interval", "0")))
        kfen = ttk.Entry(krow, textvariable=self.kf_var, width=6)
        kfen.pack(side="left", padx=(4, 12))
        add_tooltip(kfen, tr("0 = keyframes only at the cut points (smallest file). A number N "
                             "adds a keyframe every N seconds too, for faster seeking in players. "
                             "Used by all three tools (Auto / Manual / Multi)."))
        ttk.Label(krow, text=tr("Min confidence:")).pack(side="left")
        self.conf_var = tk.StringVar(value=str(saved.get("confidence", "0.32")))
        cfen = ttk.Entry(krow, textvariable=self.conf_var, width=6)
        cfen.pack(side="left", padx=4)
        add_tooltip(cfen, tr("How strong (0-1) a template match must be before a segment is "
                             "cut. Higher = safer, but misses more; lower = catches more, risks "
                             "wrong cuts. Auto-detect only."))

        self.mode_var = tk.StringVar(
            value=saved.get("mode", "cut") if saved.get("mode") in ("cut", "inject", "chapters") else "cut")
        ttk.Radiobutton(left, text=tr("Cut intro/credits (+ pre/after) out"),
                        variable=self.mode_var,
                        value="cut").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Radiobutton(left, text=tr("Keep video, inject keyframes only"),
                        variable=self.mode_var,
                        value="inject").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Radiobutton(left, text=tr("Add chapter markers only (no cut - for Plex/Jellyfin skip)"),
                        variable=self.mode_var, value="chapters").grid(row=3, column=0, sticky="w", pady=2)

        segf = ttk.Frame(left)
        segf.grid(row=4, column=0, sticky="w", pady=(6, 2))
        ttk.Label(segf, text=tr("Segments:")).pack(side="left", padx=(0, 6))
        self.use_seg = {}
        for key, txt in self.MULTI_SEGS:
            v = tk.BooleanVar(value=bool(saved.get(f"use_{key}", True)))
            cb = ttk.Checkbutton(segf, text=tr_key(txt), variable=v)
            cb.pack(side="left", padx=(0, 10))
            add_tooltip(cb, tr("Untick to leave the {seg} alone in this run - its templates are "
                               "skipped entirely (also makes the run faster)", seg=tr_key(txt)))
            self.use_seg[key] = v

        langrow = ttk.Frame(left)
        langrow.grid(row=5, column=0, sticky="w", pady=(6, 2))
        ttk.Label(langrow, text=tr("Match templates on audio:")).pack(side="left")
        _lang_labels = list(AUDIO_LANG_CHOICES)
        _saved_ml = saved.get("match_lang")
        _ml_label = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == _saved_ml), _lang_labels[0])
        self.match_lang_var = tk.StringVar(value=_ml_label)
        mlcb = KeyedCombobox(langrow, textvariable=self.match_lang_var, values=_lang_labels,
                             state="readonly", width=20)
        mlcb.pack(side="left", padx=(6, 0))
        add_tooltip(mlcb, tr("Which audio track the segment matching listens to. 'All / "
                             "default' checks every audio track and takes the best "
                             "(language-proof, recommended). Pick a language to match only that "
                             "track (falls back to all if a file doesn't have it)."))

        dmrow = ttk.Frame(left)
        dmrow.grid(row=6, column=0, sticky="w", pady=(6, 2))
        ttk.Label(dmrow, text=tr("Match templates by:")).pack(side="left")
        dm = saved.get("detect_mode")
        self.detect_mode_var = tk.StringVar(value=dm if dm in ("audio", "visual", "both")
                                            else "both")
        dmcb = mode_combobox(dmrow, self.detect_mode_var)
        dmcb.pack(side="left", padx=(6, 0))
        add_tooltip(dmcb, tr("Audio = match the template's sound (the classic matcher); "
                             "Visual = match its PICTURES (also finds an opening whose audio "
                             "differs, e.g. a dub, or a template without audio); Audio + "
                             "Visual = both - a match confirmed by both is marked "
                             "'audio + visual', a disagreement gets a ⚠. Used by Start batch, "
                             "Review first, Manual cut and Multi cut."))

        # safety margin / template edges (engine cfg margin_frames / template_margin);
        # the Plex-style scan uses the same safety margin
        self.margin_frames_var = tk.StringVar(
            value=str(norm_margin(saved.get("margin_frames"))))
        self.template_margin_var = tk.StringVar(
            value=norm_tpl_margin(saved.get("template_margin")))
        margin_widgets(left, self.margin_frames_var, self.template_margin_var).grid(
            row=7, column=0, sticky="w", pady=(6, 2))

        # ---------- RIGHT: run options (one per row - translations run longer) ----------
        self.move_done_var = tk.BooleanVar(value=bool(saved.get("move_done", False)))
        mdcb = ttk.Checkbutton(right, text=tr("Move finished videos to a 'done' subfolder"),
                               variable=self.move_done_var)
        mdcb.grid(row=0, column=0, sticky="w", pady=1)
        add_tooltip(mdcb, tr("After a video is successfully processed, move its SOURCE file "
                             "into videos/done so you can see what's left. Skipped or failed "
                             "videos stay put."))

        self.skip_incomplete_var = tk.BooleanVar(value=bool(saved.get("skip_incomplete", False)))
        sicb = ttk.Checkbutton(right, text=tr("Skip the episode if an enabled segment isn't found"),
                               variable=self.skip_incomplete_var)
        sicb.grid(row=1, column=0, sticky="w", pady=1)
        add_tooltip(sicb, tr("If a ticked segment matches too weakly on an episode, leave that "
                             "episode untouched instead of outputting a partial cut. It stays in "
                             "the videos folder so you can spot and handle it."))

        self.intro_from_start_var = tk.BooleanVar(value=bool(saved.get("intro_from_start", False)))
        ifcb = ttk.Checkbutton(right, text=tr("Intro: cut from file start to end of intro"),
                               variable=self.intro_from_start_var)
        ifcb.grid(row=2, column=0, sticky="w", pady=1)
        add_tooltip(ifcb, tr("Extend the intro removal back to 0:00, so any recap / cold-open "
                             "before the intro is cut too. Only the intro END position is "
                             "detected."))

        self.credits_to_end_var = tk.BooleanVar(value=bool(saved.get("credits_to_end", False)))
        cecb = ttk.Checkbutton(right, text=tr("Credits: cut from credits start to file end"),
                               variable=self.credits_to_end_var)
        cecb.grid(row=3, column=0, sticky="w", pady=1)
        add_tooltip(cecb, tr("Extend the credits removal to the end of the file, so credits plus "
                             "everything after (next-episode preview, etc.) are all cut. Only "
                             "the credits START position is detected."))

        self.trim_match_var = tk.BooleanVar(value=bool(saved.get("trim_to_match", False)))
        tmcb = ttk.Checkbutton(right, text=tr("Trim the cut to where the audio still matches"),
                               variable=self.trim_match_var)
        tmcb.grid(row=4, column=0, sticky="w", pady=1)
        add_tooltip(tmcb, tr("Normally the cut length equals the template length. If an "
                             "episode's segment is genuinely shorter, this shortens the cut to "
                             "where the template stops matching, so it won't chop into the "
                             "episode (never below half). Greyed out while 'Anchor the cut' is "
                             "ticked - anchoring overrides it."))
        self._trim_cb = tmcb

        anrow = ttk.Frame(right)
        anrow.grid(row=5, column=0, sticky="w", pady=1)
        self.anchor_var = tk.BooleanVar(value=bool(saved.get("anchor_cut", False)))
        ancb = ttk.Checkbutton(anrow, text=tr("Anchor the cut to start & end"),
                               variable=self.anchor_var, command=self._upd_trim_state)
        self._upd_trim_state()
        ancb.pack(side="left")
        add_tooltip(ancb, tr("Match just the first and last few seconds of the template "
                             "separately and cut between them, so the cut follows each episode's "
                             "real boundaries even when the length varies. Uses your existing "
                             "templates. (Supersedes Trim.)"))
        ttk.Label(anrow, text=" " + tr("ends of")).pack(side="left")
        self.anchor_secs_var = tk.StringVar(value=str(saved.get("anchor_secs", "15")))
        ansp = ttk.Spinbox(anrow, from_=5, to=40, textvariable=self.anchor_secs_var, width=4)
        ansp.pack(side="left", padx=(4, 2))
        add_tooltip(ansp, tr("How many seconds at each end of the template to match (5-40)"))
        ttk.Label(anrow, text=tr("sec")).pack(side="left")

        self.subs_langs = set(saved.get("subs_langs") or (["eng", "und"]
                              if saved.get("subs_english") else []))
        self.subs_filter_var = tk.BooleanVar(
            value=bool(saved.get("subs_filter", saved.get("subs_english", False))))
        sfcb = ttk.Checkbutton(right, text=tr("Keep only chosen subtitle languages"),
                               variable=self.subs_filter_var, command=self._upd_subs_label)
        sfcb.grid(row=6, column=0, sticky="w", pady=(4, 1))
        add_tooltip(sfcb, tr("Drop the subtitle languages you don't want (video and all audio "
                             "tracks are untouched). 'und' = untagged/unknown tracks, which are "
                             "often English or a forced track - keep it if unsure."))
        subrow = ttk.Frame(right)
        subrow.grid(row=7, column=0, sticky="w", pady=1)
        icons.decorate(ttk.Button(subrow, text=tr("Choose languages..."), command=self._choose_subs), "filter").pack(side="left", padx=(0, 6))
        self.subs_lbl = tk.StringVar()
        ttk.Label(subrow, textvariable=self.subs_lbl, style="Hint.TLabel").pack(side="left")
        self._upd_subs_label()

        runrow = ttk.Frame(auto)
        runrow.pack(fill="x", padx=6, pady=(10, 6))
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
        rmrow.pack(fill="x", padx=6, pady=(0, 6))
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

        # read-only summary of the shared encoding settings, shown on the Manual
        # and Multi sub-tabs (they use Cut / Edit → Auto-detect's Encoding group)
        self.enc_summary = tk.StringVar()
        for v in (self.codec_var, self.crf_var, self.crf265_var, self.preset_var,
                  self.depth_var, self.dirs["Output folder:"]):
            v.trace_add("write", lambda *a: self._upd_enc_summary())
        self._upd_enc_summary()

    def _upd_trim_state(self):
        """'Trim to match' does nothing while 'Anchor' is on (anchor overrides
        it), so grey it out then."""
        if hasattr(self, "_trim_cb"):
            self._trim_cb.configure(state="disabled" if self.anchor_var.get() else "normal")

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title=tr("Select folder"))
        if path:
            var.set(path)

    def _drop_folder(self, var, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isdir(path):
            var.set(path)

    def _choose_subs(self):
        folder = self.dirs["Videos folder:"].get().strip().strip('"')
        files = _list_media(folder) if folder else []
        if not files:
            messagebox.showinfo(tr("No videos"), tr("Set the Videos folder first so I can scan "
                                                    "which subtitle languages are in the "
                                                    "files."))
            return
        self._open_subs_dialog(files)

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
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True)
        jid = self._jid = jobreg.begin(tr("Cut / Edit review (detect only)"),
                                       stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._review_worker, args=(files, cfg, mode, jid),
                         daemon=True).start()

    def _review_worker(self, files, cfg, mode, jid=None):
        results = {}
        crashed = False
        n = len(files)
        n_warn = 0
        have = None         # kinds that have templates at all
        try:
            self.log(f"[REVIEW] detecting in {n} video(s) - nothing is cut")
            results, have, n_warn = self._detect_loop(files, cfg, "REVIEW", review=True)
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] review crashed: {e!r}")
        finally:
            stopped = self.stop_event.is_set()
            jobreg.end(jid, ok=not crashed and not stopped,
                       summary=tr("{done}/{n} reviewed, {warn} need a look",
                                  done=len(results), n=n, warn=n_warn))
            self.after(0, lambda: self._review_done(files, cfg, mode, results, stopped, have))

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
        use = cfg.get("use", {})
        entries = []
        for f in files:
            if f not in results:
                continue
            res = results[f]
            entries.append((f, self._multi_ranges_from_detect(res, cfg),
                            "; ".join(_detect_notes(res, use, have, ui=True)), True,
                            res or None))
        _new, n_upd, first, first_warn = self._multi_apply_results(entries)
        n_warn = sum(1 for e in entries if e[2])
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
        if cfg.get("mode") != "cut":
            self.log(f"[REVIEW] note: Multi cut always CUTS - the '{cfg.get('mode')}' run mode "
                     "applies to Start batch only.")
        self.status_var.set(shown)

    def _batch_cfg(self, check_output=True):
        """The run_batch cfg from the Auto-detect sub-tab, or None after an
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
            "kf_interval": kf, "confidence": conf, "mode": self.mode_var.get(),
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
        self.stop_event.clear()
        self._running(True, _list_media(cfg["video_dir"]) if cfg["move_done"] else None)
        folder = os.path.basename(os.path.normpath(cfg["video_dir"])) or cfg["video_dir"]
        jid = self._jid = jobreg.begin(tr("Cut / Edit batch - {folder}", folder=folder),
                                       stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._worker, args=(cfg, jid), daemon=True).start()
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
        crashed = False
        try:
            run_batch(cfg, self, self.stop_event)
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            jobreg.end(jid, ok=not crashed,
                       summary=tr("videos: {src} -> {out}", src=cfg.get("video_dir"),
                                  out=cfg.get("output_dir")))
            self.after(0, lambda: self._running(False))
