"""Cut / Edit: detection that fills the Multi cut list (ScanMixin).

* the shared "detect these files and put the results in Multi cut" plumbing
  used by Review first (Auto-detect sub-tab) and Multi cut's own buttons;
* Multi cut -> Auto-detect: Detect selected / Detect all (template matching);
* Multi cut -> Plex-style scan (no templates) and Review first's Plex-style
  option (engine.plexscan.scan_season)."""
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import AUDIO_LANG_CHOICES
from ..engine.detect import detect_segments, load_templates_for
from ..engine.plexscan import scan_season
from ..i18n import ntr, tr
from ..ui.widgets import KeyedCombobox, add_tooltip
from .common import approx_edge_notes, edges_for_range
from .cut_common import (_SEGS, _ask_choice, _best_ok, _detect_notes, _mmss, _src_label,
                         margin_widgets, mode_combobox, norm_margin)
from .templates_detect import _EPLEN, _SENS, eplen_values
from .. import jobs as jobreg
from ..ui import icons

class ScanMixin:
    """Detection results -> Multi cut list; Multi cut Auto-detect and the
    Plex-style scan."""

    # ---------------- shared: the Multi cut list ----------------
    def _multi_iid_for(self, path):
        key = os.path.normcase(os.path.abspath(path))
        for iid, p in self._multi_paths.items():
            if os.path.normcase(os.path.abspath(p)) == key:
                return iid
        return None

    def _multi_has_times(self, iid):
        return any(fr is not None or to is not None
                   for fr, to in self._multi_seg.get(iid, {}).values())

    def _multi_ask_policy(self, iids):
        """'overwrite' / 'fill' when some of `iids` already have times (asked
        once per run), 'overwrite' when none do, None = cancelled."""
        busy = [i for i in iids if self._multi_has_times(i)]
        if not busy:
            return "overwrite"
        choice = _ask_choice(
            self, tr("Some files already have times"),
            ntr("{n} of these files already has section times.",
                "{n} of these files already have section times.", len(busy))
            + "\n\n" + tr("Overwrite them with the detected times, only fill the sections "
                          "that are still empty, or Cancel?"),
            [("overwrite", tr("Overwrite")), ("fill", tr("Only fill empty")),
             ("cancel", tr("Cancel"))])
        return choice if choice in ("overwrite", "fill") else None

    def _multi_apply_results(self, entries, policy="overwrite"):
        """Put detection results into the Multi cut list. entries =
        [(path, ranges, notes, warn, cands[, edges])]: ranges = {key: [from, to]}
        for the sections that were searched (others are left alone); edges =
        {key: {"start", "end"}} how detection placed them (approximate ones get
        a ⚠ on their box); policy 'fill' only sets sections that are still
        empty. New files are added.
        Returns (n_new, n_updated, first_iid, first_warn_iid)."""
        n_new = n_upd = 0
        first = first_warn = None
        for path, ranges, notes, warn, cands, *more in entries:
            edges = (more[0] if more else None) or {}
            iid = self._multi_iid_for(path)
            if iid is None:
                iid = self.multi_tree.insert(
                    "", "end", values=(os.path.basename(path),) + ("-",) * len(_SEGS))
                self._multi_paths[iid] = path
                self._multi_seg[iid] = {k: [None, None] for k, _ in _SEGS}
                n_new += 1
            else:
                n_upd += 1
            cur = self._multi_seg.setdefault(iid, {k: [None, None] for k, _ in _SEGS})
            for key, rng in ranges.items():
                old = cur.get(key, [None, None])
                if policy == "fill" and (old[0] is not None or old[1] is not None):
                    continue
                cur[key] = [rng[0], rng[1]]
                self._multi_edges.setdefault(iid, {})[key] = edges_for_range(
                    edges.get(key), rng)
            self._multi_write_row(iid)
            self._multi_set_notes(iid, notes, warn=warn)
            if cands:
                self._multi_cands[path] = cands
            else:
                self._multi_cands.pop(path, None)
            first = first or iid
            if notes and warn:
                first_warn = first_warn or iid
        return n_new, n_upd, first, first_warn

    def _multi_reselect(self, pick):
        """Show Multi cut and (re)select `pick` (or keep the selected row) so
        its boxes, markers and ▾ menus show the new times."""
        self._rnb.select(self._multi_page)
        cur = self._multi_sel if self._multi_sel in self._multi_paths else None
        pick = cur or pick
        if pick:
            self._multi_sel = None
            self.multi_tree.selection_set(())      # so re-selecting the same row still fires
            self.multi_tree.selection_set(pick)    # loads it + fills the boxes
            self.multi_tree.focus(pick)
            self.multi_tree.see(pick)

    def _multi_ranges_from_detect(self, res, cfg, have=None):
        """{key: [from, to]} for every section that was searched (ticked and
        has templates): the best ok candidate, intro_from_start /
        credits_to_end applied; [None, None] when nothing confident."""
        use = cfg.get("use", {})
        out = {}
        for key, _label in _SEGS:
            if not use.get(key, True) or (have is not None and key not in have):
                continue
            best = _best_ok((res or {}).get(key))
            fr = to = None
            if best:
                fr, to = float(best["start"]), float(best["end"])
                if key == "intro" and cfg.get("intro_from_start"):
                    fr = None
                if key == "credits" and cfg.get("credits_to_end"):
                    to = None
            out[key] = [fr, to]
        return out

    def _multi_detect_entry(self, path, res, cfg, have=None, ranges_use_have=True):
        """One detect_segments result as a _multi_apply_results entry:
        problems make the row 'need a look'; approximate edges (placed by
        sound / a fade) are added to its notes and marked on the boxes."""
        ranges = self._multi_ranges_from_detect(res, cfg, have if ranges_use_have else None)
        edges = {k: edges_for_range((_best_ok((res or {}).get(k)) or {}).get("edge_src"), r)
                 for k, r in ranges.items()}
        problems = _detect_notes(res, cfg.get("use", {}), have, ui=True)
        return (path, ranges, "; ".join(problems + approx_edge_notes(edges)),
                bool(problems), res or None, edges)

    def _detect_loop(self, files, cfg, tag, review=False):
        """Worker side: load the templates once, match every file. Returns
        (results {path: detect_segments result | None}, have, n_warn); stops
        early on Stop (the interrupted file is left out)."""
        results, have, n_warn = {}, None, 0
        n = len(files)
        self.progress(0.0, tr("Loading templates..."))
        self.status(tr("Review: loading templates...") if review else tr("Loading templates..."))
        templates = load_templates_for(cfg, log=self.log, stop_event=self.stop_event)
        empty = not templates or (isinstance(templates, dict) and not any(templates.values()))
        if isinstance(templates, dict):
            have = {k for k, v in templates.items() if v}
        if empty and not self.stop_event.is_set():
            self.log(f"[{tag}] no usable templates in the template folders - nothing to "
                     "detect with.")
        for i, f in enumerate(files if not empty else (), 1):
            if self.stop_event.is_set():
                break
            name = os.path.basename(f)
            if review:
                self.status(tr("Review {i}/{n}: {name}", i=i, n=n, name=name))
                self.progress((i - 1) / n, tr("Review {i}/{n}", i=i, n=n))
            else:
                self.status(tr("Detecting {i}/{n}: {name}", i=i, n=n, name=name))
                self.progress((i - 1) / n, tr("Detecting {i}/{n}", i=i, n=n))
            try:
                res = detect_segments(f, cfg, stop_event=self.stop_event,
                                      log=None, templates=templates)
            except Exception as e:
                self.log(f"  [{i}/{n}] [FAIL] {name}: {e!r}")
                res = None
            if self.stop_event.is_set():
                break               # the interrupted file's result is partial
            results[f] = res
            notes = _detect_notes(res, cfg.get("use", {}), have)
            bits = []
            for key, label in _SEGS:
                best = _best_ok((res or {}).get(key))
                if best and cfg.get("use", {}).get(key, True):
                    src = best.get("src")
                    bits.append(f"{label} {_mmss(best['start'])}-{_mmss(best['end'])} "
                                f"({float(best.get('score') or 0):.2f}"
                                + (f", {src}" if src and src != "audio" else "") + ")")
            if notes:
                n_warn += 1
            self.log(f"  [{i}/{n}] {name}: {', '.join(bits) or 'nothing found'}"
                     + (f"   ⚠ {'; '.join(notes)}" if notes else ""))
        frac = len(results) / n if n else 0.0
        self.progress(frac, tr("Reviewed {done}/{n}", done=len(results), n=n) if review
                      else tr("Detected {done}/{n}", done=len(results), n=n))
        return results, have, n_warn

    # ---------------- Multi cut -> Auto-detect (templates) ----------------
    def _multi_detect(self, selected):
        if self._busy or not self._engine_ready():
            return
        kids = [i for i in self.multi_tree.get_children() if i in self._multi_paths]
        iids = ([i for i in kids if i in self.multi_tree.selection()] if selected else kids)
        if not iids:
            if selected:
                messagebox.showinfo(tr("No selection"), tr(
                    "Highlight one or more files in the list first (Ctrl-click or "
                    "Shift-click for several)."))
            else:
                messagebox.showinfo(tr("No files"), tr("Add some files first."))
            return
        cfg = self._batch_cfg(check_output=False)
        if cfg is None:
            return
        policy = self._multi_ask_policy(iids)
        if policy is None:
            return
        files = [self._multi_paths[i] for i in iids]
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True)
        jid = self._jid = jobreg.begin(tr("Multi cut auto-detect"),
                                       stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._multi_detect_worker, args=(files, cfg, policy, jid),
                         daemon=True).start()

    def _multi_detect_worker(self, files, cfg, policy, jid=None):
        results, have, n_warn = {}, None, 0
        crashed = False
        try:
            self.log(f"[MULTI-DETECT] matching templates in {len(files)} file(s) "
                     f"({cfg.get('detect_mode') or 'audio'}, "
                     f"{'only filling empty sections' if policy == 'fill' else 'overwriting'})")
            results, have, n_warn = self._detect_loop(files, cfg, "MULTI-DETECT")
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] auto-detect crashed: {e!r}")
        finally:
            stopped = self.stop_event.is_set()
            jobreg.end(jid, ok=not crashed and not stopped,
                       summary=tr("{done}/{n} detected, {warn} need a look",
                                  done=len(results), n=len(files), warn=n_warn))
            self.after(0, lambda: self._multi_detect_done(files, cfg, policy, results,
                                                          have, stopped))

    def _multi_detect_done(self, files, cfg, policy, results, have, stopped):
        self._running(False)
        self.bar["value"] = 0
        entries = []
        for f in files:
            if f not in results:
                continue
            entries.append(self._multi_detect_entry(f, results[f], cfg, have))
        if not entries:
            self.status_var.set(tr("Auto-detect stopped before any file finished.") if stopped
                                else tr("Auto-detect: nothing to fill."))
            return
        _new, _upd, first, first_warn = self._multi_apply_results(entries, policy)
        self._multi_reselect(first_warn or first)
        n_warn = sum(1 for e in entries if e[3])
        shown = ntr("Auto-detect filled {n} file: {ok} complete, {warn} need a look (⚠)",
                    "Auto-detect filled {n} files: {ok} complete, {warn} need a look (⚠)",
                    len(entries), ok=len(entries) - n_warn, warn=n_warn)
        if stopped:
            shown += ntr(" - stopped, {n} file not reviewed", " - stopped, {n} files not reviewed",
                         len(files) - len(entries))
        self.log(f"[MULTI-DETECT] {len(entries)} file(s) filled, {n_warn} need a look (⚠)"
                 + (" - stopped" if stopped else ""))
        self.status_var.set(shown)

    # ---------------- Plex-style scan (no templates) ----------------
    def _plex_defaults(self):
        """Scan options: last used ones, else the Templates -> Auto-detect
        settings (window, min lengths, sensitivity, episode length, language)."""
        d = dict(getattr(self, "_plex_saved", None) or {})
        tt = self._find_template_tab()

        def tv(name, default):
            var = getattr(tt, name, None) if tt is not None else None
            try:
                return var.get() if var is not None else default
            except tk.TclError:
                return default
        eplen = tv("det_eplen", _EPLEN[1])
        win, mi, mc, _pa = eplen_values(eplen)
        d.setdefault("eplen", eplen if eplen in _EPLEN else _EPLEN[1])
        d.setdefault("intro_window", tv("det_window", win))
        d.setdefault("credits_secs", 420)
        d.setdefault("min_intro", tv("det_minlen_intro", mi))
        d.setdefault("min_credits", tv("det_minlen_credits", mc))
        sens = tv("det_sens", "Medium")
        d.setdefault("sens", sens if sens in _SENS else "Medium")
        d.setdefault("intro_mode", "both")
        d.setdefault("credits_mode", "both")
        d.setdefault("snap", True)
        return d

    def _plex_lang(self):
        tt = self._find_template_tab()
        if tt is not None and hasattr(tt, "det_lang_var"):
            return AUDIO_LANG_CHOICES.get(tt.det_lang_var.get())
        return AUDIO_LANG_CHOICES.get(self.match_lang_var.get())

    def _plex_options(self, ask_policy=False):
        """The scan options dialog. Returns (opts for scan_season, policy) or
        None if cancelled. ask_policy: show Overwrite / Only fill empty."""
        d = self._plex_defaults()
        dlg = tk.Toplevel(self)
        dlg.title(tr("Plex-style scan (no templates)"))
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=tr(
            "Finds the intro (what recurs near the start of the episodes) and the credits "
            "(a recurring ending, or credits-like pictures near the end) without any "
            "templates. The intro needs at least 2 episodes of the same show."),
            wraplength=420, justify="left").grid(row=0, column=0, columnspan=4, sticky="w",
                                                 pady=(0, 8))
        v = {k: tk.StringVar(value=str(d[k])) for k in
             ("eplen", "intro_window", "credits_secs", "min_intro", "min_credits", "sens",
              "intro_mode", "credits_mode")}
        snap_v = tk.BooleanVar(value=bool(d.get("snap", True)))

        def row(r, c, label, widget, tip=None):
            ttk.Label(body, text=label).grid(row=r, column=c, sticky="e", padx=(0 if c == 0 else 14, 4),
                                            pady=2)
            widget.grid(row=r, column=c + 1, sticky="w", pady=2)
            if tip:
                add_tooltip(widget, tip)
        ep_cb = KeyedCombobox(body, textvariable=v["eplen"], values=list(_EPLEN),
                              state="readonly", width=22)

        def _eplen(_e=None):
            win, mi, mc, _pa = eplen_values(v["eplen"].get())
            v["intro_window"].set(str(win))
            v["min_intro"].set(str(mi))
            v["min_credits"].set(str(mc))
        ep_cb.bind("<<ComboboxSelected>>", _eplen)
        row(1, 0, tr("Episode length:"), ep_cb, tr(
            "Picking a preset fills the search window and min lengths for that episode "
            "length (you can still edit them afterwards)"))
        row(2, 0, tr("Intro search (first N s):"),
            ttk.Entry(body, textvariable=v["intro_window"], width=7),
            tr("How far into each episode to look for the recurring intro"))
        row(2, 2, tr("Credits search (last N s):"),
            ttk.Entry(body, textvariable=v["credits_secs"], width=7),
            tr("How far back from the end to look for the credits (recurring ending music "
               "and credits-like pictures)"))
        row(3, 0, tr("Min intro (s):"), ttk.Entry(body, textvariable=v["min_intro"], width=7))
        row(3, 2, tr("Min credits (s):"), ttk.Entry(body, textvariable=v["min_credits"], width=7))
        row(4, 0, tr("Sensitivity:"),
            KeyedCombobox(body, textvariable=v["sens"], values=list(_SENS), state="readonly",
                          width=18),
            tr("How closely the audio must match across episodes to count as the same "
               "segment. Strict = fewer false hits; loose = finds more, may grab too much."))
        row(5, 0, tr("Intro detection:"), mode_combobox(body, v["intro_mode"]), tr(
            "Audio = the opening music that recurs across episodes; Visual = the opening "
            "PICTURES that recur (finds a dubbed opening too); Audio + Visual = both, "
            "confirmed where they agree"))
        row(5, 2, tr("Credits detection:"), mode_combobox(body, v["credits_mode"]), tr(
            "Audio = ending music that recurs across episodes; Visual = credits-like "
            "pictures near the end (text on black, rolling text); Audio + Visual = both"))
        scb = ttk.Checkbutton(body, text=tr("Snap boundaries to silence / black (±1 s)"),
                              variable=snap_v)
        scb.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        # the shared safety margin of Cut / Edit → Auto-detect (Detection & mode)
        shared_m = getattr(self, "margin_frames_var", None)
        margin_v = tk.StringVar(value=str(norm_margin(
            shared_m.get() if shared_m is not None else d.get("margin_frames"))))
        margin_widgets(body, margin_v).grid(row=7, column=0, columnspan=4, sticky="w",
                                            pady=(6, 0))
        policy_v = tk.StringVar(value="overwrite")
        if ask_policy:
            pf = ttk.Frame(body)
            pf.grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))
            ttk.Label(pf, text=tr("Files that already have times:")).pack(side="left")
            ttk.Radiobutton(pf, text=tr("Overwrite"), variable=policy_v,
                            value="overwrite").pack(side="left", padx=(6, 0))
            ttk.Radiobutton(pf, text=tr("Only fill empty"), variable=policy_v,
                            value="fill").pack(side="left", padx=(6, 0))
        out = {"v": None}

        def ok():
            try:
                vals = {k: float(v[k].get()) for k in
                        ("intro_window", "credits_secs", "min_intro", "min_credits")}
                assert vals["intro_window"] > 0 and vals["credits_secs"] > 0
                assert vals["min_intro"] >= 0 and vals["min_credits"] >= 0
            except (ValueError, AssertionError):
                messagebox.showerror(tr("Error"), tr(
                    "The search windows must be more than 0 and the min lengths numbers "
                    "(0 or more)."), parent=dlg)
                return
            saved = dict(vals, eplen=v["eplen"].get(), sens=v["sens"].get(),
                         intro_mode=v["intro_mode"].get(),
                         credits_mode=v["credits_mode"].get(), snap=bool(snap_v.get()))
            margin = norm_margin(margin_v.get())
            if shared_m is not None:
                shared_m.set(str(margin))       # one safety margin for every detector
            self._plex_saved = saved
            opts = dict(vals, thresh=_SENS.get(saved["sens"], 0.8), lang=self._plex_lang(),
                        intro_mode=saved["intro_mode"], credits_mode=saved["credits_mode"],
                        snap=saved["snap"], sens_label=saved["sens"], margin_frames=margin)
            out["v"] = (opts, policy_v.get())
            dlg.destroy()
        br = ttk.Frame(body)
        br.grid(row=9, column=0, columnspan=4, sticky="e", pady=(12, 0))
        icons.decorate(ttk.Button(br, style="Accent.TButton", text=tr("Scan"), command=ok), "detect").pack(side="left")
        ttk.Button(br, text=tr("Cancel"), command=dlg.destroy).pack(side="left", padx=(6, 0))
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        self._centre_dialog(dlg, modal=True)
        self.wait_window(dlg)
        return out["v"]

    def _multi_plex_scan(self):
        if self._busy:
            return
        kids = [i for i in self.multi_tree.get_children() if i in self._multi_paths]
        sel = [i for i in kids if i in self.multi_tree.selection()]
        iids = sel if len(sel) >= 2 else kids
        if not iids:
            messagebox.showinfo(tr("No files"), tr("Add some files first."))
            return
        got = self._plex_options(ask_policy=any(self._multi_has_times(i) for i in iids))
        if not got:
            return
        opts, policy = got
        self._plex_start([self._multi_paths[i] for i in iids], opts,
                         {"mode": "multi", "policy": policy})

    def _plex_review(self, files):
        """Review first with 'Plex-style scan': scan the Videos folder and
        load the results into Multi cut."""
        got = self._plex_options(ask_policy=False)
        if not got:
            return
        mode = self._review_list_mode()
        if mode is None:
            return
        self._plex_start(files, got[0], {"mode": "review", "replace": mode == "replace"})

    def _plex_start(self, files, opts, target):
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True)
        jid = self._jid = jobreg.begin(tr("Plex-style scan"), stop_event=self.stop_event,
                                       tab=self)
        threading.Thread(target=self._plex_worker, args=(files, opts, target, jid),
                         daemon=True).start()

    def _plex_worker(self, files, opts, target, jid=None):
        res = {}
        crashed = False
        n = len(files)
        n_warn = 0
        try:
            self.log(f"[SCAN] Plex-style scan of {n} file(s) - intro {opts['intro_mode']}, "
                     f"credits {opts['credits_mode']}, sensitivity {opts.get('sens_label')}, "
                     f"first {opts['intro_window']:g}s / last {opts['credits_secs']:g}s"
                     + (", snap" if opts.get("snap") else "")
                     + f", margin {opts.get('margin_frames', 1)} frame(s)"
                     + (f", {opts['lang']} audio" if opts.get("lang") else ""))
            if n < 2:
                self.log("[SCAN] only one file: the intro needs >= 2 episodes with the same "
                         "opening - looking for credits pictures only")
            self.status(tr("Plex-style scan of {n} file(s)...", n=n))
            res = scan_season(files, opts, progress=lambda f, t="": self.progress(f, t),
                              stop_event=self.stop_event, log=self.log)
            if not self.stop_event.is_set():
                for i, f in enumerate(files, 1):
                    r = res.get(f) or {}
                    bits = []
                    for key in ("intro", "credits"):
                        rng = r.get(key)
                        if rng:
                            bits.append(f"{key} {_mmss(rng[0])}-{_mmss(rng[1])} "
                                        f"({r.get(key + '_src')})")
                    if r.get("notes"):
                        n_warn += 1
                    self.log(f"  [{i}/{n}] {os.path.basename(f)}: "
                             f"{', '.join(bits) or 'nothing found'}"
                             + (f"   ⚠ {'; '.join(r['notes'])}" if r.get("notes") else ""))
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] Plex-style scan crashed: {e!r}")
        finally:
            stopped = self.stop_event.is_set()
            jobreg.end(jid, ok=not crashed and not stopped,
                       summary=tr("{done}/{n} scanned, {warn} need a look",
                                  done=0 if (stopped or crashed) else n, n=n, warn=n_warn))
            self.after(0, lambda: self._plex_done(files, res, opts, target,
                                                  stopped or crashed))

    def _plex_notes(self, r, opts, n_files):
        """(notes text for the Notes column, warn) of one scan result."""
        parts, warn = [], False
        for key in ("intro", "credits"):
            src = r.get(key + "_src")
            if r.get(key):
                parts.append(tr("intro {source}", source=_src_label(src)) if key == "intro"
                              else tr("credits {source}", source=_src_label(src)))
                if key == "intro" and r.get("intro_conflict"):
                    parts.append(tr("intro: audio and pictures disagree"))
                    warn = True
            elif key == "intro":
                parts.append(tr("no intro (needs ≥2 episodes with the same OP)")
                             if n_files < 2 else tr("no intro found"))
                warn = True
            else:
                parts.append(tr("no credits found"))
                warn = True
        return " · ".join(parts), warn

    def _plex_done(self, files, res, opts, target, stopped):
        self._running(False)
        self.bar["value"] = 0
        if stopped or not res:
            self.status_var.set(tr("Plex-style scan stopped - nothing changed."))
            self.log("[SCAN] stopped - the Multi cut list is unchanged.")
            return
        if target.get("mode") == "review" and target.get("replace"):
            self._multi_clear()
        entries = []
        for f in files:
            r = res.get(f)
            if not r:
                continue
            ranges = {k: (list(r[k]) if r.get(k) else [None, None])
                      for k in ("intro", "credits")}
            edges = {k: edges_for_range(r.get(k + "_edge_src"), ranges[k])
                     for k in ("intro", "credits")}
            notes, warn = self._plex_notes(r, opts, len(files))
            notes = " · ".join(([notes] if notes else []) + approx_edge_notes(edges))
            cands = {}
            for key in ("intro", "credits"):
                lst = [{"start": c["start"], "end": c["end"], "score": c["score"],
                        "template": _src_label(c["source"]), "ok": True}
                       for c in (r.get("cands") or {}).get(key, [])]
                if lst:
                    cands[key] = lst
            entries.append((f, ranges, notes, warn, cands or None, edges))
        _new, _upd, first, first_warn = self._multi_apply_results(
            entries, target.get("policy", "overwrite"))
        self._multi_reselect(first_warn or first)
        n_warn = sum(1 for e in entries if e[3])
        self.status_var.set(ntr(
            "Plex-style scan: {n} file filled, {ok} complete, {warn} need a look (⚠)",
            "Plex-style scan: {n} files filled, {ok} complete, {warn} need a look (⚠)",
            len(entries), ok=len(entries) - n_warn, warn=n_warn))
        self.log(f"[SCAN] {len(entries)} file(s) loaded into Multi cut, {n_warn} need a look "
                 "(⚠). Check them in the player, fix with Set / Snap / ▾, then 'Cut all files'.")
