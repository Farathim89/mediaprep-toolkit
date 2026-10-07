"""Convert -> Audio & subtitles sub-tab (TracksMixin): one row per source
track with Keep / codec / bitrate / mixdown / default / title (audio) and
Keep / default / forced / burn-in (subtitles), quick actions and the rules
newly added files start from."""
import tkinter as tk
from tkinter import ttk

from ..engine import recode
from ..i18n import N_, tr
from ..ui import icons, themes
from ..ui.widgets import KeyedCombobox, add_tooltip, info_icon

ACODECS = {N_("Copy"): "copy", "AAC": "aac", "AC3": "ac3", "E-AC3": "eac3",
           "Opus": "libopus", "FLAC": "flac", "MP3": "libmp3lame"}
ACODEC_RULES = {**ACODECS, N_("Plex auto (copy / E-AC3)"): "plex"}
MIXES = {N_("Keep"): "keep", N_("Stereo"): "stereo", "5.1": "5.1"}
BITRATES = {N_("Auto"): "", **{b: b for b in recode.AUDIO_BITRATES}}
_RAW = {"AAC", "AC3", "E-AC3", "Opus", "FLAC", "MP3", "5.1"} | set(recode.AUDIO_BITRATES)
ON, OFF = "☑", "☐"


def _colw(header, w):
    """Column width (px at 100 % -> scaled) that also fits the header."""
    import tkinter.font as tkfont
    try:
        need = tkfont.nametofont("TkHeadingFont").measure(header) + themes.px(16)
    except Exception:
        need = 0
    return max(themes.px(w), need)


def _lbl(table, value):
    for k, v in table.items():
        if v == value:
            return k if k in _RAW else tr(k)
    return str(value)


def _labels(table):
    return [k if k in _RAW else tr(k) for k in table]


class TracksMixin:
    """Expects self._shown_job() and self._tracks_changed()."""

    def _build_tracks_tab(self, page):
        page.columnconfigure(0, weight=1)
        # ---------------- audio ----------------
        af = ttk.LabelFrame(page, text=tr("Audio tracks"))
        af.grid(row=0, column=0, sticky="we")
        af.columnconfigure(0, weight=1)
        cols = ("keep", "n", "lang", "src", "codec", "br", "mix", "def", "title")
        self.atree = ttk.Treeview(af, columns=cols, show="headings", height=3,
                                  selectmode="browse")
        for c, t, w, anc in (("keep", tr("Keep"), 50, "center"), ("n", "#", 32, "center"),
                             ("lang", tr("Language"), 76, "center"),
                             ("src", tr("Source"), 150, "w"),
                             ("codec", tr("Codec"), 92, "center"),
                             ("br", tr("Bitrate"), 74, "center"),
                             ("mix", tr("Mixdown"), 80, "center"),
                             ("def", tr("Default"), 60, "center"),
                             ("title", tr("Title"), 160, "w")):
            self.atree.heading(c, text=t)
            self.atree.column(c, width=_colw(t, w), minwidth=themes.px(28), anchor=anc,
                              stretch=c in ("src", "title"))
        self.atree.grid(row=0, column=0, sticky="we", padx=4, pady=(4, 2))
        self.atree.bind("<Button-1>", lambda e: self._tree_click(self.atree, e, "a"))
        self.atree.bind("<Double-1>", lambda e: self._tree_edit(self.atree, e, "a"))
        qa = ttk.Frame(af)
        qa.grid(row=1, column=0, sticky="we", padx=4, pady=(2, 6))
        self.keep_lang_var = tk.StringVar()
        self.keep_lang_cb = ttk.Combobox(qa, textvariable=self.keep_lang_var, width=8,
                                         state="readonly")
        b = ttk.Button(qa, text=tr("Keep only"), command=self._keep_only_lang)
        b.pack(side="left")
        add_tooltip(b, tr("Keep only the audio tracks in the language chosen on the right "
                          "(the first of them becomes the default track)"))
        self.keep_lang_cb.pack(side="left", padx=(4, 12))
        b = icons.decorate(ttk.Button(qa, text=tr("Copy all"), command=self._copy_all), "copy")
        b.pack(side="left")
        add_tooltip(b, tr("Keep every audio track, unchanged (stream copy)"))
        info_icon(qa, tr("Click Keep / Default to switch them; double-click Codec, "
                         "Bitrate, Mixdown or Title to change it. A mixdown needs a "
                         "re-encode (Copy then becomes AAC); tracks are never up-mixed. "
                         "The table belongs to the file shown at the top - with several "
                         "files selected, the same changes go to every selected file "
                         "with the same number of tracks.")).pack(side="left", padx=(10, 0))

        # ---------------- subtitles ----------------
        sf = ttk.LabelFrame(page, text=tr("Subtitle tracks"))
        sf.grid(row=1, column=0, sticky="we", pady=(8, 0))
        sf.columnconfigure(0, weight=1)
        cols = ("keep", "n", "lang", "fmt", "title", "def", "forced", "burn")
        self.stree = ttk.Treeview(sf, columns=cols, show="headings", height=3,
                                  selectmode="browse")
        for c, t, w, anc in (("keep", tr("Keep"), 50, "center"), ("n", "#", 32, "center"),
                             ("lang", tr("Language"), 76, "center"),
                             ("fmt", tr("Format"), 130, "w"),
                             ("title", tr("Title"), 220, "w"),
                             ("def", tr("Default"), 60, "center"),
                             ("forced", tr("Forced"), 60, "center"),
                             ("burn", tr("Burn in"), 64, "center")):
            self.stree.heading(c, text=t)
            self.stree.column(c, width=_colw(t, w), minwidth=themes.px(28), anchor=anc,
                              stretch=c in ("fmt", "title"))
        self.stree.grid(row=0, column=0, sticky="we", padx=4, pady=(4, 8))
        self.stree.bind("<Button-1>", lambda e: self._tree_click(self.stree, e, "s"))
        info_icon(sf, tr("Burn in paints ONE track into the picture (ASS / SRT with the "
                         "file's own fonts, PGS as an overlay) - it is then not kept as "
                         "a separate track. Picture subtitles (PGS) can't go into "
                         "MP4.")).grid(row=0, column=1, sticky="n", padx=(0, 4), pady=(6, 0))

        # ---------------- rules for new files ----------------
        rf = ttk.LabelFrame(page, text=tr("For newly added files"))
        rf.grid(row=2, column=0, sticky="we", pady=(8, 0))
        r1 = ttk.Frame(rf)
        r1.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(r1, text=tr("Audio:")).pack(side="left")
        for key, table, w in (("audio_codec", ACODEC_RULES, 22), ("audio_bitrate", BITRATES, 7),
                              ("audio_mix", MIXES, 8)):
            var = tk.StringVar()
            self._fv[key] = (var, table)
            var.trace_add("write", lambda *a: self._form_changed())
            KeyedCombobox(r1, textvariable=var, values=list(table), labels=_labels(table),
                          state="readonly", width=w).pack(side="left", padx=(6, 0))
        r2 = ttk.Frame(rf)
        r2.pack(fill="x", padx=4, pady=(2, 6))
        for key, text, tip in (
                ("audio_langs", tr("Keep audio languages:"),
                 tr("Language codes separated by commas, e.g. jpn, eng - empty keeps every "
                    "track")),
                ("sub_langs", tr("Keep subtitle languages:"),
                 tr("Language codes separated by commas, e.g. eng - empty keeps every "
                    "track"))):
            ttk.Label(r2, text=text).pack(side="left", padx=(0 if key == "audio_langs" else 14, 4))
            var = tk.StringVar()
            self._fv[key] = (var, str)
            var.trace_add("write", lambda *a: self._form_changed())
            e = ttk.Entry(r2, textvariable=var, width=12)
            e.pack(side="left")
            add_tooltip(e, tip)
        b = ttk.Button(r2, text=tr("Apply rules to the file shown"),
                       command=self._apply_rules_shown)
        b.pack(side="left", padx=(14, 0))
        add_tooltip(b, tr("Rebuild the track tables above from these rules"))

    # ------------------------------------------------------------ fill
    def _fill_tracks(self):
        job = self._shown_job()
        for t in (self.atree, self.stree):
            t.delete(*t.get_children())
        if job is None:
            self.keep_lang_cb.configure(values=())
            return
        info = job["info"]
        for i, t in enumerate(job["audio"]):
            a = info["audio"][t["src"]]
            lay = a["layout"] or f"{a['channels']}ch"
            self.atree.insert("", "end", iid=str(i), values=(
                ON if t["keep"] else OFF, t["src"] + 1, a["lang"],
                f"{a['codec'].upper()} {lay}" + (f"  '{a['title']}'" if a["title"] else ""),
                _lbl(ACODECS, t["codec"]),
                "-" if t["codec"] in ("copy", "flac") else (t["bitrate"] or tr("Auto")),
                _lbl(MIXES, t["mix"]), ON if t["default"] else OFF, t["title"]))
        for i, t in enumerate(job["subs"]):
            x = info["subs"][t["src"]]
            self.stree.insert("", "end", iid=str(i), values=(
                ON if t["keep"] else OFF, t["src"] + 1, x["lang"],
                x["codec"].upper().replace("HDMV_PGS_SUBTITLE", "PGS"),
                x["title"], ON if t["default"] else OFF, ON if t["forced"] else OFF,
                ON if t.get("burn") else OFF))
        langs = sorted({a["lang"] for a in info["audio"]})
        self.keep_lang_cb.configure(values=langs)
        if self.keep_lang_var.get() not in langs:
            self.keep_lang_var.set(langs[0] if langs else "")

    # ------------------------------------------------------------ edit
    def _tree_click(self, tree, e, kind):
        if tree.identify_region(e.x, e.y) != "cell":
            return
        col = tree.column(tree.identify_column(e.x), "id")
        row = tree.identify_row(e.y)
        job = self._shown_job()
        if not row or job is None:
            return
        tracks = job["audio"] if kind == "a" else job["subs"]
        t = tracks[int(row)]
        key = {"keep": "keep", "def": "default", "forced": "forced", "burn": "burn"}.get(col)
        if key is None:
            return
        t[key] = not t.get(key)
        if key == "default" and t[key] and kind == "a":
            for o in tracks:          # one default audio track
                if o is not t:
                    o["default"] = False
        if key == "burn" and t[key]:
            for o in tracks:          # one burned-in track
                if o is not t:
                    o["burn"] = False
        self._tracks_changed()
        return "break"

    def _tree_edit(self, tree, e, kind):
        if tree.identify_region(e.x, e.y) != "cell":
            return
        col = tree.column(tree.identify_column(e.x), "id")
        row = tree.identify_row(e.y)
        job = self._shown_job()
        if not row or job is None or col not in ("codec", "br", "mix", "title"):
            return
        t = job["audio"][int(row)]
        x, y, w, h = tree.bbox(row, col)
        if col == "title":
            var = tk.StringVar(value=t["title"])
            ed = ttk.Entry(tree, textvariable=var)
        else:
            table = {"codec": ACODECS, "br": BITRATES, "mix": MIXES}[col]
            field = {"codec": "codec", "br": "bitrate", "mix": "mix"}[col]
            var = tk.StringVar(value=next((k for k, v in table.items() if v == t[field]),
                                          next(iter(table))))
            ed = KeyedCombobox(tree, textvariable=var, values=list(table),
                               labels=_labels(table), state="readonly")
        ed.place(x=x, y=y, width=max(w, 90), height=h)
        ed.focus_set()
        done = {"v": False}

        def commit(_e=None):
            if done["v"]:
                return
            done["v"] = True
            v = var.get()
            if col == "title":
                t["title"] = v.strip()
            else:
                table = {"codec": ACODECS, "br": BITRATES, "mix": MIXES}[col]
                field = {"codec": "codec", "br": "bitrate", "mix": "mix"}[col]
                if v in table:
                    t[field] = table[v]
                if field == "codec" and t["codec"] not in ("copy", "flac") and not t["bitrate"]:
                    a = job["info"]["audio"][t["src"]]
                    t["bitrate"] = recode.default_bitrate(t["codec"], a["channels"])
            ed.destroy()
            self._tracks_changed()

        if col == "title":
            ed.bind("<Return>", commit)
            ed.bind("<FocusOut>", commit)
        else:
            ed.bind("<<ComboboxSelected>>", commit)
            ed.bind("<FocusOut>", lambda _e: ed.after(150, commit))
            ed.after(50, lambda: ed.event_generate("<Button-1>") if ed.winfo_exists() else None)
        ed.bind("<Escape>", lambda _e: (done.update(v=True), ed.destroy()))
        return "break"

    # ------------------------------------------------------------ quick actions
    def _keep_only_lang(self):
        job = self._shown_job()
        lang = self.keep_lang_var.get()
        if job is None or not lang:
            return
        first = True
        for t in job["audio"]:
            t["keep"] = job["info"]["audio"][t["src"]]["lang"] == lang
            t["default"] = t["keep"] and first
            if t["keep"]:
                first = False
        self._tracks_changed()

    def _copy_all(self):
        job = self._shown_job()
        if job is None:
            return
        for t in job["audio"]:
            t["keep"], t["codec"], t["mix"] = True, "copy", "keep"
        self._tracks_changed()

    def _apply_rules_shown(self):
        job = self._shown_job()
        if job is None:
            return
        job["audio"], job["subs"] = recode.tracks_from_rules(job["info"], job["settings"])
        self._tracks_changed()
