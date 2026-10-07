"""Check tab - verify a video (or a whole folder) isn't broken.

  Quick: ffprobe opens it and it has valid streams + a duration (catches
         truncated / corrupt-header / zero-byte files).
  Full:  decode the entire file and report any errors (slow, but catches
         corruption anywhere in the file)."""
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ..engine.probe import quick_check, full_check, probe_duration
from ..ui.widgets import (info_icon, add_tooltip, enable_file_drop, enable_paths_drop,
                          bind_status_colors, trim_text_lines, help_button)
from .. import applog
from .. import jobs
from ..i18n import tr, ntr
from ..ui import icons

_MEDIA_TYPES = [(tr("Video files"),
                 "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mpg *.mpeg *.wmv *.flv"),
                (tr("All files"), "*.*")]
_MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
               ".ts", ".mpg", ".mpeg", ".wmv", ".flv")


def _walk_media(folder):
    """Every media file under `folder`, recursively, sorted. os.walk instead of
    glob so folder names with [brackets] (e.g. 'Show [1080p]') still work."""
    out = []
    for root, _d, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith(_MEDIA_EXTS):
                out.append(os.path.join(root, fn))
    return sorted(out)


def _detail_text(detail):
    """The Detail cell: the fixed engine.probe check messages translated (they
    stay English in the log); anything else (ffmpeg errors, stream counts)
    is shown as-is."""
    fixed = {
        "missing or zero-byte file": tr("missing or zero-byte file"),
        "ffprobe could not read it (corrupt header / not media)":
            tr("ffprobe could not read it (corrupt header / not media)"),
        "no readable streams": tr("no readable streams"),
        "no valid duration": tr("no valid duration"),
        "decoded clean, no errors": tr("decoded clean, no errors"),
    }
    return fixed.get(str(detail), detail)


def _run_check(path, depth, stop_event):
    """Run the chosen check. Returns (ok, detail); ok is None if stopped and
    "timeout" if a full decode ran out of time (that is NOT proof of damage).
    The full-check timeout scales with the file's length (3x its duration +
    10 min, never below the old 30 min) so long films aren't cut short."""
    if depth != "full":
        return quick_check(path)
    dur = probe_duration(path) or 0
    ok, detail = full_check(path, stop_event, timeout=max(1800, dur * 3 + 600))
    if ok is False and str(detail).startswith("timed out"):
        return "timeout", detail
    return ok, detail


class CheckTab(ttk.Frame):
    def __init__(self, master, saved=None, bottom=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self._jid = None              # jobs registry id of the running check
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        # '?' on the right end of the sub-tab strip
        help_button(self, "check").place(in_=nb, relx=1.0, x=0, y=0, anchor="ne")
        files_tab = ttk.Frame(nb, padding=6)
        log_tab = ttk.Frame(nb, padding=6)
        nb.add(files_tab, text="  " + tr("Files") + "  ")
        nb.add(log_tab, text="  " + tr("Log") + "  ")
        files_tab.columnconfigure(0, weight=1)
        files_tab.rowconfigure(3, weight=1)

        drow = ttk.Frame(files_tab)
        drow.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(drow, text=tr("Check depth:")).pack(side="left", padx=(0, 8))
        self.depth_var = tk.StringVar(value=saved.get("check_depth", "quick"))
        ttk.Radiobutton(drow, text=tr("Quick (opens & has valid streams)"),
                        variable=self.depth_var, value="quick").pack(side="left")
        ttk.Radiobutton(drow, text=tr("Full (decode whole file - slow, thorough)"),
                        variable=self.depth_var, value="full").pack(side="left", padx=(12, 0))

        srow = ttk.Frame(files_tab)
        srow.grid(row=1, column=0, sticky="we", pady=2)
        srow.columnconfigure(1, weight=1)
        ttk.Label(srow, text=tr("Single file:")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.file_var = tk.StringVar()
        sent = ttk.Entry(srow, textvariable=self.file_var)
        sent.grid(row=0, column=1, sticky="we")
        icons.decorate(ttk.Button(srow, text=tr("Browse..."), command=self._browse_file), "folder").grid(row=0, column=2, padx=4)
        self.file_btn = icons.decorate(ttk.Button(srow, text=tr("Check file"), command=self.check_file), "check")
        self.file_btn.grid(row=0, column=3)

        frow = ttk.Frame(files_tab)
        frow.grid(row=2, column=0, sticky="we", pady=2)
        frow.columnconfigure(1, weight=1)
        ttk.Label(frow, text=tr("Folder:")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.dir_var = tk.StringVar()
        dent = ttk.Entry(frow, textvariable=self.dir_var)
        dent.grid(row=0, column=1, sticky="we")
        icons.decorate(ttk.Button(frow, text=tr("Browse..."), command=self._browse_dir), "folder").grid(row=0, column=2, padx=4)
        icons.decorate(ttk.Button(frow, text=tr("Refresh list"), command=self.refresh_list), "refresh").grid(row=0, column=3)

        tvf = ttk.Frame(files_tab)
        tvf.grid(row=3, column=0, sticky="nsew")
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tvf, columns=("sel", "file", "result", "detail"),
                                 show="headings", height=8, selectmode="extended")
        self.tree.heading("sel", text="✓", command=self._toggle_all)
        self.tree.heading("file", text=tr("File"))
        self.tree.heading("result", text=tr("Result"))
        self.tree.heading("detail", text=tr("Detail"))
        self.tree.column("sel", width=30, anchor="center", stretch=False)
        self.tree.column("file", width=260, anchor="w")
        self.tree.column("result", width=110, anchor="center", stretch=False)
        self.tree.column("detail", width=280, anchor="w")
        bind_status_colors(self.tree, {"good": "good", "bad": "bad", "unknown": "warn"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Delete>", self._remove_selected)
        self.tree.bind("<BackSpace>", self._remove_selected)
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)

        rrow = ttk.Frame(files_tab)
        rrow.grid(row=4, column=0, sticky="we", pady=(6, 2))
        self.folder_btn = icons.decorate(ttk.Button(rrow, style="Accent.TButton", text=tr("Check ticked files"), command=self.check_folder), "check")
        self.folder_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.folder_btn, tr("Run the chosen check on every ticked file in the list"))
        self.queue_btn = icons.decorate(ttk.Button(rrow, text=tr("Add to queue"), command=self.queue_folder), "queue")
        self.queue_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.queue_btn, tr("Queue a check of the ticked files (with the chosen "
                                       "depth) to run after the jobs already running / queued "
                                       "- see Queue... in the status bar"))
        self.stop_btn = icons.decorate(ttk.Button(rrow if bottom is None else bottom,
                                                  text=tr("Stop"), command=self.stop,
                                                  state="disabled"), "stop")
        if bottom is None:
            self.stop_btn.pack(side="left", padx=(6, 0))
        info_icon(rrow, tr("select rows · Del = remove · Ctrl+A = all")).pack(
            side="left", padx=(4, 0))

        # status + progress + Stop: in the window's fixed footer when `bottom`
        # is given (always visible), else under the content
        self.status_var = tk.StringVar(value="")
        if bottom is not None:
            ttk.Label(bottom, textvariable=self.status_var, style="Hint.TLabel").pack(
                anchor="w", padx=10, pady=(4, 0))
            prog = ttk.Frame(bottom)
            prog.pack(fill="x", padx=10, pady=(2, 6))
        else:
            ttk.Label(files_tab, textvariable=self.status_var, style="Hint.TLabel").grid(
                row=5, column=0, sticky="w")
            prog = ttk.Frame(files_tab)
            prog.grid(row=6, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=12).grid(row=0, column=1, sticky="w", padx=(6, 0))
        if bottom is not None:
            self.stop_btn.lift(prog)
            self.stop_btn.grid(in_=prog, row=0, column=2, padx=(6, 0))
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.logbox = tk.Text(log_tab, state="disabled", font="MPMono", wrap="none", padx=8, pady=6)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(log_tab, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=1, sticky="ns")
        icons.decorate(ttk.Button(log_tab, text=tr("Clear log"), command=self._clear_log), "trash").grid(row=1, column=0, sticky="w", pady=(4, 0))

        enable_file_drop(sent, self._drop_file)
        enable_file_drop(dent, self._drop_dir)
        enable_paths_drop(self.tree, self._drop_paths)

    # ---- helpers ----
    def _browse_file(self):
        p = filedialog.askopenfilename(title=tr("Select video"), filetypes=_MEDIA_TYPES)
        if p:
            self.file_var.set(p)

    def _browse_dir(self):
        d = filedialog.askdirectory(title=tr("Select folder"))
        if d:
            self.dir_var.set(d)
            self.refresh_list()

    def _drop_file(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.file_var.set(path)

    def _drop_dir(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            path = os.path.dirname(path)   # dropped a file -> use its folder
        if path and os.path.isdir(path):
            self.dir_var.set(path)
            self.refresh_list()

    def _drop_paths(self, paths):
        """Add dropped files to the list. Dropping a folder starts a fresh list
        (the folder's media replaces whatever was there); dropping loose files
        adds them to the current list."""
        clean = [(p or "").strip().strip('"') for p in paths]
        if any(c and os.path.isdir(c) for c in clean):
            self.tree.delete(*self.tree.get_children())
        existing = set(self.tree.get_children())
        added = 0
        for p in clean:
            if not p:
                continue
            if os.path.isdir(p):
                for fp in _walk_media(p):
                    if fp not in existing:
                        self.tree.insert("", "end", iid=fp,
                                         values=("☑", os.path.basename(fp), "-", ""))
                        existing.add(fp)
                        added += 1
            elif os.path.isfile(p):
                if p not in existing:
                    self.tree.insert("", "end", iid=p,
                                     values=("☑", os.path.basename(p), "-", ""))
                    existing.add(p)
                    added += 1
        if added:
            self.log(f"[CHECK] added {added} file(s) to the list - tick and Check.")
        else:
            self.log("[CHECK] nothing added (no media files in the drop).")

    def _list_files(self):
        d = self.dir_var.get().strip()
        if not d or not os.path.isdir(d):
            return []
        return _walk_media(d)   # recursive, same as dropping the folder

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for f in self._list_files():
            self.tree.insert("", "end", iid=f,
                             values=("☑", os.path.basename(f), "-", ""))

    def _on_tree_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.tree.identify_column(e.x) != "#1":
            return
        row = self.tree.identify_row(e.y)
        if row:
            cur = self.tree.set(row, "sel")
            self.tree.set(row, "sel", "☐" if cur == "☑" else "☑")

    def _toggle_all(self):
        rows = self.tree.get_children()
        val = "☑" if any(self.tree.set(r, "sel") == "☐" for r in rows) else "☐"
        for r in rows:
            self.tree.set(r, "sel", val)

    def _remove_selected(self, event=None):
        """Delete key: drop the highlighted rows from the list (does not touch
        the files on disk - just removes them from the check list)."""
        sel = self.tree.selection()
        if sel:
            self.tree.delete(*sel)
            self.log(f"[CHECK] removed {len(sel)} file(s) from the list.")
        return "break"

    def _select_all_rows(self, event=None):
        """Ctrl+A: highlight every row so Del can clear them in one go."""
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def log(self, msg):
        applog.record(msg)

        def _a():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg + "\n")
            trim_text_lines(self.logbox)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _a)

    def _clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def progress(self, frac, text=""):
        def _a():
            self.bar["value"] = int(frac * 1000)
            self.pct_var.set(text)
        self.after(0, _a)

    def _running(self, on):
        for b in (self.folder_btn, self.file_btn):
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set(tr("Idle"))

    def stop(self):
        self.stop_event.set()
        self.log("Stopping...")

    def snapshot(self):
        return {"check_depth": self.depth_var.get()}

    # ---- single file ----
    def check_file(self):
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror(tr("Error"), tr("Pick a valid file first."))
            return
        self.stop_event.clear()
        self._running(True)
        self._jid = jobs.begin(tr("Check file"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._file_worker, args=(f,), daemon=True).start()

    def _file_worker(self, f):
        msg = tr("Failed - see log.")
        failed = True
        jid = self._jid
        try:
            depth = self.depth_var.get()
            self.status(tr("Checking (full)...") if depth == "full"
                        else tr("Checking (quick)..."))
            self.log(f"[CHECK] {os.path.basename(f)}  ({depth})")
            ok, detail = _run_check(f, depth, self.stop_event)
            if ok is None:
                self.log("  stopped.")
                msg = tr("Stopped.")
            elif ok == "timeout":
                self.log(f"  [UNKNOWN] {detail}")
                msg = tr("UNKNOWN (timed out) - see log.")
            elif ok:
                self.log(f"  [OK] {detail}")
                msg = tr("OK - not broken.")
            else:
                self.log(f"  [BROKEN] {detail}")
                msg = tr("BROKEN - see log.")
            failed = False
        except Exception as e:
            self.log(f"  [FAIL] {e}")
        finally:
            jobs.end(jid, ok=not failed,
                     summary=f"{os.path.basename(f)}: {msg}")
            self.after(0, lambda: (self._running(False), self.status_var.set(msg)))

    # ---- folder ----
    def check_folder(self):
        files = self._ticked_files()
        if files:
            self._start_folder(files, self.depth_var.get())

    def queue_folder(self):
        """Add to queue: the ticked files and depth as they are now."""
        files = self._ticked_files()
        if not files:
            return
        depth = self.depth_var.get()
        name = (ntr("Check {n} file (full)", "Check {n} files (full)", len(files))
                if depth == "full" else
                ntr("Check {n} file (quick)", "Check {n} files (quick)", len(files)))
        jobs.enqueue(name, lambda: self._start_folder(files, depth))
        self.log(f"[QUEUE] added: Check {len(files)} file(s) ({depth})")

    def _start_folder(self, files, depth):
        """Start a folder check (Tk thread). Returns the job id, or None if a
        check is already running (a queued entry is then dropped)."""
        if self.stop_btn.instate(["!disabled"]):
            self.log("[QUEUE] Check is busy - not started.")
            return None
        self.stop_event.clear()
        self._running(True)
        self._jid = jobs.begin(tr("Check folder"), stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._folder_worker, args=(files, depth), daemon=True).start()
        return self._jid

    def _ticked_files(self):
        """The ticked rows (loads the folder if the list is empty); shows an
        error and returns [] when there is nothing to check."""
        rows = self.tree.get_children()
        if not rows:
            d = self.dir_var.get().strip()
            if d and os.path.isdir(d):
                self.refresh_list()
                rows = self.tree.get_children()
        if not rows:
            messagebox.showerror(tr("Error"), tr("Drop files/folders onto the list (or pick a "
                                                 "folder), then tick some."))
            return []
        files = [r for r in rows if self.tree.set(r, "sel") == "☑"]
        if not files:
            messagebox.showerror(tr("Error"), tr("No files ticked - tick at least one (or click "
                                                 "the checkmark header)."))
            return []
        return files

    def _folder_worker(self, files, depth="quick"):
        okc = badc = unkc = 0
        stopped = False
        failed = False
        n = len(files)
        jid = self._jid
        try:
            self.log(f"[CHECK] {n} file(s) - {depth} check")
            for i, f in enumerate(files, 1):
                if self.stop_event.is_set():
                    stopped = True
                    self.log("[CHECK] stopped.")
                    break
                name = os.path.basename(f)
                self.status(f"[{i}/{n}] {name}")
                self.progress((i - 1) / n, f"{i - 1}/{n}")
                ok, detail = _run_check(f, depth, self.stop_event)
                if ok is None:
                    stopped = True
                    self.log("[CHECK] stopped.")
                    break
                if ok == "timeout":
                    res, tag = tr("UNKNOWN (timed out)"), "unknown"
                    unkc += 1
                    self.log(f"  [UNKNOWN] {name}: {detail}")
                elif ok:
                    res, tag = tr("OK"), "good"
                    okc += 1
                else:
                    res, tag = tr("BROKEN"), "bad"
                    badc += 1
                    self.log(f"  [BROKEN] {name}: {detail}")
                self.after(0, lambda p=f, r=res, d=detail, t=tag: self._set_result(p, r, d, t))
                self.progress(i / n, f"{i}/{n}")
        except Exception as e:
            failed = True
            self.log(f"[CHECK] [FAIL] {e}")
        finally:
            extra = f", {unkc} unknown" if unkc else ""
            ui_extra = tr(", {n} unknown", n=unkc) if unkc else ""
            if stopped:
                left = n - okc - badc - unkc
                summary = f"Stopped - {okc} OK, {badc} broken{extra} ({left} not checked)."
                ui_summary = tr("Stopped - {ok} OK, {bad} broken{extra} ({left} not checked).",
                                ok=okc, bad=badc, extra=ui_extra, left=left)
            else:
                summary = f"Done - {okc} OK, {badc} broken{extra}."
                ui_summary = tr("Done - {ok} OK, {bad} broken{extra}.",
                                ok=okc, bad=badc, extra=ui_extra)
            jobs.end(jid, ok=not failed, summary=ui_summary)
            self.log(f"[CHECK] {summary}")
            self.after(0, lambda: (self._running(False), self.status_var.set(ui_summary)))

    def _set_result(self, path, res, detail, tag):
        if not self.tree.exists(path):   # row removed from the list mid-run
            return
        self.tree.set(path, "result", res)
        self.tree.set(path, "detail", _detail_text(detail))
        self.tree.item(path, tags=(tag,))
