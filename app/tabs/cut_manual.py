"""Cut / Edit -> Manual cut sub-tab (ManualCutMixin)."""
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import BIT_DEPTHS, CODECS
from ..engine.cut import keep_from_drops, run_manual
from ..engine.detect import detect_segments
from ..engine.formatting import fmt_time
from ..engine.probe import probe_duration
from ..i18n import tr
from ..ui.player import VideoPlayer
from ..ui.widgets import (info_icon, TimeEntry, add_tooltip, auto_wrap, enable_file_drop,
                          enable_file_drop_deep, help_button)
from .common import _VIDEO_TYPES, _same_file
from .cut_common import _best_ok, _cand_label, _resolve_rng, tr_key
from .. import jobs as jobreg
from ..ui import icons


class ManualCutMixin:
    """Cut / Edit -> Manual cut sub-tab: preview one video, set the
    sections by hand (or Auto-detect them) and cut it."""

    def _build_manual_tab(self, manual, saved):
        """Manual cut sub-tab: preview player + per-section cut points."""
        # file picker on top (full width), then player LEFT / cut controls RIGHT
        # side by side, like the Templates tab.
        mtop = ttk.Frame(manual)
        mtop.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(mtop, text=tr("Preview:")).pack(side="left", padx=(0, 6))
        # own settings key (the Template tab's file is saved as last_video)
        self.sel_var = tk.StringVar(value=saved.get("manual_video", saved.get("last_video", "")))
        ment = ttk.Entry(mtop, textvariable=self.sel_var)
        ment.pack(side="left", fill="x", expand=True)
        ment.bind("<Return>", lambda e: self._load_sel())
        icons.decorate(ttk.Button(mtop, text=tr("Browse..."), command=self._browse_sel), "folder").pack(side="left", padx=6)
        icons.decorate(ttk.Button(mtop, text=tr("Load"), command=self._load_sel), "load").pack(side="left")
        self.man_detect_btn = icons.decorate(ttk.Button(mtop, text=tr("Auto-detect"), command=self._manual_detect), "detect")
        self.man_detect_btn.pack(side="left", padx=(6, 0))
        add_tooltip(self.man_detect_btn, tr(
            "Match the templates (Cut / Edit → Auto-detect folders, "
            "segment ticks, audio language) against THIS video and fill the boxes "
            "below with the best match per segment; segments with no confident match "
            "stay empty. Then nudge with Set / Snap / the arrow keys, or pick another "
            "match from a row's ▾ list."))
        help_button(mtop, "cut_manual").pack(side="right", padx=(8, 0))

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

        man = ttk.LabelFrame(mright, text=tr(
            "Manual cut points for the previewed video (from / to)"))
        man.pack(fill="x")
        self._manual = {}
        for key, _label in self.MULTI_SEGS:
            self._manual[key] = (TimeEntry(man), TimeEntry(man))

        def _mrow(label, i, key):
            # From and To on their own lines, so the row fits beside the player
            # even with longer (translated) button texts
            ef, et = self._manual[key]
            seg = tr_key(label)
            r = i * 2
            ttk.Label(man, text=tr("{label}:", label=seg)).grid(
                row=r, column=0, sticky="e", padx=4, pady=(6 if i else 2, 0))
            for rr, box, which in ((r, ef, "from"), (r + 1, et, "to")):
                pady = ((6 if i else 2, 0) if which == "from" else (1, 2))
                ttk.Label(man, text=tr("From") if which == "from" else tr("To"),
                          style="Hint.TLabel").grid(row=rr, column=1, sticky="e", padx=(0, 4),
                                                    pady=pady)
                box.grid(row=rr, column=2, padx=2, pady=pady)
                b = ttk.Button(man, text=tr("Set"), width=-4,
                               command=lambda e=box: self._mark_manual(e))
                b.grid(row=rr, column=3, padx=2, pady=pady)
                s = ttk.Button(man, text=tr("Snap"), width=-5)
                s.configure(command=lambda b=s, e=box, w=which: self._snap_box(
                    self.player, e, key, w, b))
                s.grid(row=rr, column=4, padx=(0, 2), pady=pady)
                g = ttk.Button(man, text=tr("Go"), width=-3,
                               command=lambda e=box: self._goto_manual(e))
                g.grid(row=rr, column=5, padx=(0, 4), pady=pady)
                if which == "from":
                    add_tooltip(b, tr("Set the {seg} START to the current frame", seg=seg))
                    add_tooltip(s, tr("Snap the {seg} START to the nearest end of a silence / "
                                      "black frame within ±1 s (uses the From box, or the "
                                      "player position if the box is empty)", seg=seg))
                    add_tooltip(g, tr("Jump the player to the {seg} START time typed in the "
                                      "box", seg=seg))
                else:
                    add_tooltip(b, tr("Set the {seg} END to the current frame", seg=seg))
                    add_tooltip(s, tr("Snap the {seg} END to the nearest start of a silence / "
                                      "black frame within ±1 s (uses the To box, or the player "
                                      "position if the box is empty)", seg=seg))
                    add_tooltip(g, tr("Jump the player to the {seg} END time typed in the box",
                                      seg=seg))
            b3 = icons.decorate(ttk.Button(man, text="", width=3,
                            command=lambda k=key: self._preview_manual(k)), "play")
            b3.grid(row=r, column=6, padx=(2, 0), pady=(6 if i else 2, 0))
            add_tooltip(b3, tr("Play only the {seg} section (From to To) in the player",
                               seg=seg))
            cm = self._cand_button(man, key, "manual")
            cm.grid(row=r, column=7, padx=(2, 0), pady=(6 if i else 2, 0))
            for v in ef.vars + et.vars:
                v.trace_add("write", lambda *a: self._refresh_markers())

        for i, (key, label) in enumerate(self.MULTI_SEGS):
            _mrow(label, i, key)

        # subtitle-language picker for the previewed file (shares the same
        # keep-list as Cut / Edit → Auto-detect and Multi cut)
        msubf = ttk.LabelFrame(mright, text=tr("Subtitles"))
        msubf.pack(fill="x", pady=(6, 0))
        manf = ttk.Checkbutton(msubf, text=tr("Keep only chosen subtitle languages"),
                               variable=self.subs_filter_var, command=self._upd_subs_label)
        manf.pack(anchor="w")
        add_tooltip(manf, tr("Drop subtitle languages you don't want (video and all audio tracks "
                             "are untouched). This shares the same choice as Cut / Edit → "
                             "Auto-detect and Multi cut."))
        manrow = ttk.Frame(msubf)
        manrow.pack(fill="x", pady=1)
        icons.decorate(ttk.Button(manrow, text=tr("Choose languages..."), command=self._manual_choose_subs), "filter").pack(side="left", padx=(0, 6))
        self.manual_subs_lbl = tk.StringVar()
        ttk.Label(manrow, textvariable=self.manual_subs_lbl, style="Hint.TLabel").pack(side="left")

        self.cut_sel_btn = icons.decorate(ttk.Button(mright, style="Accent.TButton", text=tr("Cut previewed video (manual)"),
                                      command=self.cut_selected), "cut")
        self.cut_sel_btn.pack(fill="x", pady=(6, 0))
        add_tooltip(self.cut_sel_btn, tr("Cut ONLY the previewed video using the ranges above "
                                         "(ignores auto-detect)"))
        encrow = ttk.Frame(mright)
        encrow.pack(fill="x", pady=(6, 6))
        info_icon(encrow, tr(
            "Leave a section's boxes empty to skip it. Manual cut removes the "
            "filled ranges and keeps the rest, using the Encoding settings and the "
            "subtitle choice above. Empty Pre-intro/Intro From = start of file; "
            "empty Credits/After-credits To = end of file."),
        ).pack(side="left", anchor="n")
        auto_wrap(ttk.Label(encrow, textvariable=self.enc_summary, style="Hint.TLabel",
                            wraplength=420, justify="left")).pack(side="left", fill="x",
                                                                  expand=True)
        self._upd_subs_label()      # fill in the new label now that it exists

        enable_file_drop_deep(self.player, self._drop_load_sel)   # drop anywhere in the player
        enable_file_drop(ment, self._drop_load_sel)

    # ---------------- markers / player ----------------
    def _refresh_markers(self):
        if not hasattr(self, "player"):
            return
        dur = self.player.timeline.duration
        marks = []
        for key, (ef, et) in self._manual.items():
            s, s_ok = ef.get_seconds()
            e, e_ok = et.get_seconds()
            if not (s_ok and e_ok):
                continue
            s, end, err = _resolve_rng(key, s, e, dur)
            if s is None or err or not end:
                continue
            marks.append((s, end, key))
        self.player.set_markers(marks)

    def _preview_manual(self, key):
        if not self.player.has_video():
            messagebox.showinfo(tr("No video"), tr("Load a video in the preview first."))
            return
        ef, et = self._manual[key]
        s, s_ok = ef.get_seconds()
        e, e_ok = et.get_seconds()
        if not s_ok or not e_ok:
            self.status_var.set(tr("The section's From / To time is invalid."))
            return
        s, end, err = _resolve_rng(key, s, e, self.player.timeline.duration)
        if err or s is None or not end:
            self.status_var.set(tr("That section: {problem}.", problem=tr_key(err)) if err
                                else tr("That section is empty."))
            return
        seg = tr_key(dict(self.MULTI_SEGS).get(key, key))
        self.status_var.set(tr("Previewing {seg} section {start} -> {end}", seg=seg,
                               start=fmt_time(s), end=fmt_time(end)))
        self.player.play_range(s, end)

    def _mark_manual(self, entry):
        sec = self.player.current_seconds()
        if sec is None:
            messagebox.showinfo(tr("No video"), tr("Load a video in the preview first, then scrub."))
            return
        entry.set_seconds(sec)
        entry.flash()
        self._refresh_markers()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _goto_manual(self, entry):
        if not self.player.has_video():
            messagebox.showinfo(tr("No video"), tr("Load a video in the preview first."))
            return
        sec, ok = entry.get_seconds()
        if sec is None or not ok:
            self.status_var.set(tr("Type a valid time in the box first, then Go."))
            return
        self.player.seek_seconds(sec)
        entry.flash()
        self.player.canvas.focus_set()   # keep the Left/Right frame-step keys live

    def _drop_load_sel(self, path):
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            self.sel_var.set(path)
            self.player.load(path)
            self._refresh_markers()

    def _manual_choose_subs(self):
        video = self.sel_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showinfo(tr("No video"), tr("Load a video in the preview first so I can "
                                                   "scan which subtitle languages it contains."))
            return
        self._open_subs_dialog([video])

    def _browse_sel(self):
        path = filedialog.askopenfilename(title=tr("Select video"), filetypes=_VIDEO_TYPES)
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
            messagebox.showerror(tr("Error"), tr("Type or browse to a valid video file first."))

    @staticmethod
    def _rng(key, ef, et, label, total):
        """One Manual section -> (from, to) or None if empty. Same empty-box
        rules as Multi cut and the markers (see _resolve_rng); an empty
        Credits/After-credits To runs to `total` (end of file). The ValueError
        text is translated (it's shown in a message box)."""
        s, s_ok = ef.get_seconds()
        t, t_ok = et.get_seconds()
        seg = tr_key(label)
        if not s_ok:
            raise ValueError(tr("{seg}: invalid From time.", seg=seg))
        if not t_ok:
            raise ValueError(tr("{seg}: invalid To time.", seg=seg))
        s, t, err = _resolve_rng(key, s, t, total)
        if err:
            raise ValueError(tr("{seg}: {problem}.", seg=seg, problem=tr_key(err)))
        if s is None:
            return None
        return (s, t)

    def cut_selected(self):
        video = self.sel_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror(tr("Error"), tr("Load a video in the preview first."))
            return
        if self._refuse_same_folder(self.dirs["Output folder:"].get(), [video]):
            return
        try:
            crf = int(self.crf_var.get())
            crf265 = int(self.crf265_var.get())
            assert 0 <= crf <= 51 and 0 <= crf265 <= 51
            kf = float(self.kf_var.get())
            assert kf >= 0
        except (ValueError, AssertionError):
            messagebox.showerror(tr("Error"), tr("Check the CRF values (0-51) and keyframe "
                                                 "interval (>=0)."))
            return
        total = probe_duration(video)
        if not total:
            messagebox.showerror(tr("Error"), tr("Could not read the video's duration."))
            return
        try:
            drops = []
            for key, label in self.MULTI_SEGS:
                ef, et = self._manual[key]
                rng = self._rng(key, ef, et, label, total)
                if rng:
                    drops.append(rng)
        except ValueError as e:
            messagebox.showerror(tr("Error"), str(e))
            return
        if not drops:
            messagebox.showerror(tr("Error"), tr("Fill at least one section's times first."))
            return
        keep = keep_from_drops(total, drops)
        if not keep:
            messagebox.showerror(tr("Error"), tr("Those ranges leave nothing to keep."))
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
        if self._busy:
            return
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True, [video] if cfg["move_done"] else None)
        if cfg["move_done"] and callable(self.unload_players_hook):
            self.unload_players_hook()      # free the file so its source can move
        jid = self._jid = jobreg.begin(tr("Cut / Edit manual cut"), stop_event=self.stop_event,
                                       tab=self)
        threading.Thread(target=self._manual_worker, args=(cfg, keep, jid),
                         daemon=True).start()

    def _manual_worker(self, cfg, keep, jid=None):
        ok = False
        try:
            res = run_manual(cfg, keep, self, self.stop_event)
            ok = res is not False and not self.stop_event.is_set()
        except Exception as e:
            self.log(f"[FAIL] Unexpected error: {e!r}")
        finally:
            video = cfg["video"]
            jobreg.end(jid, ok=ok, summary=os.path.basename(video))

            def _f():
                self._running(False)
                # move_done may have moved the source to <folder>\done - point
                # the preview box there (or clear it) so Load still works
                if cfg.get("move_done") and not os.path.isfile(video) \
                        and self.sel_var.get().strip().strip('"') == video:
                    moved = os.path.join(os.path.dirname(video) or ".", "done",
                                         os.path.basename(video))
                    self.sel_var.set(moved if os.path.isfile(moved) else "")
            self.after(0, _f)

    def _manual_detect(self):
        if self._busy or not self._engine_ready():
            return
        video = self.sel_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror(tr("Error"), tr("Load a video in the preview first."))
            return
        cfg = self._batch_cfg(check_output=False)
        if cfg is None:
            return
        if self.save_hook:
            self.save_hook()
        self.stop_event.clear()
        self._running(True)
        self.status_var.set(tr("Auto-detecting segments in {name}...",
                               name=os.path.basename(video)))
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        jid = self._jid = jobreg.begin(tr("Cut / Edit auto-detect (manual)"),
                                       stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._manual_detect_worker, args=(video, cfg, jid),
                         daemon=True).start()

    def _manual_detect_worker(self, video, cfg, jid=None):
        res = None
        try:
            self.log(f"[DETECT] matching templates against {os.path.basename(video)}...")
            res = detect_segments(video, cfg, stop_event=self.stop_event, log=self.log)
        except Exception as e:
            self.log(f"[FAIL] auto-detect crashed: {e!r}")
        finally:
            stopped = self.stop_event.is_set()
            found = [tr_key(lbl) for k, lbl in self.MULTI_SEGS if res and _best_ok(res.get(k))]
            jobreg.end(jid, ok=res is not None and not stopped,
                       summary=tr("{name}: found {what}", name=os.path.basename(video),
                                  what=", ".join(found) if found else tr("nothing")))
            self.after(0, lambda: self._manual_detect_done(video, cfg, res, stopped))

    def _manual_detect_done(self, video, cfg, res, stopped):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.bar["value"] = 0
        self._running(False)
        if stopped:
            self.log("[DETECT] stopped - boxes left unchanged.")
            return
        if not isinstance(res, dict):
            self.status_var.set(tr("Auto-detect failed - see the Log."))
            return
        self._manual_cands = res
        self._manual_cands_path = video
        if not _same_file(self.sel_var.get().strip().strip('"'), video):
            self.log("[DETECT] another video was loaded meanwhile - results kept in the "
                     "▾ lists only.")
            return
        use = cfg.get("use", {})
        first = intro_at = None
        filled = []
        self.log(f"[DETECT] {os.path.basename(video)}:")
        if res.get("error"):
            self.log(f"  [FAIL] {res['error']}")
        for key, label in self.MULTI_SEGS:
            ef, et = self._manual[key]
            self._multi_clear_entry(ef)
            self._multi_clear_entry(et)
            if not use.get(key, True):
                self.log(f"  {label}: not searched (unticked on Cut / Edit → Auto-detect)")
                continue
            cands = res.get(key) or []
            for i, c in enumerate(cands, 1):
                self.log(f"    {_cand_label(key, i, c)}")
            best = _best_ok(cands)
            if not best:
                self.log(f"  {label}: " + ("only weak matches - left empty (pick one "
                                            "from ▾ if it's right)" if cands else "no match"))
                continue
            fr, to = float(best["start"]), float(best["end"])
            if key == "intro" and cfg.get("intro_from_start"):
                ef_val = None          # empty From = start of file
            else:
                ef_val = fr
            to_val = None if (key == "credits" and cfg.get("credits_to_end")) else to
            if ef_val is not None:
                ef.set_seconds(ef_val)
            if to_val is not None:
                et.set_seconds(to_val)
            filled.append(key)
            if first is None:
                first = fr
            if key == "intro":
                # where the intro audio really matched (start may be 0:00
                # when 'Intro: cut from file start' is on)
                try:
                    intro_at = float(best.get("match_start", fr))
                except (TypeError, ValueError):
                    intro_at = fr
        if not (self.player.has_video()
                and _same_file(getattr(self.player, "_path", None), video)):
            self.player.load(video)
        self._refresh_markers()
        target = intro_at if intro_at is not None else first
        if target is not None and self.player.has_video():
            self.player.seek_seconds(target, play=False)    # start reviewing at the intro
            self.player.canvas.focus_set()
        names = dict(self.MULTI_SEGS)
        msg = (f"Filled {', '.join(names[k] for k in filled)} - review in the player, nudge "
               "with Set / Snap / arrow keys" if filled
               else "No confident match - try ▾ for weak candidates")
        self.status_var.set(
            tr("Filled {segs} - review in the player, nudge with Set / Snap / arrow keys",
               segs=", ".join(tr_key(names[k]) for k in filled)) if filled
            else tr("No confident match - try ▾ for weak candidates"))
        self.log(f"[DETECT] {msg}")
