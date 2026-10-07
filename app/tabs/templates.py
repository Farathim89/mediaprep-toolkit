"""Templates tab: Cut template | Auto-detect | Templates | Log.

TemplateTab holds the Cut template sub-tab itself; the Auto-detect and
Templates sub-tabs are mixins (templates_detect.py, templates_manager.py)."""
import os
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import (AFTERCREDITS_DIR, AUDIO_LANG_CHOICES, CREDITS_DIR, INTRO_DIR,
                      limit_cmd, popen_flags, PREINTRO_DIR)
from ..engine.files import move_to_trash
from ..engine.formatting import fmt_time
from ..engine.probe import audio_track_for_lang
from ..ui.frametime import file_fps
from ..ui.player import VideoPlayer
from ..ui.widgets import (ScrollFrame, TimeEntry, add_tooltip, build_log_tab, info_icon,
                          enable_file_drop, enable_file_drop_deep, help_button,
                          trim_text_lines)
from .common import (_KIND_NAMES, _MEDIA_EXTS, _VIDEO_TYPES, _existing_template,
                     _snap_in_thread, _template_stem)
from .cut_common import _ask_choice, _src_label
from .cut_common import mode_combobox
from .templates_autodetect import TemplateAutoMixin
from .templates_detect import TemplateDetectMixin
from .templates_manager import TemplatesManagerMixin
from .. import applog, jobs as jobreg
from ..i18n import tr, N_
from ..ui import icons

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr


# "from" / "to" box names for the snap status line
_WHICH = {"from": N_("From"), "to": N_("To")}


# ======================= Templates tab =======================
class TemplateTab(TemplateDetectMixin, TemplatesManagerMixin, TemplateAutoMixin, ttk.Frame):
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
        # what's running - one place decides which buttons are enabled
        self._detecting = False
        self._cutting = False               # manual template cut
        self._autocutting = False
        self._detect_partial = False        # results came from a stopped detect
        self._tpl_detecting = False         # Cut template -> Auto-detect running
        self._tpl_cands = {}                # ... its candidates per kind (▾ menus)
        self._tpl_cands_path = ""           # ... and the video they belong to

        nb = ttk.Notebook(self)
        self._nb = nb
        nb.grid(row=0, column=0, sticky="nsew")
        # each sub-tab lives in its own ScrollFrame (like the Cut / Edit tab), so
        # a short page fills the window seamlessly and scrolls if it's too tall -
        # no bordered dead space under short content.
        main_outer = ttk.Frame(nb)
        detect_outer = ttk.Frame(nb)
        tm_outer = ttk.Frame(nb)
        nb.add(main_outer, text="  " + tr("Cut template") + "  ")
        nb.add(detect_outer, text="  " + tr("Auto-detect") + "  ")
        nb.add(tm_outer, text="  " + tr("Templates") + "  ")
        self._tm_page = tm_outer
        main_sc = ScrollFrame(main_outer)
        main_sc.pack(fill="both", expand=True)
        detect_sc = ScrollFrame(detect_outer)
        detect_sc.pack(fill="both", expand=True)
        tm_sc = ScrollFrame(tm_outer)
        tm_sc.pack(fill="both", expand=True)
        main = main_sc.interior
        detect = detect_sc.interior
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # ---- file row ----
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(top, text=tr("Video file:")).pack(side="left", padx=(0, 6))
        self.file_var = tk.StringVar(value=saved.get("last_video", ""))
        ent = ttk.Entry(top, textvariable=self.file_var)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.load_from_entry())
        icons.decorate(ttk.Button(top, text=tr("Browse..."), command=self.browse), "folder").pack(side="left", padx=6)
        icons.decorate(ttk.Button(top, text=tr("Load"), command=self.load_from_entry), "load").pack(side="left")
        self.tpl_detect_btn = icons.decorate(ttk.Button(top, text=tr("Auto-detect"), command=self.tpl_autodetect), "detect")
        self.tpl_detect_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.tpl_detect_btn, tr(
            "Fill the From / To boxes of the loaded episode automatically: compares it "
            "with up to N neighbouring episodes of its folder ('Scan at most' on "
            "Auto-detect, default 4) and takes what recurs, and matches the templates you "
            "already have (the Log says which kinds are already covered). Check the times "
            "in the player, then Cut template(s)."))
        help_button(top, "template_cut").pack(side="right", padx=(8, 0))
        # the Audio / Visual choice of Templates -> Auto-detect, shown here too
        # (the variable is created early; _build_detect_tab reuses it)
        dm = saved.get("det_mode")
        self.det_mode_var = tk.StringVar(value=dm if dm in ("audio", "visual", "both")
                                         else "both")
        tdm = mode_combobox(top, self.det_mode_var, width=14)
        tdm.pack(side="right", padx=(8, 0))
        add_tooltip(tdm, tr("Detect by: Audio, Visual (the pictures) or both - the same "
                            "setting as on the Auto-detect sub-tab"))

        # ---- body: player (left) + cut points (right), single view ----
        body = ttk.Frame(main)
        body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # right column: the four sections + cut button
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        # two grid rows per section: the enable checkbox (spanning both rows),
        # then "From: [boxes] Set Snap" and below it "To: [boxes] Set Snap"
        # ("Set" captures the frame shown in the player); ▶ spans both rows.
        # Stacking From over To keeps the column narrow enough for longer
        # translated button texts next to the player.
        seg_desc = {"preintro": N_("pre-intro (recap/logo before the intro)"),
                    "intro": N_("intro"), "credits": N_("credits"),
                    "aftercredits": N_("after-credits (teaser/preview)")}
        sect = ttk.Frame(right)
        sect.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.sections = {}
        self._snap_btns = {}                # (key, "from"/"to") -> Snap button
        saved_sect = saved.get("tpl_sections") or {}
        if not isinstance(saved_sect, dict):
            saved_sect = {}
        for i, (key, _title, default_on, _need_to) in enumerate(self.SECTION_SPECS):
            ss = saved_sect.get(key)
            if not (isinstance(ss, (list, tuple)) and len(ss) == 3):
                ss = None
            on = tk.BooleanVar(value=bool(ss[0]) if ss else default_on)
            desc = tr_key(seg_desc[key])
            r0, r1 = i * 2, i * 2 + 1
            top_pad = 0 if i == 0 else 8     # small gap between the sections
            cb = ttk.Checkbutton(sect, text=tr_key(_KIND_NAMES[key]), variable=on)
            cb.grid(row=r0, column=0, rowspan=2, sticky="w", padx=(0, 8), pady=(top_pad, 0))
            add_tooltip(cb, tr("Include the {section} when cutting templates", section=desc))
            ttk.Label(sect, text=tr("From:")).grid(row=r0, column=1, sticky="e", padx=(0, 4),
                                                   pady=(top_pad + 1, 1))
            ef = TimeEntry(sect)
            ef.grid(row=r0, column=2, sticky="w", padx=2, pady=(top_pad + 1, 1))
            bf = ttk.Button(sect, text=tr("Set"), width=-5,
                            command=lambda k=key: self._mark(k, "from"))
            bf.grid(row=r0, column=3, sticky="we", padx=2, pady=(top_pad, 0))
            add_tooltip(bf, tr("Set the START of the {section} to the frame shown in the player",
                               section=desc))
            sf = ttk.Button(sect, text=tr("Snap"), width=-5,
                            command=lambda k=key: self._snap(k, "from"))
            sf.grid(row=r0, column=4, sticky="we", padx=(0, 2), pady=(top_pad, 0))
            add_tooltip(sf, tr("Snap the START of the {section} to the nearest end of a "
                               "silence / black frame within ±1 s (uses the From box, or the "
                               "player position if the box is empty)", section=desc))
            ttk.Label(sect, text=tr("To:")).grid(row=r1, column=1, sticky="e", padx=(0, 4), pady=1)
            et = TimeEntry(sect, end=True, fps=self._tpl_fps)
            et.grid(row=r1, column=2, sticky="w", padx=2, pady=1)
            bt = ttk.Button(sect, text=tr("Set"), width=-5,
                            command=lambda k=key: self._mark(k, "to"))
            bt.grid(row=r1, column=3, sticky="we", padx=2)
            add_tooltip(bt, tr("Set the END of the {section} to the frame shown in the player "
                               "- the To frame is the last frame of the clip (inclusive)",
                               section=desc))
            st = ttk.Button(sect, text=tr("Snap"), width=-5,
                            command=lambda k=key: self._snap(k, "to"))
            st.grid(row=r1, column=4, sticky="we", padx=(0, 2))
            add_tooltip(st, tr("Snap the END of the {section} to the nearest start of a "
                               "silence / black frame within ±1 s (uses the To box, or the "
                               "player position if the box is empty)", section=desc))
            self._snap_btns[(key, "from")] = sf
            self._snap_btns[(key, "to")] = st
            bp = icons.decorate(ttk.Button(sect, text="", width=3,
                            command=lambda k=key: self._preview_section(k)), "play")
            bp.grid(row=r0, column=5, rowspan=2, padx=(6, 0), pady=(top_pad, 0))
            add_tooltip(bp, tr("Play only the {section} section (From to To) in the player",
                               section=desc))
            self._tpl_cand_button(sect, key).grid(row=r0, column=6, rowspan=2, padx=(2, 0),
                                                  pady=(top_pad, 0))
            self.sections[key] = (on, ef, et)
            if ss:                       # restore last session's times
                # saved as engine values (To = exclusive end); the To box
                # shows the frame before it once the video's fps is known
                for te, val in ((ef, ss[1]), (et, ss[2])):
                    if isinstance(val, (int, float)):
                        te.set_value(float(val))
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._refresh_markers())

        crow = ttk.Frame(right)
        crow.grid(row=5, column=0, sticky="we", pady=(10, 4))
        self.cut_btn = icons.decorate(ttk.Button(crow, style="Accent.TButton",
                                                 text=tr("Cut template(s)"),
                                                 command=self.start_cut), "cut")
        self.cut_btn.pack(side="left", fill="x", expand=True)
        info_icon(crow, tr("Cut the WHOLE segment - Cut / Edit uses the template "
                           "length as the cut length. From and To are both in the clip: "
                           "To = the last frame (inclusive). Credits / After-credits 'To' "
                           "empty = end of file; Pre-intro 'From' empty = start of "
                           "file. Pre-intro & after-credits are optional "
                           "(recaps / teasers).")).pack(side="left", padx=(6, 0))
        add_tooltip(self.cut_btn, tr("Cut each enabled section into a template clip in its folder"))

        # left column: player. Fine-tune the boundary with the player's own
        # frame-step controls - Left/Right arrows, the |< < > >| buttons.
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        self.player = VideoPlayer(left, width=480, height=270, log_fn=self.log)
        self.player.pack()
        self.player.enable_tab_shortcuts()   # arrows/space work anywhere on the tab
        self.player.auto_fit(body)           # 16:9 video as large as the page allows

        # ---- shared status + progress (anchored to the window bottom when a
        # non-scrolling `bottom` strip is provided, so they stay visible) ----
        self.status_var = tk.StringVar(value="")
        self._bottom = bottom
        self._cut_foot = None
        if bottom is not None:
            # the window's fixed footer: this row on Cut template / Templates,
            # the Auto-detect row (built in _build_detect_tab) on Auto-detect
            self._cut_foot = foot = ttk.Frame(bottom)
            foot.pack(fill="x")
            ttk.Label(foot, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prow = ttk.Frame(foot)
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
        self.cut_stop_btn = icons.decorate(ttk.Button(prow, text=tr("Stop"), command=self.stop_cut,
                                       state="disabled", width=-8), "stop")
        self.cut_stop_btn.grid(row=0, column=1, padx=(6, 0))
        add_tooltip(self.cut_stop_btn, tr("Stop the template cut (or Auto-cut all) after the "
                                          "current section finishes"))

        # ===================== Auto-detect sub-tab =====================
        self._build_detect_tab(detect, saved)

        # ===================== Templates manager tab =====================
        self._audition = None               # open Audition window, if any
        self._build_templates_tab(tm_sc.interior)
        nb.bind("<<NotebookTabChanged>>", self._on_subtab_changed, add="+")
        nb.bind("<<NotebookTabChanged>>", self._footer_row, add="+")

        self.logbox = build_log_tab(nb)

        enable_file_drop_deep(self.player, self._drop_load)       # drop anywhere in the player
        enable_file_drop(ent, self._drop_load)

    def _footer_row(self, _e=None):
        """Footer: the Auto-detect progress row on the Auto-detect sub-tab,
        the template-cut row everywhere else."""
        det = getattr(self, "_detect_foot", None)
        if self._cut_foot is None or det is None:
            return
        try:
            on_detect = self._nb.index("current") == 1
        except tk.TclError:
            return
        show, hide = (det, self._cut_foot) if on_detect else (self._cut_foot, det)
        hide.pack_forget()
        if not show.winfo_ismapped():
            show.pack(fill="x")

    def _tpl_fps(self):
        """fps of the Cut template video (the To boxes step one frame on it)."""
        path = getattr(getattr(self, "player", None), "_path", None)             or self.file_var.get().strip().strip('"')
        return file_fps(path, getattr(self, "player", None))

    # ---------------- markers ----------------
    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        dur = self.player.timeline.duration
        marks = []
        for key, (on, ef, et) in self.sections.items():
            s, s_ok = ef.get_value()
            e, e_ok = et.get_value()
            if s is None or not s_ok or not e_ok:
                continue     # an invalid To is NOT "to end" - draw nothing
            end = e if e is not None else dur
            if end and end > s:
                marks.append((s, end, key))
        self.player.set_markers(marks)

    def _preview_section(self, key):
        if not self.player.has_video():
            messagebox.showinfo(tr("No video"), tr("Load a video first."))
            return
        _on, ef, et = self.sections[key]
        s, s_ok = ef.get_value()
        e, e_ok = et.get_value()
        if s is None and s_ok and key == "preintro":
            s = 0.0            # empty pre-intro From = start of file
        if s is None or not s_ok:
            self.status_var.set(tr("Fill the From time of that section first."))
            return
        if not e_ok:
            self.status_var.set(tr("The section's To time is invalid."))
            return
        end = e if e is not None else self.player.timeline.duration
        if not end or end <= s:
            self.status_var.set(tr("The section's To time must be after its From time."))
            return
        self.status_var.set(tr("Previewing {section} section {start} -> {end}",
                               section=tr_key(_KIND_NAMES[key]), start=fmt_time(s),
                               end=fmt_time(et.shown(end))))
        self.player.play_range(s, end)

    def _mark(self, key, which):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo(tr("No video"), tr("Load a video first, then scrub to the point."))
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
        if not path:
            return
        if not os.path.isfile(path) or not path.lower().endswith(_MEDIA_EXTS):
            self.status_var.set(tr("Not a video file: {name}", name=os.path.basename(path) or path))
            return
        self.file_var.set(path)
        self.player.load(path)
        self._refresh_markers()

    def load_from_entry(self):
        path = self.file_var.get().strip().strip('"')
        if path and os.path.isfile(path):
            self.player.load(path)
            self._refresh_markers()
        else:
            messagebox.showerror(tr("Error"), tr("Type or browse to a valid video file first."))

    def browse(self):
        path = filedialog.askopenfilename(title=tr("Select video"),
                                          filetypes=[(tr_key(d), p) for d, p in _VIDEO_TYPES])
        if path:
            self.file_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def log(self, msg):
        applog.record(msg)
        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            trim_text_lines(self.logbox)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    # ---------------- cut templates (manual) ----------------
    def _det_lang(self):
        """Audio language detection listens to (None = default track)."""
        return AUDIO_LANG_CHOICES.get(self.det_lang_var.get())

    def _cut_clip(self, video, out, s, e, lang=None):
        """Cut [s, e) of video into out (visually-lossless x264, copy audio).
        Maps the first video stream and the audio track detection listens to
        (`lang`'s track, else the first one) so ffmpeg doesn't pick another
        language. e=None means to end of file. Returns (ok, stderr_text)."""
        os.makedirs(os.path.dirname(out), exist_ok=True)
        a_idx = None
        if lang:
            try:
                a_idx = audio_track_for_lang(video, lang)
            except Exception:
                a_idx = None
        if a_idx is None:
            a_idx = 0
        # s = the first frame of the clip, e = the frame AFTER its last one
        # (the To box shows the last frame, inclusive). Both cut points sit
        # half a frame early, between two frames, so millisecond rounding of
        # the times / the file's timestamps can never add or drop a frame.
        half = 0.5 / fps if (fps := file_fps(video)) else 0.0
        ss = max(0.0, s - half)
        cmd = ["ffmpeg", "-y", "-ss", f"{ss:.4f}", "-i", video, "-ss", "0"]
        if e is not None:
            if fps:
                # a frame COUNT is exact; a -t duration lost the last frame to
                # timestamp rounding on real HEVC files (2159 instead of 2160)
                n = max(1, int(round((e - s) * fps)))
                cmd += ["-frames:v", str(n), "-t", f"{n / fps + half:.4f}"]
            else:
                cmd += ["-t", f"{max(0.001, e - half - ss):.4f}"]
        cmd += ["-map", "0:v:0", "-map", f"0:a:{a_idx}?",
                "-c:v", "libx264", "-crf", "18", "-preset", "superfast", "-c:a", "copy", out]
        proc = subprocess.run(limit_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=popen_flags())
        return proc.returncode == 0, proc.stderr.decode(errors="replace")

    @staticmethod
    def _free_name(folder, base, taken):
        """First `base_vN` (N >= 2) not used in `folder` or by this batch."""
        n = 2
        while (_existing_template(folder, f"{base}_v{n}")
               or os.path.normcase(os.path.join(folder, f"{base}_v{n}.mkv")) in taken):
            n += 1
        return f"{base}_v{n}"

    def _plan_outputs(self, items):
        """items: [(tag, folder, base)] -> {tag: (out_path, replace)}. Templates
        are always written as .mkv (any source container works). When an output
        already exists, asks ONCE: Overwrite / Keep both (_v2, _v3...) / Skip.
        Skipped tags are left out; `replace` = older copies (other extension) to
        remove after a successful Overwrite."""
        rows = [(tag, folder, base, _existing_template(folder, base))
                for tag, folder, base in items]
        clash = [p for r in rows for p in r[3]]
        choice = True
        if clash:
            names = [os.path.basename(p) for p in clash]
            if len(names) > 12:
                names = names[:12] + [tr("... and {n} more", n=len(names) - 12)]
            # real Overwrite / Keep both / Skip buttons (it was Yes / No /
            # Cancel with a legend to decode)
            pick = _ask_choice(
                self, tr("Template already exists"),
                tr("These templates already exist:") + "\n\n" + "\n".join(names) + "\n\n"
                + tr("Overwrite them, keep both (the new one is saved as _v2, _v3...) "
                     "or skip those sections?"),
                [("overwrite", tr("Overwrite")), ("keep", tr("Keep both")),
                 ("skip", tr("Skip"))])
            choice = {"overwrite": True, "keep": False}.get(pick)    # None = skip
        plan, taken = {}, set()
        for tag, folder, base, existing in rows:
            name, replace = base, []
            if existing:
                if choice is None:
                    self.log(f"[CUT] skip {base} - template already exists")
                    continue
                if choice:
                    replace = [p for p in existing
                               if os.path.splitext(p)[1].lower() != ".mkv"]
                else:
                    name = self._free_name(folder, base, taken)
            out = os.path.join(folder, name + ".mkv")
            if os.path.normcase(out) in taken:      # same name twice in one batch
                name, replace = self._free_name(folder, base, taken), []
                out = os.path.join(folder, name + ".mkv")
            taken.add(os.path.normcase(out))
            plan[tag] = (out, replace)
        return plan

    @staticmethod
    def _kind_disp(c):
        name = tr_key(_KIND_NAMES.get(c["kind"], c["kind"]))
        if c.get("known"):
            return tr("✔ {kind} (have)", kind=name)
        if c.get("src") and c["src"] != "audio":
            return f"{name} ({_src_label(c['src'])})"
        return name

    def _template_saved(self, out, replace, kind, member=None, cluster=None):
        """Worker-side after a successful cut: move the older copies an
        Overwrite replaces to Data\\temp\\trash (never deleted), then mark the
        matching detected rows as 'have' so a second Auto-cut doesn't cut
        them again."""
        if replace:
            moved, _failed = move_to_trash(replace, "replaced_templates", log=self.log)
            for p in moved:
                self.log(f"  [OK] replaced older {os.path.basename(p)} (moved to Data\\temp\\trash)")
        name = os.path.basename(out)
        norm = os.path.normcase(os.path.abspath(member)) if member else None

        def _mark():
            for iid, c in self._clusters.items():
                hit = c is cluster
                if not hit and norm and c["kind"] == kind:
                    hit = any(os.path.normcase(os.path.abspath(p)) == norm for p in c["ranges"])
                if not hit or c.get("known"):
                    continue
                c["known"] = name
                if self.detect_tree.exists(iid):
                    self.detect_tree.set(iid, "kind", self._kind_disp(c))
                    self.detect_tree.item(iid, tags=("have",))
        self.after(0, _mark)

    def _sync_buttons(self):
        """Enable/disable the Template-tab buttons from what's running: a manual
        template cut and Auto-cut never run at the same time."""
        cutting = self._cutting or self._autocutting
        scanning = self._detecting or self._autocutting
        self.detect_btn.configure(state="disabled" if scanning else "normal")
        self.detect_stop_btn.configure(state="normal" if scanning else "disabled")
        self.autocut_btn.configure(state="disabled" if (scanning or cutting or self._detect_partial)
                                   else "normal")
        busy = cutting or self._tpl_detecting
        self.cut_btn.configure(state="disabled" if busy else "normal")
        self.cut_stop_btn.configure(state="normal" if busy else "disabled")
        if hasattr(self, "tpl_detect_btn"):
            self.tpl_detect_btn.configure(state="disabled" if busy else "normal")

    def start_cut(self):
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror(tr("Error"), tr("Please select a valid video file."))
            return
        jobs = []
        for key, _label, _default_on, need_to in self.SECTION_SPECS:
            on, ef, et = self.sections[key]
            if not on.get():
                continue
            sec = tr_key(_KIND_NAMES[key])
            s, s_ok = ef.get_value()
            e, e_ok = et.get_value()      # exclusive end: the frame after the To frame
            if s is None and s_ok and key == "preintro":
                s = 0.0        # pre-intro starts at the very beginning by definition
            if not s_ok or s is None:
                messagebox.showerror(tr("Error"), tr("{section}: needs a valid From time.",
                                                     section=sec))
                return
            if not e_ok:      # typed but unreadable - never treat as "to end"
                messagebox.showerror(tr("Error"), tr("{section}: the To time is invalid.",
                                                     section=sec))
                return
            if e is None:
                if need_to:
                    messagebox.showerror(tr("Error"), tr("{section}: needs a valid To time.",
                                                         section=sec))
                    return
            elif e <= s:
                messagebox.showerror(tr("Error"), tr("{section}: To must be after From.",
                                                     section=sec))
                return
            jobs.append((key, s, e))
        if not jobs:
            messagebox.showerror(tr("Error"), tr("Enable at least one section first."))
            return
        if self._autocutting or self._cutting:
            return
        name = _template_stem(os.path.splitext(os.path.basename(video))[0])
        plan = self._plan_outputs([(key, self.DIRMAP[key], f"{name}_{key}")
                                   for key, _s, _e in jobs])
        jobs = [(key, s, e) + plan[key] for key, s, e in jobs if key in plan]
        if not jobs:
            self.status_var.set(tr("Nothing cut - every section was skipped."))
            return
        self.cut_stop.clear()
        self._cutting = True
        self._sync_buttons()
        self.status_var.set(tr("Cutting template(s)..."))
        self.bar.start(12)
        jid = self._jid = jobreg.begin(tr("Template cut"), stop_event=self.cut_stop, tab=self)
        threading.Thread(target=self.worker, args=(video, jobs, self._det_lang(), jid),
                         daemon=True).start()

    def stop_cut(self):
        self.cut_stop.set()
        if self._tpl_detecting:
            self.log("[AUTO] stopping auto-detect...")
        elif self._autocutting:
            self.log("[DETECT] stopping auto-cut after the current template...")
        else:
            self.log("[CUT] stopping after the current section...")

    def worker(self, video, jobs, lang=None, jid=None):
        n_ok = n_fail = 0
        crashed = False
        try:
            stopped = False
            for key, s, e, out, replace in jobs:
                if self.cut_stop.is_set():
                    stopped = True
                    break
                self.log(f"[{key.upper()}] {fmt_time(s)} -> {fmt_time(e) if e is not None else 'end'}  ->  {out}")
                ok, err = self._cut_clip(video, out, s, e, lang)
                if ok:
                    n_ok += 1
                    self.log("  [OK] saved")
                    self._template_saved(out, replace, key, member=video)
                else:
                    n_fail += 1
                    for ln in err.strip().splitlines()[-4:]:
                        self.log(f"    | {ln}")
                    self.log(f"  [FAIL] {key} template failed")
            self.log("Stopped." if (stopped or self.cut_stop.is_set()) else "Done.")
        except Exception as exc:
            crashed = True
            self.log(f"[FAIL] template cut crashed: {exc}")
        finally:
            jobreg.end(jid, ok=not crashed and n_fail == 0,
                       summary=tr("{ok} template(s) cut, {failed} failed",
                                  ok=n_ok, failed=n_fail))
            def _f():
                self.bar.stop()
                self.status_var.set(tr("Stopped.") if self.cut_stop.is_set() else tr("Done."))
                self._cutting = False
                self._sync_buttons()
                self._tm_refresh()
            self.after(0, _f)

    # ---------------- snap to silence / black ----------------
    def _snap(self, key, which):
        """Snap a From/To box to the nearest silence edge / black boundary
        (analysed in a thread by snap.find_snap)."""
        video = getattr(self.player, "_path", None) or self.file_var.get().strip().strip('"')
        if not self.player.has_video() or not video or not os.path.isfile(video):
            messagebox.showinfo(tr("No video"), tr("Load a video first."))
            return
        on, ef, et = self.sections[key]
        entry = ef if which == "from" else et
        # engine values (a To box: the exclusive end, one frame after it shows)
        t, ok = entry.get_value()
        if not ok:
            self.status_var.set(tr("That time box is invalid - fix it or clear it to snap "
                                   "from the player position."))
            return
        if t is None:
            t = entry.engine(self.player.current_seconds())
        if t is None:
            return
        btn = self._snap_btns[(key, which)]
        btn.configure(state="disabled")
        self.status_var.set(tr("Snapping {section} {which} near {time}...",
                               section=tr_key(_KIND_NAMES[key]), which=tr_key(_WHICH[which]),
                               time=fmt_time(entry.shown(t))))
        edge = "start" if which == "from" else "end"

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
            on.set(True)
            entry.set_value(new_t)
            entry.flash()
            self._refresh_markers()
            if self.player.has_video():
                self.player.seek_seconds(entry.shown(new_t), play=False)
                self.player.canvas.focus_set()
            old_s, new_s = fmt_time(entry.shown(t)), fmt_time(entry.shown(new_t))
            msg = f"snapped {old_s} → {new_s} ({reason})"
            self.log(f"{tag} {msg}")
            self.status_var.set(tr("{section} {which} snapped {old} → {new} ({reason})",
                                   section=tr_key(_KIND_NAMES[key]), which=tr_key(_WHICH[which]),
                                   old=old_s, new=new_s, reason=reason))

        _snap_in_thread(self, video, t, edge, self._det_lang(), done)

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
            "det_mode": self.det_mode_var.get(),
            **{("det_margin" if k == "margin_frames" else "det_tpl_margin"): v
               for k, v in self._det_margins().items()},
            "tpl_sections": self._sections_snapshot(),
        }

    def _sections_snapshot(self):
        """Cut-template boxes: {key: [enabled, from_sec|None, to_sec|None]}
        (an unreadable time is saved as None)."""
        out = {}
        for key, (on, ef, et) in self.sections.items():
            s, s_ok = ef.get_value()
            e, e_ok = et.get_value()      # engine value (exclusive end), as before
            out[key] = [bool(on.get()), s if s_ok else None, e if e_ok else None]
        return out
