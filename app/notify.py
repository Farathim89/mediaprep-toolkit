"""'Job done' notifications: a Windows toast (PowerShell + WinRT, no extra
packages), falling back to a small themed popup in the bottom-right corner,
plus an optional ok/error sound and a flashing taskbar button when the window
isn't focused. Everything is best-effort - any failure is silent.

    notify.install(root, dispatch)   # once, from app.py (auto-hooks jobs.end)
    notify.notify("Cut / Edit batch finished", "12 files cut, 0 failed", ok=True)

Settings live in config.PREFS (notify_on, notify_toast, notify_sound,
notify_min_minutes)."""
import base64
import subprocess
import threading
import time
import tkinter as tk

from .config import PREFS, APP_NAME, POPEN_FLAGS
from . import jobs
from .i18n import tr

_ROOT = None
_DISPATCH = None
_POPUPS = []
TOAST_TIMEOUT = 15          # seconds before a hung powershell is killed
POPUP_SECONDS = 8

# PowerShell's own AppUserModelID: toasts shown under it appear without
# registering a Start-menu shortcut for this app
_PS_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def install(root, dispatch=None):
    """Remember the Tk root and a dispatcher (fn(callable) -> runs it on the Tk
    thread), and notify automatically when a long job ends."""
    global _ROOT, _DISPATCH
    _ROOT = root
    _DISPATCH = dispatch
    jobs.subscribe(_on_job_event)


def _on_job_event(event, info):
    if event != "end" or not info or info.get("id") is None:
        return
    try:
        min_s = float(PREFS.get("notify_min_minutes", 1.0)) * 60.0
    except (TypeError, ValueError):
        min_s = 60.0
    if info.get("elapsed", 0.0) < min_s:
        return
    name = info.get("name") or tr("Job")
    if info.get("stopped"):
        title, ok = tr("{job} stopped", job=name), False
    elif info.get("ok", True):
        title, ok = tr("{job} finished", job=name), True
    else:
        title, ok = tr("{job} failed", job=name), False
    msg = info.get("summary") or ""
    msg = (msg + ("\n" if msg else "")
           + tr("Took {time}", time=_fmt_secs(info.get("elapsed", 0))))
    left = len(jobs.queued())
    if left:
        msg += " - " + tr("{n} more queued", n=left)
    notify(title, msg, ok=ok)


def _fmt_secs(s):
    s = int(s or 0)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# --------------------------------------------------------------------- API
def notify(title, message, ok=True, force=False):
    """Show a done-notification (respecting the settings unless force=True).
    Call from any thread."""
    if not force and not PREFS.get("notify_on", True):
        return
    if PREFS.get("notify_sound", True):
        _beep(ok)
    _on_tk(_flash_taskbar)
    if PREFS.get("notify_toast", True):
        def fallback():
            _on_tk(lambda: popup(title, message, ok))
        toast(title, message, on_fail=fallback)
    else:
        _on_tk(lambda: popup(title, message, ok))


def _on_tk(fn):
    if _DISPATCH is not None:
        try:
            _DISPATCH(fn)
            return
        except Exception:
            pass
    if _ROOT is not None:
        try:
            if threading.current_thread() is threading.main_thread():
                fn()
            else:
                _ROOT.after(0, fn)
        except Exception:
            pass


# ------------------------------------------------------------------- sound
def _beep(ok):
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK if ok else winsound.MB_ICONHAND)
    except Exception:
        pass


# --------------------------------------------------------------- taskbar
def _flash_taskbar():
    """Flash the taskbar button until the window is focused (only when it
    isn't the foreground window already)."""
    if _ROOT is None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(_ROOT.winfo_id()) or _ROOT.winfo_id()
        if user32.GetForegroundWindow() == hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                          FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


# ------------------------------------------------------------------- toast
def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def toast_script(title, message):
    """The PowerShell script that shows one toast (exposed for tests)."""
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{_xml_escape(title)}</text>"
           f"<text>{_xml_escape(message)}</text>"
           f"<text placement='attribution'>{_xml_escape(APP_NAME)}</text>"
           "</binding></visual></toast>")
    # single-quoted PowerShell strings: only ' needs doubling
    xml_ps = xml.replace("'", "''")
    aumid = _PS_AUMID.replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType = WindowsRuntime] > $null\n"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml('{xml_ps}')\n"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}')"
        ".Show($t)\n")


def toast(title, message, on_fail=None, timeout=TOAST_TIMEOUT):
    """Fire a Windows toast without blocking. on_fail() is called (from a
    worker thread) if PowerShell is missing, errors or hangs."""
    def work():
        ok = False
        try:
            enc = base64.b64encode(toast_script(title, message).encode("utf-16-le")).decode()
            p = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=POPEN_FLAGS)
            try:
                ok = p.wait(timeout=timeout) == 0
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
        except Exception:
            ok = False
        if not ok and on_fail is not None:
            try:
                on_fail()
            except Exception:
                pass
    threading.Thread(target=work, daemon=True, name="toast").start()


# -------------------------------------------------------------- popup
def _colors(_widget=None):
    """(bg, fg, accent, muted, error) of the current theme."""
    from .ui import themes
    p = themes.current()
    return p["bg"], p["fg"], p["accent"], p["muted"], p["error"]


def popup(title, message, ok=True, seconds=POPUP_SECONDS):
    """Small themed popup in the bottom-right of the screen, auto-closes.
    Must run on the Tk thread."""
    if _ROOT is None:
        return None
    try:
        bg, fg, acc, muted, bad = _colors(_ROOT)
        w = tk.Toplevel(_ROOT)
        w.overrideredirect(True)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(w, bg=bg, highlightthickness=2,
                         highlightbackground=acc if ok else bad, padx=12, pady=8)
        frame.pack(fill="both", expand=True)
        head = tk.Frame(frame, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=("✔ " if ok else "✖ ") + str(title), bg=bg,
                 fg=acc if ok else bad, font=("Segoe UI", 10, "bold"),
                 anchor="w", justify="left", wraplength=320).pack(side="left", fill="x")
        x = tk.Label(head, text="✕", bg=bg, fg=fg, cursor="hand2",
                     font=("Segoe UI", 9))
        x.pack(side="right", anchor="n")
        if message:
            tk.Label(frame, text=str(message), bg=bg, fg=fg, font=("Segoe UI", 9),
                     anchor="w", justify="left", wraplength=340).pack(fill="x", pady=(4, 0))
        tk.Label(frame, text=APP_NAME, bg=bg, fg=muted,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(4, 0))

        def close(_e=None):
            try:
                if w in _POPUPS:
                    _POPUPS.remove(w)
                w.destroy()
            except Exception:
                pass
            _restack()
        for wid in (w, frame, x):
            wid.bind("<Button-1>", close)
        _POPUPS.append(w)
        w.update_idletasks()
        _restack()
        w.after(int(seconds * 1000), close)
        return w
    except Exception:
        return None


def _work_area():
    """(right, bottom) of the primary monitor's work area (excludes taskbar)."""
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):
            return r.right, r.bottom
    except Exception:
        pass
    return _ROOT.winfo_screenwidth(), _ROOT.winfo_screenheight() - 48


def _restack():
    """Stack open popups upward from the bottom-right corner."""
    if _ROOT is None:
        return
    try:
        right, bottom = _work_area()
        y = bottom - 12
        for w in reversed(_POPUPS):
            w.update_idletasks()
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            y -= wh
            w.geometry(f"+{right - ww - 12}+{y}")
            y -= 8
    except Exception:
        pass


def test_notification():
    """Fire one notification regardless of the on/off setting (Settings -> Test)."""
    notify(tr("{app} — test notification", app=APP_NAME),
           tr("Notifications are working. {time}", time=time.strftime("%H:%M:%S")),
           ok=True, force=True)
