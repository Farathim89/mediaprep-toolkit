"""The info bars at the top of the main window's content (app.py): missing
ffmpeg / optional packages, an available update (Update now / Release notes /
Later, Help -> Check for updates), how the last update went, a damaged
settings file and settings that can't be saved."""
import importlib.util
import shutil
import tkinter as tk

from .config import APP_NAME, APP_VERSION, PREFS, settings_load_info, settings_save_error
from .i18n import N_, tr
from .ui import themes
from .ui.infobar import InfoBar
from .ui.tkthread import _call_tk
from .window import _open_url
from . import applog, updater


# optional Python packages: (import name, what breaks without it)
_OPTIONAL_PACKAGES = (
    ("tkinterdnd2", N_("drag & drop")),
    ("cv2", N_("video preview")),
    ("PIL", N_("video preview")),
    ("sounddevice", N_("preview sound")),
    ("librosa", N_("intro/credits detection, auto-align")),
    ("numpy", N_("detection")),
    ("scipy", N_("detection")),
)


def _missing_packages():
    """[(name, purpose)] of optional packages that aren't installed (checked
    once at startup without importing them)."""
    out = []
    for name, why in _OPTIONAL_PACKAGES:
        try:
            if importlib.util.find_spec(name) is None:
                out.append((name, why))
        except (ImportError, ValueError):
            out.append((name, why))
    return out


class Banners:
    """The info bars, shown in `master` (a frame at the top of the content
    area). persist / any_busy / on_close: the app's save, "a job runs?" and
    close-the-app functions (the update needs them)."""

    def __init__(self, root, master, persist, any_busy, on_close):
        self.root = root
        self.master = master
        self._persist = persist
        self._any_busy = any_busy
        self._on_close = on_close
        self._banner_w = {"w": None}
        self._save_bar = {"bar": None, "err": None}

    def add(self, text, kind, **kw):
        bar = InfoBar(self.master, text, kind, **kw)
        bar.pack(fill="x", padx=themes.pad(20, 16), pady=themes.pad(10, 0))
        return bar

    def startup_problems(self):
        """Warn up front if ffmpeg/ffprobe are missing (most actions need
        them) and which optional packages aren't installed (checked once)."""
        missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
        missing_pkgs = _missing_packages()
        if missing:
            tools = " and ".join(missing)
            applog.record(f"[startup] {tools} not found on PATH - install ffmpeg (ffmpeg.org) "
                          "so cutting and detection work.")
            self.add(tr("{tools} not found on PATH - install ffmpeg (ffmpeg.org) so cutting and "
                        "detection work.", tools=" / ".join(missing)), "error")
        if missing_pkgs:
            names = ", ".join(n for n, _w in missing_pkgs)
            whats = ", ".join(dict.fromkeys(w for _n, w in missing_pkgs))
            applog.record(f"[startup] Optional packages missing: {names} ({whats} won't work) - "
                          "run Install Requirements.bat.")
            whats = ", ".join(dict.fromkeys(tr(w) for _n, w in missing_pkgs))
            self.add(tr("Optional packages missing: {names} ({features} won't work) - run "
                        "Install Requirements.bat.", names=names, features=whats), "warn")

    # ---- update info bar (shown only when a newer release exists) ----
    def close_update(self):
        if self._banner_w["w"] is not None:
            try:
                self._banner_w["w"].destroy()
            except tk.TclError:
                pass
            self._banner_w["w"] = None

    def update_now(self):
        """'Update now': download + install the latest release (ui/update_dialog)."""
        from tkinter import messagebox as _mbox
        from .ui.update_dialog import start_update
        try:
            start_update(self.root, updater.latest_release(), self._persist, self._any_busy,
                         self._on_close)
        except Exception as exc:          # never let a failed update take the app down
            applog.record(f"[update] failed: {exc!r}")
            _mbox.showerror(tr("Update"), tr("The update failed: {error}", error=exc),
                            parent=self.root)

    def show_update(self, ver, url):
        self.close_update()
        self._banner_w["w"] = self.add(
            tr("{app} {version} is available (you have {old}).", app=APP_NAME, version=ver,
               old=APP_VERSION),
            "info", links=[(tr("Update now"), self.update_now),
                           (tr("Release notes"), lambda: _open_url(url)),
                           (tr("Later"), self.close_update)])
        if updater.auto_flag():           # test hook (MEDIAPREP_UPDATE_AUTO)
            self.root.after(500, self.update_now)

    def check_for_updates(self, parent=None):
        """Help -> Check for updates: a forced check with a reply either way."""
        from tkinter import messagebox as _mbox
        root = self.root

        def done(text):
            rel = updater.latest_release()
            par = parent if parent is not None and parent.winfo_exists() else root
            if rel and updater.is_newer(rel["version"]):
                if _mbox.askyesno(tr("Check for updates"), tr(
                        "{app} {version} is available (you have {old}).\n\nUpdate now?",
                        app=APP_NAME, version=rel["version"], old=APP_VERSION), parent=par):
                    self.update_now()
            else:
                _mbox.showinfo(tr("Check for updates"), text, parent=par)
        updater.check_async(PREFS,
                            on_newer=lambda v, url: _call_tk(lambda: self.show_update(v, url)),
                            on_done=lambda text: _call_tk(lambda: done(text)), force=True,
                            save=lambda: _call_tk(self._persist))

    def last_update(self):
        """The previous run started an update: say how it went (a portable
        update replaces the exe in place - there is no old copy to clean up)."""
        _pend = updater.take_pending()
        if _pend:
            if _pend.get("done"):
                applog.record(f"[update] updated from {_pend.get('from')} to {APP_VERSION}")
                self.add(tr("Updated to version {version}.", version=APP_VERSION), "info")
            else:
                applog.record(f"[update] the update to {_pend.get('to')} did not finish "
                              f"(still {APP_VERSION})")
                msg = tr("The update to version {new} did not finish - you still have "
                         "{old}.", new=_pend.get("to"), old=APP_VERSION)
                if _pend.get("error"):          # the installer's LastSetupError
                    msg += " " + tr("The installer reported: {error}",
                                    error=str(_pend["error"]).strip())
                self.add(msg, "warn")

    def settings_damaged(self):
        """The settings file was damaged at startup (config.py restored the
        last good save, or had to start from the defaults) - say so once."""
        _load = settings_load_info()
        if _load:
            kept = _load.get("kept") or ""
            if _load.get("kind") == "restored":
                msg = tr("The settings file was damaged and has been restored from the last "
                         "good save - recent changes may be missing.")
            elif _load.get("kind") == "reset":
                msg = tr("The settings file was damaged and there was no usable backup - "
                         "the app started with the default settings.")
            else:
                msg = tr("The settings file could not be read ({error}) - the default "
                         "settings are used this time.", error=_load.get("error", ""))
            if kept:
                msg += " " + tr("The damaged file was kept as {file} in the Data folder.",
                                file=kept)
            self.add(msg, "warn")

    def check_save_error(self):
        """A save that fails (e.g. the Data folder is read-only): one banner
        per new error, closed again once saving works."""
        err = settings_save_error()
        if err == self._save_bar["err"]:
            return
        self._save_bar["err"] = err
        if self._save_bar["bar"] is not None:
            try:
                self._save_bar["bar"].destroy()
            except tk.TclError:
                pass
            self._save_bar["bar"] = None
        if err:
            # "PermissionError: [WinError 5] Access is denied: '<tmp>' -> '<file>'"
            # - the reason is enough here (the paths are in the log)
            short = err.split(": '")[0].split(': "')[0]
            self._save_bar["bar"] = self.add(
                tr("The settings could not be saved ({error}) - changes are lost when the "
                   "app closes. Check that the Data folder is not read-only.", error=short),
                "error")
