"""Templates -> Cut template: Auto-detect the loaded episode's sections
(TemplateAutoMixin).

Two sources, both following the Audio / Visual / Audio + Visual choice of
Templates -> Auto-detect ('Detect by'):
  * the templates you already have (detect_segments) - the log says which
    kinds are already covered, so you only cut a NEW variant when nothing
    matches;
  * season-based: the recurring-segment detection between the loaded episode
    and up to N neighbouring episodes of its folder; the cluster holding the
    loaded episode gives each kind's times. A lone episode gets visual
    credits only (the intro needs >= 2 episodes).
The boxes are filled from the season result (else the matching template),
every candidate is kept for the row's ▾ menu."""
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..engine.detect import detect_segments, load_templates_for
from ..engine.formatting import fmt_time
from ..engine.plexscan import detect_credits_visual
from ..engine.recurring import detect_recurring
from ..i18n import tr
from ..ui.tkthread import _call_tk
from ..ui.tooltips import add_tooltip
from .common import _KIND_NAMES, _list_media, _same_file, mark_edges, opt_kwargs
from .cut_common import _best_ok, _cand_label, tr_key
from .templates_detect import _SENS
from .. import jobs as jobreg

_KINDS = ("preintro", "intro", "credits", "aftercredits")
_DEFAULT_NEIGHBOURS = 4
_CONFIDENCE = 0.32          # the Cut / Edit default 'Min confidence'


def neighbour_episodes(video, n):
    """Up to `n` other media files from `video`'s folder, nearest by name
    (sorted order) first."""
    files = _list_media(os.path.dirname(os.path.abspath(video)))
    key = os.path.normcase(os.path.abspath(video))
    idx = next((i for i, f in enumerate(files)
                if os.path.normcase(os.path.abspath(f)) == key), None)
    others = [(i, f) for i, f in enumerate(files)
              if os.path.normcase(os.path.abspath(f)) != key]
    if idx is None:
        return [f for _i, f in others[:n]]
    others.sort(key=lambda t: (abs(t[0] - idx), t[0] < idx))
    return [f for _i, f in others[:n]]


class TemplateAutoMixin:
    """Cut template sub-tab: 'Auto-detect' button + the per-row ▾ menus."""

    def _tpl_cand_button(self, parent, key):
        mb = ttk.Menubutton(parent, text="▾", width=2)
        menu = tk.Menu(mb, tearoff=False)
        menu.configure(postcommand=lambda: self._tpl_fill_menu(menu, key))
        mb["menu"] = menu
        add_tooltip(mb, tr("Candidates: every {seg} the last Auto-detect found for this "
                           "video (season scan and template matches, best first). Pick one "
                           "to fill this row.", seg=tr_key(_KIND_NAMES[key])))
        return mb

    def _tpl_fill_menu(self, menu, key):
        menu.delete(0, "end")
        video = self.file_var.get().strip().strip('"')
        cands = (self._tpl_cands.get(key) or []) if _same_file(video, self._tpl_cands_path) \
            else []
        if not cands:
            menu.add_command(label=tr("(no {seg} candidates - press Auto-detect first)",
                                      seg=tr_key(_KIND_NAMES[key])), state="disabled")
            return
        for i, c in enumerate(cands, 1):
            menu.add_command(label=_cand_label(key, i, c, ui=True),
                             command=lambda c=c, i=i: self._tpl_pick(key, c, i))

    def _tpl_pick(self, key, c, idx=1, seek=True):
        on, ef, et = self.sections[key]
        on.set(True)
        ef.set_value(float(c["start"]))
        et.set_value(float(c["end"]))      # shows the last frame of the range
        mark_edges(ef, et, c.get("edge_src"))   # ⚠ on an edge placed by sound / a fade
        ef.flash()
        et.flash()
        self._refresh_markers()
        if seek and self.player.has_video():
            self.player.seek_seconds(float(c["start"]), play=False)
            self.player.canvas.focus_set()
        self.log(f"[AUTO] {_cand_label(key, idx, c)} -> {_KIND_NAMES[key]} row")

    # ---------------- Auto-detect ----------------
    def tpl_autodetect(self):
        if self._cutting or self._autocutting or self._tpl_detecting:
            return
        video = self.file_var.get().strip().strip('"')
        if not video or not os.path.isfile(video):
            messagebox.showerror(tr("Error"), tr("Type or browse to a valid video file first."))
            return
        try:
            window = float(self.det_window.get())
            minlens = {"intro": float(self.det_minlen_intro.get()),
                       "credits": float(self.det_minlen_credits.get()),
                       "preintro": float(self.det_minlen_pa.get()),
                       "aftercredits": float(self.det_minlen_pa.get())}
            max_eps = int(self.det_max_eps.get() or 0)
            assert window > 0
        except (ValueError, AssertionError):
            messagebox.showerror(tr("Error"), tr("Search window, min lengths and max episodes must "
                                                 "be numbers."))
            return
        kinds = ["intro", "credits"]
        if self.det_preintro.get():
            kinds.insert(0, "preintro")
        if self.det_aftercredits.get():
            kinds.append("aftercredits")
        opts = {"mode": self.det_mode_var.get(), "lang": self._det_lang(), "window": window,
                "minlens": minlens, "thresh": _SENS.get(self.det_sens.get(), 0.8),
                "neighbours": max_eps - 1 if max_eps > 1 else _DEFAULT_NEIGHBOURS,
                "kinds": kinds, **self._det_margins(),
                # read here (Tk thread): the folders Cut / Edit uses now
                "dirs": dict(self.DIRMAP)}
        if not self.player.has_video() or not _same_file(getattr(self.player, "_path", None),
                                                         video):
            self.player.load(video)
        if callable(self.save_hook):
            self.save_hook()
        # its own stop event: Template cut / Auto-cut have theirs (cut_stop)
        stop = self.tpl_detect_stop = threading.Event()
        self._tpl_detecting = True
        self._sync_buttons()
        self.bar.configure(mode="determinate", maximum=1000)
        self.bar["value"] = 0
        jid = self._jid = jobreg.begin(tr("Cut template auto-detect"), stop_event=stop,
                                       tab=self)
        threading.Thread(target=self._tpl_auto_worker, args=(video, opts, jid, stop),
                         daemon=True).start()

    def _tpl_progress(self, frac, text=""):
        def _a():
            try:
                self.bar["value"] = int(max(0.0, min(1.0, frac)) * 1000)
                if text:
                    self.status_var.set(text)
            except tk.TclError:
                pass
        _call_tk(_a)

    def _tpl_auto_worker(self, video, opts, jid=None, stop=None):
        cands = {k: [] for k in _KINDS}
        crashed = False
        name = os.path.basename(video)
        stop = stop or self.tpl_detect_stop
        dirs = opts.get("dirs") or {}
        try:
            mode = opts["mode"]
            self.log(f"[AUTO] {name}: detecting {', '.join(opts['kinds'])} (by {mode})")
            # 1) the templates you already have
            cfg = {"intro_dir": dirs.get("intro"), "credits_dir": dirs.get("credits"),
                   "preintro_dir": dirs.get("preintro"),
                   "aftercredits_dir": dirs.get("aftercredits"),
                   "confidence": _CONFIDENCE, "match_lang": opts["lang"],
                   "detect_mode": mode,
                   "margin_frames": opts["margin_frames"],
                   "template_margin": opts["template_margin"],
                   "use": {k: k in opts["kinds"] for k in _KINDS}}
            self._tpl_progress(0.02, tr("Loading templates..."))
            tpls = load_templates_for(cfg, log=self.log, stop_event=stop)
            if stop.is_set():
                return
            if any(tpls.values()):
                self._tpl_progress(0.1, tr("Matching the existing templates..."))
                res = detect_segments(
                    video, cfg, stop_event=stop, templates=tpls, log=self.log,
                    **opt_kwargs(detect_segments, progress=lambda f, _t="": self._tpl_progress(
                        0.1 + 0.1 * max(0.0, min(1.0, float(f or 0.0))),
                        tr("Matching the existing templates..."))))
                if stop.is_set():
                    return
                for k in _KINDS:
                    lst = res.get(k) or []
                    for i, c in enumerate(lst, 1):
                        self.log(f"    {_cand_label(k, i, c)}")
                    cands[k].extend(lst)
                    best = _best_ok(lst)
                    if best:
                        self.log(f"[AUTO] {k}: already covered by '{best['template']}' "
                                 f"({best['score']:.2f}) - cut a new template only if "
                                 "that one is wrong for this episode")
                    elif tpls.get(k):
                        self.log(f"[AUTO] {k}: no existing template matches - a new variant")
            else:
                self.log("[AUTO] no templates yet - season-based detection only")
            # 2) season-based: the loaded episode vs. its neighbours
            others = neighbour_episodes(video, opts["neighbours"])
            if others:
                files = [video] + others
                self.log(f"[AUTO] comparing with {len(others)} neighbouring episode(s): "
                         + ", ".join(os.path.basename(f) for f in others))
                cl = detect_recurring(
                    files, mode=mode, kinds=opts["kinds"], window=opts["window"],
                    min_lens=opts["minlens"], thresh=opts["thresh"],
                    progress=lambda f, t="": self._tpl_progress(0.2 + 0.8 * f, t),
                    stop_event=stop, lang=opts["lang"], log=self.log,
                    margin_frames=opts["margin_frames"],
                    template_margin=opts["template_margin"])
                if stop.is_set():
                    return
                key = os.path.normcase(os.path.abspath(video))
                for c in cl:
                    me = next((p for p in c["ranges"]
                               if os.path.normcase(os.path.abspath(p)) == key), None)
                    rng = c["ranges"][me] if me is not None else None
                    if not rng or c["kind"] not in cands:
                        continue
                    cand = {"start": float(rng[0]), "end": float(rng[1]),
                            "score": float(c["score"]),
                            "template": tr("season scan ({n} episodes)", n=c["count"]),
                            "ok": True, "src": c.get("src", "audio"), "season": True,
                            "edge_src": (c.get("edge_src") or {}).get(me)}
                    cands[c["kind"]].insert(0, cand)
                    self.log(f"[AUTO] {c['kind']}: {fmt_time(rng[0])} -> {fmt_time(rng[1])} "
                             f"recurs in {c['count']} episode(s) (by {cand['src']}, "
                             f"score {c['score']:.2f})")
                for k in opts["kinds"]:
                    if not any(x.get("season") for x in cands[k]):
                        self.log(f"[AUTO] {k}: nothing recurring found with the neighbours")
            else:
                self.log("[AUTO] this is the only episode in its folder - the intro needs at "
                         "least 2 episodes; looking for credits pictures only")
                self._tpl_progress(0.5, tr("Looking for credits pictures..."))
                v = detect_credits_visual(video, opts["window"], log=self.log, stop_event=stop,
                                          margin_frames=opts["margin_frames"])
                if v and not stop.is_set():
                    cands["credits"].insert(0, {
                        "start": v[0], "end": v[1], "score": v[2],
                        "template": tr("credits pictures"), "ok": True, "src": "visual",
                        "season": True})
        except Exception as e:
            crashed = True
            self.log(f"[FAIL] auto-detect crashed: {e!r}")
        finally:
            stopped = stop.is_set()
            found = [k for k in _KINDS if any(c.get("ok") for c in cands[k])]
            jobreg.end(jid, ok=not crashed and not stopped,
                       summary=tr("{name}: found {what}", name=name,
                                  what=", ".join(tr_key(_KIND_NAMES[k]) for k in found)
                                  or tr("nothing")))
            _call_tk(lambda: self._tpl_auto_done(video, opts, cands, stopped, crashed))

    def _tpl_auto_done(self, video, opts, cands, stopped, crashed):
        self._tpl_detecting = False
        self._sync_buttons()
        self.bar["value"] = 0
        self.bar.configure(mode="indeterminate")
        if stopped or crashed:
            self.status_var.set(tr("Auto-detect stopped - boxes left unchanged.") if stopped
                                else tr("Auto-detect failed - see the Log."))
            return
        self._tpl_cands = cands
        self._tpl_cands_path = video
        if not _same_file(self.file_var.get().strip().strip('"'), video):
            self.log("[AUTO] another video was loaded meanwhile - results kept in the ▾ "
                     "lists only.")
            return
        filled, starts, cleared = [], {}, []
        for k in _KINDS:
            if k not in opts["kinds"]:
                continue
            lst = cands[k]
            pick = next((c for c in lst if c.get("season")), None) or _best_ok(lst)
            if not pick:
                # searched, nothing found: don't leave the previous episode's
                # times there (ticked, they would be cut as this episode's)
                on, ef, et = self.sections[k]
                on.set(False)
                ef.clear()
                et.clear()
                mark_edges(ef, et, None)
                cleared.append(k)
                continue
            self._tpl_pick(k, pick, lst.index(pick) + 1, seek=False)
            filled.append(k)
            starts[k] = float(pick["start"])
        # start reviewing at the intro (else the first filled section)
        first = starts.get("intro", next(iter(starts.values()), None))
        if first is not None and self.player.has_video():
            self.player.seek_seconds(first, play=False)
            self.player.canvas.focus_set()
        if filled:
            self.status_var.set(tr(
                "Filled {segs} - review in the player, nudge with Set / Snap / arrow keys",
                segs=", ".join(tr_key(_KIND_NAMES[k]) for k in filled)))
        else:
            self.status_var.set(tr("No confident match - try ▾ for weak candidates"))
        if cleared:
            self._refresh_markers()
            self.log(f"[AUTO] nothing found for {', '.join(cleared)} - those rows were cleared "
                     "and unticked.")
        self.log(f"[AUTO] filled: {', '.join(filled) or 'nothing'} - check in the player, "
                 "then 'Cut template(s)'.")
