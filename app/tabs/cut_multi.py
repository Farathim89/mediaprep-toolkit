"""Cut / Edit -> Multi cut sub-tab (MultiCutMixin)."""
import os
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import BIT_DEPTHS, CODECS
from ..engine.cut import keep_from_drops, run_manual
from ..engine.formatting import fmt_time, format_seconds
from ..engine.probe import probe_duration
from ..i18n import ntr, tr
from ..ui.player import VideoPlayer
from ..ui.widgets import (TimeEntry, add_tooltip, auto_wrap, bind_status_colors,
                          enable_file_drop_deep, enable_paths_drop, help_button)
from .common import _VIDEO_TYPES
from .cut_common import _SEGS, _TO_END, _ZERO_FROM, _resolve_rng, tr_key
from .. import jobs as jobreg


class MultiCutMixin:
    """Cut / Edit -> Multi cut sub-tab: a list of files, each with its own
    section times, cut one after another."""

    def _multi_choose_subs(self):
        files = [p for p in self._multi_paths.values() if os.path.isfile(p)]
        if not files:
            messagebox.showinfo(tr("No files"), tr("Add files to the Multi cut list first so I "
                                                   "can scan which subtitle languages they "
                                                   "contain."))
            return
        self._open_subs_dialog(files)

    # ---------------- Multi cut (many files, per-file times) ----------------
    # (key, English label - shown with tr_key(label))
    MULTI_SEGS = _SEGS

    def _build_multi_tab(self, multi, saved):
        btns = ttk.Frame(multi)
        btns.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(btns, text=tr("Add files..."), command=self._multi_add).pack(side="left")
        ttk.Button(btns, text=tr("Remove selected"), command=self._multi_remove).pack(
            side="left", padx=6)
        ttk.Button(btns, text=tr("Clear all"), command=self._multi_clear).pack(side="left")
        info = ttk.Label(btns, text="ⓘ", style="Hint.TLabel", cursor="question_arrow")
        info.pack(side="left", padx=(8, 0))
        add_tooltip(info, tr("Cut the intro/credits (and pre/after) out of several files, each "
                             "with its own times. Add files, click one to set its sections - or "
                             "set them once and 'Apply to all'. Then Cut all."))
        self._multi_keep_pos_var = tk.BooleanVar(value=bool(saved.get("multi_keep_pos", True)))
        help_button(btns, "cut_multi").pack(side="right", padx=(8, 0))
        kpcb = ttk.Checkbutton(btns, text=tr("Keep player time when switching files"),
                               variable=self._multi_keep_pos_var)
        kpcb.pack(side="right")
        add_tooltip(kpcb, tr("When you click another file, open it at the SAME timestamp instead "
                             "of the start - so you can jump between episodes and instantly see "
                             "if the intro sits at the same position. Untick to always start "
                             "each clip at 0:00."))

        # detection that fills the list: template matching or the Plex-style scan
        drow = ttk.Frame(multi)
        drow.pack(fill="x", padx=6, pady=(4, 0))
        self.multi_detect_mb = ttk.Menubutton(drow, text=tr("Auto-detect"))
        dmenu = tk.Menu(self.multi_detect_mb, tearoff=False)
        dmenu.add_command(label=tr("Detect selected"), command=lambda: self._multi_detect(True))
        dmenu.add_command(label=tr("Detect all"), command=lambda: self._multi_detect(False))
        self.multi_detect_mb["menu"] = dmenu
        self.multi_detect_mb.pack(side="left")
        add_tooltip(self.multi_detect_mb, tr(
            "Match the templates (folders and Detection settings of Cut / Edit → "
            "Auto-detect) in the selected files or all files of this list and fill their "
            "times; ⚠ = not found or only weak. Asks before overwriting times a file "
            "already has."))
        self.multi_plex_btn = ttk.Button(drow, text=tr("Plex-style scan (no templates)"),
                                         command=self._multi_plex_scan)
        self.multi_plex_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.multi_plex_btn, tr(
            "Find the intro and credits WITHOUT templates, like Plex: the opening that "
            "recurs across the episodes (sound and/or pictures) and the credits near the "
            "end (a recurring ending, or credits-like pictures: text on black, rolling "
            "text). Scans every file in the list - or only the selected ones when 2 or "
            "more are selected. The intro needs at least 2 episodes of the same show."))

        tvf = ttk.Frame(multi)
        tvf.pack(fill="x", padx=6, pady=(4, 0))
        cols = ("file",) + tuple(k for k, _ in self.MULTI_SEGS) + ("notes",)
        self.multi_tree = ttk.Treeview(tvf, columns=cols, show="headings", height=6)
        self.multi_tree.heading("file", text=tr("File"))
        self.multi_tree.column("file", width=300, anchor="w", stretch=True)
        for k, txt in self.MULTI_SEGS:
            self.multi_tree.heading(k, text=tr_key(txt))
            self.multi_tree.column(k, width=100, anchor="center", stretch=False)
        self.multi_tree.heading("notes", text=tr("Notes"))
        self.multi_tree.column("notes", width=220, anchor="w", stretch=False)
        # rows 'Review first' couldn't fill confidently: ⚠ + warning colour
        bind_status_colors(self.multi_tree, {"warn": "warn"})
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

        # player LEFT / cut controls RIGHT, side by side like the Templates tab
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
        add_tooltip(self.multi_tree, tr("Up / Down arrow keys jump to the previous / next file."))

        seg = ttk.LabelFrame(mright, text=" {} ".format(
            tr("Cut sections for the selected file (from / to)")), padding=(8, 4))
        seg.pack(fill="x")
        self._multi_entries = {}
        # From and To on their own lines, so the rows fit beside the player even
        # with longer (translated) button texts
        for i, (key, label) in enumerate(self.MULTI_SEGS):
            ef, et = TimeEntry(seg), TimeEntry(seg)
            self._multi_entries[key] = (ef, et)
            name = tr_key(label)
            r = i * 2
            pad0 = (6 if i else 2, 0)
            ttk.Label(seg, text=tr("{label}:", label=name)).grid(
                row=r, column=0, sticky="e", padx=4, pady=pad0)
            for rr, box, which in ((r, ef, "from"), (r + 1, et, "to")):
                pady = pad0 if which == "from" else (1, 2)
                ttk.Label(seg, text=tr("From") if which == "from" else tr("To"),
                          style="Hint.TLabel").grid(row=rr, column=1, sticky="e", padx=(0, 4),
                                                    pady=pady)
                box.grid(row=rr, column=2, padx=2, pady=pady)
                sb_ = ttk.Button(seg, text=tr("Set"), width=-4,
                                 command=lambda e=box: self._multi_set(e))
                sb_.grid(row=rr, column=3, padx=2, pady=pady)
                sn = ttk.Button(seg, text=tr("Snap"), width=-5)
                sn.configure(command=lambda e=box, k=key, w=which, b=sn: self._snap_box(
                    self.multi_player, e, k, w, b))
                sn.grid(row=rr, column=4, padx=(0, 2), pady=pady)
                gb = ttk.Button(seg, text=tr("Go"), width=-3,
                                command=lambda e=box: self._multi_goto(e))
                gb.grid(row=rr, column=5, padx=(0, 4), pady=pady)
                if which == "from":
                    add_tooltip(sn, tr("Snap the {seg} START of the selected file to the nearest "
                                       "end of a silence / black frame within ±1 s (uses the "
                                       "From box, or the player position if the box is empty)",
                                       seg=name))
                    add_tooltip(sb_, tr("Set the {seg} START to the player's current frame",
                                        seg=name))
                    add_tooltip(gb, tr("Jump the player to the {seg} START time typed in the "
                                       "box", seg=name))
                else:
                    add_tooltip(sn, tr("Snap the {seg} END of the selected file to the nearest "
                                       "start of a silence / black frame within ±1 s (uses the "
                                       "To box, or the player position if the box is empty)",
                                       seg=name))
                    add_tooltip(sb_, tr("Set the {seg} END to the player's current frame",
                                        seg=name))
                    add_tooltip(gb, tr("Jump the player to the {seg} END time typed in the box",
                                       seg=name))
            pvb = ttk.Button(seg, text="▶", width=3, command=lambda k=key: self._multi_preview(k))
            pvb.grid(row=r, column=6, padx=(2, 0), pady=pad0)
            add_tooltip(pvb, tr("Play only the {seg} section (From to To) in the player",
                                seg=name))
            cmb = self._cand_button(seg, key, "multi")
            cmb.grid(row=r, column=7, padx=(2, 0), pady=pad0)
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._multi_sync())

        arow = ttk.Frame(mright)
        arow.pack(fill="x", pady=(6, 0))
        asb = ttk.Button(arow, text=tr("Apply to selected"), command=self._multi_apply_selected)
        asb.pack(side="left")
        add_tooltip(asb, tr("Copy the section times in the boxes to the file(s) you've "
                            "highlighted in the list. Ctrl-click or Shift-click to select "
                            "several."))
        ab = ttk.Button(arow, text=tr("Apply to ALL files"), command=self._multi_apply_all)
        ab.pack(side="left", padx=(6, 0))
        add_tooltip(ab, tr("Copy the section times currently in the boxes to EVERY file in the "
                           "list at once - handy when the intro/credits sit at the same spot in "
                           "all of them."))

        # subtitle-language picker for the Multi cut list (shares the same
        # keep-list as Cut / Edit → Auto-detect, but scans THESE files)
        subf = ttk.LabelFrame(mright, text=f" {tr('Subtitles')} ", padding=(8, 4))
        subf.pack(fill="x", pady=(6, 0))
        msfcb = ttk.Checkbutton(subf, text=tr("Keep only chosen subtitle languages"),
                                variable=self.subs_filter_var, command=self._upd_subs_label)
        msfcb.pack(anchor="w")
        add_tooltip(msfcb, tr("Drop subtitle languages you don't want (video and all audio "
                              "tracks are untouched). This shares the same choice as Cut / Edit "
                              "→ Auto-detect."))
        msubrow = ttk.Frame(subf)
        msubrow.pack(fill="x", pady=1)
        ttk.Button(msubrow, text=tr("Choose languages..."), command=self._multi_choose_subs).pack(side="left", padx=(0, 6))
        self.multi_subs_lbl = tk.StringVar()
        ttk.Label(msubrow, textvariable=self.multi_subs_lbl, style="Hint.TLabel").pack(side="left")

        crow = ttk.Frame(mright)
        crow.pack(fill="x", pady=(8, 0))
        self.multi_cut_btn = ttk.Button(crow, text=tr("Cut all files"),
                                        command=self._multi_cut_all)
        self.multi_cut_btn.pack(side="left", fill="x", expand=True)
        self.multi_queue_btn = ttk.Button(crow, text=tr("Add to queue"),
                                          command=self._enqueue_multi)
        self.multi_queue_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.multi_queue_btn, tr(
            "Queue cutting THIS list with its current times and "
            "the current encoding settings - it starts when nothing else is running. "
            "See Queue... in the status bar."))
        auto_wrap(ttk.Label(mright, text=tr(
            "Each file has its filled sections removed and the rest kept, using the "
            "Encoding settings from Cut / Edit → Auto-detect and the subtitle choice above. "
            "Files with no sections set are skipped. Empty Pre-intro/Intro From = start "
            "of file; empty Credits/After-credits To = end of file ('?' = incomplete, "
            "skipped)."),
            style="Hint.TLabel", wraplength=420, justify="left")).pack(
                anchor="w", fill="x", pady=(2, 2))
        auto_wrap(ttk.Label(mright, textvariable=self.enc_summary, style="Hint.TLabel",
                            wraplength=420, justify="left")).pack(anchor="w", fill="x",
                                                                  pady=(0, 6))
        self._upd_subs_label()      # fill in the new label now that it exists
        self._multi_restore(saved.get("multi_files"))

    def _multi_restore(self, items):
        """Re-add last session's Multi cut list with its per-file times
        (files that no longer exist are skipped). Nothing is selected, so no
        clip is opened at startup."""
        if not isinstance(items, list):
            return
        for it in items:
            try:
                p = it.get("path") or ""
                seg = it.get("seg") or {}
            except AttributeError:
                continue
            if not os.path.isfile(p) or p in self._multi_paths.values():
                continue
            ranges = {}
            for k, _ in self.MULTI_SEGS:
                v = seg.get(k) if isinstance(seg, dict) else None
                if not (isinstance(v, (list, tuple)) and len(v) == 2):
                    v = [None, None]
                ranges[k] = [float(x) if isinstance(x, (int, float)) else None for x in v]
            iid = self.multi_tree.insert("", "end",
                                         values=(os.path.basename(p),) + ("-",) * len(self.MULTI_SEGS))
            self._multi_paths[iid] = p
            self._multi_seg[iid] = ranges
            self._multi_write_row(iid)
            notes = it.get("notes")
            if isinstance(notes, str) and notes:
                self._multi_set_notes(iid, notes, warn=bool(it.get("warn", True)))

    def _multi_snapshot(self):
        return [{"path": self._multi_paths[iid],
                 "seg": {k: list(v) for k, v in self._multi_seg.get(iid, {}).items()},
                 "notes": self._multi_notes.get(iid, ""),
                 "warn": iid not in self._multi_info}
                for iid in self.multi_tree.get_children() if iid in self._multi_paths]

    def _multi_set_notes(self, iid, notes, warn=True):
        """Show a row's review notes ('intro weak 0.41; credits not found'):
        Notes column, a ⚠ before the file name and the warning colour.
        warn=False: an informational note only ('intro audio · credits
        visual') - no ⚠, normal colour."""
        notes = notes or ""
        path = self._multi_paths.get(iid, "")
        name = os.path.basename(path)
        if notes:
            self._multi_notes[iid] = notes
        else:
            self._multi_notes.pop(iid, None)
        if notes and not warn:
            self._multi_info.add(iid)
        else:
            self._multi_info.discard(iid)
        bad = bool(notes) and warn
        try:
            self.multi_tree.set(iid, "file", ("⚠ " + name) if bad else name)
            self.multi_tree.set(iid, "notes", notes)
            self.multi_tree.item(iid, tags=("warn",) if bad else ())
        except tk.TclError:
            pass

    @staticmethod
    def _short_rng(key, fr, to):
        """List-cell text; follows the same rules as the cut (_resolve_rng):
        '?' marks a half-filled section that will be skipped."""
        if fr is None and to is None:
            return "-"
        if fr is not None:
            a = format_seconds(fr)
        else:
            a = "0:00" if key in _ZERO_FROM else "?"
        if to is not None:
            b = format_seconds(to)
        else:
            b = tr("end") if key in _TO_END else "?"
        if fr is not None and to is not None and to <= fr:
            b += " ?"
        return f"{a}-{b}"

    def _multi_add(self):
        paths = filedialog.askopenfilenames(title=tr("Add videos"), filetypes=_VIDEO_TYPES)
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
            self._multi_cands.pop(self._multi_paths.pop(iid, None), None)
            self._multi_seg.pop(iid, None)
            self._multi_notes.pop(iid, None)
            self._multi_info.discard(iid)
            self.multi_tree.delete(iid)
        self._multi_sel = None
        self._multi_clear_boxes()

    def _multi_clear_boxes(self):
        """Empty the section boxes (no file is selected, so edits there would
        go nowhere)."""
        self._multi_loading = True
        for ef, et in self._multi_entries.values():
            self._multi_clear_entry(ef)
            self._multi_clear_entry(et)
        self._multi_loading = False
        self._multi_markers()

    def _multi_clear(self):
        self.multi_tree.delete(*self.multi_tree.get_children())
        self._multi_paths.clear()
        self._multi_seg.clear()
        self._multi_notes.clear()
        self._multi_info.clear()
        self._multi_cands.clear()
        self._multi_sel = None
        self._multi_clear_boxes()

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
            self.multi_tree.set(iid, key, self._short_rng(key, fr, to))

    def _multi_sync(self):
        if self._multi_loading or not self._multi_sel:
            return
        iid = self._multi_sel
        self._multi_seg[iid] = self._multi_read_entries()
        self._multi_write_row(iid)
        self._multi_markers()

    def _multi_apply_to(self, iids, selected):
        """Copy the current box times onto each iid in `iids`."""
        times = self._multi_read_entries()
        if all(fr is None and to is None for fr, to in times.values()):
            messagebox.showinfo(tr("Nothing set"), tr("Set at least one section's times first."))
            return
        for iid in iids:
            self._multi_seg[iid] = {k: list(v) for k, v in times.items()}
            self._multi_write_row(iid)
        if selected:
            msg = ntr("Applied the section times to {n} selected file.",
                      "Applied the section times to {n} selected files.", len(iids))
        else:
            msg = ntr("Applied the section times to {n} file.",
                      "Applied the section times to {n} files.", len(iids))
        self.status_var.set(msg)

    def _multi_apply_all(self):
        if not self._multi_paths:
            messagebox.showinfo(tr("No files"), tr("Add some files first."))
            return
        self._multi_apply_to(list(self._multi_paths), False)

    def _multi_apply_selected(self):
        sel = [i for i in self.multi_tree.selection() if i in self._multi_paths]
        if not sel:
            messagebox.showinfo(tr("No selection"), tr("Highlight one or more files in the list "
                                                       "first (Ctrl-click or Shift-click for "
                                                       "several)."))
            return
        self._multi_apply_to(sel, True)

    def _multi_markers(self):
        dur = self.multi_player.timeline.duration
        marks = []
        for key, (ef, et) in self._multi_entries.items():
            fr, _ = ef.get_seconds()
            to, _ = et.get_seconds()
            fr, end, err = _resolve_rng(key, fr, to, dur)
            if fr is None or err or not end:
                continue
            marks.append((fr, end, key))
        self.multi_player.set_markers(marks)

    def _multi_set(self, entry):
        sec = self.multi_player.current_seconds()
        if sec is None:
            messagebox.showinfo(tr("No video"), tr("Click a file in the list first to load it."))
            return
        entry.set_seconds(sec)
        entry.flash()
        # keep keyboard focus on the player so the arrow keys still work:
        # Up/Down move between files, Left/Right step frames
        self.multi_player.canvas.focus_set()

    def _multi_goto(self, entry):
        if not self.multi_player.has_video():
            messagebox.showinfo(tr("No video"), tr("Click a file in the list first to load it."))
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set(tr("Type a valid time in the box first, then Go."))
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
        fr, end, err = _resolve_rng(key, fr, to, self.multi_player.timeline.duration)
        if err:
            self.status_var.set(tr("That section: {problem}.", problem=tr_key(err)))
        elif fr is not None and end:
            self.multi_player.play_range(fr, end)
        self.multi_player.canvas.focus_set()   # keep arrow keys working

    def _multi_cut_all(self):
        prepared = self._multi_prepare()
        if prepared:
            self._multi_launch(prepared)

    def _enqueue_multi(self):
        """Queue the Multi cut list as it is NOW (times + encoding captured)."""
        prepared = self._multi_prepare()
        if not prepared:
            return
        n = len(prepared[0])
        name = ntr("Cut / Edit multi cut - {n} file", "Cut / Edit multi cut - {n} files", n)
        jobreg.enqueue(name, lambda p=prepared: self._multi_launch(p))
        self.log(f"[QUEUE] added: Cut / Edit multi cut - {n} file(s)")
        self.status_var.set(tr("Queued: {name}", name=name))

    def _multi_prepare(self):
        """Validate the Multi cut list -> (jobs, base_cfg, skip_lines,
        n_skipped), or None after telling the user what's wrong."""
        try:
            crf = int(self.crf_var.get()); crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get()); assert kf >= 0
        except (ValueError, AssertionError):
            messagebox.showerror(tr("Error"), tr("Check the CRF values (0-51) and keyframe "
                                                 "interval (>=0)."))
            return None
        if not self._multi_paths:
            messagebox.showerror(tr("Error"), tr("Add some files to the list first."))
            return None
        out_dir = self.dirs["Output folder:"].get()
        if self._refuse_same_folder(out_dir, list(self._multi_paths.values())):
            return None
        # two files with the same name would write the same output file
        by_out = {}
        for path in self._multi_paths.values():
            by_out.setdefault(os.path.normcase(os.path.basename(path)), []).append(path)
        dupes = [ps for ps in by_out.values() if len(ps) > 1]
        if dupes:
            lines = [p for ps in dupes for p in ps]
            if len(lines) > 12:
                lines = lines[:12] + [tr("... and {n} more", n=len(lines) - 12)]
            messagebox.showerror(
                tr("Same file name twice"),
                tr("These files have the same name, so their cut outputs would overwrite each "
                   "other in the Output folder:\n\n{files}\n\nRemove or rename one of each "
                   "pair, then Cut all again.", files="\n".join(lines)))
            return None
        jobs = []      # (path, [drop ranges])
        skip_lines = []
        for iid, path in self._multi_paths.items():
            name = os.path.basename(path)
            drops = []
            for key, label in self.MULTI_SEGS:
                fr, to = self._multi_seg.get(iid, {}).get(key, [None, None])
                # same rules as the list cells and markers; an empty To on
                # credits/after-credits stays None = end, resolved in the worker
                fr, to, err = _resolve_rng(key, fr, to)
                if err:
                    skip_lines.append(f"  [SKIP] {name} - {label}: {err}")
                elif fr is not None:
                    drops.append((fr, to))
            if drops:
                jobs.append((path, drops))
            else:
                skip_lines.append(f"  [SKIP] {name} - no usable section set")
        n_skipped = sum(1 for ln in skip_lines if ln.endswith("no usable section set"))
        if not jobs:
            for ln in skip_lines:
                self.log(ln)
            messagebox.showerror(tr("Error"), tr("No file has any valid section set."))
            return None
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
        return jobs, base_cfg, skip_lines, n_skipped

    def _multi_launch(self, prepared):
        """Start a prepared Multi cut (also the queue's start_fn). Returns the
        job id, or None if the tab is busy."""
        if self._busy:
            return None
        jobs, base_cfg, skip_lines, n_skipped = prepared
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True, [p for p, _ in jobs] if base_cfg["move_done"] else None)
        if base_cfg["move_done"] and callable(self.unload_players_hook):
            self.unload_players_hook()      # free the files so their sources can move
        jid = self._jid = jobreg.begin(tr("Cut / Edit multi cut"), stop_event=self.stop_event,
                                       tab=self)
        threading.Thread(target=self._multi_worker,
                         args=(jobs, base_cfg, skip_lines, n_skipped, jid),
                         daemon=True).start()
        return jid

    def _multi_worker(self, jobs, base_cfg, skip_lines=(), n_skipped=0, jid=None):
        n_ok = n_fail = 0
        n_skip = n_skipped
        crashed = False
        try:
            n = len(jobs)
            self.log(f"[MULTI] cutting {n} file(s), each with its own sections")
            for ln in skip_lines:
                self.log(ln)
            for i, (path, drops) in enumerate(jobs, 1):
                if self.stop_event.is_set():
                    break
                try:
                    total = probe_duration(path)
                    if not total:
                        self.log(f"  [{i}/{n}] [FAIL] could not read {os.path.basename(path)}")
                        n_fail += 1
                        continue
                    # an empty To ("end") runs to the end of this file
                    drops = [(a, total if b is None else b) for a, b in drops]
                    drops = [(a, b) for a, b in drops if b > a]
                    keep = keep_from_drops(total, drops) if drops else None
                    if not keep:
                        self.log(f"  [{i}/{n}] [SKIP] {os.path.basename(path)} - ranges leave nothing")
                        n_skip += 1
                        continue
                    removed = ", ".join(f"{fmt_time(a)}->{fmt_time(b)}" for a, b in drops)
                    self.log(f"  [{i}/{n}] {os.path.basename(path)}  remove {removed}")
                    cfg = dict(base_cfg, video=path, job_label=f"[{i}/{n}]")
                    final = os.path.join(cfg["output_dir"], os.path.basename(path))
                    t0 = time.time()
                    res = run_manual(cfg, keep, self, self.stop_event)
                    if self.stop_event.is_set():
                        break           # the interrupted file counts as neither
                    if isinstance(res, bool):
                        ok = res
                    else:               # run_manual returns nothing: check the output
                        try:
                            ok = os.path.getmtime(final) >= t0 - 2
                        except OSError:
                            ok = False
                    if ok:
                        n_ok += 1
                    else:
                        n_fail += 1
                except Exception as e:
                    self.log(f"    [FAIL] {e!r}")
                    n_fail += 1
            counts = f"{n_ok} OK, {n_fail} failed, {n_skip} skipped"
            if self.stop_event.is_set():
                self.log(f"[MULTI] stopped ({counts} before Stop).")
            else:
                self.log(f"[MULTI] done: {counts}.")
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            jobreg.end(jid, ok=not crashed and n_fail == 0,
                       summary=tr("{ok} OK, {failed} failed, {skipped} skipped", ok=n_ok,
                                  failed=n_fail, skipped=n_skip))
            self.after(0, lambda: self._running(False))
