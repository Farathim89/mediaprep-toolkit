"""The main window's shell helpers (app.py): per-monitor DPI awareness, the
saved size / position put back on a monitor that shows it, the window icon,
and how the app starts itself again (frozen exe or python + launcher)."""
import os
import re
import sys
import tkinter as tk

from .config import resource_path


def _enable_dpi_awareness():
    """Tell Windows the app handles DPI itself - per monitor (v2), so text is
    sharp on every monitor instead of a blurry bitmap-stretched window
    (ui/dpi.py; falls back to system DPI awareness). Must run before the Tk
    root exists. Tk sizes its point-based fonts from the system DPI; pixel
    sizes are scaled in main(), and everything is rescaled when the window
    moves to a monitor with another scaling."""
    from .ui import dpi
    return dpi.enable()


def _monitor_work_area(rect, primary=False):
    """(left, top, right, bottom) of the work area (the monitor minus the
    taskbar) of the monitor that shows most of rect = (left, top, right,
    bottom), in the window coordinates Tk uses. None when the rect is on no
    monitor now (e.g. that monitor was unplugged) - with primary=True the
    primary monitor's work area instead - or when it can't be asked (not
    Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u32 = ctypes.WinDLL("user32")
        u32.MonitorFromRect.restype = ctypes.c_void_p
        u32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        # MONITOR_DEFAULTTONULL (0) / MONITOR_DEFAULTTOPRIMARY (1)
        mon = u32.MonitorFromRect(ctypes.byref(wintypes.RECT(*rect)), 1 if primary else 0)
        if not mon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def _restore_geometry(root, saved, default_size):
    """Re-open at the saved size/position, clamped onto the work area of the
    monitor it was on (any monitor - also one left of / above the primary
    one; the primary monitor when that one is gone); first run or an
    unusable value -> centred at default_size. Returns the normal
    (un-maximized) geometry string it set."""
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(saved.get("window_geometry", "")))
    if not m:
        return _center_window(root, default_size)
    w, h, x, y = (int(v) for v in m.groups())
    rect = (x, y, x + w, y + h)
    area = _monitor_work_area(rect) or _monitor_work_area(rect, primary=True)
    if area is None:
        area = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    left, top, right, bottom = area
    aw, ah = max(1, right - left), max(1, bottom - top)
    mw, mh = root.minsize()
    w = max(min(w, aw), min(mw, aw))
    h = max(min(h, ah), min(mh, ah))
    x = min(max(left, x), max(left, right - w))
    y = min(max(top, y), max(top, bottom - h))
    geo = f"{w}x{h}+{x}+{y}"
    root.geometry(geo)
    if saved.get("window_zoomed"):
        def _zoom():
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        root.after(50, _zoom)     # once the window is up
    return geo


def _geo_rect(geo):
    """"WxH+X+Y" -> (left, top, right, bottom), or None."""
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(geo or ""))
    if not m:
        return None
    w, h, x, y = (int(v) for v in m.groups())
    return x, y, x + w, y + h


def _set_window_icon(root):
    """Prefer the .ico (best on Windows); fall back to the .png everywhere else.
    On Windows every window then gets the .ico's pictures at the exact icon
    sizes of its monitor's scaling (ui/winicon.py) - Tk alone shows the 16 px
    picture stretched (blurry in the title bar and the taskbar at 150 %+)."""
    ico = resource_path("assets/icon.ico")      # app/assets (or the exe bundle)
    png = resource_path("assets/icon.png")
    try:
        if os.path.exists(ico):
            root.iconbitmap(ico)
            root.iconbitmap(default=ico)    # dialogs + help windows too
            from .ui import winicon
            winicon.install(root, os.path.abspath(ico))
            return
    except Exception:
        pass
    try:
        if os.path.exists(png):
            root._icon_img = tk.PhotoImage(file=png)   # keep a ref so it survives
            root.iconphoto(True, root._icon_img)
    except Exception:
        pass


def _center_window(root, size):
    """Place the window of the given "WIDTHxHEIGHT" size in the screen centre."""
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        root.geometry(size)
        return None
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2 - 50)   # 50px above dead centre
    root.geometry(f"{w}x{h}+{x}+{y}")
    return f"{w}x{h}+{x}+{y}"


def _restart_command():
    """argv that starts this app again (frozen exe or python + launcher)."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + sys.argv[1:]
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not script or not os.path.isfile(script):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "intro_credits_toolkit.py")
    return [sys.executable, script] + sys.argv[1:]


def _restart_env():
    """Environment for the restarted copy. A one-file (portable) build must
    start as a NEW independent instance that unpacks its own bundle - not
    reuse this run's _MEI folder, which is deleted when this one exits."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for k in list(env):
            if k.startswith("_PYI_") or k == "_MEIPASS2":
                env.pop(k, None)
    return env


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
