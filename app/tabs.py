"""The two working tabs, each split into inner sub-tabs:

    Template Cutter  ->  Cut template | Auto-detect | Log
    Cut / Edit       ->  Auto-detect | Manual cut | Multi cut | Log

Both share the VideoPlayer (with a marker timeline), Set/Go buttons,
drag-and-drop, and remember the last previewed video."""
import os
import re
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR,
                     VIDEO_DIR, OUTPUT_DIR, CODECS, DEFAULT_CODEC_LABEL,
                     LEGACY_CODEC_LABELS, BIT_DEPTHS, VALID_PRESETS, POPEN_FLAGS,
                     AUDIO_LANG_CHOICES)
from .media import (fmt_time, format_seconds, probe_duration, keep_from_drops,
                    run_batch, run_manual, detect_recurring_segments,
                    representative_member, probe_subtitle_inventory)
from .player import VideoPlayer, MARKER_COLORS
from .widgets import (TimeEntry, add_tooltip, ScrollFrame, enable_file_drop,
                     enable_file_drop_deep, enable_paths_drop, build_log_tab)
from . import applog

_VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")]
_MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
               ".ts", ".mpg", ".mpeg", ".wmv", ".flv")


def _template_stem(name):
    """Short template name: keep the show name + S00E00 tag, drop the episode
    title ('One Piece - S09E0285 - Obtain the 5 Keys!...' -> 'One Piece -
    S09E0285'). Falls back to the full name if there is no SxxExx tag."""
    m = re.search(r"^(.*?[Ss]\d{1,4}[Ee]\d{1,4})", name)
    return m.group(1).strip() if m else name


def _list_media(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names
                  if n.lower().endswith(_MEDIA_EXTS)
                  and os.path.isfile(os.path.join(folder, n)))


# ======================= Tab 1 - Template Cutter =======================
class TemplateTab(ttk.Frame):
    SECTION_SPECS = [
        ("preintro",     "Pre-intro (before intro)",      False, True),
        ("intro",        "Intro",                         True,  True),
        ("credits",      "Credits",                       True,  False),
        ("aftercredits", "After-credits (after credits)", False, False),
    ]
    DIRMAP = {"preintro": PREINTRO_DIR, "intro": INTRO_DIR,
              "credits": CREDITS_DIR, "aftercredits": AFTERCREDITS_DIR}

    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.detect_stop = threading.Event()
        self.cut_stop = threading.Event()   # stops the template-cut worker
        self._clusters = {}                 # tree iid -> detected cluster
        self.save_hook = None               # set by app.py: persist settings

        nb = ttk.Notebook(self)
        self._nb = nb
        nb.grid(row=0, column=0, sticky="nsew")
        # each sub-tab lives in its own ScrollFrame (like the Cut / Edit tab), so
        # a short page fills the window seamlessly and scrolls if it's too tall -
        # no bordered dead space under short content.
        main_outer = ttk.Frame(nb)
        detect_outer = ttk.Frame(nb)
        nb.add(main_outer, text="  Cut template  ")
        nb.add(detect_outer, text="  Auto-detect  ")
        main_sc = ScrollFrame(main_outer)
        main_sc.pack(fill="both", expand=True)
        detect_sc = ScrollFrame(detect_outer)
        detect_sc.pack(fill="both", expand=True)
        main = main_sc.interior
        detect = detect_sc.interior
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # ---- file row ----
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(top, text="Video file:").pack(side="left", padx=(0, 6))
        self.file_var = tk.StringVar(value=saved.get("last_video", ""))
        ent = ttk.Entry(top, textvariable=self.file_var)
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left", padx=6)
        ttk.Button(top, text="Load", command=self.load_from_entry).pack(side="left")

        # ---- body: player (left) + cut points (right), single view ----
        body = ttk.Frame(main)
        body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # right column: the four sections + cut button
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        # one row per section: enable-checkbox label, From boxes + Set button,
        # To boxes + Set button ("Set" captures the frame shown in the player)
        seg_desc = {"preintro": "pre-intro (recap/logo before the intro)",
                    "intro": "intro", "credits": "credits",
                    "aftercredits": "after-credits (teaser/preview)"}
        sect = ttk.Frame(right)
        sect.grid(row=0, column=0, pady=(0, 4))
        self.sections = {}
        for i, (key, title, default_on, _need_to) in enumerate(self.SECTION_SPECS):
            on = tk.BooleanVar(value=default_on)
            short = title.split(" (")[0]
            cb = ttk.Checkbutton(sect, text=f"{short} from / to:", variable=on)
            cb.grid(row=i, column=0, sticky="e", padx=(0, 4), pady=3)
            add_tooltip(cb, f"Include the {seg_desc[key]} when cutting templates")
            ef = TimeEntry(sect)
            ef.grid(row=i, column=1, padx=2, pady=3)
            bf = ttk.Button(sect, text="Set", width=5,
                            command=lambda k=key: self._mark(k, "from"))
            bf.grid(row=i, column=2, padx=(2, 12))
            add_tooltip(bf, f"Set the START of the {seg_desc[key]} to the frame shown in the player")
            et = TimeEntry(sect)
            et.grid(row=i, column=3, padx=2, pady=3)
            bt = ttk.Button(sect, text="Set", width=5,
                            command=lambda k=key: self._mark(k, "to"))
            bt.grid(row=i, column=4, padx=2)
            add_tooltip(bt, f"Set the END of the {seg_desc[key]} to the frame shown in the player")
            bp = ttk.Button(sect, text="▶", width=3,
                            command=lambda k=key: self._preview_section(k))
            bp.grid(row=i, column=5, padx=(6, 0))
            add_tooltip(bp, f"Play only the {seg_desc[key]} section (From to To) in the player")
            self.sections[key] = (on, ef, et)
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._refresh_markers())

        ttk.Label(right, text="Cut the WHOLE segment - the Remover uses the template length as "
                             "the cut length. Credits / After-credits 'To' empty = end of file; "
                             "Pre-intro 'From' empty = start of file. "
                             "Pre-intro & after-credits are optional (recaps / teasers).",
                  style="Hint.TLabel", wraplength=360, justify="left").grid(
                      row=4, column=0, sticky="w", pady=(8, 4))
        self.cut_btn = ttk.Button(right, text="Cut template(s)", command=self.start_cut)
        self.cut_btn.grid(row=5, column=0, sticky="we", pady=4)
        add_tooltip(self.cut_btn, "Cut each enabled section into a template clip in its folder")

        # left column: player. Fine-tune the boundary with the player's own
        # frame-step controls - Left/Right arrows, the |< < > >| buttons.
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        self.player = VideoPlayer(left, width=480, height=270, log_fn=self.log)
        self.player.pack()
        self.player.enable_tab_shortcuts()   # arrows/space work anywhere on the tab

        # ---- shared status + progress (anchored to the window bottom when a
        # non-scrolling `bottom` strip is provided, so they stay visible) ----
        self.status_var = tk.StringVar(value="")
        if bottom is not None:
            ttk.Label(bottom, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prow = ttk.Frame(bottom)
            prow.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(main, textvariable=self.status_var, style="Hint.TLabel").grid(
                row=2, column=0, sticky="w", pady=(6, 0))
            prow = ttk.Frame(main)
            prow.grid(row=3, column=0, sticky="we", pady=(2, 2))
        prow.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prow, mode="indeterminate")
        self.bar.grid(row=0, column=0, sticky="we")
        # Stop in the right corner beside the progress bar
        self.cut_stop_btn = ttk.Button(prow, text="Stop", command=self.stop_cut,
                                       state="disabled", width=8)
        self.cut_stop_btn.grid(row=0, column=1, padx=(6, 0))
        add_tooltip(self.cut_stop_btn, "Stop after the current section finishes")

        # ===================== Auto-detect tab =====================
        self._build_detect_tab(detect, saved)

        self.logbox = build_log_tab(nb)

        enable_file_drop_deep(self.player, self._drop_load)       # drop anywhere in the player
        enable_file_drop(ent, self._drop_load)

    # ---------------- Auto-detect UI ----------------
    def _build_detect_tab(self, detect, saved):
        detect.columnconfigure(0, weight=1)
        detect.rowconfigure(5, weight=1)

        fr = ttk.Frame(detect)
        fr.grid(row=0, column=0, sticky="we", pady=(0, 4))
        fr.columnconfigure(1, weight=1)
        ttk.Label(fr, text="Season folder:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.detect_dir = tk.StringVar(value=saved.get("detect_dir", VIDEO_DIR))
        dent = ttk.Entry(fr, textvariable=self.detect_dir)
        dent.grid(row=0, column=1, sticky="we")
        ttk.Button(fr, text="Browse...", command=self._browse_detect).grid(row=0, column=2, padx=4)
        enable_file_drop(dent, self._drop_detect)

        opt = ttk.LabelFrame(detect, text=" What to detect ", padding=(8, 4))
        opt.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self.det_intro = tk.BooleanVar(value=bool(saved.get("det_intro", True)))
        self.det_credits = tk.BooleanVar(value=bool(saved.get("det_credits", True)))
        ttk.Checkbutton(opt, text="Intro", variable=self.det_intro).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Checkbutton(opt, text="Credits", variable=self.det_credits).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(opt, text="Search first / last N sec:").grid(row=0, column=2, sticky="e", padx=(16, 4))
        self.det_window = tk.StringVar(value=str(saved.get("det_window", "420")))
        ttk.Entry(opt, textvariable=self.det_window, width=6).grid(row=0, column=3, sticky="w")
        ttk.Label(opt, text="Min intro (s):").grid(row=0, column=4, sticky="e", padx=(16, 4))
        self.det_minlen_intro = tk.StringVar(value=str(saved.get("det_minlen_intro", "10")))
        ttk.Entry(opt, textvariable=self.det_minlen_intro, width=5).grid(row=0, column=5, sticky="w")
        ttk.Label(opt, text="Min credits (s):").grid(row=0, column=6, sticky="e", padx=(16, 4))
        self.det_minlen_credits = tk.StringVar(value=str(saved.get("det_minlen_credits", "10")))
        ttk.Entry(opt, textvariable=self.det_minlen_credits, width=5).grid(row=0, column=7, sticky="w")
        self.det_preintro = tk.BooleanVar(value=bool(saved.get("det_preintro", False)))
        self.det_aftercredits = tk.BooleanVar(value=bool(saved.get("det_aftercredits", False)))
        pcb = ttk.Checkbutton(opt, text="Pre-intro", variable=self.det_preintro)
        pcb.grid(row=1, column=0, sticky="w", padx=4, pady=(4, 0))
        add_tooltip(pcb, "Also find a recurring bit BEFORE the intro (recap jingle / "
                    "studio logo). The intro is detected first to know where to look.")
        accb = ttk.Checkbutton(opt, text="After-credits", variable=self.det_aftercredits)
        accb.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        add_tooltip(accb, "Also find a recurring bit AFTER the credits (teaser / "
                    "next-episode preview). The credits are detected first to know where to look.")
        ttk.Label(opt, text="Min pre/after (s):").grid(row=1, column=2, sticky="e", padx=(16, 4), pady=(4, 0))
        self.det_minlen_pa = tk.StringVar(value=str(saved.get("det_minlen_pa", "4")))
        ttk.Entry(opt, textvariable=self.det_minlen_pa, width=6).grid(row=1, column=3, sticky="w", pady=(4, 0))
        ttk.Label(opt, text="Scan at most (eps):").grid(row=1, column=4, sticky="e", padx=(16, 4), pady=(4, 0))
        self.det_max_eps = tk.StringVar(value=str(saved.get("det_max_eps", "0")))
        sc = ttk.Entry(opt, textvariable=self.det_max_eps, width=5)
        sc.grid(row=1, column=5, sticky="w", pady=(4, 0))
        add_tooltip(sc, "0 = scan every episode (recommended - catches a variant even if "
                    "it only covers the last few episodes; episodes matching templates "
                    "you already cut are recognized quickly and skipped by the slow "
                    "clustering). Set a number to fingerprint only that many episodes, "
                    "picked evenly across the season, when you need a faster scan.")
        ttk.Label(opt, text="0 = all (recommended)",
                  style="Hint.TLabel").grid(row=1, column=6, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(opt, text="Sensitivity:").grid(row=2, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        self.det_sens = tk.StringVar(value=str(saved.get("det_sens", "Medium")))
        ttk.Combobox(opt, textvariable=self.det_sens, state="readonly", width=14,
                     values=["High (strict)", "Medium", "Low (loose)", "Very loose"]).grid(
                         row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(opt, text="lower if an intro isn't found; higher if it grabs too much",
                  style="Hint.TLabel").grid(row=2, column=3, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(opt, text="Episode length:").grid(row=3, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        self.det_eplen = tk.StringVar(value=str(saved.get("det_eplen", "Standard (20-40 min)")))
        cb = ttk.Combobox(opt, textvariable=self.det_eplen, state="readonly", width=18,
                          values=["Short (3-8 min)", "Standard (20-40 min)", "Long (45+ min)"])
        cb.grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        cb.bind("<<ComboboxSelected>>", self._apply_eplen_preset)
        ttk.Label(opt, text="presets the search window & min length for you",
                  style="Hint.TLabel").grid(row=3, column=3, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(opt, text="Detect on audio:").grid(row=4, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        _dl_labels = list(AUDIO_LANG_CHOICES)
        _saved_dl = saved.get("det_lang")
        _dl_label = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == _saved_dl), _dl_labels[0])
        self.det_lang_var = tk.StringVar(value=_dl_label)
        dlcb = ttk.Combobox(opt, textvariable=self.det_lang_var, values=_dl_labels,
                            state="readonly", width=18)
        dlcb.grid(row=4, column=1, columnspan=2, sticky="w", pady=(4, 0))
        add_tooltip(dlcb, "Which audio track to fingerprint for finding intros/credits. "
                    "'All / default' uses the file's default track; pick a language (e.g. English) "
                    "so a foreign default track doesn't skew detection. Falls back to the default "
                    "on files without that language.")

        rr = ttk.Frame(detect)
        rr.grid(row=2, column=0, sticky="we", pady=(8, 2))
        self.detect_btn = ttk.Button(rr, text="Detect intro / credits", command=self.start_detect)
        self.detect_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.detect_btn, "Fingerprint the episodes and find the intro/credits that "
                    "recur across them - even if the show uses more than one opening")
        self.detect_stop_btn = ttk.Button(rr, text="Stop", command=self.stop_detect,
                                           state="disabled", width=8)
        self.detect_stop_btn.pack(side="left", padx=(6, 0))

        self.detect_status = tk.StringVar(value="")
        ttk.Label(detect, textvariable=self.detect_status, style="Hint.TLabel").grid(
            row=3, column=0, sticky="w")
        pf = ttk.Frame(detect)
        pf.grid(row=4, column=0, sticky="we", pady=(2, 2))
        pf.columnconfigure(0, weight=1)
        self.detect_bar = ttk.Progressbar(pf, mode="determinate", maximum=1000)
        self.detect_bar.grid(row=0, column=0, sticky="we")
        self.detect_pct = tk.StringVar(value="")
        ttk.Label(pf, textvariable=self.detect_pct, width=20).grid(row=0, column=1, padx=(6, 0))

        tvf = ttk.Frame(detect)
        tvf.grid(row=5, column=0, sticky="nsew", pady=(2, 0))
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        cols = ("kind", "eps", "start", "end", "len", "example")
        self.detect_tree = ttk.Treeview(tvf, columns=cols, show="headings", height=8)
        for c, txt, w in [("kind", "Kind", 90), ("eps", "Eps", 45), ("start", "Start", 90),
                          ("end", "End", 90), ("len", "Length", 70), ("example", "Example episode", 260)]:
            self.detect_tree.heading(c, text=txt)
            self.detect_tree.column(c, width=w, anchor=("w" if c in ("kind", "example") else "center"),
                                    stretch=(c == "example"))
        self.detect_tree.grid(row=0, column=0, sticky="nsew")
        # rows already covered by a template you cut earlier show green + ✔
        self.detect_tree.tag_configure("have", foreground="#3aa657")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.detect_tree.yview)
        self.detect_tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.detect_tree.bind("<Double-1>", lambda e: self._fill_from_selected())

        ar = ttk.Frame(detect)
        ar.grid(row=6, column=0, sticky="we", pady=(6, 2))
        b1 = ttk.Button(ar, text="Fill times from selected (review)", command=self._fill_from_selected)
        b1.pack(side="left")
        add_tooltip(b1, "Load the example episode into the player and fill the intro/credits From/To "
                    "on the Cut template tab so you can review and nudge, then cut")
        b2 = ttk.Button(ar, text="Auto-cut all templates", command=self._autocut_all)
        b2.pack(side="left", padx=(6, 0))
        add_tooltip(b2, "Cut a template clip for every detected intro/credits variant straight into "
                    "its input folder (one per row) - no manual step")
        self.autocut_btn = b2

        ttk.Label(detect, text="Needs a folder of episodes from the same show (>=2). It finds the "
                             "segment that repeats across them; a show with several openings shows "
                             "one row per opening. Pre-intro is searched before each episode's "
                             "detected intro, after-credits after its detected credits. "
                             "Double-click a row to review it.",
                  style="Hint.TLabel", wraplength=640, justify="left").grid(
                      row=7, column=0, sticky="w", pady=(4, 0))

    def _apply_eplen_preset(self, event=None):
        """Fill the search window + min length from an episode-length preset.
        Short episodes (shorts) need a small window so the intro and credits
        searches don't overlap, and a shorter min length for brief openings."""
        preset = self.det_eplen.get()
        if preset.startswith("Short"):
            self.det_window.set("90")
            self.det_minlen_intro.set("6")
            self.det_minlen_credits.set("6")
            self.det_minlen_pa.set("3")
        elif preset.startswith("Long"):
            self.det_window.set("600")
            self.det_minlen_intro.set("12")
            self.det_minlen_credits.set("15")
            self.det_minlen_pa.set("5")
        else:
            # generous window: long recaps (e.g. late One Piece) push the
            # intro past 4 minutes into the episode
            self.det_window.set("420")
            self.det_minlen_intro.set("10")
            self.det_minlen_credits.set("10")
            self.det_minlen_pa.set("4")

    # ---------------- markers ----------------
    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        dur = self.player.timeline.duration
        marks = []
        for key, (on, ef, et) in self.sections.items():
            s, _ = ef.get_seconds()
            e, _ = et.get_seconds()
            if s is None:
                continue
            end = e if e is not None else dur
            if end and end > s:
                marks.append((s, end, MARKER_COLORS.get(key, "#888888")))
        self.player.set_markers(marks)

    def _preview_section(self, key):
        if not self.player.has_video():
            messagebox.showinfo("No video", "Load a video first.")
            return
        _on, ef, et = self.sections[key]
        s, s_ok = ef.get_seconds()
        e, e_ok = et.get_seconds()
        if s is None and s_ok and key == "preintro":
            s = 0.0            # empty pre-intro From = start of file
        if s is None or not s_ok:
            self.status_var.set("Fill the From time of that section first.")
            return
        if not e_ok:
            self.status_var.set("The section's To time is invalid.")
            return
        end = e if e is not None else self.player.timeline.duration
        if not end or end <= s:
            self.status_var.set("The section's To time must be after its From time.")
            return
        self.status_var.set(f"Previewing {key} section {fmt_time(s)} -> {fmt_time(end)}")
        self.player.play_range(s, end)

    def _mark(self, key, which):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo("No video", "Load a video first, then scrub to the point.")
            return
        on, ef, et = self.sections[key]
        on.set(True)
        entry = ef if which == "from" else et
        entry.set_seconds(sec)
        entry.flash()
        self._refresh_markers()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _drop_load(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def load_from_entry(self):
        path = self.file_var.get().strip().strip('"')
        if path and os.path.isfile(path):
            self.player.load(path)
            self._refresh_markers()
        else:
            messagebox.showerror("Error", "Type or browse to a valid video file first.")

    def browse(self):
        path = filedialog.askopenfilename(title="Select video", filetypes=_VIDEO_TYPES)
        if path:
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    # ---------------- cut templates (manual) ----------------
    def _cut_clip(self, video, out, s, e):
        """Cut [s, e) of video into out (visually-lossless x264, copy audio).
        e=None means to end of file. Returns (ok, stderr_text)."""
        os.makedirs(os.path.dirname(out), exist_ok=True)
        cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", video, "-ss", "0"]
        if e is not None:
            cmd += ["-t", f"{e - s:.3f}"]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "superfast", "-c:a", "copy", out]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=POPEN_FLAGS)
        return proc.returncode == 0, proc.stderr.decode(errors="replace")

    def start_cut(self):
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror("Error", "Please select a valid video file.")
            return
        jobs = []
        for key, label, _default_on, need_to in self.SECTION_SPECS:
            on, ef, et = self.sections[key]
            if not on.get():
                continue
            s, s_ok = ef.get_seconds()
            e, e_ok = et.get_seconds()
            if s is None and s_ok and key == "preintro":
                s = 0.0        # pre-intro starts at the very beginning by definition
            if not s_ok or s is None:
                messagebox.showerror("Error", f"{label.split(' (')[0]}: needs a valid From time.")
                return
            if e is None:
                if need_to:
                    messagebox.showerror("Error", f"{label.split(' (')[0]}: needs a valid To time.")
                    return
            elif not e_ok or e <= s:
                messagebox.showerror("Error", f"{label.split(' (')[0]}: To must be after From.")
                return
            jobs.append((key, s, e))
        if not jobs:
            messagebox.showerror("Error", "Enable at least one section first.")
            return
        self.cut_stop.clear()
        self.cut_btn.configure(state="disabled")
        self.cut_stop_btn.configure(state="normal")
        self.status_var.set("Cutting template(s)...")
        self.bar.start(12)
        threading.Thread(target=self.worker, args=(video, jobs), daemon=True).start()

    def stop_cut(self):
        self.cut_stop.set()
        self.log("[CUT] stopping after the current section...")

    def worker(self, video, jobs):
        try:
            name, ext = os.path.splitext(os.path.basename(video))
            name = _template_stem(name)
            stopped = False
            for key, s, e in jobs:
                if self.cut_stop.is_set():
                    stopped = True
                    break
                out = os.path.join(self.DIRMAP[key], f"{name}_{key}{ext}")
                self.log(f"[{key.upper()}] {fmt_time(s)} -> {fmt_time(e) if e is not None else 'end'}  ->  {out}")
                ok, err = self._cut_clip(video, out, s, e)
                if ok:
                    self.log("  [OK] saved")
                else:
                    for ln in err.strip().splitlines()[-4:]:
                        self.log(f"    | {ln}")
                    self.log(f"  [FAIL] {key} template failed")
            self.log("Stopped." if (stopped or self.cut_stop.is_set()) else "Done.")
        except Exception as exc:
            self.log(f"[FAIL] template cut crashed: {exc}")
        finally:
            def _f():
                self.bar.stop()
                self.status_var.set("Stopped." if self.cut_stop.is_set() else "Done.")
                self.cut_btn.configure(state="normal")
                self.cut_stop_btn.configure(state="disabled")
            self.after(0, _f)

    # ---------------- auto-detect logic ----------------
    def _browse_detect(self):
        d = filedialog.askdirectory(title="Select the season / show folder")
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

    def _detect_running(self, on):
        self.detect_btn.configure(state="disabled" if on else "normal")
        self.autocut_btn.configure(state="disabled" if on else "normal")
        self.detect_stop_btn.configure(state="normal" if on else "disabled")

    def _detect_progress(self, frac, msg=""):
        def _a():
            self.detect_bar["value"] = int(frac * 1000)
            self.detect_status.set(msg)
        self.after(0, _a)

    def start_detect(self):
        folder = self.detect_dir.get().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Pick a valid season / show folder first.")
            return
        files = _list_media(folder)
        if len(files) < 2:
            messagebox.showerror("Error", "Need at least 2 episodes in the folder to detect what recurs.")
            return
        if not (self.det_intro.get() or self.det_credits.get()
                or self.det_preintro.get() or self.det_aftercredits.get()):
            messagebox.showerror("Error", "Tick at least one segment type to detect.")
            return
        try:
            window = float(self.det_window.get())
            pa = float(self.det_minlen_pa.get())
            minlens = {"intro": float(self.det_minlen_intro.get()),
                       "credits": float(self.det_minlen_credits.get()),
                       "preintro": pa, "aftercredits": pa}
            max_eps = int(self.det_max_eps.get() or 0)
        except ValueError:
            messagebox.showerror("Error", "Search window, min lengths and max episodes must be numbers.")
            return
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
        thresh = {"High (strict)": 0.9, "Medium": 0.8,
                  "Low (loose)": 0.7, "Very loose": 0.62}.get(self.det_sens.get(), 0.8)
        self.detect_stop.clear()
        self.detect_tree.delete(*self.detect_tree.get_children())
        self._clusters.clear()
        self._detect_running(True)
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
        threading.Thread(target=self._detect_worker,
                         args=(files, kinds, window, minlens, thresh, known, lang,
                               self.det_sens.get()),
                         daemon=True).start()

    def _detect_worker(self, files, kinds, window, minlens, thresh, known=None, lang=None,
                       sens_label=""):
        found = []
        try:
            self.log(f"[DETECT] {len(files)} episode(s) - detecting {', '.join(kinds)} "
                     f"(sensitivity {sens_label}"
                     + (f", {lang} audio" if lang else "") + ")")
            diag = {}
            found = detect_recurring_segments(
                files, kinds=kinds, window=window, min_lens=minlens, thresh=thresh,
                progress=self._detect_progress, stop_event=self.detect_stop,
                diag_out=diag, known=known, lang=lang)
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
                        self.log(f"[DETECT] {kind}: {len(new_cl)} NEW variant(s) found")
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
            self.log(f"[FAIL] detect crashed: {exc}")
        finally:
            def _fill():
                self._detect_running(False)
                self.detect_bar["value"] = 0
                n = 0
                for c in found:
                    rep = representative_member(c)
                    a, b = c["ranges"][rep]
                    iid = f"c{n}"
                    kind_disp = ("✔ " + c["kind"] + " (have)") if c.get("known") else c["kind"]
                    self.detect_tree.insert(
                        "", "end", iid=iid,
                        values=(kind_disp, c["count"], fmt_time(a), fmt_time(b),
                                f"{b - a:.1f}s", os.path.basename(rep)),
                        tags=("have",) if c.get("known") else ())
                    self._clusters[iid] = c
                    n += 1
                if not found:
                    self.detect_status.set("No recurring intro/credits found (try a larger window "
                                           "or lower min length).")
                else:
                    self.detect_status.set(f"Found {len(found)} segment(s). Select one -> Fill times, "
                                           "or Auto-cut all.")
            self.after(0, _fill)

    def _fill_from_selected(self):
        sel = self.detect_tree.selection()
        if not sel:
            messagebox.showinfo("Pick a row", "Select a detected row first.")
            return
        c = self._clusters.get(sel[0])
        if not c:
            return
        rep = representative_member(c)
        a, b = c["ranges"][rep]
        kind = c["kind"]
        self.file_var.set(rep)
        self.player.load(rep)
        on, ef, et = self.sections[kind]
        on.set(True)
        ef.set_seconds(a)
        et.set_seconds(b)
        self._refresh_markers()
        self._nb.select(0)
        self.log(f"[DETECT] filled {kind} {fmt_time(a)}->{fmt_time(b)} from {os.path.basename(rep)} "
                 "- review in the player and nudge if needed.")

    def _autocut_all(self):
        if not self._clusters:
            messagebox.showinfo("Nothing to cut", "Run Detect first.")
            return
        self.detect_stop.clear()
        self._detect_running(True)
        clusters = list(self._clusters.values())
        threading.Thread(target=self._autocut_worker, args=(clusters,), daemon=True).start()

    def _autocut_worker(self, clusters):
        try:
            self.log(f"[DETECT] auto-cutting {len(clusters)} template(s)...")
            for c in clusters:
                if self.detect_stop.is_set():
                    self.log("[DETECT] auto-cut stopped.")
                    break
                if c.get("known"):
                    self.log(f"[DETECT] skip {c['kind']} x{c['count']} - already covered "
                             f"by template '{c['known']}'")
                    continue
                rep = representative_member(c)
                a, b = c["ranges"][rep]
                kind = c["kind"]
                name, ext = os.path.splitext(os.path.basename(rep))
                out = os.path.join(self.DIRMAP[kind], f"{_template_stem(name)}_{kind}{ext}")
                self.log(f"[{kind.upper()}] {fmt_time(a)} -> {fmt_time(b)}  ->  {out}")
                ok, err = self._cut_clip(rep, out, a, b)
                if ok:
                    self.log("  [OK] saved")
                else:
                    for ln in err.strip().splitlines()[-4:]:
                        self.log(f"    | {ln}")
                    self.log(f"  [FAIL] {kind} template failed")
            self.log("[DETECT] auto-cut done.")
        except Exception as exc:
            self.log(f"[FAIL] auto-cut crashed: {exc}")
        finally:
            self.after(0, lambda: self._detect_running(False))

    def snapshot(self):
        """Auto-detect settings to persist between sessions."""
        return {
            "detect_dir": self.detect_dir.get(),
            "det_intro": self.det_intro.get(),
            "det_credits": self.det_credits.get(),
            "det_preintro": self.det_preintro.get(),
            "det_aftercredits": self.det_aftercredits.get(),
            "det_window": self.det_window.get(),
            "det_minlen_intro": self.det_minlen_intro.get(),
            "det_minlen_credits": self.det_minlen_credits.get(),
            "det_minlen_pa": self.det_minlen_pa.get(),
            "det_max_eps": self.det_max_eps.get(),
            "det_sens": self.det_sens.get(),
            "det_eplen": self.det_eplen.get(),
            "det_lang": AUDIO_LANG_CHOICES.get(self.det_lang_var.get()),
        }


# ======================= Tab 2 - Cut / Edit =======================
class RemoverTab(ttk.Frame):
    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.save_hook = None
        self.unload_players_hook = None   # set by app.py: frees files locked by players
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        auto_outer = ttk.Frame(nb)
        manual_outer = ttk.Frame(nb)
        multi_outer = ttk.Frame(nb)
        nb.add(auto_outer, text="  Auto-detect  ")
        nb.add(manual_outer, text="  Manual cut  ")
        nb.add(multi_outer, text="  Multi cut  ")
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
        # one-time migration: after the template folders moved under input/, use the
        # new defaults once instead of any old root-level paths a user had saved.
        migrate = not saved.get("input_folders_v1", False)
        def _fd(key, const):
            return const if migrate else saved.get(key, const)

        self.dirs = {}
        folders = ttk.LabelFrame(auto, text=" Folders ", padding=(8, 4))
        folders.pack(fill="x", padx=6, pady=(6, 0))
        folders.columnconfigure(1, weight=1)      # left group entry stretches
        folders.columnconfigure(4, weight=1)      # right group entry stretches
        folder_specs = [
            ("Videos folder:", _fd("video_dir", VIDEO_DIR)),
            ("Intro templates:", _fd("intro_dir", INTRO_DIR)),
            ("Credits templates:", _fd("credits_dir", CREDITS_DIR)),
            ("Pre-intro templates:", _fd("preintro_dir", PREINTRO_DIR)),
            ("After-credits templates:", _fd("aftercredits_dir", AFTERCREDITS_DIR)),
            ("Output folder:", _fd("output_dir", OUTPUT_DIR)),
        ]
        for i, (label, default) in enumerate(folder_specs):
            col = 0 if i < 3 else 3               # first 3 left, next 3 right
            r = i % 3
            ttk.Label(folders, text=label).grid(row=r, column=col, sticky="w",
                                                padx=(4 if col == 0 else 14, 4), pady=2)
            var = tk.StringVar(value=default)
            self.dirs[label] = var
            fe = ttk.Entry(folders, textvariable=var)
            fe.grid(row=r, column=col + 1, sticky="we", padx=4, pady=2)
            enable_file_drop(fe, lambda p, v=var: self._drop_folder(v, p))
            ttk.Button(folders, text="...", width=3,
                       command=lambda v=var: self._pick_dir(v)).grid(row=r, column=col + 2, padx=2)

        enc = ttk.LabelFrame(auto, text=" Encoding ", padding=(8, 4))
        enc.pack(fill="x", padx=6, pady=(8, 0))
        enc.columnconfigure(1, weight=1)
        # LEFT column = codec / bit depth; RIGHT column = quality / preset
        eleft = ttk.Frame(enc)
        eleft.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        eright = ttk.Frame(enc)
        eright.grid(row=0, column=1, sticky="nw")

        ttk.Label(eleft, text="Codec:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        codec_saved = saved.get("codec", DEFAULT_CODEC_LABEL)
        codec_saved = LEGACY_CODEC_LABELS.get(codec_saved, codec_saved)   # old "Auto" -> "Auto - CPU"
        self.codec_var = tk.StringVar(value=codec_saved if codec_saved in CODECS else DEFAULT_CODEC_LABEL)
        codec_cb = ttk.Combobox(eleft, textvariable=self.codec_var, values=list(CODECS.keys()),
                                state="readonly", width=34)
        codec_cb.grid(row=0, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        add_tooltip(codec_cb, "Auto picks the encoder that matches each source's codec "
                    "family (HEVC source -> libx265, AV1 -> SVT-AV1, else libx264) so "
                    "the output doesn't balloon in size")
        ttk.Label(eleft, text="Bit depth:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        depth_saved = saved.get("bit_depth", "Auto (match source)")
        self.depth_var = tk.StringVar(value=depth_saved if depth_saved in BIT_DEPTHS else "Auto (match source)")
        ttk.Combobox(eleft, textvariable=self.depth_var, values=list(BIT_DEPTHS.keys()),
                     state="readonly", width=18).grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=2)

        # H.264 CRF on top; H.265/AV1 CRF sits on the Preset row so the two
        # quality fields stack and the row reads evenly
        lbl264 = ttk.Label(eright, text="CRF/CQ H.264:")
        lbl264.grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.crf_var = tk.StringVar(value=str(saved.get("crf", "18")))
        sp264 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf_var, width=5)
        sp264.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp264, "Quality for H.264 outputs (lower = better/bigger). 18-20 is a good range.")
        lbl265 = ttk.Label(eright, text="H.265 / AV1:")
        lbl265.grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.crf265_var = tk.StringVar(value=str(saved.get("crf_h265", "22")))
        sp265 = ttk.Spinbox(eright, from_=0, to=51, textvariable=self.crf265_var, width=5)
        sp265.grid(row=1, column=1, sticky="w", padx=4, pady=2)
        add_tooltip(sp265, "Quality for H.265 / AV1 outputs. These codecs match H.264's "
                    "quality at a higher number - H.264 CRF 18 is roughly H.265 CRF 22-23.")
        ttk.Label(eright, text="Preset:").grid(row=1, column=2, sticky="e", padx=(14, 4), pady=2)
        preset_saved = saved.get("preset", "slow")
        self.preset_var = tk.StringVar(value=preset_saved if preset_saved in VALID_PRESETS else "slow")
        preset_cb = ttk.Combobox(eright, textvariable=self.preset_var, values=VALID_PRESETS,
                                 state="readonly", width=10)
        preset_cb.grid(row=1, column=3, sticky="w", padx=4, pady=2)
        # size/speed impact vs medium at the same CRF (rough, codec-dependent)
        preset_info = {
            "ultrafast": "≈ +60-100% size, ~10x faster",
            "superfast": "≈ +40-70% size, ~8x faster",
            "veryfast": "≈ +20-40% size, ~5x faster",
            "faster": "≈ +10-20% size, ~3x faster",
            "fast": "≈ +5-15% size, ~2x faster",
            "medium": "baseline size & speed",
            "slow": "≈ 5-10% smaller, ~2x slower",
            "slower": "≈ 8-12% smaller, ~4x slower",
            "veryslow": "≈ 10-15% smaller, ~8x slower",
        }
        self.preset_hint = tk.StringVar()
        ttk.Label(eright, textvariable=self.preset_hint, style="Hint.TLabel").grid(
            row=1, column=4, sticky="w", padx=8, pady=2)

        def _upd_preset_hint(_e=None):
            self.preset_hint.set(preset_info.get(self.preset_var.get(), ""))
        preset_cb.bind("<<ComboboxSelected>>", _upd_preset_hint)
        _upd_preset_hint()

        det = ttk.LabelFrame(auto, text=" Detection & mode ", padding=(8, 4))
        det.pack(fill="x", padx=6, pady=(8, 0))
        det.columnconfigure(1, weight=1)
        # LEFT column = detection settings; RIGHT column = the run options
        left = ttk.Frame(det)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        right = ttk.Frame(det)
        right.grid(row=0, column=1, sticky="nw")

        # ---------- LEFT: keyframe/confidence, mode, segments, match audio ----------
        krow = ttk.Frame(left)
        krow.grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(krow, text="Keyframe every N s (0=cut pts):").pack(side="left")
        self.kf_var = tk.StringVar(value=str(saved.get("kf_interval", "0")))
        ttk.Entry(krow, textvariable=self.kf_var, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(krow, text="Min confidence:").pack(side="left")
        self.conf_var = tk.StringVar(value=str(saved.get("confidence", "0.32")))
        ttk.Entry(krow, textvariable=self.conf_var, width=6).pack(side="left", padx=4)

        self.mode_var = tk.StringVar(
            value=saved.get("mode", "cut") if saved.get("mode") in ("cut", "inject", "chapters") else "cut")
        ttk.Radiobutton(left, text="Cut intro/credits (+ pre/after) out", variable=self.mode_var,
                        value="cut").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Radiobutton(left, text="Keep video, inject keyframes only", variable=self.mode_var,
                        value="inject").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Radiobutton(left, text="Add chapter markers only (no cut - for Plex/Jellyfin skip)",
                        variable=self.mode_var, value="chapters").grid(row=3, column=0, sticky="w", pady=2)

        segf = ttk.Frame(left)
        segf.grid(row=4, column=0, sticky="w", pady=(6, 2))
        ttk.Label(segf, text="Segments:").pack(side="left", padx=(0, 6))
        self.use_seg = {}
        for key, txt in (("preintro", "Pre-intro"), ("intro", "Intro"),
                         ("credits", "Credits"), ("aftercredits", "After-credits")):
            v = tk.BooleanVar(value=bool(saved.get(f"use_{key}", True)))
            cb = ttk.Checkbutton(segf, text=txt, variable=v)
            cb.pack(side="left", padx=(0, 10))
            add_tooltip(cb, f"Untick to leave the {txt.lower()} alone in this run - its "
                        "templates are skipped entirely (also makes the run faster)")
            self.use_seg[key] = v

        langrow = ttk.Frame(left)
        langrow.grid(row=5, column=0, sticky="w", pady=(6, 2))
        ttk.Label(langrow, text="Match templates on audio:").pack(side="left")
        _lang_labels = list(AUDIO_LANG_CHOICES)
        _saved_ml = saved.get("match_lang")
        _ml_label = next((k for k, v in AUDIO_LANG_CHOICES.items() if v == _saved_ml), _lang_labels[0])
        self.match_lang_var = tk.StringVar(value=_ml_label)
        mlcb = ttk.Combobox(langrow, textvariable=self.match_lang_var, values=_lang_labels,
                            state="readonly", width=18)
        mlcb.pack(side="left", padx=(6, 0))
        add_tooltip(mlcb, "Which audio track the segment matching listens to. 'All / default' "
                    "checks every audio track and takes the best (language-proof, recommended). "
                    "Pick a language to match only that track (falls back to all if a file "
                    "doesn't have it).")

        # ---------- RIGHT: run options (paired in two sub-columns) ----------
        self.move_done_var = tk.BooleanVar(value=bool(saved.get("move_done", False)))
        mdcb = ttk.Checkbutton(right, text="Move finished videos to a 'done' subfolder",
                               variable=self.move_done_var)
        mdcb.grid(row=0, column=0, sticky="w", pady=1, padx=(0, 16))
        add_tooltip(mdcb, "After a video is successfully processed, move its SOURCE file into "
                    "videos/done so you can see what's left. Skipped or failed videos stay put.")

        self.intro_from_start_var = tk.BooleanVar(value=bool(saved.get("intro_from_start", False)))
        ifcb = ttk.Checkbutton(right, text="Intro: cut from file start to end of intro",
                               variable=self.intro_from_start_var)
        ifcb.grid(row=0, column=1, sticky="w", pady=1)
        add_tooltip(ifcb, "Extend the intro removal back to 0:00, so any recap / cold-open before "
                    "the intro is cut too. Only the intro END position is detected.")

        self.skip_incomplete_var = tk.BooleanVar(value=bool(saved.get("skip_incomplete", False)))
        sicb = ttk.Checkbutton(right, text="Skip the episode if an enabled segment isn't found",
                               variable=self.skip_incomplete_var)
        sicb.grid(row=1, column=0, sticky="w", pady=1, padx=(0, 16))
        add_tooltip(sicb, "If a ticked segment matches too weakly on an episode, leave that "
                    "episode untouched instead of outputting a partial cut. It stays in the "
                    "videos folder so you can spot and handle it.")

        self.credits_to_end_var = tk.BooleanVar(value=bool(saved.get("credits_to_end", False)))
        cecb = ttk.Checkbutton(right, text="Credits: cut from credits start to file end",
                               variable=self.credits_to_end_var)
        cecb.grid(row=1, column=1, sticky="w", pady=1)
        add_tooltip(cecb, "Extend the credits removal to the end of the file, so credits plus "
                    "everything after (next-episode preview, etc.) are all cut. Only the credits "
                    "START position is detected.")

        self.trim_match_var = tk.BooleanVar(value=bool(saved.get("trim_to_match", False)))
        tmcb = ttk.Checkbutton(right, text="Trim the cut to where the audio still matches",
                               variable=self.trim_match_var)
        tmcb.grid(row=2, column=0, columnspan=2, sticky="w", pady=1)
        add_tooltip(tmcb, "Normally the cut length equals the template length. If an episode's "
                    "segment is genuinely shorter, this shortens the cut to where the template "
                    "stops matching, so it won't chop into the episode (never below half).")

        anrow = ttk.Frame(right)
        anrow.grid(row=3, column=0, columnspan=2, sticky="w", pady=1)
        self.anchor_var = tk.BooleanVar(value=bool(saved.get("anchor_cut", False)))
        ancb = ttk.Checkbutton(anrow, text="Anchor the cut to start & end",
                               variable=self.anchor_var)
        ancb.pack(side="left")
        add_tooltip(ancb, "Match just the first and last few seconds of the template separately "
                    "and cut between them, so the cut follows each episode's real boundaries even "
                    "when the length varies. Uses your existing templates. (Supersedes Trim.)")
        ttk.Label(anrow, text=" ends of").pack(side="left")
        self.anchor_secs_var = tk.StringVar(value=str(saved.get("anchor_secs", "15")))
        ttk.Spinbox(anrow, from_=5, to=40, textvariable=self.anchor_secs_var, width=4).pack(side="left", padx=(4, 2))
        ttk.Label(anrow, text="sec").pack(side="left")

        self.subs_langs = set(saved.get("subs_langs") or (["eng", "und"]
                              if saved.get("subs_english") else []))
        self.subs_filter_var = tk.BooleanVar(
            value=bool(saved.get("subs_filter", saved.get("subs_english", False))))
        sfcb = ttk.Checkbutton(right, text="Keep only chosen subtitle languages",
                               variable=self.subs_filter_var, command=self._upd_subs_label)
        sfcb.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 1))
        add_tooltip(sfcb, "Drop the subtitle languages you don't want (video and all audio "
                    "tracks are untouched). 'und' = untagged/unknown tracks, which are often "
                    "English or a forced track - keep it if unsure.")
        subrow = ttk.Frame(right)
        subrow.grid(row=5, column=0, columnspan=2, sticky="w", pady=1)
        ttk.Button(subrow, text="Choose languages...", command=self._choose_subs).pack(side="left", padx=(0, 6))
        self.subs_lbl = tk.StringVar()
        ttk.Label(subrow, textvariable=self.subs_lbl, style="Hint.TLabel").pack(side="left")
        self._upd_subs_label()

        runrow = ttk.Frame(auto)
        runrow.pack(fill="x", padx=6, pady=(10, 6))
        self.start_btn = ttk.Button(runrow, text="Start batch (auto-detect)", command=self.start)
        self.start_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.start_btn, "Auto-detect intro, credits, and (if you have templates) "
                    "pre-intro and after-credits in every video, then cut them out")
        # Stop lives beside the progress bar at the bottom (shared by all tools)

        # ===================== Manual cut: player + points =====================
        # file picker on top (full width), then player LEFT / cut controls RIGHT
        # side by side, like the Template Cutter.
        mtop = ttk.Frame(manual)
        mtop.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(mtop, text="Preview:").pack(side="left", padx=(0, 6))
        self.sel_var = tk.StringVar(value=saved.get("last_video", ""))
        ment = ttk.Entry(mtop, textvariable=self.sel_var)
        ment.pack(side="left", fill="x", expand=True)
        ttk.Button(mtop, text="Browse...", command=self._browse_sel).pack(side="left", padx=6)
        ttk.Button(mtop, text="Load", command=self._load_sel).pack(side="left")

        mbody = ttk.Frame(manual)
        mbody.pack(fill="x", padx=6, pady=(6, 0))
        mbody.columnconfigure(1, weight=1)
        mleft = ttk.Frame(mbody)
        mleft.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        mright = ttk.Frame(mbody)
        mright.grid(row=0, column=1, sticky="nsew")

        self.player = VideoPlayer(mleft, width=480, height=270, log_fn=self.log)
        self.player.pack()
        self.player.enable_tab_shortcuts()   # arrows/space work anywhere on the tab

        man = ttk.LabelFrame(mright, text=" Manual cut points for the previewed video ", padding=(8, 4))
        man.pack(fill="x")
        self._manual = {}
        for key in ("preintro", "intro", "credits", "aftercredits"):
            self._manual[key] = (TimeEntry(man), TimeEntry(man))

        def _mrow(label, r, key):
            ef, et = self._manual[key]
            ttk.Label(man, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=2)
            ef.grid(row=r, column=1, padx=2, pady=2)
            b1 = ttk.Button(man, text="Set", width=5, command=lambda: self._mark_manual(ef))
            b1.grid(row=r, column=2, padx=2)
            add_tooltip(b1, f"Set the {key} START to the current frame")
            g1 = ttk.Button(man, text="Go", width=4, command=lambda: self._goto_manual(ef))
            g1.grid(row=r, column=3, padx=(0, 8))
            add_tooltip(g1, f"Jump the player to the {key} START time typed in the box")
            et.grid(row=r, column=4, padx=2, pady=2)
            b2 = ttk.Button(man, text="Set", width=5, command=lambda: self._mark_manual(et))
            b2.grid(row=r, column=5, padx=2)
            add_tooltip(b2, f"Set the {key} END to the current frame")
            g2 = ttk.Button(man, text="Go", width=4, command=lambda: self._goto_manual(et))
            g2.grid(row=r, column=6, padx=(0, 8))
            add_tooltip(g2, f"Jump the player to the {key} END time typed in the box")
            b3 = ttk.Button(man, text="▶", width=3,
                            command=lambda k=key: self._preview_manual(k))
            b3.grid(row=r, column=7, padx=(6, 0))
            add_tooltip(b3, f"Play only the {key} section (From to To) in the player")
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._refresh_markers())

        _mrow("Pre-intro from / to:", 0, "preintro")
        _mrow("Intro from / to:", 1, "intro")
        _mrow("Credits from / to:", 2, "credits")
        _mrow("After-credits from / to:", 3, "aftercredits")

        # subtitle-language picker for the previewed file (shares the same
        # keep-list as the Auto-detect and Multi cut tabs)
        msubf = ttk.LabelFrame(mright, text=" Subtitles ", padding=(8, 4))
        msubf.pack(fill="x", pady=(6, 0))
        manf = ttk.Checkbutton(msubf, text="Keep only chosen subtitle languages",
                               variable=self.subs_filter_var, command=self._upd_subs_label)
        manf.pack(anchor="w")
        add_tooltip(manf, "Drop subtitle languages you don't want (video and all audio tracks are "
                    "untouched). This shares the same choice as the Auto-detect and Multi cut tabs.")
        manrow = ttk.Frame(msubf)
        manrow.pack(fill="x", pady=1)
        ttk.Button(manrow, text="Choose languages...", command=self._manual_choose_subs).pack(side="left", padx=(0, 6))
        self.manual_subs_lbl = tk.StringVar()
        ttk.Label(manrow, textvariable=self.manual_subs_lbl, style="Hint.TLabel").pack(side="left")

        self.cut_sel_btn = ttk.Button(mright, text="Cut previewed video (manual)", command=self.cut_selected)
        self.cut_sel_btn.pack(fill="x", pady=(6, 0))
        add_tooltip(self.cut_sel_btn, "Cut ONLY the previewed video using the ranges above (ignores auto-detect)")
        ttk.Label(mright, text="Leave a section's boxes empty to skip it. Manual cut removes the "
                             "filled ranges and keeps the rest, using the Encoding settings and the "
                             "subtitle choice above.",
                  style="Hint.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(2, 6))
        self._upd_subs_label()      # fill in the new label now that it exists

        enable_file_drop_deep(self.player, self._drop_load_sel)   # drop anywhere in the player
        enable_file_drop(ment, self._drop_load_sel)

        # ===================== Multi cut: many files, per-file times =============
        self._build_multi_tab(multi, saved)

        # ===================== shared status + progress + log =====================
        # anchored to the window bottom (non-scrolling) when `bottom` is given
        self.status_var = tk.StringVar(value="Idle")
        host = bottom if bottom is not None else self
        if bottom is not None:
            ttk.Label(host, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prog = ttk.Frame(host)
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(host, textvariable=self.status_var, style="Hint.TLabel").grid(
                row=1, column=0, sticky="w", pady=(6, 0))
            prog = ttk.Frame(host)
            prog.grid(row=2, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=44).grid(row=0, column=1, sticky="w", padx=(6, 0))
        # single Stop in the right corner, beside the progress bar - it's always
        # visible and drives whichever tool (Auto-detect / Manual / Multi) is running
        self.stop_btn = ttk.Button(prog, text="Stop", command=self.stop, state="disabled", width=8)
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, "Stop after the current ffmpeg step finishes")
        self.logbox = build_log_tab(nb)

    # ---------------- markers / player ----------------
    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        dur = self.player.timeline.duration
        marks = []
        for key, (ef, et) in self._manual.items():
            s, _ = ef.get_seconds()
            e, _ = et.get_seconds()
            if s is None:
                continue
            end = e if e is not None else dur
            if end and end > s:
                marks.append((s, end, MARKER_COLORS.get(key, "#888888")))
        self.player.set_markers(marks)

    def _preview_manual(self, key):
        if not self.player.has_video():
            messagebox.showinfo("No video", "Load a video in the preview first.")
            return
        ef, et = self._manual[key]
        s, s_ok = ef.get_seconds()
        e, e_ok = et.get_seconds()
        if s is None and s_ok and key == "preintro":
            s = 0.0            # empty pre-intro From = start of file
        if s is None or not s_ok:
            self.status_var.set("Fill the From time of that section first.")
            return
        if not e_ok:
            self.status_var.set("The section's To time is invalid.")
            return
        end = e if e is not None else self.player.timeline.duration
        if not end or end <= s:
            self.status_var.set("The section's To time must be after its From time.")
            return
        self.status_var.set(f"Previewing {key} section {fmt_time(s)} -> {fmt_time(end)}")
        self.player.play_range(s, end)

    def _mark_manual(self, entry):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo("No video", "Load a video in the preview first, then scrub.")
            return
        entry.set_seconds(sec)
        entry.flash()
        self._refresh_markers()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _goto_manual(self, entry):
        if not self.player.has_video():
            messagebox.showinfo("No video", "Load a video in the preview first.")
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set("Type a valid time in the box first, then Go.")
            return
        self.player.seek_seconds(sec)
        entry.flash()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="Select folder")
        if path:
            var.set(path)

    def _drop_folder(self, var, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isdir(path):
            var.set(path)

    def _drop_load_sel(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.sel_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    # ---------------- subtitle language filter ----------------
    def _upd_subs_label(self):
        if not self.subs_filter_var.get():
            txt = "(keeping all subtitle tracks)"
        elif self.subs_langs:
            txt = "keeping: " + ", ".join(sorted(self.subs_langs))
        else:
            txt = "none chosen - click 'Choose languages...'"
        self.subs_lbl.set(txt)
        if hasattr(self, "multi_subs_lbl"):      # mirror onto the Multi cut tab
            self.multi_subs_lbl.set(txt)
        if hasattr(self, "manual_subs_lbl"):     # ... and the Manual cut tab
            self.manual_subs_lbl.set(txt)

    def _centre_dialog(self, dlg):
        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = max(0, (dlg.winfo_screenwidth() - w) // 2)
        y = max(0, (dlg.winfo_screenheight() - h) // 2)
        dlg.geometry(f"+{x}+{y}")

    def _choose_subs(self):
        folder = self.dirs["Videos folder:"].get().strip().strip('"')
        files = _list_media(folder) if folder else []
        if not files:
            messagebox.showinfo("No videos", "Set the Videos folder first so I can scan "
                                "which subtitle languages are in the files.")
            return
        self._open_subs_dialog(files)

    def _multi_choose_subs(self):
        files = [p for p in self._multi_paths.values() if os.path.isfile(p)]
        if not files:
            messagebox.showinfo("No files", "Add files to the Multi cut list first so I can "
                                "scan which subtitle languages they contain.")
            return
        self._open_subs_dialog(files)

    def _manual_choose_subs(self):
        video = self.sel_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showinfo("No video", "Load a video in the preview first so I can scan "
                                "which subtitle languages it contains.")
            return
        self._open_subs_dialog([video])

    def _open_subs_dialog(self, files):
        # open the dialog right away with a progress bar; it fills in when done
        dlg = tk.Toplevel(self)
        dlg.title("Choose subtitle languages to keep")
        dlg.transient(self.winfo_toplevel())
        body = ttk.Frame(dlg, padding=10)
        body.pack(fill="both", expand=True)
        scan_stop = threading.Event()
        status = tk.StringVar(value=f"Scanning {len(files)} file(s) for subtitle tracks...")
        ttk.Label(body, textvariable=status).pack(anchor="w")
        bar = ttk.Progressbar(body, mode="determinate", maximum=len(files), length=400)
        bar.pack(fill="x", pady=(8, 6))
        _cancel = lambda: (scan_stop.set(), dlg.destroy())
        ttk.Button(body, text="Cancel", command=_cancel).pack(anchor="e")
        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        self._centre_dialog(dlg)
        dlg.grab_set()

        def prog(done, total):
            def _u():
                if dlg.winfo_exists():
                    bar.configure(value=done)
                    status.set(f"Scanning subtitles {done}/{total}...")
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
            ttk.Label(body, text="No subtitle tracks found in the files.").pack(anchor="w")
            ttk.Button(body, text="Close", command=dlg.destroy).pack(anchor="e", pady=(8, 0))
            self._centre_dialog(dlg)
            return

        ttk.Label(body, text="Tick the subtitle languages to KEEP (others are dropped). "
                  "'und' = untagged/unknown - often English or forced.",
                  wraplength=420, justify="left").pack(anchor="w", pady=(0, 6))
        vars_by_lang = {}
        for d in inv:
            v = tk.BooleanVar(value=(d["lang"] in self.subs_langs) or not self.subs_langs)
            vars_by_lang[d["lang"]] = v
            bits = [d["lang"]]
            if d.get("title"):
                bits.append(f"“{d['title']}”")
            if d.get("forced"):
                bits.append("[forced]")
            bits.append(f"- in {d['count']} file(s)")
            ttk.Checkbutton(body, text="  ".join(bits), variable=v).pack(anchor="w", padx=6, pady=1)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        def _ok():
            self.subs_langs = {lang for lang, v in vars_by_lang.items() if v.get()}
            self.subs_filter_var.set(True)
            self._upd_subs_label()
            dlg.destroy()
        ttk.Button(btns, text="OK", command=_ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(0, 6))
        self._centre_dialog(dlg)

    def _browse_sel(self):
        path = filedialog.askopenfilename(title="Select video", filetypes=_VIDEO_TYPES)
        if path:
            self.sel_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def _load_sel(self):
        path = self.sel_var.get().strip().strip('"')
        if path and os.path.isfile(path):
            self.player.load(path)
            self._refresh_markers()
        else:
            messagebox.showerror("Error", "Type or browse to a valid video file first.")

    @staticmethod
    def _rng(ef, et, label, zero_from=False):
        s, s_ok = ef.get_seconds()
        t, t_ok = et.get_seconds()
        if not s_ok:
            raise ValueError(f"{label}: invalid From time.")
        if not t_ok:
            raise ValueError(f"{label}: invalid To time.")
        if s is None and t is None:
            return None
        if s is None and s_ok and zero_from:
            s = 0.0            # empty From = start of file (pre-intro)
        if not s_ok or s is None:
            raise ValueError(f"{label}: invalid From time.")
        if t is None or not t_ok or t <= s:
            raise ValueError(f"{label}: To must be a valid time after From.")
        return (s, t)

    def cut_selected(self):
        video = self.sel_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror("Error", "Load a video in the preview first.")
            return
        try:
            crf = int(self.crf_var.get())
            crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get())
            assert kf >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("Error", "Check the CRF values (0-51) and keyframe interval (>=0).")
            return
        try:
            drops = []
            for key, label in (("preintro", "Pre-intro"), ("intro", "Intro"),
                               ("credits", "Credits"), ("aftercredits", "After-credits")):
                ef, et = self._manual[key]
                rng = self._rng(ef, et, label, zero_from=(key == "preintro"))
                if rng:
                    drops.append(rng)
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        total = probe_duration(video)
        if not total:
            messagebox.showerror("Error", "Could not read the video's duration.")
            return
        keep = keep_from_drops(total, drops)
        if not keep:
            messagebox.showerror("Error", "Those ranges leave nothing to keep.")
            return
        cfg = {
            "video": video,
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": crf, "crf_h265": crf265, "preset": self.preset_var.get(),
            "encoder": CODECS.get(self.codec_var.get(), "libx264"),
            "bit_depth": BIT_DEPTHS.get(self.depth_var.get(), "auto"),
            "kf_interval": kf,
            "subs_langs": (sorted(self.subs_langs)
                           if self.subs_filter_var.get() and self.subs_langs else None),
            "move_done": self.move_done_var.get(),
        }
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True, [video] if cfg["move_done"] else None)
        if cfg["move_done"] and callable(self.unload_players_hook):
            self.unload_players_hook()      # free the file so its source can move
        threading.Thread(target=self._manual_worker, args=(cfg, keep), daemon=True).start()

    def _manual_worker(self, cfg, keep):
        try:
            run_manual(cfg, keep, self, self.stop_event)
        except Exception as e:
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            self.after(0, lambda: self._running(False))

    # ---------------- Multi cut (many files, per-file times) ----------------
    MULTI_SEGS = (("preintro", "Pre-intro"), ("intro", "Intro"),
                  ("credits", "Credits"), ("aftercredits", "After-credits"))

    def _build_multi_tab(self, multi, saved):
        btns = ttk.Frame(multi)
        btns.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(btns, text="Add files...", command=self._multi_add).pack(side="left")
        ttk.Button(btns, text="Remove selected", command=self._multi_remove).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear all", command=self._multi_clear).pack(side="left")
        info = ttk.Label(btns, text="ⓘ", style="Hint.TLabel", cursor="question_arrow")
        info.pack(side="left", padx=(8, 0))
        add_tooltip(info, "Cut the intro/credits (and pre/after) out of several files, each with "
                    "its own times. Add files, click one to set its sections - or set them once "
                    "and 'Apply to all'. Then Cut all.")
        self._multi_keep_pos_var = tk.BooleanVar(value=bool(saved.get("multi_keep_pos", True)))
        kpcb = ttk.Checkbutton(btns, text="Keep player time when switching files",
                               variable=self._multi_keep_pos_var)
        kpcb.pack(side="right")
        add_tooltip(kpcb, "When you click another file, open it at the SAME timestamp instead of "
                    "the start - so you can jump between episodes and instantly see if the intro "
                    "sits at the same position. Untick to always start each clip at 0:00.")

        tvf = ttk.Frame(multi)
        tvf.pack(fill="x", padx=6, pady=(4, 0))
        cols = ("file",) + tuple(k for k, _ in self.MULTI_SEGS)
        self.multi_tree = ttk.Treeview(tvf, columns=cols, show="headings", height=6)
        self.multi_tree.heading("file", text="File")
        self.multi_tree.column("file", width=300, anchor="w", stretch=True)
        for k, txt in self.MULTI_SEGS:
            self.multi_tree.heading(k, text=txt)
            self.multi_tree.column(k, width=100, anchor="center", stretch=False)
        self.multi_tree.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.multi_tree.yview)
        self.multi_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")
        self.multi_tree.bind("<<TreeviewSelect>>", self._multi_on_select)
        enable_paths_drop(self.multi_tree, self._multi_add_paths)

        self._multi_paths = {}          # iid -> full path
        self._multi_seg = {}            # iid -> {key: [from_sec|None, to_sec|None]}
        self._multi_sel = None
        self._multi_loading = False
        self._multi_last_pos = 0.0      # remembered player time across files

        # player LEFT / cut controls RIGHT, side by side like the Template Cutter
        mbody = ttk.Frame(multi)
        mbody.pack(fill="x", padx=6, pady=(6, 0))
        mbody.columnconfigure(1, weight=1)
        mleft = ttk.Frame(mbody)
        mleft.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        mright = ttk.Frame(mbody)
        mright.grid(row=0, column=1, sticky="nsew")

        self.multi_player = VideoPlayer(mleft, width=480, height=270, log_fn=self.log)
        self.multi_player.pack()
        self.multi_player.enable_tab_shortcuts()   # arrows/space work anywhere on the sub-tab
        enable_file_drop_deep(self.multi_player, self._multi_drop)

        # Up/Down = previous / next episode, anywhere on the Multi cut sub-tab
        # (unless you're typing in a text box), matching the player's tab-wide
        # Left/Right frame-step keys.
        top = self.winfo_toplevel()
        top.bind("<Up>", self._multi_nav_key(-1), add="+")
        top.bind("<Down>", self._multi_nav_key(1), add="+")
        add_tooltip(self.multi_tree, "Up / Down arrow keys jump to the previous / next file.")

        seg = ttk.LabelFrame(mright, text=" Cut sections for the selected file ", padding=(8, 4))
        seg.pack(fill="x")
        self._multi_entries = {}
        for r, (key, label) in enumerate(self.MULTI_SEGS):
            ef, et = TimeEntry(seg), TimeEntry(seg)
            self._multi_entries[key] = (ef, et)
            ttk.Label(seg, text=f"{label} from / to:").grid(row=r, column=0, sticky="e", padx=4, pady=2)
            ef.grid(row=r, column=1, padx=2)
            sfb = ttk.Button(seg, text="Set", width=5, command=lambda e=ef: self._multi_set(e))
            sfb.grid(row=r, column=2, padx=2)
            gfb = ttk.Button(seg, text="Go", width=4, command=lambda e=ef: self._multi_goto(e))
            gfb.grid(row=r, column=3, padx=(0, 8))
            et.grid(row=r, column=4, padx=2)
            stb = ttk.Button(seg, text="Set", width=5, command=lambda e=et: self._multi_set(e))
            stb.grid(row=r, column=5, padx=2)
            gtb = ttk.Button(seg, text="Go", width=4, command=lambda e=et: self._multi_goto(e))
            gtb.grid(row=r, column=6, padx=(0, 8))
            ttk.Button(seg, text="▶", width=3, command=lambda k=key: self._multi_preview(k)).grid(row=r, column=7, padx=(6, 0))
            if r == 0:
                add_tooltip(gfb, "Jump the player to the time typed in this box (manual entry).")
                add_tooltip(sfb, "Copy the player's current time into this box.")
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._multi_sync())

        arow = ttk.Frame(mright)
        arow.pack(fill="x", pady=(6, 0))
        asb = ttk.Button(arow, text="Apply to selected", command=self._multi_apply_selected)
        asb.pack(side="left")
        add_tooltip(asb, "Copy the section times in the boxes to the file(s) you've highlighted in "
                    "the list. Ctrl-click or Shift-click to select several.")
        ab = ttk.Button(arow, text="Apply to ALL files", command=self._multi_apply_all)
        ab.pack(side="left", padx=(6, 0))
        add_tooltip(ab, "Copy the section times currently in the boxes to EVERY file in the list "
                    "at once - handy when the intro/credits sit at the same spot in all of them.")

        # subtitle-language picker for the Multi cut list (shares the same
        # keep-list as the Auto-detect tab, but scans THESE files)
        subf = ttk.LabelFrame(mright, text=" Subtitles ", padding=(8, 4))
        subf.pack(fill="x", pady=(6, 0))
        msfcb = ttk.Checkbutton(subf, text="Keep only chosen subtitle languages",
                                variable=self.subs_filter_var, command=self._upd_subs_label)
        msfcb.pack(anchor="w")
        add_tooltip(msfcb, "Drop subtitle languages you don't want (video and all audio tracks are "
                    "untouched). This shares the same choice as the Auto-detect tab.")
        msubrow = ttk.Frame(subf)
        msubrow.pack(fill="x", pady=1)
        ttk.Button(msubrow, text="Choose languages...", command=self._multi_choose_subs).pack(side="left", padx=(0, 6))
        self.multi_subs_lbl = tk.StringVar()
        ttk.Label(msubrow, textvariable=self.multi_subs_lbl, style="Hint.TLabel").pack(side="left")

        self.multi_cut_btn = ttk.Button(mright, text="Cut all files", command=self._multi_cut_all)
        self.multi_cut_btn.pack(fill="x", pady=(8, 0))
        ttk.Label(mright, text="Each file has its filled sections removed and the rest kept, using the "
                  "Encoding settings from the Auto-detect tab and the subtitle choice above. "
                  "Files with no sections set are skipped.",
                  style="Hint.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(2, 6))
        self._upd_subs_label()      # fill in the new label now that it exists

    @staticmethod
    def _short_rng(fr, to):
        if fr is None and to is None:
            return "-"
        a = format_seconds(fr) if fr is not None else "0:00"
        b = format_seconds(to) if to is not None else "end"
        return f"{a}-{b}"

    def _multi_add(self):
        paths = filedialog.askopenfilenames(title="Add videos", filetypes=_VIDEO_TYPES)
        self._multi_add_paths(paths)

    def _multi_add_paths(self, paths):
        added = None
        for p in (paths or []):
            p = (p or "").strip().strip('"')
            if not p or not os.path.isfile(p) or p in self._multi_paths.values():
                continue
            iid = self.multi_tree.insert("", "end",
                                         values=(os.path.basename(p),) + ("-",) * len(self.MULTI_SEGS))
            self._multi_paths[iid] = p
            self._multi_seg[iid] = {k: [None, None] for k, _ in self.MULTI_SEGS}
            added = iid
        if added:
            self.multi_tree.selection_set(added)
            self.multi_tree.see(added)

    def _multi_drop(self, path):
        self._multi_add_paths([path])

    def _multi_remove(self):
        for iid in self.multi_tree.selection():
            self._multi_paths.pop(iid, None)
            self._multi_seg.pop(iid, None)
            self.multi_tree.delete(iid)
        self._multi_sel = None

    def _multi_clear(self):
        self.multi_tree.delete(*self.multi_tree.get_children())
        self._multi_paths.clear()
        self._multi_seg.clear()
        self._multi_sel = None

    def _multi_nav_key(self, delta):
        """Toplevel Up/Down handler: only act while the Multi cut player is the
        visible one and you're not typing in a text box, so it doesn't hijack the
        arrow keys on other tabs or inside the time entries."""
        def handler(_e):
            if self.focus_get() is self.multi_tree:
                return None   # the tree's own Up/Down already moves the selection
            if not self.multi_player.winfo_viewable() or self.multi_player.typing_focused():
                return None
            return self._multi_nav(delta)
        return handler

    def _multi_nav(self, delta):
        """Move the file selection up/down the list (arrow keys) so you can flip
        between episodes fast. Selecting fires _multi_on_select, which loads the
        clip (and keeps the player time if that option is on)."""
        kids = self.multi_tree.get_children()
        if not kids:
            return "break"
        sel = self.multi_tree.selection()
        cur = sel[0] if (sel and sel[0] in kids) else (
            self._multi_sel if self._multi_sel in kids else None)
        idx = (kids.index(cur) + delta) if cur is not None else (0 if delta > 0 else len(kids) - 1)
        idx = max(0, min(idx, len(kids) - 1))
        nid = kids[idx]
        self.multi_tree.selection_set(nid)
        self.multi_tree.focus(nid)
        self.multi_tree.see(nid)
        return "break"

    def _multi_on_select(self, _e=None):
        sel = self.multi_tree.selection()
        if not sel:
            return
        iid = sel[0]
        self._multi_sel = iid
        # remember where we were in the clip we're leaving, so the next one
        # opens at the SAME timestamp - a quick way to check whether the intro
        # sits at the same spot in the next episode.
        prev = self.multi_player.current_seconds()
        if prev is not None:
            self._multi_last_pos = prev
        path = self._multi_paths.get(iid)
        if path and os.path.isfile(path):
            if self.multi_player.load(path) and self._multi_keep_pos_var.get() \
                    and self._multi_last_pos:
                self.multi_player.seek_seconds(self._multi_last_pos)
        # fill the section boxes from this file's stored times (without the
        # trace writing an empty range back mid-update)
        self._multi_loading = True
        ranges = self._multi_seg.get(iid, {})
        for key, (ef, et) in self._multi_entries.items():
            fr, to = ranges.get(key, [None, None])
            ef.set_seconds(fr) if fr is not None else self._multi_clear_entry(ef)
            et.set_seconds(to) if to is not None else self._multi_clear_entry(et)
        self._multi_loading = False
        self._multi_markers()

    @staticmethod
    def _multi_clear_entry(te):
        for v in te.vars:
            v.set("")

    def _multi_read_entries(self):
        """Current section times from the boxes: {key: [from, to]}."""
        out = {}
        for key, (ef, et) in self._multi_entries.items():
            fr, _ = ef.get_seconds()
            to, _ = et.get_seconds()
            out[key] = [fr, to]
        return out

    def _multi_write_row(self, iid):
        ranges = self._multi_seg.get(iid, {})
        for key, _ in self.MULTI_SEGS:
            fr, to = ranges.get(key, [None, None])
            self.multi_tree.set(iid, key, self._short_rng(fr, to))

    def _multi_sync(self):
        if self._multi_loading or not self._multi_sel:
            return
        iid = self._multi_sel
        self._multi_seg[iid] = self._multi_read_entries()
        self._multi_write_row(iid)
        self._multi_markers()

    def _multi_apply_to(self, iids, what):
        """Copy the current box times onto each iid in `iids`."""
        times = self._multi_read_entries()
        if all(fr is None and to is None for fr, to in times.values()):
            messagebox.showinfo("Nothing set", "Set at least one section's times first.")
            return
        for iid in iids:
            self._multi_seg[iid] = {k: list(v) for k, v in times.items()}
            self._multi_write_row(iid)
        self.status_var.set(f"Applied the section times to {len(iids)} {what} file(s).")

    def _multi_apply_all(self):
        if not self._multi_paths:
            messagebox.showinfo("No files", "Add some files first.")
            return
        self._multi_apply_to(list(self._multi_paths), "")

    def _multi_apply_selected(self):
        sel = [i for i in self.multi_tree.selection() if i in self._multi_paths]
        if not sel:
            messagebox.showinfo("No selection", "Highlight one or more files in the list first "
                                "(Ctrl-click or Shift-click for several).")
            return
        self._multi_apply_to(sel, "selected")

    def _multi_markers(self):
        dur = self.multi_player.timeline.duration
        marks = []
        for key, (ef, et) in self._multi_entries.items():
            fr, _ = ef.get_seconds()
            to, _ = et.get_seconds()
            if fr is None:
                continue
            end = to if to is not None else dur
            if end and end > fr:
                marks.append((fr, end, MARKER_COLORS.get(key, "#888888")))
        self.multi_player.set_markers(marks)

    def _multi_set(self, entry):
        sec = self.multi_player.current_seconds()
        if sec is None:
            messagebox.showinfo("No video", "Click a file in the list first to load it.")
            return
        entry.set_seconds(sec)
        entry.flash()
        # keep keyboard focus on the player so the arrow keys still work:
        # Up/Down move between files, Left/Right step frames
        self.multi_player.canvas.focus_set()

    def _multi_goto(self, entry):
        if not self.multi_player.has_video():
            messagebox.showinfo("No video", "Click a file in the list first to load it.")
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set("Type a valid time in the box first, then Go.")
            return
        self.multi_player.seek_seconds(sec)
        entry.flash()
        self.multi_player.canvas.focus_set()   # keep arrow keys working after Go

    def _multi_preview(self, key):
        if not self.multi_player.has_video():
            return
        ef, et = self._multi_entries[key]
        fr, _ = ef.get_seconds()
        to, _ = et.get_seconds()
        if fr is None and key == "preintro":
            fr = 0.0
        end = to if to is not None else self.multi_player.timeline.duration
        if fr is not None and end and end > fr:
            self.multi_player.play_range(fr, end)
        self.multi_player.canvas.focus_set()   # keep arrow keys working

    def _multi_cut_all(self):
        try:
            crf = int(self.crf_var.get()); crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get()); assert kf >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("Error", "Check the CRF values (0-51) and keyframe interval (>=0).")
            return
        jobs = []      # (path, [drop ranges])
        for iid, path in self._multi_paths.items():
            drops = []
            for key, _ in self.MULTI_SEGS:
                fr, to = self._multi_seg.get(iid, {}).get(key, [None, None])
                if fr is None and to is None:
                    continue
                if key == "preintro" and fr is None:
                    fr = 0.0
                if fr is None:
                    continue
                if to is None:
                    if key in ("credits", "aftercredits"):
                        drops.append((fr, None))    # "end": resolved in the worker
                    continue
                if to <= fr:
                    continue
                drops.append((fr, to))
            if drops:
                jobs.append((path, drops))
        if not jobs:
            messagebox.showerror("Error", "No file has any valid section set.")
            return
        base_cfg = {
            "output_dir": self.dirs["Output folder:"].get(),
            "crf": crf, "crf_h265": crf265, "preset": self.preset_var.get(),
            "encoder": CODECS.get(self.codec_var.get(), "libx264"),
            "bit_depth": BIT_DEPTHS.get(self.depth_var.get(), "auto"),
            "kf_interval": kf,
            "subs_langs": (sorted(self.subs_langs)
                           if self.subs_filter_var.get() and self.subs_langs else None),
            "move_done": self.move_done_var.get(),
        }
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True, [p for p, _ in jobs] if base_cfg["move_done"] else None)
        if callable(self.unload_players_hook):
            self.unload_players_hook()      # free files before cutting
        threading.Thread(target=self._multi_worker, args=(jobs, base_cfg), daemon=True).start()

    def _multi_worker(self, jobs, base_cfg):
        try:
            n = len(jobs)
            self.log(f"[MULTI] cutting {n} file(s), each with its own sections")
            for i, (path, drops) in enumerate(jobs, 1):
                if self.stop_event.is_set():
                    self.log("[MULTI] stopped.")
                    break
                try:
                    total = probe_duration(path)
                    if not total:
                        self.log(f"  [{i}/{n}] [FAIL] could not read {os.path.basename(path)}")
                        continue
                    # an empty To ("end") runs to the end of this file
                    drops = [(a, total if b is None else b) for a, b in drops]
                    drops = [(a, b) for a, b in drops if b > a]
                    keep = keep_from_drops(total, drops) if drops else None
                    if not keep:
                        self.log(f"  [{i}/{n}] [SKIP] {os.path.basename(path)} - ranges leave nothing")
                        continue
                    removed = ", ".join(f"{fmt_time(a)}->{fmt_time(b)}" for a, b in drops)
                    self.log(f"  [{i}/{n}] {os.path.basename(path)}  remove {removed}")
                    cfg = dict(base_cfg, video=path, job_label=f"[{i}/{n}]")
                    run_manual(cfg, keep, self, self.stop_event)
                except Exception as e:
                    self.log(f"    [FAIL] {e!r}")
            self.log("[MULTI] done.")
        except Exception as e:
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            self.after(0, lambda: self._running(False))

    # --- ui callbacks (thread-safe) ---
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
        self.start_btn.configure(state="disabled" if on else "normal")
        self.cut_sel_btn.configure(state="disabled" if on else "normal")
        if hasattr(self, "multi_cut_btn"):
            self.multi_cut_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set("Idle")

    def start(self):
        try:
            crf = int(self.crf_var.get())
            crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get())
            assert kf >= 0
            conf = float(self.conf_var.get())
            anchor_secs = float(self.anchor_secs_var.get())
            assert anchor_secs > 0
        except (ValueError, AssertionError):
            messagebox.showerror("Error", "Check the CRF values (0-51), keyframe interval (>=0), "
                                 "confidence and anchor seconds.")
            return
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
            "subs_langs": (sorted(self.subs_langs)
                           if self.subs_filter_var.get() and self.subs_langs else None),
        }
        if not any(cfg["use"].values()):
            messagebox.showerror("Error", "Tick at least one segment type to process.")
            return
        # moving finished sources needs no player holding the file open (Windows
        # locks it) - release every preview player before the run starts
        if cfg["move_done"] and callable(self.unload_players_hook):
            self.unload_players_hook()
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True, _list_media(cfg["video_dir"]) if cfg["move_done"] else None)
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg):
        try:
            run_batch(cfg, self, self.stop_event)
        except Exception as e:
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            self.after(0, lambda: self._running(False))

    def stop(self):
        self.stop_event.set()
        self.log("Stopping after current ffmpeg call...")

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
            "subs_filter": self.subs_filter_var.get(),
            "subs_langs": sorted(self.subs_langs),
            "multi_keep_pos": self._multi_keep_pos_var.get(),
            **{f"use_{k}": v.get() for k, v in self.use_seg.items()},
        }
