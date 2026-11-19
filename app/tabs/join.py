"""Join page: several videos -> one file (engine/join.py).

The list (order = the order in the joined file), the compatibility line
under it (lossless stream copy possible, or why every file is re-encoded),
the output (folder, file name, MKV / MP4) and the re-encode settings
(Convert's presets, a codec / quality override) - used only when the files
differ. Running a join (Join / Add to queue / Stop, the footer progress) is
JoinRunMixin (join_run.py)."""
import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from ..config import APP_ROOT, MEDIA_EXTS, OUTPUT_DIR
from ..engine import join as jengine
from ..engine.formatting import format_seconds, format_size
from ..i18n import N_, ntr, tr
from ..ui import icons, themes
from ..ui.helpwin import help_button
from ..ui.tkthread import _call_tk
from ..ui.tooltips import add_tooltip, info_icon
from ..ui.widgets import (KeyedCombobox, auto_wrap, bind_status_colors, build_log_tab,
                          elide_label, enable_file_drop, enable_paths_drop, trim_text_lines)
from . import convert_presets as cpresets
from .convert_settings import VCODECS
from .join_run import JoinRunMixin, safe_name
from .. import applog

_VIDEO_TYPES = [(tr("Video files"), " ".join("*" + e for e in MEDIA_EXTS)),
                (tr("All files"), "*.*")]
# why the files can't be joined losslessly (engine codes -> shown labels)
REASON_LABELS = {
    "video_codec": N_("video codec"), "profile": N_("video profile"),
    "resolution": N_("resolution"), "sar": N_("pixel aspect ratio"),
    "pix_fmt": N_("pixel format"), "fps": N_("frame rate"), "field_order": N_("interlacing"),
    "extradata": N_("video encoder settings"), "audio_count": N_("number of audio tracks"),
    "audio_codec": N_("audio codec"), "audio_channels": N_("audio channels"),
    "audio_rate": N_("audio sample rate"), "sub_count": N_("number of subtitle tracks"),
    "sub_codec": N_("subtitle format"), "layout": N_("stream order"),
    "container": N_("container"),
}
# codec override: "" = the preset's own codec
CODECS = {N_("Preset's codec"): ""}
CODECS.update({k: v for k, v in VCODECS.items() if v != "copy"})
CONTAINERS = {"MKV": "mkv", "MP4": "mp4"}


def natural_key(path):
    """Sort key: 'E2' before 'E10' (digits compared as numbers)."""
    name = os.path.basename(path).lower()
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _abs_dir(p):
    if isinstance(p, str) and p.strip() and not os.path.isabs(p):
        return os.path.normpath(os.path.join(APP_ROOT, p))
    return p


def _rel_dir(p):
    if not (isinstance(p, str) and p.strip() and os.path.isabs(p)):
        return p
    try:
        rel = os.path.relpath(os.path.normpath(p), APP_ROOT)
    except ValueError:
        return p
    if rel == os.curdir or rel.startswith(os.pardir):
        return p
    return rel.replace(os.sep, "/")


def _fps_text(fps):
    return f"{fps:.3f}".rstrip("0").rstrip(".") if fps else "?"


def video_text(fi):
    v = fi["video"]
    return f"{v['codec'].upper()} {v['width']}x{v['height']} {_fps_text(v['fps'])}"


def tracks_text(tracks):
    if not tracks:
        return "–"
    langs = ", ".join(t["lang"] for t in tracks[:4]) + (" …" if len(tracks) > 4 else "")
    return f"{len(tracks)}: {langs}"


class JoinTab(JoinRunMixin, ttk.Frame):
    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.items = {}            # tree iid -> {"path", "info" (probe_file / None / "?")}
        self._seq = 0
        self._busy = False
        self._jid = None
        self._auto_name = ""       # the file name we filled in (replaced when it changes)
        self._compat = None        # engine reasons of the list, None = can't tell yet
        self.save_hook = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        self._nb = nb
        page = ttk.Frame(nb, padding=(4, 10, 4, 4))
        nb.add(page, text=f"  {tr('Files')}  ")
        self._build_files(page)
        self._build_options(page, saved)
        self.logbox = build_log_tab(nb)

        # ---- footer: status + progress + Stop (the window's fixed footer) ----
        self.status_var = tk.StringVar(value=tr("Idle"))
        host = bottom if bottom is not None else self
        lbl = elide_label(host, textvariable=self.status_var, style="Hint.TLabel")
        prog = ttk.Frame(host)
        if bottom is not None:
            lbl.pack(fill="x", padx=10, pady=(4, 0))
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            lbl.grid(row=1, column=0, sticky="we", pady=(6, 0))
            prog.grid(row=2, column=0, sticky="we")
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        elide_label(prog, textvariable=self.pct_var, width=34).grid(row=0, column=1, sticky="w",
                                                                    padx=(6, 0))
        self.stop_btn = icons.decorate(ttk.Button(prog, text=tr("Stop"), command=self.stop,
                                                  state="disabled", width=-8), "stop")
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, tr("Stop now: kills ffmpeg and removes the unfinished "
                                      "file (finished files are kept)"))
        self._refresh()

    # ================================================================ build
    def _build_files(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="we", pady=(0, 6))
        icons.decorate(ttk.Button(bar, text=tr("Add files..."), command=self._add_files),
                       "add").pack(side="left")
        icons.decorate(ttk.Button(bar, text=tr("Add folder..."), command=self._add_folder),
                       "folder").pack(side="left", padx=(6, 0))
        self.rm_btn = icons.decorate(ttk.Button(bar, text=tr("Remove"),
                                                command=self._remove_selected), "remove")
        self.rm_btn.pack(side="left", padx=(14, 0))
        add_tooltip(self.rm_btn, tr("Remove the selected files from the list (Delete key)"))
        self.clear_btn = icons.decorate(ttk.Button(bar, text=tr("Clear"), command=self._clear),
                                        "trash")
        self.clear_btn.pack(side="left", padx=(6, 0))
        help_button(bar, "join").pack(side="right")
        self.qbtn = icons.decorate(ttk.Button(bar, text=tr("Add to queue"),
                                              command=self._enqueue), "queue")
        self.qbtn.pack(side="right", padx=(0, 6))
        add_tooltip(self.qbtn, tr("Join the files after the jobs already running / queued "
                                  "- see Queue... in the status bar"))
        self.start_btn = icons.decorate(ttk.Button(bar, text=tr("Join"), style="Accent.TButton",
                                                   command=self.start), "join")
        self.start_btn.pack(side="right", padx=(0, 6))
        add_tooltip(self.start_btn, tr("Join the files, from the top of the list to the "
                                       "bottom, into one file"))

        tv = ttk.Frame(page)
        tv.grid(row=1, column=0, sticky="nsew")
        tv.columnconfigure(0, weight=1)
        tv.rowconfigure(0, weight=1)
        cols = ("n", "file", "dur", "video", "audio", "subs", "status")
        self.tree = ttk.Treeview(tv, columns=cols, show="headings", height=7)
        for c, t, w, anc, st in (("n", "#", 34, "center", False),
                                 ("file", tr("File"), 230, "w", True),
                                 ("dur", tr("Duration"), 72, "center", False),
                                 ("video", tr("Video"), 150, "w", False),
                                 ("audio", tr("Audio"), 96, "w", False),
                                 ("subs", tr("Subtitles"), 86, "w", False),
                                 ("status", tr("Status"), 110, "w", False)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=themes.px(w), minwidth=themes.px(30), anchor=anc,
                             stretch=st)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        # the order of the list = the order in the joined file
        side = ttk.Frame(tv)
        side.grid(row=0, column=2, sticky="n", padx=(8, 0))
        self.up_btn = icons.decorate(ttk.Button(side, text=tr("Move up"),
                                                command=lambda: self._move(-1)), "up")
        self.up_btn.pack(fill="x")
        add_tooltip(self.up_btn, tr("Move the selected files up (Ctrl+Up)"))
        self.down_btn = icons.decorate(ttk.Button(side, text=tr("Move down"),
                                                  command=lambda: self._move(1)), "down")
        self.down_btn.pack(fill="x", pady=(6, 0))
        add_tooltip(self.down_btn, tr("Move the selected files down (Ctrl+Down)"))
        self.sort_btn = icons.decorate(ttk.Button(side, text=tr("Sort by name"),
                                                  command=self._sort), "list")
        self.sort_btn.pack(fill="x", pady=(14, 0))
        add_tooltip(self.sort_btn, tr("Sort the list by file name (E2 before E10)"))
        bind_status_colors(self.tree, {"ok": "good", "bad": "bad", "warn": "warn"})
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<Control-Up>", lambda e: (self._move(-1), "break")[1])
        self.tree.bind("<Control-Down>", lambda e: (self._move(1), "break")[1])
        enable_paths_drop(self.tree, self._drop)

        foot = ttk.Frame(page)
        foot.grid(row=2, column=0, sticky="we", pady=(6, 0))
        foot.columnconfigure(0, weight=1)
        self.compat_var = tk.StringVar(value="")
        self.compat_lbl = ttk.Label(foot, textvariable=self.compat_var, justify="left")
        auto_wrap(self.compat_lbl, width=900)
        self.compat_lbl.grid(row=0, column=0, sticky="w")
        self._compat_kind = "hint"
        themes.on_palette(self.compat_lbl, lambda pal: self._color_compat(pal))
        info_icon(foot, tr("Drop files or folders onto the list. The files are joined from "
                           "top to bottom: Move up / Move down (Ctrl+Up / Ctrl+Down) and Sort "
                           "by name change the order.")).grid(row=0, column=1, sticky="ne")
        self.sum_var = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.sum_var, style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 0))

    def _build_options(self, page, saved):
        opts = ttk.Frame(page)
        opts.grid(row=3, column=0, sticky="we", pady=(8, 0))
        opts.columnconfigure(0, weight=1, uniform="jn")
        opts.columnconfigure(1, weight=1, uniform="jn")

        box = ttk.LabelFrame(opts, text=tr("Output"))
        box.grid(row=0, column=0, sticky="nswe", padx=(0, 8))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=tr("Output folder:")).grid(row=0, column=0, sticky="w", padx=4,
                                                       pady=3)
        of = ttk.Frame(box)
        of.grid(row=0, column=1, sticky="we", padx=4, pady=3)
        of.columnconfigure(0, weight=1)
        self.out_dir = tk.StringVar(value=_abs_dir(saved.get("jn_out_dir")) or OUTPUT_DIR)
        oe = ttk.Entry(of, textvariable=self.out_dir, width=22)
        oe.grid(row=0, column=0, sticky="we")
        enable_file_drop(oe, lambda p: os.path.isdir(p) and self.out_dir.set(p))
        icons.decorate(ttk.Button(of, text="...", width=3, command=self._pick_out),
                       "folder").grid(row=0, column=1, padx=(4, 0))
        ttk.Label(box, text=tr("File name:")).grid(row=1, column=0, sticky="w", padx=4, pady=3)
        nf = ttk.Frame(box)
        nf.grid(row=1, column=1, sticky="we", padx=4, pady=3)
        nf.columnconfigure(0, weight=1)
        self.name_var = tk.StringVar(value="")
        ttk.Entry(nf, textvariable=self.name_var, width=22).grid(row=0, column=0, sticky="we")
        self.container_var = tk.StringVar(value=saved.get("jn_container") if saved.get(
            "jn_container") in CONTAINERS.values() else "mkv")
        self.ext_var = tk.StringVar(value="." + self.container_var.get())
        ttk.Label(nf, textvariable=self.ext_var).grid(row=0, column=1, padx=(4, 0))
        info_icon(nf, tr("Default: the first file's name + ' (joined)'. An existing file is "
                         "never silently replaced: Join asks - Overwrite, Keep both (adds "
                         "(2)) or Skip.")).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(box, text=tr("Container:")).grid(row=2, column=0, sticky="w", padx=4, pady=3)
        self._ckey = tk.StringVar(value="MP4" if self.container_var.get() == "mp4" else "MKV")
        ccb = KeyedCombobox(box, textvariable=self._ckey, values=list(CONTAINERS),
                            translate=False, state="readonly", width=8)
        ccb.grid(row=2, column=1, sticky="w", padx=4, pady=(3, 8))
        add_tooltip(ccb, tr("MP4 can't hold ASS / PGS subtitles or fonts: text subtitles "
                            "become mov_text, picture subtitles and attachments are "
                            "dropped (each with a log line). MKV keeps everything."))
        self._ckey.trace_add("write", lambda *a: self._container_changed())

        rb = ttk.LabelFrame(opts, text=tr("Re-encode settings"))
        rb.grid(row=0, column=1, sticky="nswe", padx=(8, 0))
        rb.columnconfigure(1, weight=1)
        ttk.Label(rb, text=tr("Preset:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.preset_var = tk.StringVar()
        self.preset_cb = KeyedCombobox(rb, textvariable=self.preset_var, state="readonly",
                                       width=24, postcommand=self._preset_values)
        self.preset_cb.grid(row=0, column=1, sticky="we", padx=4, pady=3)
        self._preset_values()
        last = str(saved.get("jn_preset") or "")
        self.preset_var.set(last if last in cpresets.BUILTIN or last in cpresets.user_names()
                            else "Plex-ready HEVC")
        ttk.Label(rb, text=tr("Codec:")).grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.codec_var = tk.StringVar()
        self.codec_cb = KeyedCombobox(rb, textvariable=self.codec_var, values=list(CODECS),
                                      state="readonly", width=24)
        self.codec_cb.grid(row=1, column=1, sticky="we", padx=4, pady=3)
        codec = str(saved.get("jn_codec") or "")
        self.codec_var.set(next((k for k, v in CODECS.items() if v == codec), "Preset's codec"))
        ttk.Label(rb, text=tr("CRF / CQ:")).grid(row=2, column=0, sticky="w", padx=4, pady=3)
        self.crf_var = tk.StringVar(value=str(saved.get("jn_crf") or ""))
        self.crf_spin = ttk.Spinbox(rb, from_=0, to=63, increment=1, textvariable=self.crf_var,
                                    width=6)
        self.crf_spin.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        self.rb_hint = tk.StringVar(value="")
        auto_wrap(ttk.Label(rb, textvariable=self.rb_hint, style="Hint.TLabel",
                            justify="left"), width=380).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(2, 8))
        self._rb_widgets = (self.preset_cb, self.codec_cb, self.crf_spin)
        if not self.crf_var.get().strip():
            self._preset_crf()
        self.preset_var.trace_add("write", lambda *a: self._preset_crf())

    # ================================================================ presets
    def _preset_values(self):
        users = cpresets.user_names()
        self.preset_cb.configure(values=list(cpresets.BUILTIN) + users,
                                 labels=[tr(k) for k in cpresets.BUILTIN] + users)

    def _preset_crf(self):
        """A new preset: show its quality (the override starts from it)."""
        p = cpresets.load(self.preset_var.get()) or {}
        s = cpresets.preset_settings({}, p)
        self.crf_var.set(str(s["crf"]))

    def _container_changed(self):
        self.container_var.set(CONTAINERS.get(self._ckey.get(), "mkv"))
        self.ext_var.set("." + self.container_var.get())
        self._refresh()

    def _pick_out(self):
        d = filedialog.askdirectory(title=tr("Select folder"), initialdir=self.out_dir.get()
                                    if os.path.isdir(self.out_dir.get()) else OUTPUT_DIR)
        if d:
            self.out_dir.set(os.path.normpath(d))

    # ================================================================ list
    def _add_files(self):
        paths = filedialog.askopenfilenames(title=tr("Select videos"), filetypes=_VIDEO_TYPES,
                                            initialdir=self._input_dir())
        if paths:
            self._add_paths(list(paths))

    def _add_folder(self):
        d = filedialog.askdirectory(title=tr("Select folder"), initialdir=self._input_dir())
        if d:
            self._add_paths([d])

    def _input_dir(self):
        paths = [it["path"] for it in self.items.values()]
        if paths and os.path.isdir(os.path.dirname(paths[-1])):
            return os.path.dirname(paths[-1])
        from ..config import VIDEO_DIR
        return VIDEO_DIR

    def _drop(self, paths):
        self._add_paths([p for p in paths if p])

    def _add_paths(self, paths):
        """Files as given; a folder adds its videos in name order (E2 before
        E10)."""
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        files = []
        for p in paths:
            p = os.path.normpath(str(p).strip().strip('"'))
            if os.path.isdir(p):
                try:
                    found = [os.path.join(p, f) for f in os.listdir(p)
                             if f.lower().endswith(MEDIA_EXTS)
                             and os.path.isfile(os.path.join(p, f))]
                except OSError:
                    found = []
                files += sorted(found, key=natural_key)
            elif os.path.isfile(p) and p.lower().endswith(MEDIA_EXTS):
                files.append(p)
        have = {os.path.normcase(os.path.abspath(it["path"])) for it in self.items.values()}
        new = []
        for f in files:
            key = os.path.normcase(os.path.abspath(f))
            if key in have:
                continue
            have.add(key)
            self._seq += 1
            iid = f"j{self._seq}"
            self.items[iid] = {"path": f, "info": "?"}
            self.tree.insert("", "end", iid=iid, values=("", os.path.basename(f), "", "", "",
                                                         "", tr("Reading...")))
            new.append((iid, f))
        if not new:
            self.status_var.set(tr("No new video files found."))
            return
        self._refresh()
        threading.Thread(target=self._probe_worker, args=(new,), daemon=True).start()

    def _probe_worker(self, items):
        for iid, f in items:
            try:
                info = jengine.probe_file(f)
            except Exception:
                info = None
            _call_tk(lambda iid=iid, info=info: self._probed(iid, info))

    def _probed(self, iid, info):
        it = self.items.get(iid)
        if it is None or not self.tree.exists(iid):
            return
        it["info"] = info
        if info is None:
            self.log(f"[ADD] not a readable video: {it['path']}")
        self._refresh()

    def _remove_selected(self):
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._refresh()

    def _clear(self):
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._refresh()

    def _move(self, d):
        """Move the selected rows one place up (d=-1) / down (d=1)."""
        if self._busy:
            return
        kids = list(self.tree.get_children())
        sel = [i for i in kids if i in self.tree.selection()]
        if not sel:
            return
        order = kids[:]
        for iid in (sel if d < 0 else reversed(sel)):
            k = order.index(iid)
            j = k + d
            if 0 <= j < len(order) and order[j] not in sel:
                order[k], order[j] = order[j], order[k]
        for n, iid in enumerate(order):
            self.tree.move(iid, "", n)
        self.tree.see(sel[0] if d < 0 else sel[-1])
        self._refresh()

    def _sort(self):
        if self._busy:
            return
        order = sorted(self.tree.get_children(), key=lambda i: natural_key(self.items[i]["path"]))
        for n, iid in enumerate(order):
            self.tree.move(iid, "", n)
        self._refresh()

    def ordered(self):
        """[(iid, item)] in list order."""
        return [(i, self.items[i]) for i in self.tree.get_children() if i in self.items]

    # ================================================================ state
    def _refresh(self):
        """Row numbers / columns, the compatibility line, the default output
        name, the re-encode box state."""
        rows = self.ordered()
        for n, (iid, it) in enumerate(rows, 1):
            info = it["info"]
            if info == "?":
                vals = (n, os.path.basename(it["path"]), "", "", "", "", tr("Reading..."))
                tag = ""
            elif info is None:
                vals = (n, os.path.basename(it["path"]), "", "", "", "",
                        tr("Not a readable video"))
                tag = "bad"
            else:
                st = it.get("status") or tr("Ready")
                vals = (n, os.path.basename(it["path"]),
                        format_seconds(info["vduration"] or info["duration"] or 0),
                        video_text(info), tracks_text(info["audio"]), tracks_text(info["subs"]),
                        st)
                tag = it.get("tag", "")
            self.tree.item(iid, values=vals, tags=(tag,) if tag else ())
        infos = [it["info"] for _i, it in rows]
        ready = [x for x in infos if isinstance(x, dict)]
        if len(rows) < 2:
            kind, text = "hint", tr("Add at least 2 videos - they are joined in the order of "
                                    "the list.")
            self._compat = None
        elif any(x == "?" for x in infos):
            kind, text = "hint", tr("Reading the files...")
            self._compat = None
        elif len(ready) < len(infos):
            kind, text = "bad", tr("Some files can't be read - remove them first.")
            self._compat = None
        else:
            self._compat = jengine.compare(ready, self.container_var.get())
            if not self._compat:
                kind, text = "ok", "✔ " + tr("Lossless join possible (no re-encode)")
            else:
                why = "; ".join(f"{tr(REASON_LABELS.get(c, c))} ({d})" for c, d in self._compat)
                kind, text = "warn", "⚠ " + tr(
                    "The files differ: {reasons} - all files will be re-encoded with the "
                    "settings below", reasons=why)
        self.compat_var.set(text)
        self._compat_kind = kind
        self._color_compat(themes.current())
        # re-encode box: only used when the files differ
        lossless = self._compat == []
        state = "disabled" if lossless else "readonly"
        for w in self._rb_widgets:
            try:
                w.configure(state=state if w is not self.crf_spin else
                            ("disabled" if lossless else "normal"))
            except tk.TclError:
                pass
        self.rb_hint.set(tr("Not needed: the files match and are joined without "
                            "re-encoding.") if lossless else
                         tr("Used because the files differ: every file is re-encoded to the "
                            "first file's picture size and frame rate.") if self._compat else
                         tr("Used only when the files differ."))
        # summary + default output name (from the first file)
        size = sum(x["size"] for x in ready)
        total = sum(x["vduration"] or x["duration"] or 0 for x in ready)
        n = len(rows)
        self.sum_var.set((ntr("{n} file, {size}", "{n} files, {size}", n, size=format_size(size))
                          + (f" · {format_seconds(total)}" if total else "")) if n
                         else tr("No files yet."))
        first = rows[0][1]["path"] if rows else ""
        # (the suffix is translated - safe_name keeps the result a valid name)
        auto = safe_name(tr("{name} (joined)", name=os.path.splitext(
            os.path.basename(first))[0])) if first else ""
        if self.name_var.get().strip() in ("", self._auto_name):
            self.name_var.set(auto)
        self._auto_name = auto

    def _color_compat(self, pal):
        key = {"ok": "ok", "warn": "warn", "bad": "error"}.get(self._compat_kind)
        try:
            self.compat_lbl.configure(foreground=pal[key] if key else pal["muted"])
        except (tk.TclError, KeyError):
            pass

    def join_settings(self):
        """Convert settings for a re-encode: the preset, with the codec /
        quality shown (the output folder / name are the page's)."""
        s = cpresets.preset_settings({}, cpresets.load(self.preset_var.get()) or {})
        codec = CODECS.get(self.codec_var.get(), "")
        if codec:
            s["vcodec"] = codec
        try:
            s["crf"] = max(0, min(63, int(float(self.crf_var.get()))))
        except (TypeError, ValueError):
            pass
        s["container"] = self.container_var.get()
        return s

    # ================================================================ misc
    def log(self, msg):
        applog.record(msg)

        def _a():
            try:
                self.logbox.configure(state="normal")
                self.logbox.insert("end", msg + "\n")
                trim_text_lines(self.logbox)
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
            except tk.TclError:
                pass
        _call_tk(_a)

    def snapshot(self):
        try:
            crf = int(float(self.crf_var.get()))
        except (TypeError, ValueError):
            crf = None
        return {"jn_out_dir": _rel_dir(self.out_dir.get().strip()),
                "jn_container": self.container_var.get(),
                "jn_preset": self.preset_var.get(),
                "jn_codec": CODECS.get(self.codec_var.get(), ""),
                "jn_crf": crf}
