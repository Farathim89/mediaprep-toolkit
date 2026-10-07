"""Convert page (HandBrake-style re-encode): Queue | Video | Audio &
subtitles | Output | Log, the preset row and the footer progress strip.

Each queued file is a job (engine/recode.py) with its OWN settings and
track tables. The form shows the first selected file; changing it changes
every selected file (with no file selected it sets the defaults new files
start from). "Apply to all files" copies the shown settings to every file.
The settings form is SettingsMixin (convert_settings.py), the track tables
TracksMixin (convert_tracks.py), presets convert_presets.py."""
import copy
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import MEDIA_EXTS
from ..engine import recode
from ..engine.formatting import format_seconds, format_size, parse_time
from ..i18n import ntr, tr
from ..ui import icons, themes
from ..ui.dialogs import ask_choice, ask_string
from ..ui.tkthread import _call_tk
from ..ui.widgets import (add_tooltip, bind_status_colors, build_log_tab, enable_paths_drop,
                          help_button, info_icon, trim_text_lines, KeyedCombobox)
from . import convert_presets as cpresets
from .convert_settings import SettingsMixin
from .convert_tracks import TracksMixin
from .. import applog, jobs

_VIDEO_TYPES = [(tr("Video files"), " ".join("*" + e for e in MEDIA_EXTS)),
                (tr("All files"), "*.*")]


def _media_in(folder, recursive):
    out = []
    if recursive:
        for root, _d, files in os.walk(folder):
            out += [os.path.join(root, f) for f in files if f.lower().endswith(MEDIA_EXTS)]
    else:
        try:
            out = [os.path.join(folder, f) for f in os.listdir(folder)
                   if f.lower().endswith(MEDIA_EXTS)
                   and os.path.isfile(os.path.join(folder, f))]
        except OSError:
            pass
    return sorted(out, key=str.lower)


def _src_text(info):
    v = info["video"]
    hdr = {"smpte2084": " HDR10", "arib-std-b67": " HLG"}.get(v["transfer"], "")
    dur = format_seconds(info["duration"] or 0)
    return f"{v['w']}x{v['h']} {v['codec'].upper()} {v['bit_depth']}-bit{hdr} · {dur}"


class ConvertTab(SettingsMixin, TracksMixin, ttk.Frame):
    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.jobs_by_iid = {}          # tree iid -> job dict
        self._fv = {}                  # settings key -> (tk var, kind)
        self._loading = False
        self._shown = None             # iid of the job shown in the form
        self._busy = False
        self._jid = None
        self._last_preview = None      # (source segment, sample)
        self.open_dual = None          # set by app.py: fn(path_a, path_b)
        self.unload_players_hook = None
        self.save_hook = None
        d = recode.normalize_settings(saved.get("cv_settings") or {})
        if not d["out_dir"]:
            d["out_dir"] = recode.CONVERT_OUTPUT
        self.defaults = d
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_preset_row(saved)
        ctx = ttk.Frame(self)
        ctx.grid(row=1, column=0, sticky="we", pady=(0, 6))
        self.ctx_var = tk.StringVar()
        ttk.Label(ctx, textvariable=self.ctx_var, style="Strong.TLabel").pack(side="left")
        self.all_btn = icons.decorate(ttk.Button(ctx, text=tr("Apply to all files"),
                                                 command=self._apply_to_all), "copy")
        self.all_btn.pack(side="right")
        add_tooltip(self.all_btn, tr("Give every file in the queue the settings shown "
                                     "(track tables too, for files with the same tracks; "
                                     "the others get them from the audio / subtitle rules)"))

        nb = ttk.Notebook(self)
        nb.grid(row=2, column=0, sticky="nsew")
        self._nb = nb
        pages = []
        for title in (tr("Queue"), tr("Video"), tr("Audio & subtitles"), tr("Output")):
            f = ttk.Frame(nb, padding=(4, 10, 4, 4))
            nb.add(f, text=f"  {title}  ")
            pages.append(f)
        self._build_queue_tab(pages[0], saved)
        self._build_video_tab(pages[1])
        self._build_tracks_tab(pages[2])
        self._build_output_tab(pages[3])
        self.logbox = build_log_tab(nb)
        try:
            nb.select(int(saved.get("cv_tab", 0)) % 4)
        except (TypeError, ValueError, tk.TclError):
            pass

        # ---- footer: status + progress + Stop (the window's fixed footer) ----
        self.status_var = tk.StringVar(value=tr("Idle"))
        host = bottom if bottom is not None else self
        lbl = ttk.Label(host, textvariable=self.status_var, style="Hint.TLabel")
        prog = ttk.Frame(host)
        if bottom is not None:
            lbl.pack(anchor="w", padx=10, pady=(4, 0))
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            lbl.grid(row=3, column=0, sticky="w", pady=(6, 0))
            prog.grid(row=4, column=0, sticky="we")
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=34).grid(row=0, column=1, sticky="w",
                                                                  padx=(6, 0))
        self.stop_btn = icons.decorate(ttk.Button(prog, text=tr("Stop"), command=self.stop,
                                                  state="disabled", width=-8), "stop")
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        add_tooltip(self.stop_btn, tr("Stop now: kills ffmpeg and removes the unfinished "
                                      "file (finished files are kept)"))

        self._set_form(self.defaults)
        self._on_select()

    # ================================================================ top rows
    def _build_preset_row(self, saved):
        row = ttk.Frame(self)
        row.grid(row=0, column=0, sticky="we", pady=(0, 8))
        ttk.Label(row, text=tr("Preset:")).pack(side="left", padx=(0, 6))
        self.preset_var = tk.StringVar()
        self.preset_cb = KeyedCombobox(row, textvariable=self.preset_var, state="readonly",
                                       width=36, postcommand=self._preset_values)
        self.preset_cb.pack(side="left")
        self._preset_values()
        last = str(saved.get("cv_preset") or "")
        if last in cpresets.BUILTIN or last in cpresets.user_names():
            self.preset_var.set(last)
        for txt, cmd, icon, tip in (
                (tr("Apply"), self._preset_apply, "check",
                 tr("Apply the preset to the selected files (no file selected: to the "
                    "settings new files get). The output folder and file name stay.")),
                (tr("Save as..."), self._preset_save, "save",
                 tr("Save the settings shown as your own preset "
                    "(Data\\presets\\convert)")),
                (tr("Delete"), self._preset_delete, "trash",
                 tr("Move your preset to Data\\temp\\trash (recoverable - built-in "
                    "presets can't be deleted)"))):
            b = icons.decorate(ttk.Button(row, text=txt, command=cmd), icon)
            b.pack(side="left", padx=(6, 0))
            add_tooltip(b, tip)
        help_button(row, "convert").pack(side="right")

    def _preset_values(self):
        users = cpresets.user_names()
        keys = list(cpresets.BUILTIN) + users
        self.preset_cb.configure(values=keys,
                                 labels=[tr(k) for k in cpresets.BUILTIN] + users)

    def _preset_apply(self):
        name = self.preset_var.get()
        p = cpresets.load(name) if name else None
        if p is None:
            self.status_var.set(tr("Pick a preset first."))
            return
        s = cpresets.preset_settings(self._get_form(), p)
        self._set_form(s)
        self._form_changed(rebuild_tracks=True)
        self.log(f"[PRESET] applied: {name}")
        self.status_var.set(tr("Preset applied: {name}", name=self.preset_cb.label()))

    def _preset_save(self):
        name = ask_string(self, tr("Save preset"), tr("Preset name:"),
                          initialvalue="" if self.preset_var.get() in cpresets.BUILTIN
                          else self.preset_var.get())
        if not name:
            return
        try:
            saved = cpresets.save(name, self._get_form())
        except ValueError:
            messagebox.showerror(tr("Error"), tr("Give the preset a name (letters / numbers) "
                                                 "that isn't a built-in preset's name."))
            return
        except OSError as e:
            messagebox.showerror(tr("Error"), str(e))
            return
        self._preset_values()
        self.preset_var.set(saved)
        self.log(f"[PRESET] saved: {saved}")

    def _preset_delete(self):
        name = self.preset_var.get()
        if not name or name in cpresets.BUILTIN:
            self.status_var.set(tr("Built-in presets can't be deleted."))
            return
        if not messagebox.askyesno(tr("Delete preset"),
                                   tr("Move the preset '{name}' to the trash?", name=name)):
            return
        try:
            cpresets.trash(name)
        except OSError as e:
            messagebox.showerror(tr("Error"), str(e))
            return
        self.preset_var.set("")
        self._preset_values()
        self.log(f"[PRESET] moved to the trash: {name}")

    # ================================================================ queue tab
    def _build_queue_tab(self, page, saved):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="we", pady=(0, 6))
        icons.decorate(ttk.Button(bar, text=tr("Add files..."), command=self._add_files),
                       "add").pack(side="left")
        icons.decorate(ttk.Button(bar, text=tr("Add folder..."), command=self._add_folder),
                       "folder").pack(side="left", padx=(6, 0))
        self.recursive_var = tk.BooleanVar(value=bool(saved.get("cv_recursive", True)))
        ttk.Checkbutton(bar, text=tr("Include sub-folders"),
                        variable=self.recursive_var).pack(side="left", padx=(10, 0))
        self.rm_btn = icons.decorate(ttk.Button(bar, text=tr("Remove"),
                                                command=self._remove_selected), "remove")
        self.rm_btn.pack(side="left", padx=(14, 0))
        add_tooltip(self.rm_btn, tr("Remove the selected files from the list (Delete key)"))
        self.clear_btn = icons.decorate(ttk.Button(bar, text=tr("Clear"), command=self._clear),
                                        "trash")
        self.clear_btn.pack(side="left", padx=(6, 0))
        self.qbtn = icons.decorate(ttk.Button(bar, text=tr("Add to queue"),
                                              command=self._enqueue), "queue")
        self.qbtn.pack(side="right")
        add_tooltip(self.qbtn, tr("Convert the waiting files (with their settings as they "
                                  "are now) after the jobs already running / queued - see "
                                  "Queue... in the status bar"))
        self.start_btn = icons.decorate(ttk.Button(bar, text=tr("Start converting"), style="Accent.TButton",
                                                   command=self.start), "run")
        self.start_btn.pack(side="right", padx=(0, 6))
        add_tooltip(self.start_btn, tr("Convert every waiting file, one after another"))

        tv = ttk.Frame(page)
        tv.grid(row=1, column=0, sticky="nsew")
        tv.columnconfigure(0, weight=1)
        tv.rowconfigure(0, weight=1)
        cols = ("file", "src", "status", "before", "after", "time")
        self.tree = ttk.Treeview(tv, columns=cols, show="headings", height=12)
        for c, t, w, anc, st in (("file", tr("File"), 300, "w", True),
                                 ("src", tr("Source"), 230, "w", True),
                                 ("status", tr("Status"), 150, "w", False),
                                 ("before", tr("Size before"), 100, "e", False),
                                 ("after", tr("Size after"), 110, "e", False),
                                 ("time", tr("Time / ETA"), 84, "e", False)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=themes.px(w), minwidth=themes.px(50), anchor=anc, stretch=st)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        bind_status_colors(self.tree, {"ok": "good", "bad": "bad", "warn": "warn"})
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        enable_paths_drop(self.tree, self._drop)
        foot = ttk.Frame(page)
        foot.grid(row=2, column=0, sticky="we", pady=(6, 0))
        self.sum_var = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.sum_var, style="Hint.TLabel").pack(side="left")
        info_icon(foot, tr("Drop files or folders onto the list. Select a file to see and "
                           "change its settings on the other sub-tabs (Ctrl / Shift-click "
                           "selects several - they all get the changes). Output default: "
                           "Media\\convert\\output.")).pack(side="right")

    # ---------------------------------------------------------------- adding
    def _add_files(self):
        paths = filedialog.askopenfilenames(title=tr("Select videos"), filetypes=_VIDEO_TYPES,
                                            initialdir=self._input_dir())
        if paths:
            self._add_paths(list(paths))

    def _add_folder(self):
        d = filedialog.askdirectory(title=tr("Select folder"), initialdir=self._input_dir())
        if d:
            self._add_paths([d])

    @staticmethod
    def _input_dir():
        try:
            os.makedirs(recode.CONVERT_INPUT, exist_ok=True)
        except OSError:
            pass
        return recode.CONVERT_INPUT

    def _drop(self, paths):
        self._add_paths([p for p in paths if p])

    def _add_paths(self, paths):
        files = []
        for p in paths:
            p = os.path.normpath(p.strip().strip('"'))
            if os.path.isdir(p):
                files += _media_in(p, self.recursive_var.get())
            elif os.path.isfile(p) and p.lower().endswith(MEDIA_EXTS):
                files.append(p)
        new = []
        for f in files:
            iid = os.path.normcase(os.path.abspath(f))
            if self.tree.exists(iid):
                continue
            self.tree.insert("", "end", iid=iid, values=(os.path.basename(f), "",
                                                         tr("Reading..."), "", "", ""))
            new.append((iid, f))
        if not new:
            if paths:
                self.status_var.set(tr("No new video files found."))
            return
        self._update_summary()
        threading.Thread(target=self._probe_worker, args=(new,), daemon=True).start()

    def _probe_worker(self, items):
        for iid, f in items:
            try:
                info = recode.probe_job(f)
            except Exception:
                info = None
            _call_tk(lambda iid=iid, f=f, info=info: self._probed(iid, f, info))

    def _probed(self, iid, f, info):
        if not self.tree.exists(iid):
            return
        if info is None:
            self.tree.item(iid, values=(os.path.basename(f), "", tr("Not a readable video"),
                                        "", "", ""), tags=("bad",))
            self.log(f"[ADD] skipped (no readable video): {f}")
            return
        job = recode.new_job(f, self.defaults, info)
        self.jobs_by_iid[iid] = job
        self.tree.item(iid, values=(os.path.basename(f), _src_text(info), tr("Waiting"),
                                    format_size(info["size"]), "", ""), tags=())
        if not self.tree.selection():
            self.tree.selection_set(iid)
        elif iid == self._shown:
            self._on_select()
        self._update_summary()

    def _remove_selected(self):
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.jobs_by_iid.pop(iid, None)
        self._on_select()
        self._update_summary()

    def _clear(self):
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return
        self.tree.delete(*self.tree.get_children())
        self.jobs_by_iid.clear()
        self._on_select()
        self._update_summary()

    def _update_summary(self):
        n = len(self.tree.get_children())
        size = sum(j["info"]["size"] for j in self.jobs_by_iid.values())
        self.sum_var.set(ntr("{n} file, {size}", "{n} files, {size}", n,
                             size=format_size(size)) if n else tr("No files yet."))

    # ---------------------------------------------------------------- selection
    def _selected_jobs(self):
        return [self.jobs_by_iid[i] for i in self.tree.selection() if i in self.jobs_by_iid]

    def _shown_job(self):
        return self.jobs_by_iid.get(self._shown)

    def _on_select(self):
        sel = [i for i in self.tree.selection() if i in self.jobs_by_iid]
        self._shown = sel[0] if sel else None
        job = self._shown_job()
        if job is None:
            self.ctx_var.set(tr("Settings for newly added files (no file selected)"))
            self._set_form(self.defaults)
        else:
            name = os.path.basename(job["input"])
            self.ctx_var.set(tr("Settings of: {name}", name=name) if len(sel) == 1 else
                             ntr("Settings of: {name} (+{n} more selected)",
                                 "Settings of: {name} (+{n} more selected)", len(sel) - 1,
                                 name=name))
            self._set_form(job["settings"])
        self._fill_tracks()
        self._show_source_info(job)
        self._update_example()

    def _form_changed(self, rebuild_tracks=False):
        if self._loading:
            return
        self._update_enables()
        jobs_ = self._selected_jobs()
        base = jobs_[0]["settings"] if jobs_ else self.defaults
        s = self._get_form(base)
        self.defaults = dict(s)          # new files start from the last settings
        for j in jobs_:
            j["settings"] = dict(s)
            if j.pop("done", False):     # changed after converting: convert it again
                iid = os.path.normcase(os.path.abspath(j["input"]))
                if self.tree.exists(iid):
                    self.tree.set(iid, "status", tr("Waiting"))
                    self.tree.item(iid, tags=())
            if rebuild_tracks:
                j["audio"], j["subs"] = recode.tracks_from_rules(j["info"], s)
        if rebuild_tracks:
            self._fill_tracks()
        self._update_example()

    def _tracks_changed(self):
        job = self._shown_job()
        if job is not None:
            for j in self._selected_jobs():
                if j is job:
                    continue
                if len(j["audio"]) == len(job["audio"]):
                    j["audio"] = copy.deepcopy(job["audio"])
                if len(j["subs"]) == len(job["subs"]):
                    j["subs"] = copy.deepcopy(job["subs"])
        self._fill_tracks()

    def _apply_to_all(self):
        job = self._shown_job()
        s = self._get_form(job["settings"] if job else self.defaults)
        n = 0
        for iid, j in self.jobs_by_iid.items():
            if j is job:
                continue
            j["settings"] = dict(s)
            if job is not None and len(j["audio"]) == len(job["audio"]) and \
                    len(j["subs"]) == len(job["subs"]):
                j["audio"], j["subs"] = copy.deepcopy(job["audio"]), copy.deepcopy(job["subs"])
            else:
                j["audio"], j["subs"] = recode.tracks_from_rules(j["info"], s)
            n += 1
        self.status_var.set(ntr("Settings copied to {n} file.", "Settings copied to {n} files.",
                                n))

    def _update_example(self):
        job = self._shown_job()
        if job is None:
            self.name_example.set("")
            return
        tmp = dict(job, settings=self._get_form(job["settings"]))
        self.name_example.set("→ " + recode.output_name(tmp))

    # ================================================================ run
    def _set_busy(self, on, text=None):
        self._busy = on
        for b in (self.start_btn, self.qbtn, self.preview_btn, self.crop_btn, self.rm_btn,
                  self.clear_btn):
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set(text or tr("Idle"))
            self.bar.configure(value=0)
            self.pct_var.set("")

    def _pending(self):
        out = []
        for iid in self.tree.get_children():
            j = self.jobs_by_iid.get(iid)
            if j is not None and not j.get("done"):
                out.append((iid, copy.deepcopy(j)))
        return out

    def _plan_outputs(self, items):
        """[(iid, job, output or None)]: asks ONCE what to do with outputs
        that exist already (Overwrite / Keep both / Skip). None = cancelled."""
        planned = [(iid, j, recode.output_path(j)) for iid, j in items]
        same = [p for _i, j, p in planned
                if os.path.normcase(os.path.abspath(p)) == os.path.normcase(
                    os.path.abspath(j["input"]))]
        if same:
            messagebox.showerror(tr("Error"), tr(
                "The output would replace its source:\n\n{path}\n\nChoose another output "
                "folder or file name.", path=same[0]))
            return None
        exist = [p for _i, _j, p in planned if os.path.exists(p)]
        if not exist:
            return planned
        choice = ask_choice(self, tr("Files exist"), ntr(
            "{n} output file exists already, e.g.\n{path}\n\nWhat should happen to it?",
            "{n} output files exist already, e.g.\n{path}\n\nWhat should happen to them?",
            len(exist), path=os.path.basename(exist[0])),
            [("over", tr("Overwrite")), ("both", tr("Keep both")), ("skip", tr("Skip")),
             ("cancel", tr("Cancel"))], default="both", cancel="cancel")
        if choice in (None, "cancel"):
            return None
        out = []
        for iid, j, p in planned:
            if os.path.exists(p):
                p = (p if choice == "over" else
                     recode.keep_both_path(p) if choice == "both" else None)
            out.append((iid, j, p))
        return out

    def start(self):
        if self._busy:
            return
        items = self._pending()
        if not items:
            self.status_var.set(tr("Nothing to convert - add files first."))
            return
        if self.save_hook:
            try:
                self.save_hook()
            except Exception:
                pass
        self._launch(items)

    def _launch(self, items):
        """Start converting `items` [(iid, job)] (Tk thread). Returns the job
        id, or None when nothing was started."""
        if self._busy:
            self.log("[QUEUE] Convert is busy - not started.")
            return None
        planned = self._plan_outputs(items)
        if not planned:
            return None
        self.stop_event.clear()
        self._set_busy(True)
        self.status_var.set(tr("Converting..."))
        self._jid = jobs.begin(ntr("Convert - {n} file", "Convert - {n} files", len(planned)),
                               stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._worker, args=(planned, self._jid), daemon=True).start()
        return self._jid

    def _enqueue(self):
        items = self._pending()
        if not items:
            self.status_var.set(tr("Nothing to convert - add files first."))
            return
        jobs.enqueue(ntr("Convert - {n} file", "Convert - {n} files", len(items)),
                     lambda: self._launch(items))
        self.log(f"[QUEUE] added: Convert - {len(items)} file(s)")
        self.status_var.set(tr("Added to the queue."))

    def _row(self, iid, **cols):
        def _u():
            if self.tree.exists(iid):
                tag = cols.pop("tag", None)
                for k, v in cols.items():
                    self.tree.set(iid, k, v)
                if tag is not None:
                    self.tree.item(iid, tags=(tag,) if tag else ())
        _call_tk(_u)

    def _worker(self, planned, jid):
        n = len(planned)
        ok = fail = skipped = 0
        t_all = time.time()
        self.log(f"=== Convert: {n} file(s) ===")
        try:
            for k, (iid, job, out) in enumerate(planned):
                if self.stop_event.is_set():
                    break
                name = os.path.basename(job["input"])
                if out is None:
                    skipped += 1
                    self._row(iid, status=tr("Skipped (exists)"), tag="warn")
                    self.log(f"[SKIP] {name}: output exists")
                    continue
                if not os.path.isfile(job["input"]):
                    fail += 1
                    self._row(iid, status=tr("Failed"), tag="bad")
                    self.log(f"[FAIL] {name}: the source is gone")
                    continue
                self.log(f"[{k + 1}/{n}] {name} -> {out}")
                self.log(f"   {recode.summary_line(job)}")
                self._row(iid, status=tr("Converting..."), tag="")
                t0 = time.time()

                def prog(frac, text, k=k, iid=iid, t0=t0):
                    el = time.time() - t0
                    eta = format_seconds(el / frac * (1 - frac)) if 0.01 < frac < 1 else "--:--"
                    pct = f"{frac * 100:.1f}%"

                    def _u():
                        self.bar.configure(value=int((k + frac) / n * 1000))
                        self.pct_var.set(f"{k + 1}/{n} · {text.strip()}")
                        if self.tree.exists(iid):
                            self.tree.set(iid, "status", f"{tr('Converting...')} {pct}")
                            self.tree.set(iid, "time", eta)
                    _call_tk(_u)
                _call_tk(lambda name=name, k=k: self.status_var.set(
                    tr("Converting {i}/{n}: {name}", i=k + 1, n=n, name=name)))
                res = recode.encode(job, out, progress=prog, stop_event=self.stop_event,
                                    log=self.log)
                el = format_seconds(time.time() - t0)
                if res["stopped"]:
                    self._row(iid, status=tr("Stopped"), time=el, tag="warn")
                    break
                if res["ok"]:
                    ok += 1
                    _call_tk(lambda iid=iid: self.jobs_by_iid.get(iid, {}).update(done=True))
                    si, so = res["size_in"], res["size_out"]
                    pct = f" ({so / si * 100:.0f}%)" if si else ""
                    self._row(iid, status=tr("Done") if not res["warnings"]
                              else tr("Done - check the log"),
                              after=format_size(so) + pct, time=el,
                              tag="ok" if not res["warnings"] else "warn")
                else:
                    fail += 1
                    self._row(iid, status=tr("Failed"), time=el, tag="bad")
        except Exception as e:
            fail += 1
            self.log(f"[FAIL] {e}")
        finally:
            stopped = self.stop_event.is_set()
            summary = (f"{ok} converted, {fail} failed, {skipped} skipped"
                       + (" - stopped" if stopped else ""))
            self.log(("STOPPED by user. " if stopped else "FINISHED. ") + summary
                     + f" - {format_seconds(time.time() - t_all)}")
            jobs.end(jid, ok=fail == 0 and not stopped, summary=summary)
            text = (tr("Stopped.") if stopped else
                    tr("Finished: {ok} converted, {fail} failed, {skip} skipped.",
                       ok=ok, fail=fail, skip=skipped))
            _call_tk(lambda: self._set_busy(False, text))

    def stop(self):
        self.stop_event.set()
        self.log("Stopping (the file in progress is abandoned)...")

    # ================================================================ detect / preview
    def _current_job(self):
        """The job shown (or the first in the list) with the form applied."""
        job = self._shown_job()
        if job is None:
            kids = [i for i in self.tree.get_children() if i in self.jobs_by_iid]
            job = self.jobs_by_iid[kids[0]] if kids else None
        return job

    def _detect_now(self):
        job = self._current_job()
        if job is None or self._busy:
            self.status_var.set(tr("Add a file first."))
            return
        self._set_busy(True)
        self.status_var.set(tr("Detecting black bars and interlacing..."))
        jid = jobs.begin(tr("Convert - detect"), stop_event=self.stop_event, tab=self)
        self.stop_event.clear()

        def work():
            c = d = None
            try:
                c = recode.detect_crop(job["input"], job["info"], stop_event=self.stop_event)
                d = recode.detect_interlace(job["input"], job["info"],
                                            stop_event=self.stop_event)
            finally:
                jobs.end(jid, ok=c is not None)
                _call_tk(lambda: self._detected(job, c, d))
        threading.Thread(target=work, daemon=True).start()

    def _detected(self, job, c, d):
        self._set_busy(False)
        job.setdefault("detect", {})
        if c is not None:
            job["detect"]["crop"] = c
            self.log(f"[DETECT] {os.path.basename(job['input'])}: crop {c['w']}x{c['h']} "
                     f"(L{c['l']} R{c['r']} T{c['t']} B{c['b']}, {c['votes']}/{c['samples']})")
            if job is self._shown_job():
                for k in ("l", "r", "t", "b"):
                    self._fv[f"crop_{k}"][0].set(str(c[k]))
        if d is not None:
            job["detect"]["interlace"] = d
            self.log(f"[DETECT] interlace: TFF {d['tff']} BFF {d['bff']} progressive "
                     f"{d['progressive']}")
        self._show_source_info(job)

    def preview(self):
        job = self._current_job()
        if job is None or self._busy:
            self.status_var.set(tr("Add a file first."))
            return
        txt = self.sample_var.get().strip()
        start = None
        if txt:
            start = parse_time(txt)
            if start is None:
                messagebox.showerror(tr("Error"), tr("Sample from: enter a time like 12:30."))
                return
        if self.unload_players_hook:          # the Dual Player may hold the old sample
            try:
                self.unload_players_hook()
            except Exception:
                pass
        job = copy.deepcopy(job)
        self._set_busy(True)
        self.status_var.set(tr("Encoding the preview sample..."))
        self.preview_info.set(tr("Working..."))
        self.stop_event.clear()
        jid = jobs.begin(tr("Convert - preview"), stop_event=self.stop_event, tab=self)

        def prog(frac, text):
            _call_tk(lambda: (self.bar.configure(value=int(frac * 1000)),
                              self.pct_var.set(text.strip())))

        def work():
            res = {}
            try:
                self.log(f"[PREVIEW] {os.path.basename(job['input'])}")
                res = recode.make_preview(job, start=start, progress=prog,
                                          stop_event=self.stop_event, log=self.log)
            except Exception as e:
                self.log(f"[FAIL] preview: {e}")
            finally:
                jobs.end(jid, ok=bool(res.get("ok")))
                _call_tk(lambda: self._previewed(job, res))
        threading.Thread(target=work, daemon=True).start()

    def _previewed(self, job, res):
        self._set_busy(False)
        if not res.get("ok"):
            self.preview_info.set(tr("Preview stopped.") if res.get("stopped")
                                  else tr("Preview failed - see the Log sub-tab."))
            return
        src = job["info"]["size"]
        est = res.get("est_size") or 0
        lines = [tr("Estimated size: {est} (source {src}, {pct}%)", est=format_size(est),
                    src=format_size(src), pct=round(est / src * 100) if src else 0),
                 tr("Estimated time: {t} ({x:.1f}x real time)",
                    t=format_seconds(res.get("est_time") or 0), x=res.get("speed") or 0)]
        if res.get("ssim") is not None:
            lines.append(tr("Sample SSIM: {v:.4f} (0.98+ = no visible difference)",
                            v=res["ssim"]))
        lines.append(tr("Sample: {t} from {start}", t="10 s",
                        start=format_seconds(res["start"])))
        self.preview_info.set("\n".join(lines))
        self.log("   " + " | ".join(lines))
        self._last_preview = (res["source"], res["sample"])
        self.dual_btn.configure(state="normal")
        self._open_dual()

    def _open_dual(self):
        if self._last_preview and self.open_dual:
            a, b = self._last_preview
            if os.path.isfile(a) and os.path.isfile(b):
                self.open_dual(a, b)

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
            tab = self._nb.index("current")
        except tk.TclError:
            tab = 0
        return {"cv_settings": dict(self.defaults), "cv_recursive": bool(self.recursive_var.get()),
                "cv_preset": self.preset_var.get(), "cv_tab": tab if tab < 4 else 0}
