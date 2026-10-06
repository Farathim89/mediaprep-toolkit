"""Check tab - verify a video (or a whole folder) isn't broken.

  Quick: ffprobe opens it and it has valid streams + a duration (catches
         truncated / corrupt-header / zero-byte files).
  Full:  decode the entire file and report any errors (slow, but catches
         corruption anywhere in the file)."""
import os
import glob
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .media import quick_check, full_check, probe_duration
from .widgets import add_tooltip, enable_file_drop, enable_paths_drop
from . import applog

_MEDIA_GLOBS = ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.webm", "*.m4v",
                "*.ts", "*.mpg", "*.mpeg", "*.wmv", "*.flv")
_MEDIA_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mpg *.mpeg *.wmv *.flv"),
                ("All files", "*.*")]
_MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
               ".ts", ".mpg", ".mpeg", ".wmv", ".flv")


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
    def __init__(self, master, saved=None):
        super().__init__(master, padding=8)
        saved = saved or {}
        self.stop_event = threading.Event()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        files_tab = ttk.Frame(nb, padding=6)
        log_tab = ttk.Frame(nb, padding=6)
        nb.add(files_tab, text="  Files  ")
        nb.add(log_tab, text="  Log  ")
        files_tab.columnconfigure(0, weight=1)
        files_tab.rowconfigure(3, weight=1)

        drow = ttk.Frame(files_tab)
        drow.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(drow, text="Check depth:").pack(side="left", padx=(0, 8))
        self.depth_var = tk.StringVar(value=saved.get("check_depth", "quick"))
        ttk.Radiobutton(drow, text="Quick (opens & has valid streams)",
                        variable=self.depth_var, value="quick").pack(side="left")
        ttk.Radiobutton(drow, text="Full (decode whole file - slow, thorough)",
                        variable=self.depth_var, value="full").pack(side="left", padx=(12, 0))

        srow = ttk.Frame(files_tab)
        srow.grid(row=1, column=0, sticky="we", pady=2)
        srow.columnconfigure(1, weight=1)
        ttk.Label(srow, text="Single file:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.file_var = tk.StringVar()
        sent = ttk.Entry(srow, textvariable=self.file_var)
        sent.grid(row=0, column=1, sticky="we")
        ttk.Button(srow, text="Browse...", command=self._browse_file).grid(row=0, column=2, padx=4)
        self.file_btn = ttk.Button(srow, text="Check file", command=self.check_file)
        self.file_btn.grid(row=0, column=3)

        frow = ttk.Frame(files_tab)
        frow.grid(row=2, column=0, sticky="we", pady=2)
        frow.columnconfigure(1, weight=1)
        ttk.Label(frow, text="Folder:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.dir_var = tk.StringVar()
        dent = ttk.Entry(frow, textvariable=self.dir_var)
        dent.grid(row=0, column=1, sticky="we")
        ttk.Button(frow, text="Browse...", command=self._browse_dir).grid(row=0, column=2, padx=4)
        ttk.Button(frow, text="Refresh list", command=self.refresh_list).grid(row=0, column=3)

        tvf = ttk.Frame(files_tab)
        tvf.grid(row=3, column=0, sticky="nsew")
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tvf, columns=("sel", "file", "result", "detail"),
                                 show="headings", height=12, selectmode="extended")
        self.tree.heading("sel", text="✓", command=self._toggle_all)
        self.tree.heading("file", text="File")
        self.tree.heading("result", text="Result")
        self.tree.heading("detail", text="Detail")
        self.tree.column("sel", width=30, anchor="center", stretch=False)
        self.tree.column("file", width=260, anchor="w")
        self.tree.column("result", width=80, anchor="center", stretch=False)
        self.tree.column("detail", width=280, anchor="w")
        self.tree.tag_configure("good", foreground="#2e9e4f")
        self.tree.tag_configure("bad", foreground="#d43a3a")
        self.tree.tag_configure("unknown", foreground="#e0902e")
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
        self.folder_btn = ttk.Button(rrow, text="Check ticked files", command=self.check_folder)
        self.folder_btn.pack(side="left", fill="x", expand=True)
        add_tooltip(self.folder_btn, "Run the chosen check on every ticked file in the list")
        self.stop_btn = ttk.Button(rrow, text="Stop", command=self.stop, state="disabled", width=8)
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Label(rrow, text="  select rows · Del = remove · Ctrl+A = all",
                  style="Hint.TLabel").pack(side="left")

        self.status_var = tk.StringVar(value="")
        ttk.Label(files_tab, textvariable=self.status_var, style="Hint.TLabel").grid(row=5, column=0, sticky="w")
        prog = ttk.Frame(files_tab)
        prog.grid(row=6, column=0, sticky="we", pady=(2, 2))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="we")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.pct_var, width=12).grid(row=0, column=1, sticky="w", padx=(6, 0))
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.logbox = tk.Text(log_tab, state="disabled", font=("Consolas", 9), wrap="none")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(log_tab, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=1, sticky="ns")
        ttk.Button(log_tab, text="Clear log", command=self._clear_log).grid(row=1, column=0, sticky="w", pady=(4, 0))

        enable_file_drop(sent, self._drop_file)
        enable_file_drop(dent, self._drop_dir)
        enable_paths_drop(self.tree, self._drop_paths)

    # ---- helpers ----
    def _browse_file(self):
        p = filedialog.askopenfilename(title="Select video", filetypes=_MEDIA_TYPES)
        if p:
            self.file_var.set(p)

    def _browse_dir(self):
        d = filedialog.askdirectory(title="Select folder")
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
                for root, _d, files in os.walk(p):
                    for fn in sorted(files):
                        if fn.lower().endswith(_MEDIA_EXTS):
                            fp = os.path.join(root, fn)
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
        files = []
        for pat in _MEDIA_GLOBS:
            files += glob.glob(os.path.join(d, pat))
        return sorted(set(files))

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
            self.status_var.set("Idle")

    def stop(self):
        self.stop_event.set()
        self.log("Stopping...")

    def snapshot(self):
        return {"check_depth": self.depth_var.get()}

    # ---- single file ----
    def check_file(self):
        f = self.file_var.get().strip().strip('"')
        if not f or not os.path.isfile(f):
            messagebox.showerror("Error", "Pick a valid file first.")
            return
        self.stop_event.clear()
        self._running(True)
        threading.Thread(target=self._file_worker, args=(f,), daemon=True).start()

    def _file_worker(self, f):
        msg = "Failed - see log."
        try:
            depth = self.depth_var.get()
            self.status(f"Checking ({depth})...")
            self.log(f"[CHECK] {os.path.basename(f)}  ({depth})")
            ok, detail = _run_check(f, depth, self.stop_event)
            if ok is None:
                self.log("  stopped.")
                msg = "Stopped."
            elif ok == "timeout":
                self.log(f"  [UNKNOWN] {detail}")
                msg = "UNKNOWN (timed out) - see log."
            elif ok:
                self.log(f"  [OK] {detail}")
                msg = "OK - not broken."
            else:
                self.log(f"  [BROKEN] {detail}")
                msg = "BROKEN - see log."
        except Exception as e:
            self.log(f"  [FAIL] {e}")
        finally:
            self.after(0, lambda: (self._running(False), self.status_var.set(msg)))

    # ---- folder ----
    def check_folder(self):
        rows = self.tree.get_children()
        if not rows:
            d = self.dir_var.get().strip()
            if d and os.path.isdir(d):
                self.refresh_list()
                rows = self.tree.get_children()
        if not rows:
            messagebox.showerror("Error", "Drop files/folders onto the list (or pick a folder), then tick some.")
            return
        files = [r for r in rows if self.tree.set(r, "sel") == "☑"]
        if not files:
            messagebox.showerror("Error", "No files ticked - tick at least one (or click the checkmark header).")
            return
        self.stop_event.clear()
        self._running(True)
        threading.Thread(target=self._folder_worker, args=(files,), daemon=True).start()

    def _folder_worker(self, files):
        okc = badc = unkc = 0
        try:
            depth = self.depth_var.get()
            n = len(files)
            self.log(f"[CHECK] {n} file(s) - {depth} check")
            for i, f in enumerate(files, 1):
                if self.stop_event.is_set():
                    self.log("[CHECK] stopped.")
                    break
                name = os.path.basename(f)
                self.status(f"[{i}/{n}] {name}")
                self.progress((i - 1) / n, f"{i - 1}/{n}")
                ok, detail = _run_check(f, depth, self.stop_event)
                if ok is None:
                    self.log("[CHECK] stopped.")
                    break
                if ok == "timeout":
                    res, tag = "UNKNOWN (timed out)", "unknown"
                    unkc += 1
                    self.log(f"  [UNKNOWN] {name}: {detail}")
                elif ok:
                    res, tag = "OK", "good"
                    okc += 1
                else:
                    res, tag = "BROKEN", "bad"
                    badc += 1
                    self.log(f"  [BROKEN] {name}: {detail}")
                self.after(0, lambda p=f, r=res, d=detail, t=tag: self._set_result(p, r, d, t))
                self.progress(i / n, f"{i}/{n}")
        except Exception as e:
            self.log(f"[CHECK] [FAIL] {e}")
        finally:
            extra = f", {unkc} unknown" if unkc else ""
            self.log(f"[CHECK] done: {okc} OK, {badc} broken{extra}.")
            self.after(0, lambda: (self._running(False),
                                   self.status_var.set(f"Done - {okc} OK, {badc} broken{extra}.")))

    def _set_result(self, path, res, detail, tag):
        if not self.tree.exists(path):   # row removed from the list mid-run
            return
        self.tree.set(path, "result", res)
        self.tree.set(path, "detail", detail)
        self.tree.item(path, tags=(tag,))
