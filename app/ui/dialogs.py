"""App-level dialogs: the job Queue window and Settings (incl. the language
picker and the theme picker, which shares themes.theme_var() with the top
bar)."""
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import APP_VERSION, PREFS
from ..i18n import tr
from . import themes
from .tkthread import _call_tk
from .widgets import KeyedCombobox, add_tooltip
from .. import i18n, jobs, notify, updater
from . import icons


def _fmt_elapsed(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class QueueDialog(tk.Toplevel):
    """Small window listing running + queued jobs: remove / reorder / clear,
    pause/resume the queue and Stop all. One instance (see open_)."""
    _inst = None

    @classmethod
    def open_(cls, root, stop_all):
        if cls._inst is not None:
            try:
                if cls._inst.winfo_exists():
                    cls._inst.deiconify()
                    cls._inst.lift()
                    return cls._inst
            except tk.TclError:
                pass
        cls._inst = cls(root, stop_all)
        return cls._inst

    def __init__(self, root, stop_all):
        super().__init__(root)
        self.title(tr("Job queue"))
        self.transient(root)
        self._stop_all = stop_all
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        self.run_lbl = ttk.Label(f, text="", justify="left", wraplength=520)
        self.run_lbl.pack(anchor="w", fill="x")
        ttk.Label(f, text=tr("Queued (run one after another when nothing else runs):"),
                  style="Hint.TLabel", wraplength=520).pack(anchor="w", pady=(8, 2))
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        self.lb = tk.Listbox(mid, height=8, width=60, activestyle="none",
                             exportselection=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        side = ttk.Frame(mid)
        side.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(side, text="▲ " + tr("Up"), command=lambda: self._move(-1)).pack(fill="x")
        ttk.Button(side, text="▼ " + tr("Down"),
                   command=lambda: self._move(1)).pack(fill="x", pady=2)
        icons.decorate(ttk.Button(side, text=tr("Remove"), command=self._remove), "remove").pack(fill="x")
        icons.decorate(ttk.Button(side, text=tr("Clear"), command=jobs.clear), "trash").pack(fill="x", pady=2)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 0))
        self.pause_btn = icons.decorate(ttk.Button(bar, text=tr("Pause queue"), command=self._toggle_pause), "pause")
        self.pause_btn.pack(side="left")
        stop = icons.decorate(ttk.Button(bar, text=tr("Stop all"), command=self._stop), "stop")
        stop.pack(side="left", padx=6)
        add_tooltip(stop, tr("Stop every running job and pause the queue"))
        ttk.Button(bar, text=tr("Close"), command=self.destroy).pack(side="right")
        self.lb.bind("<Delete>", lambda e: self._remove())
        self.bind("<Escape>", lambda e: self.destroy())
        themes.recolor(self)
        jobs.subscribe(self._on_event)
        self.bind("<Destroy>", self._gone, add="+")
        self._refresh()
        self._tick()

    def _gone(self, e):
        if e.widget is self:
            jobs.unsubscribe(self._on_event)
            if QueueDialog._inst is self:
                QueueDialog._inst = None

    def _on_event(self, _event, _info):
        self._refresh()

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._refresh(list_too=False)
        self.after(1000, self._tick)

    def _refresh(self, list_too=True):
        try:
            run = jobs.running()
            if run:
                txt = tr("Running:") + "\n" + "\n".join(
                    f"  ▶ {j['name']}  ({_fmt_elapsed(j['elapsed'])})" for j in run)
            else:
                txt = tr("Nothing registered as running.")
            if jobs.is_paused():
                txt += "\n" + tr("Queue is PAUSED.")
            self.run_lbl.configure(text=txt)
            self.pause_btn.configure(
                text=tr("Resume queue") if jobs.is_paused() else tr("Pause queue"))
            icons.set_icon(self.pause_btn, "play" if jobs.is_paused() else "pause")
            if list_too:
                sel = self.lb.curselection()
                self.lb.delete(0, "end")
                for i, q in enumerate(jobs.queued(), 1):
                    self.lb.insert("end", f"{i}. {q['name']}")
                if sel and sel[0] < self.lb.size():
                    self.lb.selection_set(sel[0])
        except tk.TclError:
            pass

    def _sel(self):
        s = self.lb.curselection()
        return s[0] if s else None

    def _move(self, d):
        i = self._sel()
        if i is not None and jobs.move(i, d):
            self.lb.selection_clear(0, "end")
            self.after_idle(lambda: self.lb.selection_set(i + d))

    def _remove(self):
        i = self._sel()
        if i is not None:
            jobs.remove(i)

    def _toggle_pause(self):
        if jobs.is_paused():
            jobs.resume()
        else:
            jobs.pause()

    def _stop(self):
        self._stop_all()
        self._refresh()


class SettingsDialog(tk.Toplevel):
    """App-wide settings: language, theme, notifications, update check, log
    retention."""
    _inst = None
    WRAP = 420                    # wrap width of the longer texts (px)

    @classmethod
    def open_(cls, root, persist, on_update, restart=None):
        if cls._inst is not None:
            try:
                if cls._inst.winfo_exists():
                    cls._inst.lift()
                    return cls._inst
            except tk.TclError:
                pass
        cls._inst = cls(root, persist, on_update, restart)
        return cls._inst

    def __init__(self, root, persist, on_update, restart=None):
        super().__init__(root)
        self.title(tr("Settings"))
        self.transient(root)
        self.resizable(False, False)
        self._app_root = root
        self._persist = persist
        self._on_update = on_update
        self._restart = restart
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        th = ttk.LabelFrame(f, text=tr("Appearance"))
        th.pack(fill="x")
        th.columnconfigure(1, weight=1)
        ttk.Label(th, text=tr("Language:")).grid(row=0, column=0, sticky="w")
        win_lang = i18n.windows_language() or i18n.DEFAULT
        codes = [i18n.AUTO] + [c for c, _n in i18n.available_languages()]
        labels = [tr("Automatic ({language})", language=i18n.native_name(win_lang))]
        labels += [name for _c, name in i18n.available_languages()]
        cur = PREFS.get("language", i18n.AUTO)
        self.v_lang = tk.StringVar(value=cur if cur in codes else i18n.AUTO)
        lbox = KeyedCombobox(th, textvariable=self.v_lang, values=codes, labels=labels,
                             state="readonly", width=24)
        lbox.grid(row=0, column=1, sticky="w", padx=6)
        add_tooltip(lbox, tr("The language of the app. 'Automatic' uses the Windows display "
                             "language. A change takes effect after a restart."))
        ttk.Label(th, text=tr("Theme:")).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tbox = KeyedCombobox(th, textvariable=themes.theme_var(), values=themes.THEME_NAMES,
                             state="readonly", width=24)
        tbox.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        add_tooltip(tbox, tr("Applies right away to every window and is remembered. "
                             "'Follow Windows' switches Light / Dark with the Windows "
                             "app mode."))

        n = ttk.LabelFrame(f, text=tr("Notifications when a job finishes"))
        n.pack(fill="x", pady=(10, 0))
        self.v_on = tk.BooleanVar(value=PREFS["notify_on"])
        self.v_toast = tk.BooleanVar(value=PREFS["notify_toast"])
        self.v_sound = tk.BooleanVar(value=PREFS["notify_sound"])
        self.v_min = tk.StringVar(value=f"{PREFS['notify_min_minutes']:g}")
        wrap = self.WRAP
        ttk.Checkbutton(n, text=tr("Notify when a job finishes"), variable=self.v_on).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(n, text=tr("Windows notification (else a popup in the corner)"),
                        variable=self.v_toast).grid(row=1, column=0, columnspan=3,
                                                    sticky="w", padx=(18, 0))
        ttk.Checkbutton(n, text=tr("Play a sound"), variable=self.v_sound).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=(18, 0))
        # "Only for jobs longer than [ 1 ] minutes" - one row, the spinbox in
        # its own column so a long translation can't push it off the dialog
        ttk.Label(n, text=tr("Only for jobs longer than"), wraplength=wrap - 140).grid(
            row=3, column=0, sticky="w", padx=(18, 0), pady=(4, 0))
        ttk.Spinbox(n, from_=0, to=600, increment=1, width=6,
                    textvariable=self.v_min).grid(row=3, column=1, padx=4, pady=(4, 0))
        ttk.Label(n, text=tr("minutes")).grid(row=3, column=2, sticky="w", pady=(4, 0))
        icons.decorate(ttk.Button(n, text=tr("Test notification"), command=notify.test_notification), "info").grid(
            row=4, column=0, sticky="w", padx=(18, 0), pady=(6, 0))

        u = ttk.LabelFrame(f, text=tr("Updates"))
        u.pack(fill="x", pady=(10, 0))
        self.v_upd = tk.BooleanVar(value=PREFS["update_check"])
        ttk.Checkbutton(u, text=tr("Check for a new version at startup (once a day)"),
                        variable=self.v_upd).grid(row=0, column=0, columnspan=2, sticky="w")
        icons.decorate(ttk.Button(u, text=tr("Check now"), command=self._check_now), "refresh").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.upd_lbl = ttk.Label(u, text=tr("Installed: v{version}", version=APP_VERSION),
                                 style="Hint.TLabel", wraplength=wrap - 120)
        self.upd_lbl.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))

        lg = ttk.LabelFrame(f, text=tr("Logs"))
        lg.pack(fill="x", pady=(10, 0))
        self.v_days = tk.StringVar(value=str(PREFS["log_keep_days"]))
        ttk.Label(lg, text=tr("Move session logs older than")).grid(row=0, column=0,
                                                                   sticky="w")
        ttk.Spinbox(lg, from_=1, to=3650, width=6, textvariable=self.v_days).grid(
            row=0, column=1, padx=4, sticky="w")
        # the second half on its own line (long in some languages)
        ttk.Label(lg, text=tr("days to Data\\temp\\trash (at startup)"),
                  wraplength=wrap).grid(row=1, column=0, columnspan=2, sticky="w",
                                        pady=(2, 0))

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text=tr("Cancel"), command=self.destroy).pack(side="right")
        ttk.Button(bar, style="Accent.TButton", text=tr("OK"), command=self._ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", self._gone, add="+")
        themes.recolor(self)

    def _gone(self, e):
        if e.widget is self and SettingsDialog._inst is self:
            SettingsDialog._inst = None

    def _check_now(self):
        self.upd_lbl.configure(text=tr("Checking..."))

        def show(text):
            try:
                if self.upd_lbl.winfo_exists():
                    self.upd_lbl.configure(text=text)
            except tk.TclError:
                pass
        updater.check_async(
            PREFS, on_newer=lambda v, url: _call_tk(lambda: self._on_update(v, url)),
            on_done=lambda text: _call_tk(lambda: show(text)), force=True,
            save=lambda: _call_tk(self._persist))

    def _ok(self):
        PREFS["notify_on"] = bool(self.v_on.get())
        PREFS["notify_toast"] = bool(self.v_toast.get())
        PREFS["notify_sound"] = bool(self.v_sound.get())
        PREFS["update_check"] = bool(self.v_upd.get())
        try:
            PREFS["notify_min_minutes"] = max(0.0, float(self.v_min.get().replace(",", ".")))
        except ValueError:
            pass
        try:
            PREFS["log_keep_days"] = max(1, int(self.v_days.get()))
        except ValueError:
            pass
        new_lang = self.v_lang.get()
        lang_changed = new_lang != PREFS.get("language", i18n.AUTO)
        PREFS["language"] = new_lang
        try:
            self._persist()
        except Exception:
            pass
        self.destroy()
        if lang_changed:
            self._offer_restart(new_lang)

    def _offer_restart(self, code):
        """The language is applied at startup - offer to restart right away."""
        name = (i18n.native_name(i18n.resolve(code)))
        if self._restart is None:
            messagebox.showinfo(tr("Language"), tr(
                "The language is set to {language}. It takes effect the next time "
                "the app starts.", language=name), parent=self._app_root)
            return
        if messagebox.askyesno(tr("Restart now?"), tr(
                "The language is set to {language}. It takes effect after a restart.\n\n"
                "Restart the app now?", language=name), parent=self._app_root):
            self._restart()
