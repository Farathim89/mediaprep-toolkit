"""Templates -> Templates sub-tab (TemplatesManagerMixin)."""
import os
import re
import time
import shutil
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..engine.files import move_to_trash
from ..engine.probe import probe_audio_streams, probe_duration
from ..ui.player import VideoPlayer
from ..ui.widgets import (KeyedCombobox, add_tooltip, auto_wrap, bind_status_colors,
                          enable_paths_drop, help_button)
from .common import (_KIND_NAMES, _MEDIA_EXTS, _VIDEO_TYPES, _existing_template,
                     _list_media, _same_dir, _same_file)
from .. import applog
from ..i18n import tr, ntr, N_

# tr() for a variable key whose literals are marked with N_() / tr() elsewhere
# (a plain tr(var) works the same, but the extractor flags it)
tr_key = tr


def _fmt_bytes(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{max(n, 0) / 1024:.0f} KB"


def _reveal_in_explorer(path):
    """Open Explorer with `path` selected (its folder if it's gone)."""
    path = os.path.abspath(path)
    try:
        if os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(os.path.dirname(path))
    except Exception as exc:
        applog.record(f"[explorer] could not open {path}: {exc}")


class TemplatesManagerMixin:
    """Templates -> Templates sub-tab: list, play, rename, import and trash
    the template clips."""

    # ---------------- Templates manager ----------------
    def _build_templates_tab(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 4))
        for txt, cmd, tip in [
                (N_("Refresh"), self._tm_refresh, N_("Re-read the four template folders")),
                (N_("Play"), self._tm_play, N_("Load the selected template into the player and "
                                              "play it (double-click a row does the same)")),
                (N_("Rename..."), self._tm_rename,
                 N_("Rename the selected template (the extension is kept)")),
                (N_("Reveal in Explorer"), self._tm_reveal,
                 N_("Open its folder with the file selected")),
                (N_("Move to trash"), self._tm_trash,
                 N_("Move the selected template(s) to Data\\temp\\trash "
                    "(recoverable - nothing is deleted)"))]:
            b = ttk.Button(bar, text=tr_key(txt), command=cmd)
            b.pack(side="left", padx=(0, 6))
            add_tooltip(b, tr_key(tip))
        ttk.Label(bar, text=tr("Import into:")).pack(side="left", padx=(12, 4))
        # the variable keeps the English kind name (_tm_import maps it back)
        self.tm_kind = tk.StringVar(value=_KIND_NAMES["intro"])
        kcb = KeyedCombobox(bar, textvariable=self.tm_kind, state="readonly", width=15,
                            values=[_KIND_NAMES[k] for k, *_r in self.SECTION_SPECS])
        kcb.pack(side="left")
        add_tooltip(kcb, tr("Which template folder a dropped / imported video is COPIED into"))
        ib = ttk.Button(bar, text=tr("Import..."), command=self._tm_import_browse)
        ib.pack(side="left", padx=(6, 0))
        add_tooltip(ib, tr("Copy existing clip(s) into the chosen template folder "
                           "(or drop videos onto the list)"))
        help_button(bar, "templates_manager").pack(side="right", padx=(8, 0))

        tvf = ttk.Frame(page)
        tvf.grid(row=1, column=0, sticky="nsew")
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        cols = ("kind", "file", "len", "lang", "size", "mod")
        self.tm_tree = ttk.Treeview(tvf, columns=cols, show="headings", height=14,
                                    selectmode="extended")
        for c, txt, w, anc in [("kind", N_("Kind"), 95, "w"), ("file", N_("File"), 280, "w"),
                               ("len", N_("Length"), 70, "center"),
                               ("lang", N_("Audio"), 80, "center"),
                               ("size", N_("Size"), 75, "e"),
                               ("mod", N_("Modified"), 120, "center")]:
            self.tm_tree.heading(c, text=tr_key(txt))
            self.tm_tree.column(c, width=w, anchor=anc, stretch=(c == "file"))
        self.tm_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.tm_tree.yview)
        self.tm_tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        bind_status_colors(self.tm_tree, {"bad": "bad"})
        self.tm_tree.bind("<Double-1>", lambda e: self._tm_play())
        self.tm_tree.bind("<Delete>", lambda e: self._tm_trash())
        self.tm_tree.bind("<F2>", lambda e: self._tm_rename())
        enable_paths_drop(self.tm_tree, self._tm_import)

        pf = ttk.Frame(page)
        pf.grid(row=1, column=1, sticky="n", padx=(10, 0))
        self.tm_player = VideoPlayer(pf, width=400, height=225, log_fn=self.log)
        self.tm_player.pack()
        self.tm_status = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.tm_status, style="Hint.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        hint = ttk.Label(page, text=tr("Every template Cut / Edit matches against "
                                       "(Media\\templates\\preintro, intro, credits, "
                                       "aftercredits). Length = what gets cut from each episode. "
                                       "Drop a video on the list to copy it into the 'Import "
                                       "into' folder. Del = move to trash, F2 = rename."),
                         style="Hint.TLabel", wraplength=640, justify="left")
        hint.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        auto_wrap(hint)
        self._tm_paths = {}       # tree iid -> path
        self._tm_gen = 0          # bumps on every refresh; stale probes are dropped
        self.after(300, self._tm_refresh)

    def _on_subtab_changed(self, _e=None):
        try:
            if self._nb.select() == str(self._tm_page):
                self._tm_refresh()
        except tk.TclError:
            pass

    def _tm_refresh(self):
        if not hasattr(self, "tm_tree"):
            return
        sel = {self._tm_paths.get(i) for i in self.tm_tree.selection()}
        self.tm_tree.delete(*self.tm_tree.get_children())
        self._tm_paths = {}
        self._tm_gen += 1
        gen = self._tm_gen
        rows = []
        for key, *_r in self.SECTION_SPECS:
            for p in _list_media(self.DIRMAP[key]):
                try:
                    st = os.stat(p)
                    size, mod = st.st_size, time.strftime("%Y-%m-%d %H:%M",
                                                          time.localtime(st.st_mtime))
                except OSError:
                    size, mod = 0, "?"
                iid = f"t{len(rows)}"
                self.tm_tree.insert("", "end", iid=iid, values=(
                    tr_key(_KIND_NAMES[key]), os.path.basename(p), "...", "...", _fmt_bytes(size),
                    mod))
                self._tm_paths[iid] = p
                rows.append((iid, p))
                if p in sel:
                    self.tm_tree.selection_add(iid)
        counts = {}
        for key, *_r in self.SECTION_SPECS:
            counts[key] = sum(1 for p in self._tm_paths.values()
                              if _same_dir(os.path.dirname(p), self.DIRMAP[key]))
        self.tm_status.set(ntr("{n} template", "{n} templates", len(rows)) + ": "
                           + ", ".join(f"{tr_key(_KIND_NAMES[k])} {counts[k]}"
                                       for k, *_r in self.SECTION_SPECS))
        if rows:
            threading.Thread(target=self._tm_probe_worker, args=(gen, rows),
                             daemon=True).start()

    def _tm_probe_worker(self, gen, rows):
        """Fill Length / Audio for each row (ffprobe) off the UI thread."""
        for iid, p in rows:
            if gen != self._tm_gen:
                return
            try:
                dur = probe_duration(p)
                langs = []
                for s in probe_audio_streams(p):
                    if s.get("lang") not in langs:
                        langs.append(s.get("lang"))
            except Exception:
                dur, langs = None, []
            ln = f"{dur:.2f}s" if dur else tr("unreadable")
            lg = ", ".join(langs) if langs else tr("none")

            def _set(iid=iid, ln=ln, lg=lg, bad=not dur):
                if gen != self._tm_gen or not self.tm_tree.exists(iid):
                    return
                self.tm_tree.set(iid, "len", ln)
                self.tm_tree.set(iid, "lang", lg)
                if bad:
                    self.tm_tree.item(iid, tags=("bad",))
            self.after(0, _set)

    def _tm_selected(self, many=False):
        paths = [self._tm_paths[i] for i in self.tm_tree.selection() if i in self._tm_paths]
        if not paths:
            messagebox.showinfo(tr("Pick a template"), tr("Select a template in the list first."))
            return [] if many else None
        return paths if many else paths[0]

    def _tm_release(self, paths):
        """Close any player holding one of `paths` open (Windows locks open files)."""
        players = [self.tm_player, self.player]
        if self._audition is not None and self._audition.winfo_exists():
            players.append(self._audition.player)
        for pl in players:
            cur = getattr(pl, "_path", None)
            if cur and any(_same_file(cur, p) for p in paths):
                pl.unload()

    def _tm_play(self):
        p = self._tm_selected()
        if p and self.tm_player.load(p):
            self.tm_player.play()
            self.tm_status.set(tr("Playing {name}", name=os.path.basename(p)))

    def _tm_reveal(self):
        p = self._tm_selected()
        if p:
            _reveal_in_explorer(p)

    def _tm_trash(self):
        paths = self._tm_selected(many=True)
        if not paths:
            return
        names = [os.path.basename(p) for p in paths]
        if len(names) > 12:
            names = names[:12] + [tr("... and {n} more", n=len(names) - 12)]
        if not messagebox.askyesno(tr("Move to trash"),
                                   tr("Move these templates to Data\\temp\\trash?") + "\n\n"
                                   + "\n".join(names) + "\n\n"
                                   + tr("Cut / Edit stops using them. They can be recovered "
                                        "from Data\\temp\\trash.")):
            return
        self._tm_release(paths)
        moved, failed = move_to_trash(paths, "templates", log=self.log)
        for p in moved:
            self.log(f"[TEMPLATES] moved {os.path.basename(p)} to Data\\temp\\trash")
        if failed:
            messagebox.showwarning(tr("Not moved"), tr("{n} file(s) could not be moved "
                                                       "(in use?) - see the Log tab.",
                                                       n=len(failed)))
        self._tm_refresh()

    def _tm_rename(self):
        p = self._tm_selected()
        if not p:
            return
        stem, ext = os.path.splitext(os.path.basename(p))
        new = simpledialog.askstring(tr("Rename template"),
                                     tr("New name for {name}:\n(the {ext} extension is kept)",
                                        name=stem + ext, ext=ext),
                                     initialvalue=stem, parent=self)
        if new is None:
            return
        new = new.strip()
        if new.lower().endswith(ext.lower()):
            new = new[:-len(ext)].rstrip()
        if not new or re.search(r'[<>:"/\\|?*]', new) or new.rstrip(". ") != new:
            messagebox.showerror(tr("Rename"), tr("That isn't a valid file name."))
            return
        if new == stem:
            return
        dest = os.path.join(os.path.dirname(p), new + ext)
        if os.path.exists(dest) and not _same_file(dest, p):
            messagebox.showerror(tr("Rename"), tr("{name} already exists in that folder.",
                                                  name=new + ext))
            return
        self._tm_release([p])
        try:
            os.rename(p, dest)
            self.log(f"[TEMPLATES] renamed {stem}{ext} -> {new}{ext}")
        except OSError as exc:
            messagebox.showerror(tr("Rename"), tr("Could not rename:\n{error}", error=exc))
        self._tm_refresh()

    def _tm_import_browse(self):
        paths = filedialog.askopenfilenames(title=tr("Copy clip(s) into the template folder"),
                                            filetypes=[(tr_key(d), p) for d, p in _VIDEO_TYPES])
        if paths:
            self._tm_import(list(paths))

    def _tm_import(self, paths):
        """Copy videos into the 'Import into' kind's folder (thread; the
        originals are left where they are)."""
        kind = next((k for k, v in _KIND_NAMES.items() if v == self.tm_kind.get()), "intro")
        folder = self.DIRMAP[kind]
        vids = [p for p in (paths or []) if os.path.isfile(p) and p.lower().endswith(_MEDIA_EXTS)]
        if not vids:
            self.tm_status.set(tr("Drop video files (not folders) to import them."))
            return
        self.tm_status.set(tr("Copying {n} file(s) into {folder}...", n=len(vids), folder=folder))

        def work():
            try:
                os.makedirs(folder, exist_ok=True)
                for src in vids:
                    if _same_dir(os.path.dirname(src), folder):
                        self.log(f"[TEMPLATES] {os.path.basename(src)} is already in {folder}")
                        continue
                    base, ext = os.path.splitext(os.path.basename(src))
                    dest, n = os.path.join(folder, base + ext), 2
                    while _existing_template(folder, os.path.splitext(os.path.basename(dest))[0]):
                        dest = os.path.join(folder, f"{base}_v{n}{ext}")
                        n += 1
                    try:
                        shutil.copy2(src, dest)
                        self.log(f"[TEMPLATES] copied {os.path.basename(src)} -> {dest}")
                    except OSError as exc:
                        self.log(f"[TEMPLATES] [FAIL] could not copy {os.path.basename(src)}: {exc}")
            except Exception as exc:
                self.log(f"[FAIL] import crashed: {exc}")
            finally:
                self.after(0, self._tm_refresh)
        threading.Thread(target=work, daemon=True).start()
