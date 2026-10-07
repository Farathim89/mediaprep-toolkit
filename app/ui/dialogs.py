"""App-level dialogs: the job Queue window and Settings (incl. the language
picker and the theme picker, which shares themes.theme_var() with the top
bar)."""
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import APP_VERSION, CPU_THREAD_CHOICES, PREFS
from ..i18n import tr
from . import bgmode, themes
from .tkthread import _call_tk
from .widgets import KeyedCombobox, add_tooltip
from .. import i18n, jobs, notify, updater
from . import icons


# ---------------------------------------------------------------- placing
def owner_window(parent=None):
    """The toplevel a dialog belongs to: `parent`'s toplevel when that is on
    screen, else the window that has the focus / a grab, else the main
    window. (A dialog made transient for a withdrawn or minimised window is
    never shown by Windows - it would wait forever, invisible.)"""
    root = tk._default_root
    cands = []
    if parent is not None:
        try:
            cands.append(parent.winfo_toplevel())
        except tk.TclError:
            pass
    if root is not None:
        try:
            f = root.focus_get()
            if f is not None:
                cands.append(f.winfo_toplevel())
        except (tk.TclError, KeyError):
            pass
        try:
            g = root.grab_current()
            if g is not None:
                cands.append(g.winfo_toplevel())
        except (tk.TclError, KeyError):
            pass
        cands.append(root)
    for w in cands:
        try:
            if w.winfo_exists() and w.winfo_viewable() and w.state() in ("normal", "zoomed"):
                return w
        except tk.TclError:
            continue
    return root


def _work_area(win):
    """(left, top, right, bottom) of the work area of the monitor `win` is
    on (multi-monitor aware; falls back to the screen size)."""
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        hwnd = int(win.wm_frame(), 16)
        mon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)   # nearest
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()


def place_dialog(dlg, parent=None, modal=False, focus=None):
    """Show a dialog Toplevel properly: transient for an on-screen owner,
    themed before it is first drawn, centred on the owner (kept on the
    owner's monitor), raised and focused; modal=True also grabs the input
    once it is viewable (a grab on a not-yet-mapped window fails). Call it
    after the dialog's widgets are built."""
    owner = owner_window(parent)
    try:
        shown = bool(dlg.winfo_ismapped())     # re-centring after a re-layout
        if not shown:
            dlg.withdraw()
        if owner is not None and owner is not dlg:
            dlg.transient(owner)
        themes.recolor(dlg)
        dlg.update_idletasks()
        w, h = max(dlg.winfo_reqwidth(), 120), max(dlg.winfo_reqheight(), 60)
        if owner is not None:
            x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
            y = owner.winfo_rooty() + max(0, (owner.winfo_height() - h) // 3)
            left, top, right, bottom = _work_area(owner)
        else:
            left, top = 0, 0
            right, bottom = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
            x, y = (right - w) // 2, (bottom - h) // 3
        # keep it - title bar included - on the owner's monitor
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h - themes.px(32)))
        dlg.geometry(f"+{x}+{y}")
        if bgmode.enabled():                   # scripted test run: never steal focus
            bgmode.apply(dlg)
            bgmode.place_offscreen(dlg)
        if not shown:
            dlg.deiconify()
        dlg.lift()
    except tk.TclError:
        return dlg

    def take(tries=40):
        try:
            if not dlg.winfo_exists():
                return
            if not dlg.winfo_viewable():
                if tries:
                    dlg.after(25, take, tries - 1)
                return
            if modal:
                try:
                    dlg.grab_set()
                except tk.TclError:
                    pass
            if bgmode.enabled():
                (focus if focus is not None else dlg).focus_set()   # no window activation
                bgmode.give_back_focus(dlg)
            else:
                (focus if focus is not None else dlg).focus_force()
        except tk.TclError:
            pass
    try:
        dlg.update_idletasks()
    except tk.TclError:
        pass
    take()
    return dlg


def _modal_run(dlg, parent):
    """Wait until `dlg` is closed (returns at once if it already is)."""
    try:
        (owner_window(parent) or dlg).wait_window(dlg)
    except tk.TclError:
        pass


def _dialog_buttons(dlg, row, choices, default, pick):
    btns = {}
    for key, text in choices:
        b = ttk.Button(row, text=text, command=lambda k=key: pick(k),
                       style="Accent.TButton" if key == default else "TButton")
        b.pack(side="left", padx=(themes.px(6), 0))
        btns[key] = b

    def on_return(_e=None):
        f = dlg.focus_get()
        for k, b in btns.items():
            if f is b:
                pick(k)
                return "break"
        if default is not None:
            pick(default)
        return "break"
    dlg.bind("<Return>", on_return)
    dlg.bind("<KP_Enter>", on_return)
    return btns


def ask_choice(parent, title, message, choices, default=None, cancel=None):
    """Modal dialog with one button per choice; returns the chosen key, or
    `cancel` (default None) when closed / Escape. choices = [(key, text)].
    Enter = the focused button, else `default` (default: the first choice);
    the default button is the accent one and has the focus."""
    if default is None and choices:
        default = choices[0][0]
    dlg = tk.Toplevel(parent)
    dlg.withdraw()
    dlg.title(title)
    dlg.resizable(False, False)
    out = {"v": cancel}
    body = ttk.Frame(dlg, padding=themes.pad(18, 16, 18, 14))
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, wraplength=themes.px(440), justify="left").pack(
        anchor="w")
    row = ttk.Frame(body)
    row.pack(anchor="e", pady=themes.pad(16, 0))

    def pick(v):
        out["v"] = v
        dlg.destroy()
    btns = _dialog_buttons(dlg, row, choices, default, pick)
    dlg.protocol("WM_DELETE_WINDOW", lambda: pick(cancel))
    dlg.bind("<Escape>", lambda e: pick(cancel))
    place_dialog(dlg, parent, modal=True, focus=btns.get(default))
    _modal_run(dlg, parent)
    return out["v"]


def ask_string(parent, title, prompt, initialvalue=""):
    """Themed replacement of simpledialog.askstring: returns the text, or
    None when cancelled (Escape / Cancel / closed). Enter = OK."""
    dlg = tk.Toplevel(parent)
    dlg.withdraw()
    dlg.title(title)
    dlg.resizable(False, False)
    out = {"v": None}
    body = ttk.Frame(dlg, padding=themes.pad(18, 16, 18, 14))
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=prompt, wraplength=themes.px(420), justify="left").pack(anchor="w")
    var = tk.StringVar(master=dlg, value=initialvalue or "")
    ent = ttk.Entry(body, textvariable=var, width=44)
    ent.pack(fill="x", pady=themes.pad(8, 0))
    ent.select_range(0, "end")
    ent.icursor("end")
    row = ttk.Frame(body)
    row.pack(anchor="e", pady=themes.pad(16, 0))

    def pick(v):
        out["v"] = var.get() if v == "ok" else None
        dlg.destroy()
    _dialog_buttons(dlg, row, [("ok", tr("OK")), ("cancel", tr("Cancel"))], "ok", pick)
    dlg.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
    dlg.bind("<Escape>", lambda e: pick("cancel"))
    place_dialog(dlg, parent, modal=True, focus=ent)
    _modal_run(dlg, parent)
    return out["v"]


def install_default_parent(root):
    """Message boxes / file dialogs opened without parent= belong to the
    main window by default - also while a modal dialog (Clean up, a choice
    dialog ...) has the input, so the prompt could open behind it. Default
    their parent to the active window instead."""
    from tkinter import commondialog
    if getattr(commondialog.Dialog, "_mp_parent", False):
        return
    orig = commondialog.Dialog.__init__

    def __init__(self, master=None, **options):
        if master is None and options.get("parent") is None:
            try:
                options["parent"] = owner_window(None)
            except Exception:
                pass
        orig(self, master, **options)
    commondialog.Dialog.__init__ = __init__
    commondialog.Dialog._mp_parent = True


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
                    cls._inst.focus_force()
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
        jobs.subscribe(self._on_event)
        self.bind("<Destroy>", self._gone, add="+")
        self._refresh()
        self._tick()
        place_dialog(self, root)

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
                    cls._inst.deiconify()
                    cls._inst.lift()
                    cls._inst.focus_force()
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

        # video preview engine (ui/player.py): Auto / mpv / OpenCV
        vp = ttk.LabelFrame(f, text=tr("Video player"))
        vp.pack(fill="x", pady=(10, 0))
        vp.columnconfigure(1, weight=1)
        ttk.Label(vp, text=tr("Video player engine:")).grid(row=0, column=0, sticky="w")
        cur = str(PREFS.get("player_engine", "auto")).lower()
        self.v_engine = tk.StringVar(value=cur if cur in ("auto", "mpv", "opencv") else "auto")
        ebox = KeyedCombobox(vp, textvariable=self.v_engine, values=["auto", "mpv", "opencv"],
                             labels=[tr("Auto (mpv if available)"), "mpv", "OpenCV"],
                             state="readonly", width=24)
        ebox.grid(row=0, column=1, sticky="w", padx=6)
        add_tooltip(ebox, tr("mpv plays smoothly and steps frames fast using the graphics "
                             "card, with its own sound. OpenCV is the simple built-in "
                             "preview. Auto uses mpv when it is found and starts, else "
                             "OpenCV. Applies to videos loaded after the change."))
        from . import mpvplayer
        exe = mpvplayer.find_mpv()
        ttk.Label(vp, text=(tr("mpv found: {path}", path=exe) if exe else
                            tr("mpv not found - the OpenCV preview is used")),
                  style="Hint.TLabel", wraplength=self.WRAP).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Limit CPU use: below-normal priority + thread caps for ffmpeg / mpv
        # (config.popen_flags / limit_cmd; the math-library cap at startup)
        pf = ttk.LabelFrame(f, text=tr("Performance"))
        pf.pack(fill="x", pady=(10, 0))
        self.v_cpu = tk.BooleanVar(value=bool(PREFS.get("cpu_limit", True)))
        cur = str(PREFS.get("cpu_threads", "auto")).lower()
        self.v_threads = tk.StringVar(value=cur if cur in CPU_THREAD_CHOICES else "auto")
        cpu_cb = ttk.Checkbutton(pf, text=tr("Limit CPU use (recommended)"),
                                 variable=self.v_cpu)
        cpu_cb.grid(row=0, column=0, columnspan=2, sticky="w")
        add_tooltip(cpu_cb, tr("Runs ffmpeg and the video player at below-normal priority "
                               "and caps how many threads encodes and decodes use, so the "
                               "PC stays responsive and cooler during long jobs. Encodes "
                               "can take a little longer. GPU (NVENC) encodes are not "
                               "slowed. Applies to the next job; the analysis libraries "
                               "pick it up after a restart."))
        ttk.Label(pf, text=tr("Max encoder threads:")).grid(row=1, column=0, sticky="w",
                                                            padx=(18, 0), pady=(4, 0))
        thr_labels = [tr("Auto (half the cores, max 8)")] + list(CPU_THREAD_CHOICES[1:-1])             + [tr("All")]
        self.thr_box = KeyedCombobox(pf, textvariable=self.v_threads,
                                     values=list(CPU_THREAD_CHOICES), labels=thr_labels,
                                     state="readonly", width=24)
        self.thr_box.grid(row=1, column=1, sticky="w", padx=6, pady=(4, 0))
        add_tooltip(self.thr_box, tr("The most CPU threads one ffmpeg job may use. 'All' "
                                     "lets ffmpeg decide (only the lower priority stays)."))

        def _cpu_toggle(*_a):
            self.thr_box.configure(state="readonly" if self.v_cpu.get() else "disabled")
        self.v_cpu.trace_add("write", _cpu_toggle)
        _cpu_toggle()

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
        place_dialog(self, root)

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
        PREFS["player_engine"] = self.v_engine.get() or "auto"
        PREFS["cpu_limit"] = bool(self.v_cpu.get())
        PREFS["cpu_threads"] = self.v_threads.get() or "auto"
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
