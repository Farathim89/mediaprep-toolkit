"""In-app update: the "Update now" flow and its download dialog.

start_update() picks the release file for this copy (updater.install_type),
asks only when something is unusual (no checksum, a job running), downloads
it with a progress dialog (Cancel), checks its SHA-256 and then:
  installed -> starts Setup.exe /S /RUN /D=<this folder> elevated, closes
  portable  -> replaces the portable exe the user started IN PLACE (same
               name and folder; the previous one is kept as <exe>.old until
               the next start), starts it again, closes
  zip/source -> explains and opens the release page.
Only one update runs at a time: "Update now" again brings the running
download dialog to the front (or says that the update waits for a job).
"Update when the job has finished" pauses the queue; any end other than the
app closing for the update (Cancel, a failed download / install, UAC
declined) resumes it again (unless it had been paused before).
The UI-free parts live in app/updater.py."""
import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import applog, config as C, jobs, updater
from ..i18n import tr
from .dialogfit import ScrollBody
from .dialogs import place_dialog
from .tkthread import _call_tk

# the update in progress: {"dialog": UpdateDialog | None, "waiting": bool}
_STATE = {"dialog": None, "waiting": False}


def _busy_update(root):
    """True (and the user told / the dialog shown) when an update is already
    running or waiting for a job."""
    dlg = _STATE["dialog"]
    if dlg is not None:
        try:
            if dlg.winfo_exists():
                dlg.deiconify()
                dlg.lift()
                dlg.focus_force()
                return True
        except tk.TclError:
            pass
        _STATE["dialog"] = None
    if _STATE["waiting"]:
        messagebox.showinfo(tr("Update"), tr(
            "The update starts automatically when the current job has finished."),
            parent=root)
        return True
    return False


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _mb(n):
    return f"{n / 2**20:.1f}"


class UpdateDialog(tk.Toplevel):
    """Download progress + Cancel; runs the download in a thread and calls
    on_done(path) / on_fail(message) on the Tk thread."""

    def __init__(self, root, asset, version, verify, on_done, on_abort=None):
        super().__init__(root)
        _STATE["dialog"] = self
        self.on_abort = on_abort         # called once when it ends without updating
        self.title(tr("Update {app}", app=C.APP_NAME))
        self.resizable(False, False)
        self.asset, self.on_done = asset, on_done
        self.cancel = threading.Event()
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        # (scrolls only on a screen too small for it - ui/dialogfit.py; the
        # Cancel button stays visible below it)
        body = ScrollBody(f)
        c = body.interior
        ttk.Label(c, text=tr("Downloading {app} {version}...", app=C.APP_NAME,
                             version=version), style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(c, text=asset["name"], style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        self.bar = ttk.Progressbar(c, mode="determinate", maximum=1000, length=420)
        self.bar.pack(fill="x")
        self.status = tk.StringVar(value=tr("Connecting..."))
        ttk.Label(c, textvariable=self.status, style="Hint.TLabel").pack(anchor="w",
                                                                          pady=(6, 0))
        bar = ttk.Frame(f)
        bar.pack(side="bottom", fill="x", pady=(12, 0))
        body.pack(fill="both", expand=True)
        self.cancel_btn = ttk.Button(bar, text=tr("Cancel"), command=self._cancel)
        self.cancel_btn.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        place_dialog(self, root)
        threading.Thread(target=self._work, args=(verify,), daemon=True,
                         name="update-download").start()

    def _cancel(self):
        self.cancel.set()
        self.status.set(tr("Cancelling..."))

    def _progress(self, done, total):
        def ui():
            try:
                if total:
                    self.bar.configure(value=int(1000 * done / total))
                    self.status.set(tr("{done} of {total} MB", done=_mb(done),
                                       total=_mb(total)))
                else:
                    self.status.set(tr("{done} MB", done=_mb(done)))
            except tk.TclError:
                pass
        _call_tk(ui)

    def _work(self, verify):
        try:
            path = updater.download(self.asset, progress=self._progress, cancel=self.cancel,
                                    verify=verify)
        except updater.Cancelled:
            applog.record("[update] download cancelled")
            _call_tk(self._cancelled)
            return
        except updater.UpdateError as exc:
            msg = str(exc)
            applog.record(f"[update] {msg}")
            _call_tk(lambda: self._fail(msg))
            return
        except Exception as exc:          # never leave the dialog hanging
            msg = tr("The download failed: {error}", error=exc)
            applog.record(f"[update] {msg}")
            _call_tk(lambda: self._fail(msg))
            return
        applog.record(f"[update] downloaded {self.asset['name']}"
                      + (" (SHA-256 verified)" if verify else " (NOT verified)"))
        _call_tk(lambda: self._finish(path))

    def _close(self):
        if _STATE["dialog"] is self:
            _STATE["dialog"] = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _aborted(self):
        cb, self.on_abort = self.on_abort, None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _cancelled(self):
        self._close()
        self._aborted()

    def _fail(self, msg):
        self._close()
        self._aborted()
        messagebox.showerror(tr("Update"), msg)

    def _finish(self, path):
        try:
            self.status.set(tr("Starting the update..."))
            self.cancel_btn.state(["disabled"])
            self.update_idletasks()
        except tk.TclError:
            pass
        try:
            self.on_done(path)
        except updater.UpdateError as exc:
            applog.record(f"[update] {exc}")
            self._fail(str(exc))
            return
        except Exception as exc:          # never leave the dialog hanging
            applog.record(f"[update] failed: {exc!r}")
            self._fail(tr("The update failed: {error}", error=exc))
            return
        self.on_abort = None              # the app is closing for the update
        self._close()


def start_update(root, release, persist, is_busy, close_app):
    """The "Update now" button. release = updater.latest_release()."""
    if _busy_update(root):
        return
    if not release:
        _open_url(updater.RELEASES_URL)
        return
    ver, page = release["version"], release["url"]
    kind = updater.install_type()
    applog.record(f"[update] update to {ver} requested ({kind} copy)")
    if kind == "source":
        messagebox.showinfo(tr("Update"), tr(
            "This copy runs from the Python source. Update it with 'git pull' or download "
            "the new version from the release page (it opens now)."))
        _open_url(page)
        return
    if kind == "zip":
        messagebox.showinfo(tr("Update"), tr(
            "This copy was unpacked from a zip. To update it: close the app, download "
            "{name} from the release page (it opens now) and extract it over this "
            "folder. Your Media and Data folders are not in the zip and stay as they "
            "are.", name=updater.asset_name("zip", ver)))
        _open_url(page)
        return
    asset = updater.pick_asset(release, kind)
    if asset is None:
        messagebox.showinfo(tr("Update"), tr(
            "Release {version} has no {name} to download. The release page opens "
            "instead.", version=ver, name=updater.asset_name(kind, ver)))
        _open_url(page)
        return
    verify = bool(asset.get("sha256"))
    if not verify and not messagebox.askyesno(tr("Update"), tr(
            "The release lists no SHA-256 checksum for {name}, so the download can't be "
            "verified.\n\nDownload and install it anyway?", name=asset["name"]),
            default="no"):
        return

    paused = {"by_us": False}

    def resume_queue():
        """The update ended without closing the app: undo our pause."""
        if paused["by_us"]:
            paused["by_us"] = False
            jobs.resume()
            applog.record("[update] the job queue runs again")

    def go():
        _STATE["waiting"] = False
        try:
            UpdateDialog(root, asset, ver, verify,
                         lambda path: _install(path, kind, ver, persist, close_app),
                         on_abort=resume_queue)
        except Exception:
            _STATE["dialog"] = None
            resume_queue()
            raise

    if is_busy():
        if messagebox.askyesno(tr("Update"), tr(
                "A job is running - the app can't update now.\n\nUpdate automatically "
                "when the current job has finished? (The queue is paused until then.)")):
            if not jobs.is_paused():
                jobs.pause()
                paused["by_us"] = True
            _STATE["waiting"] = True
            applog.record("[update] waiting for the running job to finish")

            def wait():
                try:
                    if not root.winfo_exists():
                        return
                except tk.TclError:
                    return
                if is_busy():
                    root.after(2000, wait)
                    return
                try:
                    go()
                except Exception as exc:
                    _STATE["waiting"] = False
                    applog.record(f"[update] failed: {exc!r}")
                    messagebox.showerror(tr("Update"),
                                         tr("The update failed: {error}", error=exc))
            root.after(2000, wait)
        return
    go()


def _install(path, kind, ver, persist, close_app):
    """The verified download is ready: hand over to it and close the app."""
    try:
        persist()
    except Exception:
        pass
    if kind == "installed":
        updater.write_pending(kind, ver)
        try:
            verb, params = updater.run_installer(path, C.APP_ROOT)
        except updater.UpdateError:
            updater.clear_pending()       # nothing happened - forget the marker
            raise
        applog.record(f"[update] started the installer ({verb}): {path} {params}")
    else:                                 # portable
        exe = updater.launcher_path()
        if not exe:
            raise updater.UpdateError(tr(
                "The portable exe was not found. The new version was saved as:\n{path}",
                path=path))
        # the new exe takes the place (and name) of the one the user started
        updater.write_pending(kind, ver)
        try:
            backup = updater.replace_launcher(path, exe)
        except updater.UpdateError:
            updater.clear_pending()       # nothing changed - forget the marker
            raise
        finally:
            updater.discard_download(path)
        applog.record(f"[update] replaced {exe} with version {ver} (the previous one is "
                      f"{os.path.basename(backup)} until the next start)")
        # the settings were saved above; close_app() saves them once more on
        # the way out - the new version waits for this process to end before
        # it reads them (MEDIAPREP_WAIT_PID), so it always gets the final save
        try:
            updater.start_portable(exe, args=sys.argv[1:], wait_pid=os.getpid())
        except updater.UpdateError:
            updater.clear_pending()
            if updater.undo_replace(exe, backup):
                applog.record(f"[update] the new version did not start - put the "
                              f"previous {os.path.basename(exe)} back")
            raise
        applog.record(f"[update] started the new portable version: {exe}")
    close_app()
