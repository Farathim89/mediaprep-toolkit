"""Cut / Edit -> Settings sub-tab (CutSettingsMixin).

The folders, encoding, detection & mode and subtitle / output options that
every Cut / Edit tool uses: Auto-detect's Start batch and Review first,
Manual cut and Multi cut. The variables built here (self.dirs, codec_var,
crf_var, cut_method_var, mode_var, use_seg, ...) keep their names - the
batch cfg (_batch_cfg), the presets (PRESET_KEYS), snapshot() and the jobs
read them. The other sub-tabs show a read-only summary of them with a
'Change...' button that opens this sub-tab (_settings_summary_row).

Cut method (Encoding card): 'smart' = smart cut (engine/smartcut.py - the
source's packets copied, only the frames around each cut re-encoded) for
EVERY cut: Manual / Multi cut (cfg["smart_cut"]) and Start batch's Cut run
mode (cfg mode "smartcut"); the codec / CRF / bit depth settings are then
only the fallback for a file that can't be smart-cut. 'reencode' = the
whole video re-encoded with them (the default)."""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import (AFTERCREDITS_DIR, AUDIO_LANG_CHOICES, BIT_DEPTHS, CODECS,
                      CREDITS_DIR, DEFAULT_CODEC_LABEL, INTRO_DIR, LEGACY_CODEC_LABELS,
                      OUTPUT_DIR, PREINTRO_DIR, VALID_PRESETS, VIDEO_DIR)
from ..i18n import N_, tr
from ..ui.helpwin import help_button
from ..ui.tooltips import add_tooltip, info_icon
from ..ui.widgets import KeyedCombobox, auto_wrap, enable_file_drop, wrap_label
from .common import _list_media
from .cut_common import (_MODE_LABELS, _is_plex, margin_widgets, mode_combobox,
                         norm_margin, norm_tpl_margin, tr_key)
from ..ui import icons

# run mode key -> its label (the radio buttons and the Auto-detect summary)
RUN_MODES = (("cut", N_("Cut intro/credits (+ pre/after) out")),
             ("inject", N_("Keep video, inject keyframes only")),
             ("chapters", N_("Add chapter markers only (no cut - for Plex/Jellyfin skip)")))

# cut method key (settings / presets "cut_method") -> its label
CUT_METHODS = (("smart", N_("Smart cut (original quality)")),
               ("reencode", N_("Re-encode (codec settings below)")))


def norm_cut_method(saved):
    """The cut method of saved settings / a preset: 'smart' or 'reencode'
    (the default; an old cfg-style mode 'smartcut' means smart)."""
    if saved.get("cut_method") in ("smart", "reencode"):
        return saved["cut_method"]
    return "smart" if saved.get("mode") == "smartcut" else "reencode"

# size/speed impact of an encoder preset vs medium at the same CRF (rough,
# codec-dependent)
_PRESET_INFO = {
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

_CARD_GAP = 14          # space between the cards (px at 100 %)
_HALF_GAP = 12          # ... and between the two halves of a card


def _halves(card, row=0):
    """Two equally wide columns inside a card (frames), in grid row `row`.
    Every card is as wide as the page, so the right halves of all cards
    line up."""
    card.columnconfigure(0, weight=1, uniform="half")
    card.columnconfigure(1, weight=1, uniform="half")
    left = ttk.Frame(card)
    left.grid(row=row, column=0, sticky="nw", padx=(0, _HALF_GAP))
    right = ttk.Frame(card)
    right.grid(row=row, column=1, sticky="nw", padx=(_HALF_GAP, 0))
    return left, right


class CutSettingsMixin:
    """Cut / Edit -> Settings sub-tab: Folders, Encoding, Detection & mode,
    Subtitles & output options."""

    def _build_settings_tab(self, page, saved):
        """Settings sub-tab: the cards with every shared Cut / Edit setting."""
        # one-time migration: after the template folders moved under input/, use the
        # new defaults once instead of any old root-level paths a user had saved.
        migrate = not saved.get("input_folders_v1", False)

        def _fd(key, const):
            return const if migrate else saved.get(key, const)

        page.columnconfigure(0, weight=1)
        head = ttk.Frame(page)
        head.grid(row=0, column=0, sticky="we", padx=6, pady=(6, 0))
        help_button(head, "cut_settings").pack(side="right")
        auto_wrap(ttk.Label(head, style="Hint.TLabel", justify="left", text=tr(
            "The folders, encoding and detection settings every Cut / Edit tool uses: "
            "Start batch and Review first on Auto-detect, Manual cut and Multi cut. "
            "Show presets (the row above) save and load them.")), width=860).pack(
            side="left", anchor="w")

        # ---------------- Folders ----------------
        self.dirs = {}
        folders = ttk.LabelFrame(page, text=tr("Folders"))
        folders.grid(row=1, column=0, sticky="we", padx=6, pady=(10, 0))
        self._folders_card = folders
        fleft, fright = _halves(folders)
        # self.dirs is keyed by these English labels; the widgets show tr(label)
        folder_specs = [
            (N_("Videos folder:"), _fd("video_dir", VIDEO_DIR)),
            (N_("Intro templates:"), _fd("intro_dir", INTRO_DIR)),
            (N_("Credits templates:"), _fd("credits_dir", CREDITS_DIR)),
            (N_("Pre-intro templates:"), _fd("preintro_dir", PREINTRO_DIR)),
            (N_("After-credits templates:"), _fd("aftercredits_dir", AFTERCREDITS_DIR)),
            (N_("Output folder:"), _fd("output_dir", OUTPUT_DIR)),
        ]
        for half in (fleft, fright):
            half.grid_configure(sticky="we")
            half.columnconfigure(1, weight=1)       # the entries stretch
        for i, (label, default) in enumerate(folder_specs):
            half = fleft if i < 3 else fright       # first 3 left, next 3 right
            r = i % 3
            ttk.Label(half, text=tr_key(label)).grid(row=r, column=0, sticky="w",
                                                     padx=(4, 4), pady=2)
            var = tk.StringVar(value=default)
            self.dirs[label] = var
            fe = ttk.Entry(half, textvariable=var)
            fe.grid(row=r, column=1, sticky="we", padx=4, pady=2)
            enable_file_drop(fe, lambda p, v=var: self._drop_folder(v, p))
            icons.decorate(ttk.Button(half, text="...", width=3,
                                      command=lambda v=var: self._pick_dir(v)),
                           "folder").grid(row=r, column=2, padx=2)

        # ---------------- Encoding ----------------
        enc = ttk.LabelFrame(page, text=tr("Encoding"))
        enc.grid(row=2, column=0, sticky="we", padx=6, pady=(_CARD_GAP, 0))
        # TOP (full width): how every cut is made - smart cut or re-encode
        cm = ttk.Frame(enc)
        cm.grid(row=0, column=0, columnspan=2, sticky="we")
        cmrow = ttk.Frame(cm)
        cmrow.pack(anchor="w")
        ttk.Label(cmrow, text=tr("Cut method:")).pack(side="left", padx=4, pady=2)
        self.cut_method_var = tk.StringVar(value=norm_cut_method(saved))
        smart_info = tr(
            "Smart cut works on H.264 and HEVC video in MKV, MP4 and MOV files. The video "
            "and audio are copied as they are - only the frames from each cut point to the "
            "next keyframe (a few seconds) are re-encoded, by the CPU encoder of the same "
            "codec (x264 / x265) at near-lossless quality with the Preset below, and every "
            "join is checked against the source. A file it can't handle (another codec or "
            "container, interlaced video, an unusual pixel format) or a join that fails the "
            "check is re-encoded the normal way with the codec settings below - the Log "
            "shows a [WARN] line. Used by Manual cut, Multi cut and Start batch's Cut run "
            "mode.")
        for i, (key, label) in enumerate(CUT_METHODS):
            rb = ttk.Radiobutton(cmrow, text=tr_key(label), variable=self.cut_method_var,
                                 value=key)
            rb.pack(side="left", padx=(6 if i == 0 else 18, 0), pady=2)
            add_tooltip(rb, smart_info if key == "smart" else tr(
                "Re-encode the whole video with the codec, bit depth, CRF and preset below "
                "(any source, any output codec; slower, and the quality is the encoder's)."))
        info_icon(cmrow, smart_info).pack(side="left", padx=(10, 0))
        self.cut_method_hint = tk.StringVar()
        auto_wrap(ttk.Label(cm, textvariable=self.cut_method_hint, style="Hint.TLabel",
                            justify="left"), width=820).pack(anchor="w", padx=4, pady=(0, 6))
        eleft, eright = _halves(enc, row=1)
        # LEFT = codec / bit depth / keyframes; RIGHT = quality / preset. With
        # smart cut, codec / bit depth / keyframes / CRF are only the fallback:
        # their labels are greyed (the boxes stay editable)
        self._fallback_lbls = []
        lbl = ttk.Label(eleft, text=tr("Codec:"))
        lbl.grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._fallback_lbls.append(lbl)
        codec_saved = saved.get("codec", DEFAULT_CODEC_LABEL)
        codec_saved = LEGACY_CODEC_LABELS.get(codec_saved, codec_saved)   # old "Auto" -> "Auto - CPU"
        self.codec_var = tk.StringVar(value=codec_saved if codec_saved in CODECS
                                      else DEFAULT_CODEC_LABEL)
        codec_cb = KeyedCombobox(eleft, textvariable=self.codec_var, values=list(CODECS.keys()),
                                 state="readonly", width=34)
        codec_cb.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self._codec_cb = codec_cb
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
        lbl = ttk.Label(eleft, text=tr("Bit depth:"))
        lbl.grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._fallback_lbls.append(lbl)
        depth_saved = saved.get("bit_depth", "Auto (match source)")
        self.depth_var = tk.StringVar(value=depth_saved if depth_saved in BIT_DEPTHS
                                      else "Auto (match source)")
        KeyedCombobox(eleft, textvariable=self.depth_var, values=list(BIT_DEPTHS.keys()),
                      state="readonly", width=22).grid(row=1, column=1, sticky="w", padx=4, pady=2)
        # (own row: its long label would push the combo boxes to the right)
        krow = ttk.Frame(eleft)
        krow.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 2))
        lbl = ttk.Label(krow, text=tr("Keyframe every N s (0=cut pts):"))
        lbl.pack(side="left")
        self._fallback_lbls.append(lbl)
        self.kf_var = tk.StringVar(value=str(saved.get("kf_interval", "0")))
        kfen = ttk.Entry(krow, textvariable=self.kf_var, width=6)
        kfen.pack(side="left", padx=(6, 0))
        add_tooltip(kfen, tr("0 = keyframes only at the cut points (smallest file). A number N "
                             "adds a keyframe every N seconds too, for faster seeking in players. "
                             "Used by all three tools (Auto / Manual / Multi)."))

        # H.264 CRF on top; H.265/AV1 CRF sits on the Preset row so the two
        # quality fields stack and the row reads evenly
        lbl = ttk.Label(eright, text=tr("CRF/CQ H.264:"))
        lbl.grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._fallback_lbls.append(lbl)
        self.crf_var = tk.StringVar(value=str(saved.get("crf", "18")))
        sp264 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf_var, width=5)
        sp264.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp264, tr("Quality for H.264 outputs (lower = better/bigger). 18-20 is a "
                              "good range."))
        lbl = ttk.Label(eright, text="H.265 / AV1:")
        lbl.grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._fallback_lbls.append(lbl)
        self.crf265_var = tk.StringVar(value=str(saved.get("crf_h265", "22")))
        sp265 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf265_var, width=5)
        sp265.grid(row=1, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp265, tr("Quality for H.265 / AV1 outputs. These codecs match H.264's "
                              "quality at a higher number - H.264 CRF 18 is roughly H.265 CRF "
                              "22-23."))
        ttk.Label(eright, text=tr("Preset:")).grid(row=1, column=2, sticky="e", padx=(14, 4),
                                                  pady=2)
        preset_saved = saved.get("preset", "slow")
        self.preset_var = tk.StringVar(value=preset_saved if preset_saved in VALID_PRESETS
                                       else "slow")
        preset_cb = ttk.Combobox(eright, textvariable=self.preset_var, values=VALID_PRESETS,
                                 state="readonly", width=10)
        preset_cb.grid(row=1, column=3, sticky="w", padx=4, pady=2)
        add_tooltip(preset_cb, tr("Encoder speed vs. size (x264/x265 names). NVENC and SVT-AV1 "
                                  "have fewer speed levels, so neighbouring presets map to the "
                                  "same level there and the size/speed hint is only a rough "
                                  "guide."))
        # the size/speed hint under the preset (wraps: it never widens the card)
        self.preset_hint = tk.StringVar()
        auto_wrap(ttk.Label(eright, textvariable=self.preset_hint, style="Hint.TLabel",
                            justify="left"), width=280).grid(
            row=2, column=2, columnspan=2, sticky="nw", padx=4, pady=(2, 2))

        def _upd_preset_hint(*_a):
            hint = _PRESET_INFO.get(self.preset_var.get(), "")
            hint = tr_key(hint) if hint else ""
            encoder = CODECS.get(self.codec_var.get(), "")
            if _is_plex(self.codec_var.get()):
                plex = tr("Plex-ready: bitrate capped below source")
                hint = f"{hint} - {plex}" if hint else plex
            elif hint and ("nvenc" in encoder or encoder in ("libsvtav1", "auto_gpu")):
                hint += " " + tr("(x264 scale - fewer levels here)")
            self.preset_hint.set(hint)
        preset_cb.bind("<<ComboboxSelected>>", _upd_preset_hint)
        self.codec_var.trace_add("write", _upd_preset_hint)
        _upd_preset_hint()

        # ---------------- Detection & mode ----------------
        det = ttk.LabelFrame(page, text=tr("Detection & mode"))
        det.grid(row=3, column=0, sticky="we", padx=6, pady=(_CARD_GAP, 0))
        left, right = _halves(det)

        # LEFT: run mode (Start batch only), segments, min confidence
        mrow = ttk.Frame(left)
        mrow.grid(row=0, column=0, sticky="w", pady=(2, 0))
        ttk.Label(mrow, text=tr("Run mode:")).pack(side="left")
        ttk.Label(mrow, text=tr("(Start batch on Auto-detect only)"),
                  style="Hint.TLabel").pack(side="left", padx=(6, 0))
        info_icon(mrow, tr("The run mode is used by Start batch on the Auto-detect "
                           "sub-tab. Manual cut and Multi cut always cut; Review first only "
                           "detects and loads the results into Multi cut.")).pack(
            side="left", padx=(6, 0))
        self.mode_var = tk.StringVar(
            value=saved.get("mode", "cut") if saved.get("mode") in ("cut", "inject", "chapters")
            else "cut")
        self.cut_how_var = tk.StringVar()       # "(smart cut)" / "(re-encode)"
        for i, (key, label) in enumerate(RUN_MODES, 1):
            rrow = ttk.Frame(left)
            rrow.grid(row=i, column=0, sticky="w", padx=(4, 0), pady=1)
            ttk.Radiobutton(rrow, text=tr_key(label), variable=self.mode_var,
                            value=key).pack(side="left")
            if key == "cut":            # Cut cuts with the Encoding card's cut method
                how = ttk.Label(rrow, textvariable=self.cut_how_var, style="Hint.TLabel")
                how.pack(side="left", padx=(6, 0))
                add_tooltip(how, tr("Cut uses the Cut method chosen at the top of the "
                                    "Encoding card (Smart cut or Re-encode)."))

        segf = ttk.Frame(left)
        segf.grid(row=4, column=0, sticky="w", pady=(8, 2))
        ttk.Label(segf, text=tr("Segments:")).pack(side="left", padx=(0, 6))
        self.use_seg = {}
        for key, txt in self.MULTI_SEGS:
            v = tk.BooleanVar(value=bool(saved.get(f"use_{key}", True)))
            cb = ttk.Checkbutton(segf, text=tr_key(txt), variable=v)
            cb.pack(side="left", padx=(0, 10))
            add_tooltip(cb, tr("Untick to leave the {seg} alone in this run - its templates are "
                               "skipped entirely (also makes the run faster)", seg=tr_key(txt)))
            self.use_seg[key] = v

        crow = ttk.Frame(left)
        crow.grid(row=5, column=0, sticky="w", pady=(6, 2))
        ttk.Label(crow, text=tr("Min confidence:")).pack(side="left")
        self.conf_var = tk.StringVar(value=str(saved.get("confidence", "0.32")))
        cfen = ttk.Entry(crow, textvariable=self.conf_var, width=6)
        cfen.pack(side="left", padx=(6, 0))
        add_tooltip(cfen, tr("How strong (0-1) a template match must be before a segment is "
                             "cut. Higher = safer, but misses more; lower = catches more, risks "
                             "wrong cuts. Auto-detect only."))

        # RIGHT: how templates are matched, margins, trim / anchor
        dmrow = ttk.Frame(right)
        dmrow.grid(row=0, column=0, sticky="w", pady=(2, 2))
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

        langrow = ttk.Frame(right)
        langrow.grid(row=1, column=0, sticky="w", pady=(6, 2))
        ttk.Label(langrow, text=tr("Match templates on audio:")).pack(side="left")
        _lang_labels = list(AUDIO_LANG_CHOICES)
        _saved_ml = saved.get("match_lang")
        _ml_label = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == _saved_ml),
                         _lang_labels[0])
        self.match_lang_var = tk.StringVar(value=_ml_label)
        mlcb = KeyedCombobox(langrow, textvariable=self.match_lang_var, values=_lang_labels,
                             state="readonly", width=20)
        mlcb.pack(side="left", padx=(6, 0))
        add_tooltip(mlcb, tr("Which audio track the segment matching listens to. 'All / "
                             "default' checks every audio track and takes the best "
                             "(language-proof, recommended). Pick a language to match only that "
                             "track (falls back to all if a file doesn't have it)."))

        # safety margin / template edges (engine cfg margin_frames / template_margin);
        # the Plex-style scan uses the same safety margin
        self.margin_frames_var = tk.StringVar(
            value=str(norm_margin(saved.get("margin_frames"))))
        self.template_margin_var = tk.StringVar(
            value=norm_tpl_margin(saved.get("template_margin")))
        margin_widgets(right, self.margin_frames_var, self.template_margin_var).grid(
            row=2, column=0, sticky="w", pady=(6, 2))

        self.trim_match_var = tk.BooleanVar(value=bool(saved.get("trim_to_match", False)))
        tmcb = ttk.Checkbutton(right, text=tr("Trim the cut to where the audio still matches"),
                               variable=self.trim_match_var)
        tmcb.grid(row=3, column=0, sticky="w", pady=(6, 1))
        add_tooltip(tmcb, tr("Normally the cut length equals the template length. If an "
                             "episode's segment is genuinely shorter, this shortens the cut to "
                             "where the template stops matching, so it won't chop into the "
                             "episode (never below half). Greyed out while 'Anchor the cut' is "
                             "ticked - anchoring overrides it."))
        self._trim_cb = tmcb

        anrow = ttk.Frame(right)
        anrow.grid(row=4, column=0, sticky="w", pady=1)
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

        # ---------------- Subtitles & output options ----------------
        outc = ttk.LabelFrame(page, text=tr("Subtitles & output options"))
        outc.grid(row=4, column=0, sticky="we", padx=6, pady=(_CARD_GAP, 6))
        oleft, oright = _halves(outc)

        # LEFT: subtitle language filter, move finished sources
        self.subs_langs = set(saved.get("subs_langs") or (["eng", "und"]
                              if saved.get("subs_english") else []))
        self.subs_filter_var = tk.BooleanVar(
            value=bool(saved.get("subs_filter", saved.get("subs_english", False))))
        sfcb = ttk.Checkbutton(oleft, text=tr("Keep only chosen subtitle languages"),
                               variable=self.subs_filter_var, command=self._upd_subs_label)
        sfcb.grid(row=0, column=0, sticky="w", pady=(2, 1))
        add_tooltip(sfcb, tr("Drop the subtitle languages you don't want (video and all audio "
                             "tracks are untouched). 'und' = untagged/unknown tracks, which are "
                             "often English or a forced track - keep it if unsure."))
        subrow = ttk.Frame(oleft)
        subrow.grid(row=1, column=0, sticky="w", pady=1)
        icons.decorate(ttk.Button(subrow, text=tr("Choose languages..."),
                                  command=self._choose_subs), "filter").pack(
            side="left", padx=(0, 6))
        self.subs_lbl = tk.StringVar()
        ttk.Label(subrow, textvariable=self.subs_lbl, style="Hint.TLabel").pack(side="left")
        self._upd_subs_label()

        self.move_done_var = tk.BooleanVar(value=bool(saved.get("move_done", False)))
        mdcb = ttk.Checkbutton(oleft, text=tr("Move finished videos to a 'done' subfolder"),
                               variable=self.move_done_var)
        mdcb.grid(row=2, column=0, sticky="w", pady=(6, 1))
        add_tooltip(mdcb, tr("After a video is successfully processed, move its SOURCE file "
                             "into videos/done so you can see what's left. Skipped or failed "
                             "videos stay put."))

        # RIGHT: what a cut covers, partial results
        self.intro_from_start_var = tk.BooleanVar(value=bool(saved.get("intro_from_start", False)))
        ifcb = ttk.Checkbutton(oright, text=tr("Intro: cut from file start to end of intro"),
                               variable=self.intro_from_start_var)
        ifcb.grid(row=0, column=0, sticky="w", pady=(2, 1))
        add_tooltip(ifcb, tr("Extend the intro removal back to 0:00, so any recap / cold-open "
                             "before the intro is cut too. Only the intro END position is "
                             "detected."))

        self.credits_to_end_var = tk.BooleanVar(value=bool(saved.get("credits_to_end", False)))
        cecb = ttk.Checkbutton(oright, text=tr("Credits: cut from credits start to file end"),
                               variable=self.credits_to_end_var)
        cecb.grid(row=1, column=0, sticky="w", pady=1)
        add_tooltip(cecb, tr("Extend the credits removal to the end of the file, so credits plus "
                             "everything after (next-episode preview, etc.) are all cut. Only "
                             "the credits START position is detected."))

        self.skip_incomplete_var = tk.BooleanVar(value=bool(saved.get("skip_incomplete", False)))
        sicb = ttk.Checkbutton(oright, text=tr("Skip the episode if an enabled segment isn't found"),
                               variable=self.skip_incomplete_var)
        sicb.grid(row=2, column=0, sticky="w", pady=(6, 1))
        add_tooltip(sicb, tr("If a ticked segment matches too weakly on an episode, leave that "
                             "episode untouched instead of outputting a partial cut. It stays in "
                             "the videos folder so you can spot and handle it."))

        # read-only summaries of these settings for the other sub-tabs
        self.enc_summary = tk.StringVar()       # encoding (+ output folder)
        self.cut_summary = tk.StringVar()       # ... + "Manual / Multi always CUT"
        self.auto_summary = tk.StringVar()      # Auto-detect: videos, encoding, mode
        watched = [self.codec_var, self.crf_var, self.crf265_var, self.preset_var,
                   self.depth_var, self.dirs["Output folder:"], self.dirs["Videos folder:"],
                   self.mode_var, self.detect_mode_var, self.conf_var, self.cut_method_var,
                   *self.use_seg.values()]
        for v in watched:
            v.trace_add("write", lambda *a: self._upd_enc_summary())
        self.cut_method_var.trace_add("write", lambda *a: self._upd_cut_method())
        self._upd_cut_method()
        self._upd_enc_summary()

    def smart_cut_on(self):
        """True when every cut is a smart cut (Cut method = Smart cut)."""
        return self.cut_method_var.get() == "smart"

    def _upd_cut_method(self):
        """The Encoding card for the chosen cut method: its hint, the greyed
        fallback-only labels, the '(smart cut)' note beside run mode Cut."""
        smart = self.smart_cut_on()
        self.cut_method_hint.set(tr(
            "Copies the video and audio untouched and re-encodes only a few seconds around "
            "each cut: the original quality, about the size of the source, and much faster. "
            "Codec, bit depth, CRF and keyframes below are used only when a file can't be "
            "smart-cut (it is then re-encoded with them); Preset also sets the speed of "
            "those few seconds.") if smart else tr(
            "Re-encodes the whole video with the codec, bit depth, CRF and preset below."))
        for lbl in self._fallback_lbls:
            lbl.state(["disabled"] if smart else ["!disabled"])
        self.cut_how_var.set(tr("(smart cut)") if smart else tr("(re-encode)"))

    # ---------------- summaries on the other sub-tabs ----------------
    def _upd_enc_summary(self):
        codec = tr_key(self.codec_var.get()).split(" (")[0]
        if _is_plex(self.codec_var.get()):
            codec += tr(" (MKV, Plex-friendly audio, bitrate capped below the source - "
                        "files never grow)")
        out = self.dirs["Output folder:"].get().strip() or tr("(not set)")
        args = dict(codec=codec, crf=self.crf_var.get(), crf265=self.crf265_var.get(),
                    preset=self.preset_var.get(), depth=tr_key(self.depth_var.get()), out=out)
        plain = tr("Encoding in use: {codec}, CRF {crf} (H.265/AV1 {crf265}), preset {preset}, "
                   "bit depth {depth} -> {out}.", **args)
        smart = self.smart_cut_on()
        enc = tr("Encoding in use: Smart cut (original quality), fallback: {codec}, CRF {crf} "
                 "(H.265/AV1 {crf265}), preset {preset}, bit depth {depth} -> {out}.",
                 **args) if smart else plain
        self.enc_summary.set(enc)
        self.cut_summary.set(enc + " " + tr("Manual / Multi always CUT - the run mode "
                                            "applies to Auto-detect only."))
        videos = self.dirs["Videos folder:"].get().strip() or tr("(not set)")
        mode_key = self.mode_var.get()
        mode = tr_key(dict(RUN_MODES).get(mode_key, RUN_MODES[0][1]))
        if mode_key == "cut":
            mode += " " + (tr("(smart cut)") if smart else tr("(re-encode)"))
        segs = [tr_key(txt) for key, txt in self.MULTI_SEGS if self.use_seg[key].get()]
        by = _MODE_LABELS.get(self.detect_mode_var.get(), _MODE_LABELS["both"])
        self.auto_summary.set("\n".join((
            f"{tr('Videos folder:')} {videos}",
            # (inject keyframes always re-encodes the whole video)
            enc if mode_key == "cut" else plain,
            tr("Run mode: {mode} - applies to Start batch only (Review first only detects).",
               mode=mode),
            tr("Detection: match templates by {by}, segments {segs}, min confidence {conf}.",
               by=tr_key(by), segs=", ".join(segs) or "-", conf=self.conf_var.get().strip()))))

    def show_settings(self):
        """Switch Cut / Edit to its Settings sub-tab."""
        try:
            self._rnb.select(self._settings_page)
        except (AttributeError, tk.TclError):
            pass

    def _settings_summary_row(self, parent, variable, info=None, wrap=420, below=False):
        """'ⓘ <summary> [Change...]' - the read-only summary of these settings
        on another sub-tab, with a button that opens the Settings sub-tab
        (beside the text, or below it). wrap = the text's wrap width (px at
        100 %). Returns (frame, Change button); the caller places the frame."""
        row = ttk.Frame(parent)
        if info:
            info_icon(row, info).grid(row=0, column=0, sticky="nw", padx=(0, 4))
        # (a long folder path is shortened in the middle - it never widens the
        # page; the whole text shows on hover)
        wrap_label(row, variable, width=wrap, style="Hint.TLabel").grid(
            row=0, column=1, sticky="nw")
        btn = icons.decorate(ttk.Button(row, text=tr("Change..."), command=self.show_settings),
                             "settings")
        if below:
            btn.grid(row=1, column=1, sticky="w", pady=(8, 0))
        else:
            btn.grid(row=0, column=2, sticky="ne", padx=(8, 0))
        add_tooltip(btn, tr("Open the Settings sub-tab (folders, encoding, detection & mode, "
                            "subtitles)"))
        return row, btn

    # ---------------- helpers of the Settings widgets ----------------
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
