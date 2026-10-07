"""Templates -> Auto-detect sub-tab (TemplateDetectMixin)."""
import os
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from ..config import AUDIO_LANG_CHOICES, VIDEO_DIR
from ..engine.formatting import fmt_time
from ..engine.recurring import detect_recurring, representative_member
from ..ui.widgets import (info_icon, KeyedCombobox, add_tooltip, bind_status_colors,
                          enable_file_drop, help_button)
from .audition import _AuditionWindow
from .common import _KIND_NAMES, _list_media, _template_stem, edge_is_approx, mark_edges
from .cut_common import margin_widgets, mode_combobox, norm_margin, norm_tpl_margin
from .. import jobs as jobreg
from ..i18n import tr, N_
from ..ui import icons, themes

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr

# detection sensitivity: combobox key -> match threshold (the key is saved)
_SENS = {N_("High (strict)"): 0.9, N_("Medium"): 0.8,
         N_("Low (loose)"): 0.7, N_("Very loose"): 0.62}
# episode-length presets (the key is saved; _apply_eplen_preset reads it)
_EPLEN = (N_("Short (3-8 min)"), N_("Standard (20-40 min)"), N_("Long (45+ min)"))
# preset -> (search window, min intro, min credits, min pre/after) in seconds.
# Short episodes need a small window so the intro and credits searches don't
# overlap; the standard window is generous because long recaps (e.g. late One
# Piece) push the intro past 4 minutes into the episode.
_EPLEN_VALUES = {_EPLEN[0]: (90, 6, 6, 3), _EPLEN[1]: (420, 10, 10, 4),
                 _EPLEN[2]: (600, 12, 15, 5)}


def eplen_values(preset):
    """(window, min_intro, min_credits, min_pa) for an episode-length preset
    key (unknown = Standard)."""
    return _EPLEN_VALUES.get(preset, _EPLEN_VALUES[_EPLEN[1]])


class TemplateDetectMixin:
    """Templates -> Auto-detect sub-tab: find the segments that recur across
    episodes (no templates needed), audition them and save them as templates."""

    # ---------------- Auto-detect UI ----------------
    def _build_detect_tab(self, detect, saved):
        detect.columnconfigure(0, weight=1)
        detect.rowconfigure(5, weight=1)

        fr = ttk.Frame(detect)
        fr.grid(row=0, column=0, sticky="we", pady=(0, 4))
        fr.columnconfigure(1, weight=1)
        ttk.Label(fr, text=tr("Season folder:")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.detect_dir = tk.StringVar(value=saved.get("detect_dir", VIDEO_DIR))
        dent = ttk.Entry(fr, textvariable=self.detect_dir)
        dent.grid(row=0, column=1, sticky="we")
        dent.bind("<Return>", lambda e: self.start_detect())
        icons.decorate(ttk.Button(fr, text=tr("Browse..."), command=self._browse_detect), "folder").grid(row=0, column=2, padx=4)
        help_button(fr, "template_detect").grid(row=0, column=3, padx=(8, 0), sticky="e")
        enable_file_drop(dent, self._drop_detect)

        opt = ttk.LabelFrame(detect, text=tr("What to detect"))
        opt.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self.det_intro = tk.BooleanVar(value=bool(saved.get("det_intro", True)))
        self.det_credits = tk.BooleanVar(value=bool(saved.get("det_credits", True)))
        icb = ttk.Checkbutton(opt, text=tr("Intro"), variable=self.det_intro)
        icb.grid(row=0, column=0, sticky="w", padx=4)
        add_tooltip(icb, tr("Find the opening that recurs near the START of the episodes"))
        ccb = ttk.Checkbutton(opt, text=tr("Credits"), variable=self.det_credits)
        ccb.grid(row=0, column=1, sticky="w", padx=4)
        add_tooltip(ccb, tr("Find the ending credits that recur near the END of the episodes"))
        ttk.Label(opt, text=tr("Search first / last N sec:")).grid(row=0, column=2, sticky="e", padx=(16, 4))
        self.det_window = tk.StringVar(value=str(saved.get("det_window", "420")))
        wen = ttk.Entry(opt, textvariable=self.det_window, width=6)
        wen.grid(row=0, column=3, sticky="w")
        add_tooltip(wen, tr("How far into each episode to look for the intro (first N seconds) "
                            "and how far back from the end to look for the credits (last N "
                            "seconds). Must be > 0."))
        ttk.Label(opt, text=tr("Min intro (s):")).grid(row=0, column=4, sticky="e", padx=(16, 4))
        self.det_minlen_intro = tk.StringVar(value=str(saved.get("det_minlen_intro", "10")))
        mien = ttk.Entry(opt, textvariable=self.det_minlen_intro, width=5)
        mien.grid(row=0, column=5, sticky="w")
        add_tooltip(mien, tr("Shortest recurring audio (seconds) that counts as an intro. "
                             "Lower it if a short opening isn't found."))
        ttk.Label(opt, text=tr("Min credits (s):")).grid(row=0, column=6, sticky="e", padx=(16, 4))
        self.det_minlen_credits = tk.StringVar(value=str(saved.get("det_minlen_credits", "10")))
        mcen = ttk.Entry(opt, textvariable=self.det_minlen_credits, width=5)
        mcen.grid(row=0, column=7, sticky="w")
        add_tooltip(mcen, tr("Shortest recurring audio (seconds) that counts as credits. "
                             "Lower it if short credits aren't found."))
        self.det_preintro = tk.BooleanVar(value=bool(saved.get("det_preintro", False)))
        self.det_aftercredits = tk.BooleanVar(value=bool(saved.get("det_aftercredits", False)))
        pcb = ttk.Checkbutton(opt, text=tr("Pre-intro"), variable=self.det_preintro)
        pcb.grid(row=1, column=0, sticky="w", padx=4, pady=(4, 0))
        add_tooltip(pcb, tr("Also find a recurring bit BEFORE the intro (recap jingle / "
                            "studio logo). The intro is detected first to know where to look."))
        accb = ttk.Checkbutton(opt, text=tr("After-credits"), variable=self.det_aftercredits)
        accb.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        add_tooltip(accb, tr("Also find a recurring bit AFTER the credits (teaser / "
                             "next-episode preview). The credits are detected first to know "
                             "where to look."))
        ttk.Label(opt, text=tr("Min pre/after (s):")).grid(row=1, column=2, sticky="e", padx=(16, 4), pady=(4, 0))
        self.det_minlen_pa = tk.StringVar(value=str(saved.get("det_minlen_pa", "4")))
        paen = ttk.Entry(opt, textvariable=self.det_minlen_pa, width=6)
        paen.grid(row=1, column=3, sticky="w", pady=(4, 0))
        add_tooltip(paen, tr("Shortest recurring audio (seconds) that counts as a pre-intro or "
                             "after-credits bit"))
        ttk.Label(opt, text=tr("Scan at most (eps):")).grid(row=1, column=4, sticky="e", padx=(16, 4), pady=(4, 0))
        self.det_max_eps = tk.StringVar(value=str(saved.get("det_max_eps", "0")))
        sc = ttk.Entry(opt, textvariable=self.det_max_eps, width=5)
        sc.grid(row=1, column=5, sticky="w", pady=(4, 0))
        add_tooltip(sc, tr("0 = scan every episode (recommended - catches a variant even if "
                           "it only covers the last few episodes; episodes matching templates "
                           "you already cut are recognized quickly and skipped by the slow "
                           "clustering). Set a number to fingerprint only that many episodes, "
                           "picked evenly across the season, when you need a faster scan."))
        info_icon(opt, tr("0 = all (recommended)")).grid(row=1, column=6, sticky="w",
                                                         pady=(4, 0))
        ttk.Label(opt, text=tr("Sensitivity:")).grid(row=2, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        self.det_sens = tk.StringVar(value=str(saved.get("det_sens", "Medium")))
        sens_cb = KeyedCombobox(opt, textvariable=self.det_sens, state="readonly", width=18,
                                values=list(_SENS))
        sens_cb.grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))
        add_tooltip(sens_cb, tr("How closely the audio must match across episodes to count as "
                                "the same segment. Strict = fewer false hits; loose = finds more, "
                                "may grab too much."))
        info_icon(opt, tr("lower if an intro isn't found; higher if it grabs too much")).grid(
            row=2, column=3, sticky="w", pady=(4, 0))
        ttk.Label(opt, text=tr("Episode length:")).grid(row=3, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        self.det_eplen = tk.StringVar(value=str(saved.get("det_eplen", "Standard (20-40 min)")))
        cb = KeyedCombobox(opt, textvariable=self.det_eplen, state="readonly", width=22,
                           values=list(_EPLEN))
        cb.grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        # only a user pick fills the window/min lengths - startup/restore keeps
        # whatever custom values were saved
        cb.bind("<<ComboboxSelected>>", self._apply_eplen_preset)
        add_tooltip(cb, tr("Picking a preset fills the search window and min lengths for that "
                           "episode length (you can still edit them afterwards)"))
        info_icon(opt, tr("presets the search window & min length for you")).grid(
            row=3, column=3, sticky="w", pady=(4, 0))
        ttk.Label(opt, text=tr("Detect on audio:")).grid(row=4, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        _dl_labels = list(AUDIO_LANG_CHOICES)
        _saved_dl = saved.get("det_lang")
        _dl_label = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == _saved_dl), _dl_labels[0])
        self.det_lang_var = tk.StringVar(value=_dl_label)
        dlcb = KeyedCombobox(opt, textvariable=self.det_lang_var, values=_dl_labels,
                             state="readonly", width=22)
        dlcb.grid(row=4, column=1, columnspan=2, sticky="w", pady=(4, 0))
        add_tooltip(dlcb, tr("Which audio track to fingerprint for finding intros/credits. "
                             "'All / default' uses the file's default track; pick a language "
                             "(e.g. English) so a foreign default track doesn't skew detection. "
                             "Falls back to the default on files without that language."))
        ttk.Label(opt, text=tr("Detect by:")).grid(row=4, column=3, columnspan=2, sticky="e",
                                                   padx=(16, 4), pady=(4, 0))
        if not hasattr(self, "det_mode_var"):
            dm = saved.get("det_mode")
            self.det_mode_var = tk.StringVar(value=dm if dm in ("audio", "visual", "both")
                                             else "both")
        dmcb = mode_combobox(opt, self.det_mode_var)
        dmcb.grid(row=4, column=5, columnspan=3, sticky="w", pady=(4, 0))
        add_tooltip(dmcb, tr("Audio = the sound that recurs across the episodes; Visual = the "
                             "PICTURES that recur (intro and credits - finds an opening whose "
                             "audio differs, e.g. a dub); Audio + Visual = both, merged per "
                             "episode. Pre-intro / after-credits always use audio. Also used "
                             "by Auto-detect on the Cut template sub-tab."))
        # safety margin + template edges (also used by Cut template → Auto-detect)
        self.det_margin_var = tk.StringVar(value=str(norm_margin(saved.get("det_margin"))))
        self.det_tpl_margin_var = tk.StringVar(
            value=norm_tpl_margin(saved.get("det_tpl_margin")))
        margin_widgets(opt, self.det_margin_var, self.det_tpl_margin_var).grid(
            row=5, column=0, columnspan=8, sticky="w", padx=4, pady=(6, 2))

        rr = ttk.Frame(detect)
        rr.grid(row=2, column=0, sticky="we", pady=(8, 2))
        self.detect_btn = icons.decorate(ttk.Button(rr, style="Accent.TButton", text=tr("Detect intro / credits"), command=self.start_detect), "detect")
        self.detect_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.detect_btn, tr("Fingerprint the episodes and find the intro/credits "
                                        "that recur across them - even if the show uses more "
                                        "than one opening"))
        # the progress row (status, bar, %, Stop) goes to the window's fixed
        # footer when there is one (shown while this sub-tab is selected)
        foot = getattr(self, "_bottom", None)
        if foot is not None:
            self._detect_foot = ttk.Frame(foot)
            pf = ttk.Frame(self._detect_foot)
        else:
            self._detect_foot = None
            pf = ttk.Frame(detect)
        self.detect_stop_btn = icons.decorate(ttk.Button(pf if foot is not None else rr,
                                                         text=tr("Stop"), command=self.stop_detect,
                                                         state="disabled", width=-8), "stop")
        if foot is None:
            self.detect_stop_btn.pack(side="left", padx=(6, 0))
        info_icon(rr, tr("Needs a folder of episodes from the same show (>=2). "
                         "It finds the segment that repeats across them; a show "
                         "with several openings shows one row per opening. "
                         "Pre-intro is searched before each episode's detected "
                         "intro, after-credits after its detected credits. "
                         "Double-click a row to audition it in a few episodes; "
                         "Fill times loads it for review.")).pack(side="left", padx=(6, 0))
        add_tooltip(self.detect_stop_btn, tr("Stop detecting (or auto-cutting). Results of a "
                                             "stopped detect are partial - review them, but "
                                             "Auto-cut stays off until a full run."))

        self.detect_status = tk.StringVar(value="")
        if foot is not None:
            ttk.Label(self._detect_foot, textvariable=self.detect_status,
                      style="Hint.TLabel").pack(anchor="w", padx=10, pady=(4, 0))
            pf.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(detect, textvariable=self.detect_status, style="Hint.TLabel").grid(
                row=3, column=0, sticky="w")
            pf.grid(row=4, column=0, sticky="we", pady=(2, 2))
        pf.columnconfigure(0, weight=1)
        self.detect_bar = ttk.Progressbar(pf, mode="determinate", maximum=1000)
        self.detect_bar.grid(row=0, column=0, sticky="we")
        self.detect_pct = tk.StringVar(value="")
        ttk.Label(pf, textvariable=self.detect_pct, width=20).grid(row=0, column=1, padx=(6, 0))
        if foot is not None:
            self.detect_stop_btn.grid(row=0, column=2, padx=(6, 0))

        tvf = ttk.Frame(detect)
        tvf.grid(row=5, column=0, sticky="nsew", pady=(2, 0))
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        cols = ("kind", "eps", "start", "end", "len", "example")
        self.detect_tree = ttk.Treeview(tvf, columns=cols, show="headings", height=5)
        for c, txt, w in [("kind", N_("Kind"), 90), ("eps", N_("Eps"), 45),
                          ("start", N_("Start"), 90), ("end", N_("End"), 90),
                          ("len", N_("Length"), 70), ("example", N_("Example episode"), 260)]:
            self.detect_tree.heading(c, text=tr_key(txt))
            # px at 100 % -> DPI-scaled, and never narrower than the heading
            w = max(themes.px(w), tkfont.nametofont("MPCaption").measure(tr_key(txt))
                    + themes.px(20))
            self.detect_tree.column(c, width=w, anchor=("w" if c in ("kind", "example") else "center"),
                                    stretch=(c == "example"))
        self.detect_tree.grid(row=0, column=0, sticky="nsew")
        # rows already covered by a template you cut earlier show green + ✔
        bind_status_colors(self.detect_tree, {"have": "good"})   # readable on every theme
        self.detect_tree.column("kind", width=themes.px(130))
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.detect_tree.yview)
        self.detect_tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.detect_tree.bind("<Double-1>", self._on_detect_double)

        ar = ttk.Frame(detect)
        ar.grid(row=6, column=0, sticky="we", pady=(6, 2))
        b1 = icons.decorate(ttk.Button(ar, text=tr("Fill times from selected (review)"),
                        command=self._fill_from_selected), "edit")
        b1.pack(side="left")
        add_tooltip(b1, tr("Load the example episode into the player and fill the intro/credits "
                           "From/To on the Cut template tab so you can review and nudge, then cut"))
        b3 = icons.decorate(ttk.Button(ar, text=tr("Audition"), command=self._audition_selected), "headphones")
        b3.pack(side="left", padx=(6, 0))
        add_tooltip(b3, tr("Listen to the selected row: plays its detected range in up to 3 of "
                           "its episodes one after another, in a small player window "
                           "(double-click a row does the same)"))
        b2 = icons.decorate(ttk.Button(ar, text=tr("Auto-cut all templates"), command=self._autocut_all), "cut")
        b2.pack(side="left", padx=(6, 0))
        add_tooltip(b2, tr("Cut a template clip for every detected intro/credits variant "
                           "straight into its input folder (one per row) - no manual step"))
        self.autocut_btn = b2


    def _apply_eplen_preset(self, event=None):
        """Fill the search window + min length from an episode-length preset.
        Short episodes (shorts) need a small window so the intro and credits
        searches don't overlap, and a shorter min length for brief openings."""
        win, mi, mc, pa = eplen_values(self.det_eplen.get())
        self.det_window.set(str(win))
        self.det_minlen_intro.set(str(mi))
        self.det_minlen_credits.set(str(mc))
        self.det_minlen_pa.set(str(pa))

    # ---------------- auto-detect logic ----------------
    def _browse_detect(self):
        d = filedialog.askdirectory(title=tr("Select the season / show folder"))
        if d:
            self.detect_dir.set(d)

    def _drop_detect(self, path):
        path = (path or "").strip().strip('"')
        if not path:
            return
        if os.path.isdir(path):
            self.detect_dir.set(path)
        elif os.path.isfile(path):
            self.detect_dir.set(os.path.dirname(path))

    def stop_detect(self):
        self.detect_stop.set()
        self.log("[DETECT] stopping...")


    def _detect_progress(self, frac, msg=""):
        def _a():
            self.detect_bar["value"] = int(frac * 1000)
            self.detect_status.set(msg)
        self.after(0, _a)

    def start_detect(self):
        folder = self.detect_dir.get().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            messagebox.showerror(tr("Error"), tr("Pick a valid season / show folder first."))
            return
        files = _list_media(folder)
        if len(files) < 2:
            messagebox.showerror(tr("Error"),
                                 tr("Need at least 2 episodes in the folder to detect what recurs."))
            return
        if not (self.det_intro.get() or self.det_credits.get()
                or self.det_preintro.get() or self.det_aftercredits.get()):
            messagebox.showerror(tr("Error"), tr("Tick at least one segment type to detect."))
            return
        if self._detecting or self._autocutting:
            return
        try:
            window = float(self.det_window.get())
            pa = float(self.det_minlen_pa.get())
            minlens = {"intro": float(self.det_minlen_intro.get()),
                       "credits": float(self.det_minlen_credits.get()),
                       "preintro": pa, "aftercredits": pa}
            max_eps = int(self.det_max_eps.get() or 0)
        except ValueError:
            messagebox.showerror(tr("Error"), tr("Search window, min lengths and max episodes must "
                                                 "be numbers."))
            return
        if window <= 0:
            messagebox.showerror(tr("Error"), tr("The search window (first / last N sec) must be "
                                                 "more than 0."))
            return
        if min(minlens.values()) < 0:
            messagebox.showerror(tr("Error"), tr("Min lengths can't be negative."))
            return
        if max_eps < 0:
            messagebox.showerror(tr("Error"), tr("'Scan at most' must be 0 (all episodes) or a "
                                                 "number of episodes."))
            return
        if max_eps == 1:
            # one episode has nothing to compare against - 2 is the minimum
            max_eps = 2
            self.det_max_eps.set("2")
            self.log("[DETECT] 'Scan at most' 1 can't find anything that recurs - using 2 (the minimum).")
        if max_eps > 1 and len(files) > max_eps:
            # sample evenly across the sorted season so every intro/credits era
            # (they run in contiguous blocks) is still hit several times
            total = len(files)
            idx = sorted({round(i * (total - 1) / (max_eps - 1))
                          for i in range(max_eps)})
            files = [files[i] for i in idx]
            self.log(f"[DETECT] {total} episodes in folder - fingerprinting "
                     f"{len(files)} of them, spread evenly (raise 'Scan at most' "
                     "if a variant is missed)")
        thresh = _SENS.get(self.det_sens.get(), 0.8)
        self.detect_stop.clear()
        self.detect_tree.delete(*self.detect_tree.get_children())
        self._clusters.clear()
        self._detect_partial = False
        self._detecting = True
        self._sync_buttons()
        kinds = [k for k, v in (("preintro", self.det_preintro),
                                ("intro", self.det_intro),
                                ("credits", self.det_credits),
                                ("aftercredits", self.det_aftercredits)) if v.get()]
        # templates already cut into the input folders: episodes matching one
        # are reported as covered instead of being re-clustered
        known = {k: _list_media(d) for k, d in self.DIRMAP.items()}
        lang = AUDIO_LANG_CHOICES.get(self.det_lang_var.get())
        if callable(self.save_hook):
            self.save_hook()          # remember these detect settings
        jid = self._jid = jobreg.begin(tr("Template auto-detect"), stop_event=self.detect_stop,
                                       tab=self)
        threading.Thread(target=self._detect_worker,
                         args=(files, kinds, window, minlens, thresh, known, lang,
                               self.det_sens.get(), jid, self.det_mode_var.get(),
                               self._det_margins()),
                         daemon=True).start()

    def _detect_worker(self, files, kinds, window, minlens, thresh, known=None, lang=None,
                       sens_label="", jid=None, mode="audio", margins=None):
        found = []
        crashed = False
        try:
            self.log(f"[DETECT] {len(files)} episode(s) - detecting {', '.join(kinds)} "
                     f"(sensitivity {sens_label}, by {mode}"
                     + (f", {lang} audio" if lang else "") + ")")
            diag = {}
            found = detect_recurring(
                files, mode=mode, kinds=kinds, window=window, min_lens=minlens,
                thresh=thresh, progress=self._detect_progress, stop_event=self.detect_stop,
                diag_out=diag, known=known, lang=lang, log=self.log, **(margins or {}))
            order = {"preintro": 0, "intro": 1, "credits": 2, "aftercredits": 3}
            found.sort(key=lambda c: (order.get(c["kind"], 9),
                                      c.get("known") is not None, -c["count"]))
            if self.detect_stop.is_set():
                self.log("[DETECT] stopped.")
            else:
                for kind in kinds:
                    clusters = [c for c in found if c["kind"] == kind]
                    known_cl = [c for c in clusters if c.get("known")]
                    new_cl = [c for c in clusters if not c.get("known")]
                    for c in known_cl:
                        self.log(f"[DETECT] {kind}: {c['count']} ep(s) already covered by "
                                 f"template '{c['known']}'")
                    if new_cl:
                        self.log(f"[DETECT] {kind}: {len(new_cl)} NEW variant(s) found ("
                                 + ", ".join(f"{c['count']} ep(s) by {c.get('src', 'audio')}"
                                             for c in new_cl) + ")")
                        continue
                    if known_cl:
                        self.log(f"[DETECT] {kind}: no new variants - existing template(s) "
                                 "cover the matched episodes")
                        continue
                    if kind in ("preintro", "aftercredits"):
                        need = "intro" if kind == "preintro" else "credits"
                        usable = diag.get(kind + "_files", 0)
                        if usable < 2:
                            self.log(f"[DETECT] {kind}: skipped - the {need} must be found "
                                     f"first to know where to look ({usable} episode(s) had "
                                     "a usable region).")
                            continue
                    bl, bs = diag.get(kind, (0.0, 0.0))
                    if bl > 0:
                        self.log(f"[DETECT] {kind}: none passed. Best recurring audio was "
                                 f"{bl:.1f}s at score {bs:.2f}. Try a lower min length (< {bl:.0f}s) "
                                 "or a looser sensitivity.")
                    else:
                        self.log(f"[DETECT] {kind}: no shared audio found at all - check the folder "
                                 "holds episodes from the same show, or raise the search window.")
        except Exception as exc:
            crashed = True
            self.log(f"[FAIL] detect crashed: {exc}")
        finally:
            stopped = self.detect_stop.is_set()
            jobreg.end(jid, ok=not crashed,
                       summary=tr("{found} segment(s) found in {eps} episode(s)",
                                  found=len(found), eps=len(files)))

            def _fill():
                self._detecting = False
                # a stopped detect's rows are partial: show them for review,
                # but Auto-cut stays off until a full run
                self._detect_partial = stopped and bool(found)
                self._sync_buttons()
                self.detect_bar["value"] = 0
                n = 0
                for c in found:
                    rep = representative_member(c)
                    a, b = c["ranges"][rep]
                    iid = f"c{n}"
                    # ⚠ before an edge placed by sound / a fade (approximate)
                    es = (c.get("edge_src") or {}).get(rep) or {}
                    sa = ("⚠ " if edge_is_approx(es.get("start")) else "") + fmt_time(a)
                    sb = ("⚠ " if edge_is_approx(es.get("end")) else "") + fmt_time(b)
                    self.detect_tree.insert(
                        "", "end", iid=iid,
                        values=(self._kind_disp(c), c["count"], sa, sb,
                                f"{b - a:.1f}s", os.path.basename(rep)),
                        tags=("have",) if c.get("known") else ())
                    self._clusters[iid] = c
                    n += 1
                if stopped:
                    self.detect_status.set(tr("Stopped - {n} partial result(s). Run Detect "
                                              "to the end before Auto-cut.", n=len(found)))
                elif not found:
                    self.detect_status.set(tr("No recurring intro/credits found (try a larger "
                                              "window or lower min length)."))
                else:
                    self.detect_status.set(tr("Found {n} segment(s). Select one -> Fill times, "
                                              "or Auto-cut all.", n=len(found)))
            self.after(0, _fill)

    def _det_margins(self):
        """{'margin_frames', 'template_margin'} for the detectors."""
        return {"margin_frames": norm_margin(self.det_margin_var.get()),
                "template_margin": norm_tpl_margin(self.det_tpl_margin_var.get())}

    def _fill_from_selected(self):
        sel = self.detect_tree.selection()
        if not sel:
            messagebox.showinfo(tr("Pick a row"), tr("Select a detected row first."))
            return
        c = self._clusters.get(sel[0])
        if not c:
            return
        rep = representative_member(c)
        a, b = c["ranges"][rep]
        kind = c["kind"]
        self.file_var.set(rep)
        loaded = self.player.load(rep)
        # untick the other sections: their times belong to another episode
        for k, (o, _ef, _et) in self.sections.items():
            if k != kind:
                o.set(False)
        on, ef, et = self.sections[kind]
        on.set(True)
        ef.set_value(a)
        et.set_value(b)          # engine end -> shows the last frame
        mark_edges(ef, et, (c.get("edge_src") or {}).get(rep))   # ⚠ sound / fade edges
        self._refresh_markers()
        if loaded:
            self.player.seek_seconds(a)     # start reviewing at the boundary
        self._nb.select(0)
        self.log(f"[DETECT] filled {kind} {fmt_time(a)}->{fmt_time(b)} from {os.path.basename(rep)} "
                 "- review in the player and nudge if needed.")

    def _autocut_all(self):
        if not self._clusters:
            messagebox.showinfo(tr("Nothing to cut"), tr("Run Detect first."))
            return
        if self._detect_partial:
            messagebox.showinfo(tr("Partial results"), tr("These rows come from a stopped detect. "
                                                          "Run Detect to the end before Auto-cut."))
            return
        if self._cutting or self._autocutting or self._detecting:
            return
        clusters = list(self._clusters.values())
        todo = [c for c in clusters if not c.get("known")]
        for c in clusters:
            if c.get("known"):
                self.log(f"[DETECT] skip {c['kind']} x{c['count']} - already covered "
                         f"by template '{c['known']}'")
        if not todo:
            messagebox.showinfo(tr("Nothing to cut"), tr("Every detected segment is already "
                                                         "covered by a template."))
            return
        items = []
        for i, c in enumerate(todo):
            name = os.path.splitext(os.path.basename(representative_member(c)))[0]
            items.append((i, self.DIRMAP[c["kind"]], f"{_template_stem(name)}_{c['kind']}"))
        plan = self._plan_outputs(items)        # asks once for the whole batch
        jobs = [(c,) + plan[i] for i, c in enumerate(todo) if i in plan]
        if not jobs:
            self.detect_status.set(tr("Nothing cut - every template was skipped."))
            return
        self.detect_stop.clear()
        self.cut_stop.clear()
        self._autocutting = True
        self._sync_buttons()
        jid = self._jid = jobreg.begin(tr("Template auto-cut"), stop_event=self.cut_stop, tab=self)
        threading.Thread(target=self._autocut_worker, args=(jobs, self._det_lang(), jid),
                         daemon=True).start()

    def _autocut_worker(self, jobs, lang=None, jid=None):
        stopped = False
        n_ok = n_fail = 0
        crashed = False
        try:
            self.log(f"[DETECT] auto-cutting {len(jobs)} template(s)...")
            for c, out, replace in jobs:
                # the Template-tab Stop and the detect Stop both end auto-cut
                if self.detect_stop.is_set() or self.cut_stop.is_set():
                    stopped = True
                    break
                rep = representative_member(c)
                a, b = c["ranges"][rep]
                kind = c["kind"]
                self.log(f"[{kind.upper()}] {fmt_time(a)} -> {fmt_time(b)}  ->  {out}")
                ok, err = self._cut_clip(rep, out, a, b, lang)
                if ok:
                    n_ok += 1
                    self.log("  [OK] saved")
                    self._template_saved(out, replace, kind, cluster=c)
                else:
                    n_fail += 1
                    for ln in err.strip().splitlines()[-4:]:
                        self.log(f"    | {ln}")
                    self.log(f"  [FAIL] {kind} template failed")
            self.log("[DETECT] auto-cut stopped." if stopped else "[DETECT] auto-cut done.")
        except Exception as exc:
            crashed = True
            self.log(f"[FAIL] auto-cut crashed: {exc}")
        finally:
            jobreg.end(jid, ok=not crashed and n_fail == 0,
                       summary=tr("{ok} template(s) cut, {failed} failed",
                                  ok=n_ok, failed=n_fail))
            def _f():
                self._autocutting = False
                self._sync_buttons()
                self._tm_refresh()
            self.after(0, _f)

    # ---------------- detected-row audition ----------------
    def _on_detect_double(self, event):
        iid = self.detect_tree.identify_row(event.y)
        if iid:
            self.detect_tree.selection_set(iid)
            self._audition_selected()

    def _audition_selected(self):
        sel = self.detect_tree.selection()
        if not sel:
            messagebox.showinfo(tr("Pick a row"), tr("Select a detected row first."))
            return
        c = self._clusters.get(sel[0])
        if not c:
            return
        rep = representative_member(c)
        others = sorted(m for m in c["ranges"] if m != rep)
        picks = [rep]
        if others:      # spread the extra picks across the season
            for i in sorted({0, len(others) - 1} if len(others) > 1 else {0}):
                picks.append(others[i])
        clips = [(p, c["ranges"][p][0], c["ranges"][p][1]) for p in picks[:3]]
        title = tr("Audition - {kind} ({n} eps)",
                   kind=tr_key(_KIND_NAMES.get(c["kind"], c["kind"])), n=c["count"])
        if self._audition is not None and self._audition.winfo_exists():
            self._audition.start(title, clips)
            self._audition.lift()
        else:
            self._audition = _AuditionWindow(self, title, clips, log_fn=self.log)
