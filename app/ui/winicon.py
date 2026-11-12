"""Sharp window icons at every display scaling (Windows).

Tk's `wm iconbitmap` takes the FIRST picture of the .ico (16 px) for both
icons of a window and Windows stretches it to the icon size of the display
scaling: at 250 % the title bar and the taskbar showed a 16 px picture blown
up to 80 px - blurry next to every other icon. So every top-level window
(the main window and each dialog, when it is mapped) gets the two icons
Windows asks for at ITS monitor's DPI - ICON_BIG = SM_CXICON, ICON_SMALL =
SM_CXSMICON for that DPI - loaded from the multi-size .ico at exactly those
sizes (LoadImageW picks the picture of that size; the .ico has one for each
common scaling). A window that moves to a monitor with another scaling gets
them again (ui/dpi.py calls dpi_changed() from its WM_DPICHANGED handler:
plain Win32 there, no Tk call). The class icon (windows without their own)
follows the main window.

The icons are loaded once per DPI and kept alive for the whole session -
Windows doesn't copy an icon given with WM_SETICON - and shared by every
window at that DPI: one pair per monitor scaling, so nothing piles up when a
window moves back and forth."""
import sys
import tkinter as tk

_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_SM_CXICON, _SM_CXSMICON = 11, 49
_GCLP_HICON, _GCLP_HICONSM = -14, -34


class _S:
    ico = None            # path of the .ico
    u32 = None
    cache = {}            # dpi -> (big HICON, small HICON)
    main = 0              # the main window's frame


def _api():
    if _S.u32 is None and sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes as wt
            u32 = ctypes.WinDLL("user32")
            u32.LoadImageW.restype = ctypes.c_void_p
            u32.LoadImageW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                                       ctypes.c_int, wt.UINT]
            u32.DestroyIcon.argtypes = [ctypes.c_void_p]
            u32.SendMessageW.restype = ctypes.c_ssize_t
            u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            u32.GetSystemMetrics.argtypes = [ctypes.c_int]
            if hasattr(u32, "GetSystemMetricsForDpi"):
                u32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wt.UINT]
            setc = getattr(u32, "SetClassLongPtrW", None) or u32.SetClassLongW
            setc.restype = ctypes.c_size_t
            setc.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_size_t]
            _S.setclass = setc
            _S.u32 = u32
        except Exception:
            _S.u32 = False
    return _S.u32 or None


def _metric(index, dpi):
    u32 = _api()
    if hasattr(u32, "GetSystemMetricsForDpi"):
        v = int(u32.GetSystemMetricsForDpi(index, int(dpi)))
        if v > 0:
            return v
    # old Windows: the system DPI's size, scaled
    from . import dpi as _dpi
    return max(1, round(u32.GetSystemMetrics(index) * dpi / float(_dpi.system_dpi())))


def icons_for(dpi):
    """(big, small) HICON for `dpi` - loaded once, then cached - or None."""
    u32 = _api()
    if u32 is None or not _S.ico or not dpi:
        return None
    dpi = int(dpi)
    got = _S.cache.get(dpi)
    if got is None:
        out = []
        for index in (_SM_CXICON, _SM_CXSMICON):
            n = _metric(index, dpi)
            # IMAGE_ICON (1), LR_LOADFROMFILE (0x10): the picture of that size
            out.append(u32.LoadImageW(None, _S.ico, 1, n, n, 0x10) or 0)
        if not all(out):
            for h in out:
                if h:
                    u32.DestroyIcon(h)
            return None
        got = _S.cache[dpi] = tuple(out)
    return got


def set_for(hwnd, dpi):
    """Give frame window `hwnd` the icons for `dpi` (Win32 only, no Tk call -
    safe inside a window procedure). True when set."""
    u32 = _api()
    pair = icons_for(dpi) if hwnd else None
    if pair is None:
        return False
    try:
        u32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, pair[0])
        u32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, pair[1])
        if hwnd == _S.main:
            # windows without icons of their own show the class icons
            # (Tk set them from the 16 px picture as well)
            _S.setclass(hwnd, _GCLP_HICON, pair[0])
            _S.setclass(hwnd, _GCLP_HICONSM, pair[1])
    except Exception:
        return False
    return True


def dpi_changed(hwnd, dpi):
    """WM_DPICHANGED of a top-level frame (ui/dpi.py, window procedure)."""
    if _S.ico:
        set_for(hwnd, dpi)


def _frame_dpi(hwnd):
    from . import dpi as _dpi
    return _dpi.window_dpi(hwnd) or _dpi.system_dpi()


def _on_map(e):
    w = e.widget
    if isinstance(w, str):
        return
    try:
        if w.winfo_toplevel() is not w or w.wm_overrideredirect():
            return                    # tooltips, menus, pop-ups: no icon
        from .dpi import frame_of
        h = frame_of(w)
    except tk.TclError:
        return
    if h:
        if str(w) == ".":
            _S.main = h
        set_for(h, _frame_dpi(h))


def install(root, ico):
    """Windows: sharp icons from the .ico `ico` for the main window and every
    dialog (each one when it is mapped). Returns True when active."""
    if _api() is None or not ico:
        return False
    _S.ico = ico
    try:
        root.bind_class("Tk", "<Map>", _on_map, add="+")
        root.bind_class("Toplevel", "<Map>", _on_map, add="+")
    except tk.TclError:
        return False
    return True
