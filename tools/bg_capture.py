"""Screenshot a window that is off-screen or behind others (PrintWindow),
for scripted test runs started with MEDIAPREP_BACKGROUND=1.

    python tools/bg_capture.py "MediaPrep Toolkit" out.png      # by title prefix
    from tools.bg_capture import capture; capture(hwnd).save("x.png")
"""
import ctypes
import sys
from ctypes import wintypes

_u32 = ctypes.windll.user32
_gdi = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2


def find_window(title_prefix):
    """First top-level window whose title starts with title_prefix (or None)."""
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        n = _u32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(title_prefix):
                found.append(hwnd)
                return False
        return True
    _u32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


def capture(hwnd):
    """PIL image of the whole window (works off-screen; needs it not minimized)."""
    from PIL import Image
    r = wintypes.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc_win = _u32.GetWindowDC(hwnd)
    hdc = _gdi.CreateCompatibleDC(hdc_win)
    bmp = _gdi.CreateCompatibleBitmap(hdc_win, w, h)
    _gdi.SelectObject(hdc, bmp)
    _u32.PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    _gdi.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    _gdi.DeleteObject(bmp)
    _gdi.DeleteDC(hdc)
    _u32.ReleaseDC(hwnd, hdc_win)
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


if __name__ == "__main__":
    h = find_window(sys.argv[1] if len(sys.argv) > 1 else "MediaPrep Toolkit")
    if not h:
        sys.exit("window not found")
    capture(h).save(sys.argv[2] if len(sys.argv) > 2 else "capture.png")
