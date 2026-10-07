"""Background test mode: MEDIAPREP_BACKGROUND=1 makes every app window
INVISIBLE (fully transparent), click-through, without a taskbar button, and
it never keeps the focus - so scripted test runs / screenshots don't interrupt
whoever is using the PC. The window stays on-screen on purpose: Windows only
paints visible windows, and an off-screen one would screenshot blank.
Windows only; a no-op otherwise.

Screenshots of such windows: tools/bg_capture.py (PrintWindow works for
windows that are off-screen or behind others)."""
import os
import sys

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000      # clicking/showing never activates it
_WS_EX_TOOLWINDOW = 0x00000080      # no taskbar button / Alt+Tab entry
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020     # mouse clicks go through to what's below


def enabled():
    return os.environ.get("MEDIAPREP_BACKGROUND", "").strip() not in ("", "0")


def _hwnd(win):
    """The Win32 frame window of a Tk toplevel."""
    import ctypes
    win.update_idletasks()
    hid = win.winfo_id()
    return ctypes.windll.user32.GetParent(hid) or hid


def offscreen_xy():
    """A point far right of any monitor setup (Win32 coordinates go to 32767)."""
    return 30000, 0


_prev_fg = {"h": None}


def remember_foreground():
    """Note the window the user is in BEFORE we show anything."""
    if enabled() and sys.platform == "win32":
        try:
            import ctypes
            _prev_fg["h"] = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass


def give_back_focus(win):
    """Tk activates a toplevel when it is first mapped; if that took the
    foreground away from the user, hand it straight back."""
    h0 = _prev_fg["h"]
    if not h0 or not enabled() or sys.platform != "win32":
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        if u32.GetForegroundWindow() == _hwnd(win) and u32.IsWindow(h0):
            u32.SetForegroundWindow(h0)
    except Exception:
        pass


def apply(win):
    """Make `win` (a withdrawn or new Toplevel/Tk) non-activating and keep it
    off the taskbar. Call BEFORE it is shown. Returns True when applied."""
    if not enabled() or sys.platform != "win32":
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        h = _hwnd(win)
        ex = u32.GetWindowLongW(h, _GWL_EXSTYLE)
        u32.SetWindowLongW(h, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
                           | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
        u32.SetLayeredWindowAttributes(h, 0, 0, 0x02)   # LWA_ALPHA, alpha 0 = invisible
        return True
    except Exception:
        return False


def place_offscreen(win, size=None):
    """Kept for callers: the window now stays on-screen but invisible (see the
    module docstring); only the size is applied."""
    try:
        if size:
            win.geometry(size)
    except Exception:
        pass
