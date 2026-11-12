"""Per-monitor DPI awareness (Windows "per-monitor v2").

The app opts in before the Tk root exists (enable()), so Windows never
bitmap-stretches it: on every monitor it is drawn at that monitor's own
scaling (e.g. 150 % on one screen, 300 % on a 4K one) - sharp text, icons
and Fluent elements.

Tk 8.6 knows nothing about this: it has ONE scaling (fonts, 'tk scaling')
for the whole app. So the app has one UI scale at a time, the DPI of the
monitor of the window that moved there last - normally the main window -
and every window of the app is redrawn at it:

    WM_DPICHANGED  a top-level window (main window or a dialog) moved to a
                   monitor with another DPI: the window takes the size
                   Windows suggests (keeps its physical size) right away,
                   inside the window procedure (no Tk call there); the UI
                   is rescaled ONCE on the Tk thread - after the drag ends
                   and the stream of messages has settled (rescale()).
    rescale        tk scaling, every named font, the px() factor
                   (themes), the Fluent element pictures and icon glyphs,
                   every pixel option of every widget (padding, borders,
                   wrap lengths, list columns, geometry-manager paddings,
                   window min sizes ... - a Tcl walk, see dpi_tcl.py), then the
                   widgets that size themselves in code (on_change hooks:
                   the sidebar, the page view, the video players ...).
                   Pixel values remember the value they were scaled from,
                   so moving back and forth never drifts.
    a dialog       moved to another monitor takes the UI scale with it
                   while it is open; when it closes the scale goes back to
                   the main window's monitor.

The UI is always BUILT at the system DPI (the primary monitor's) - the
scale the hand-tuned pixel sizes were made for - and rescaled from there:
a start on another monitor (saved window position) rescales once before
the window is shown, exactly like a move would.

Speed (a rescale is one freeze of ~0.5-1 s on ~1300 widgets): the pages
are taken out of their canvases while everything is re-laid out (::mpdpi::
detach - a canvas-managed frame re-lays out 5-10x slower), the Fluent
pictures / icons of the other monitors' DPI are drawn ahead in a worker
thread (prewarm) and kept per (theme, DPI). Tk's event loop was seen to
fall asleep after the window's DPI changed, so a helper thread posts
WM_NULL until the rescale has run (_keep_awake). WM_DISPLAYCHANGE (a
monitor added / removed) makes Tk reset 'tk scaling' - put back at once.

Elsewhere: non-Windows / old Windows -> enable() falls back to system DPI
awareness and nothing here does anything.
"""
import sys
import time
import tkinter as tk

from .dpi_tcl import _TCL

_PMV2 = -4                       # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
WM_DPICHANGED = 0x02E0
_MODE = {"mode": ""}            # "pmv2" / "pm" / "system" / ""


# ---------------------------------------------------------------- Win32
class _W:
    u32 = None
    ok = False


def _win():
    if _W.u32 is None and sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes as wt
            u32 = ctypes.WinDLL("user32")
            u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wt.UINT]
            u32.MonitorFromRect.restype = ctypes.c_void_p
            u32.MonitorFromRect.argtypes = [ctypes.POINTER(wt.RECT), wt.DWORD]
            u32.MonitorFromWindow.restype = ctypes.c_void_p
            u32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
            u32.SendMessageW.restype = ctypes.c_ssize_t
            u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            u32.RedrawWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.HANDLE, wt.UINT]
            u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
            u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            if hasattr(u32, "GetDpiForWindow"):
                u32.GetDpiForWindow.restype = wt.UINT
                u32.GetDpiForWindow.argtypes = [wt.HWND]
            _W.u32 = u32
            _W.ok = True
        except Exception:
            _W.ok = False
    return _W.u32 if _W.ok else None


def enable():
    """Opt the process (or at least the Tk thread) in to per-monitor v2 DPI
    awareness - call it BEFORE the Tk root is created. Falls back to
    per-monitor v1 (Windows 8.1), then to system awareness. Returns the
    mode ("pmv2", "pm", "system" or "")."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32")
        setp = getattr(u32, "SetProcessDpiAwarenessContext", None)
        if setp is not None:
            setp.argtypes = [ctypes.c_void_p]
            if setp(ctypes.c_void_p(_PMV2)):
                _MODE["mode"] = "pmv2"
                return "pmv2"
            # already decided (an exe manifest says dpiAware): per-thread
            # awareness still works - every window is made on the Tk thread
            if _thread_is_pmv2(u32):
                _MODE["mode"] = "pmv2"
                return "pmv2"
            sett = getattr(u32, "SetThreadDpiAwarenessContext", None)
            if sett is not None:
                sett.restype = ctypes.c_void_p
                sett.argtypes = [ctypes.c_void_p]
                if sett(ctypes.c_void_p(_PMV2)) and _thread_is_pmv2(u32):
                    _MODE["mode"] = "pmv2"
                    return "pmv2"
    except Exception:
        pass
    try:
        import ctypes
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:     # Windows 8.1
            _MODE["mode"] = "pm"
            return "pm"
    except Exception:
        pass
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        _MODE["mode"] = "system"
        return "system"
    except Exception:
        return ""


def _thread_is_pmv2(u32):
    import ctypes
    try:
        get = u32.GetThreadDpiAwarenessContext
        get.restype = ctypes.c_void_p
        eq = u32.AreDpiAwarenessContextsEqual
        eq.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(eq(ctypes.c_void_p(get()), ctypes.c_void_p(_PMV2)))
    except Exception:
        return False


def per_monitor():
    """True when the windows follow each monitor's DPI (enable() worked)."""
    return _MODE["mode"] in ("pmv2", "pm")


def system_dpi():
    """The system DPI (the primary monitor's when the user signed in)."""
    u32 = _win()
    try:
        if u32 is not None and hasattr(u32, "GetDpiForSystem"):
            return int(u32.GetDpiForSystem()) or 96
    except Exception:
        pass
    return 96


def _monitor_dpi(mon):
    try:
        import ctypes
        from ctypes import wintypes as wt
        x, y = wt.UINT(), wt.UINT()
        if mon and ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0,
                                                         ctypes.byref(x), ctypes.byref(y)) == 0:
            return int(x.value) or None
    except Exception:
        pass
    return None


def window_dpi(hwnd):
    """The DPI of top-level window `hwnd` (its monitor's), or None."""
    u32 = _win()
    if u32 is None or not hwnd or not per_monitor():
        return None
    try:
        if hasattr(u32, "GetDpiForWindow"):
            d = int(u32.GetDpiForWindow(hwnd))
            if d:
                return d
        return _monitor_dpi(u32.MonitorFromWindow(hwnd, 2))     # MONITOR_DEFAULTTONEAREST
    except Exception:
        return None


def rect_dpi(rect):
    """The DPI of the monitor showing most of rect = (left, top, right,
    bottom) (the nearest one when it is on none), or None."""
    u32 = _win()
    if u32 is None or not per_monitor():
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt
        return _monitor_dpi(u32.MonitorFromRect(ctypes.byref(wt.RECT(*rect)), 2))
    except Exception:
        return None


def monitor_dpis():
    """The DPI of every monitor (empty when it can't be asked)."""
    u32 = _win()
    if u32 is None or not per_monitor():
        return []
    out = []
    try:
        import ctypes
        from ctypes import wintypes as wt
        proto = ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, wt.HDC,
                                   ctypes.POINTER(wt.RECT), wt.LPARAM)

        def cb(mon, _hdc, _rc, _lp):
            d = _monitor_dpi(mon)
            if d:
                out.append(d)
            return True
        u32.EnumDisplayMonitors(None, None, proto(cb), 0)
    except Exception:
        pass
    return out


def _work_area(hwnd):
    """(left, top, right, bottom) of the work area of hwnd's monitor."""
    u32 = _win()
    try:
        import ctypes
        from ctypes import wintypes as wt

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = u32.MonitorFromWindow(hwnd, 2)
        if mon and u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def _rect_work_area(rect):
    """(left, top, right, bottom) of the work area of the monitor showing
    most of rect, or None."""
    u32 = _win()
    try:
        import ctypes
        from ctypes import wintypes as wt

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = u32.MonitorFromRect(ctypes.byref(wt.RECT(*rect)), 2)
        if mon and u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def frame_of(top):
    """The Win32 frame (wrapper) window of a Tk toplevel, or 0."""
    try:
        h = int(top.wm_frame(), 16)
        return h if h and h != top.winfo_id() else 0
    except (tk.TclError, ValueError):
        return 0


# ---------------------------------------------------------------- hooks
_HOOKS = []          # (widget or None, fn(ratio), late)


def on_change(widget, fn, late=False):
    """Call fn(ratio) after every UI rescale (ratio = new / old scale) for as
    long as `widget` lives (widget=None: for the whole session). For things
    sized in code: they recompute their sizes from themes.px() there.
    late=True: once the rescaled widgets have been laid out (to measure)."""
    _HOOKS.append((widget, fn, late))
    return fn


def _run_hooks(ratio, late=False):
    keep = []
    for w, fn, lt in _HOOKS:
        try:
            if w is not None and not w.winfo_exists():
                continue
        except tk.TclError:
            continue
        keep.append((w, fn, lt))
        if lt != late:
            continue
        try:
            fn(ratio)
        except Exception as exc:
            sys.stderr.write(f"[dpi] hook {fn!r} failed: {exc!r}\n")
    _HOOKS[:] = keep


# ---------------------------------------------------------------- the manager
class _TopHook:
    """Window procedure hook on the frame of a dialog / secondary window:
    WM_DPICHANGED (handed to the manager), Windows' move / size loop
    (`sizing`) and the end of the window. Never calls into Tk."""

    def __init__(self, hwnd, mgr):
        import ctypes
        from ctypes import wintypes as wt
        u32 = ctypes.WinDLL("user32")
        lres = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(lres, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        setp = getattr(u32, "SetWindowLongPtrW", None) or u32.SetWindowLongW
        setp.restype = ctypes.c_void_p
        setp.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        self._call = u32.CallWindowProcW
        self._call.restype = lres
        self._call.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self._setp = setp
        self._mgr = mgr
        self.hwnd = hwnd
        self.sizing = False
        self.alive = False
        self._fn = proto(self._proc)               # keep a reference (GC = crash)
        self._old = setp(hwnd, -4, ctypes.cast(self._fn, ctypes.c_void_p))
        self.alive = bool(self._old)

    def _proc(self, hwnd, msg, wp, lp):
        old = self._old
        try:
            if msg == WM_DPICHANGED:
                if self._mgr.wm_dpichanged(hwnd, wp, lp, main=False):
                    return 0
            elif msg == 0x0024:                    # WM_GETMINMAXINFO
                res = self._call(old, hwnd, msg, wp, lp)
                try:
                    self._mgr.minmax(hwnd, lp)
                except Exception:
                    pass
                return res
            elif msg == 0x0231:                    # WM_ENTERSIZEMOVE
                self.sizing = True
            elif msg == 0x0232:                    # WM_EXITSIZEMOVE
                self.sizing = False
            elif msg == 0x0082:                    # WM_NCDESTROY
                self.alive = self.sizing = False
                self._setp(hwnd, -4, old)
                self._mgr.window_gone(hwnd)
        except Exception:
            pass
        return self._call(old, hwnd, msg, wp, lp)


class _Manager:
    SETTLE_MS = 150          # quiet time after the last WM_DPICHANGED
    DETACH_ALL = True        # also take the visible page out of its canvas (::mpdpi::detach)

    def __init__(self):
        self.root = None
        self.dpi = 96             # the DPI the UI is scaled for now
        self.main_dpi = None      # the DPI the main window's SIZE is for
        self.wins = {}            # dialog frame hwnd -> {"dpi", "top", "hook"}
        self.anchor = None        # hwnd the UI scale follows (None = main window)
        self.target = None
        self._job = None
        self._last = 0.0
        self.busy = False
        self.stats = []           # (old dpi, new dpi, ms, windows, phases) per rescale
        self._shrink = {}         # hwnd -> factor while it takes a lower-DPI size
        self._wake = (None, 0.0)  # (hwnd, until): see _keep_awake
        self._waker = None
        self._pw_job = None

    # ---- setup
    def install(self, root):
        self.root = root
        try:
            self.dpi = int(round(float(root.tk.call("tk", "scaling")) * 72.0))
        except (tk.TclError, ValueError):
            self.dpi = 96
        sd = system_dpi()
        if abs(sd - self.dpi) <= 2:
            self.dpi = sd
        root.tk.eval(_TCL)
        from . import pageview, themes
        pageview._SizeHook.dpi_handler = self.wm_dpichanged
        pageview._SizeHook.minmax_handler = self.minmax
        pageview._SizeHook.display_handler = self.display_changed
        root.bind_class("Toplevel", "<Map>", self._on_map, add="+")
        # the pictures for the other monitors' scaling, drawn ahead after
        # every theme change (the first one at startup), so the first move
        # there is quicker
        themes.subscribe(lambda _n, _p: self._prewarm_soon())

    def _prewarm_soon(self):
        if self.root is not None and self._pw_job is None:
            self._pw_job = self.root.after(3000, self.prewarm)

    def prewarm(self):
        """Draw the Fluent element pictures and the icons for the DPI of
        every other monitor in a worker thread (cache only, no Tk call)."""
        self._pw_job = None
        root = self.root
        dpis = sorted(set(monitor_dpis()) - {self.dpi})
        if root is None or not dpis:
            return
        from . import fluent, icons, themes
        scales = []
        try:
            cur = root.tk.call("tk", "scaling")
            for d in dpis:
                # the factor the UI will use there (Tk rounds 'tk scaling')
                root.tk.call("tk", "scaling", d / 72.0)
                scales.append(max(1.0, float(root.winfo_fpixels("1i")) / 96.0))
            root.tk.call("tk", "scaling", cur)
        except (tk.TclError, ValueError):
            return
        pal = themes.current()
        use_fluent = fluent.available()

        def work():
            for s in scales:
                try:
                    if use_fluent:
                        fluent.prewarm(pal, s)
                    icons.prewarm(s)
                except Exception:
                    pass
        import threading
        threading.Thread(target=work, name="dpi-prewarm", daemon=True).start()

    def _on_map(self, e):
        w = e.widget
        if isinstance(w, str) or w is self.root:
            return
        try:
            if w.winfo_toplevel() is not w or w.wm_overrideredirect():
                return
        except tk.TclError:
            return
        h = frame_of(w)
        if not h or h in self.wins:
            return
        try:
            hook = _TopHook(h, self)
        except Exception:
            return
        if hook.alive:
            self.wins[h] = {"dpi": self.dpi, "top": w, "hook": hook}

    # ---- window procedure side (no Tk calls in here)
    def wm_dpichanged(self, hwnd, wp, lp, main=True):
        """WM_DPICHANGED of a top-level window: take the size Windows
        suggests unless the window is already sized for that DPI, and ask
        for a rescale on the Tk thread. Returns True (handled)."""
        import ctypes
        from ctypes import wintypes as wt
        dpi = int(wp & 0xFFFF)
        if not dpi:
            return False
        if main:
            basis = self.main_dpi or self.dpi
        else:
            rec = self.wins.get(hwnd)
            basis = rec["dpi"] if rec else self.dpi
        u32 = _win()
        if dpi != basis and lp and u32 is not None:
            rc = ctypes.cast(lp, ctypes.POINTER(wt.RECT)).contents
            x, y, w, h = rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
            # grown with the scaling it may be larger than the new monitor:
            # no larger than its work area right away (one resize, not two;
            # the position stays Windows' - it may be in a drag - and is
            # corrected after the rescale, see _fit_on_monitor)
            area = _rect_work_area((rc.left, rc.top, rc.right, rc.bottom))
            if area is not None:
                left, top, right, bottom = area
                w, h = min(w, right - left), min(h, bottom - top)
            # Tk's minimum size is still the one for the old DPI (until the
            # rescale): lowered for this one move (see minmax)
            if dpi < basis:
                self._shrink[hwnd] = dpi / float(basis)
            try:
                u32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)   # NOZORDER|NOACTIVATE
            finally:
                self._shrink.pop(hwnd, None)
        if main:
            self.main_dpi = dpi
        elif hwnd in self.wins:
            self.wins[hwnd]["dpi"] = dpi
        from .tkthread import _call_tk
        _call_tk(lambda: self.request(dpi, None if main else hwnd))
        self._keep_awake(hwnd)
        return True

    def _keep_awake(self, hwnd, secs=6.0):
        """Tk's event loop was seen to fall asleep after a DPI change (no
        timer firing until the next mouse move), so the queued rescale
        waited. Until it is done, a helper thread posts WM_NULL to the
        window every 40 ms (Win32 only - it never touches Tk)."""
        import threading
        self._wake = (hwnd, time.monotonic() + secs)
        t = self._waker
        if t is None or not t.is_alive():
            self._waker = threading.Thread(target=self._wake_loop, name="dpi-wake",
                                           daemon=True)
            self._waker.start()

    def _wake_loop(self):
        u32 = _win()
        while u32 is not None:
            hwnd, until = self._wake
            if not hwnd or time.monotonic() > until:
                return
            try:
                u32.PostMessageW(hwnd, 0, 0, 0)                 # WM_NULL
            except Exception:
                return
            time.sleep(0.04)

    def _awake_done(self):
        self._wake = (None, 0.0)

    def minmax(self, hwnd, lp):
        """WM_GETMINMAXINFO after Tk filled it in: while a window takes its
        smaller size for a lower DPI, its minimum is scaled down too."""
        f = self._shrink.get(hwnd)
        if not f or not lp:
            return
        import ctypes
        from ctypes import wintypes as wt

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", wt.POINT), ("ptMaxSize", wt.POINT),
                        ("ptMaxPosition", wt.POINT), ("ptMinTrackSize", wt.POINT),
                        ("ptMaxTrackSize", wt.POINT)]
        mmi = ctypes.cast(lp, ctypes.POINTER(MINMAXINFO)).contents
        mmi.ptMinTrackSize.x = int(mmi.ptMinTrackSize.x * f)
        mmi.ptMinTrackSize.y = int(mmi.ptMinTrackSize.y * f)

    def display_changed(self, hwnd):
        """WM_DISPLAYCHANGE (a monitor added / removed, a resolution
        changed) - from the window procedure. Tk answers it by setting 'tk
        scaling' back to the system DPI; the UI's own is put back (Tk
        thread) and the main window's monitor checked again."""
        from .tkthread import _call_tk
        _call_tk(self._display_changed)
        self._keep_awake(hwnd, 2.0)

    def _display_changed(self):
        root = self.root
        if root is None:
            return
        try:
            if abs(float(root.tk.call("tk", "scaling")) * 72.0 - self.dpi) > 0.6:
                root.tk.call("tk", "scaling", self.dpi / 72.0)
        except (tk.TclError, ValueError):
            pass
        root.after(500, self.check_main)

    def window_gone(self, hwnd):
        from .tkthread import _call_tk
        _call_tk(lambda: self._gone(hwnd))

    # ---- Tk thread
    def _gone(self, hwnd):
        self.wins.pop(hwnd, None)
        if self.anchor == hwnd:
            self.anchor = None
            d = self.main_window_dpi()
            if d and d != self.dpi:
                self.request(d, None)

    def main_window_dpi(self):
        if self.root is None:
            return None
        return window_dpi(frame_of(self.root))

    def request(self, dpi, anchor=None):
        """Rescale the UI to `dpi` once things have settled (the latest
        request wins)."""
        if self.root is None or not dpi:
            return
        self.anchor = anchor
        self.target = int(dpi)
        self._last = time.monotonic()
        if self._job is None:
            self._job = self.root.after(self.SETTLE_MS, self._settle)

    def _sizing(self):
        from . import pageview
        if pageview._sizing():
            return True
        return any(r["hook"].sizing for r in self.wins.values())

    def _settle(self):
        self._job = None
        if self.target is None:
            return
        quiet = time.monotonic() - self._last >= self.SETTLE_MS / 1000.0 - 0.01
        if not quiet or self._sizing():
            self._job = self.root.after(80, self._settle)
            # (a long drag: still awake when it ends)
            self._keep_awake(self._wake[0] or frame_of(self.root), 2.0)
            return
        dpi, self.target = self.target, None
        try:
            if dpi != self.dpi:
                self.rescale(dpi)
        finally:
            if self.target is None:
                self._awake_done()

    def check_main(self):
        """Safety net (startup, after a monitor change): the UI follows the
        main window's monitor when no dialog holds the scale."""
        if self.anchor is None and self.target is None:
            d = self.main_window_dpi()
            if d and d != self.dpi:
                if self.main_dpi is None:
                    self.main_dpi = d
                self.request(d, None)

    # ---- the rescale
    # (No WM_SETREDRAW around it: switching a top-level window's redraw off
    # detaches its DWM surface - it kept showing the old picture.)
    def rescale(self, dpi):
        """Redraw the whole UI for `dpi` (see the module docstring)."""
        from . import themes
        root = self.root
        if root is None or self.busy:
            return
        t0 = time.perf_counter()
        old = themes.scale()
        old_dpi = self.dpi
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
            new = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except (tk.TclError, ValueError):
            return
        self.dpi = int(dpi)
        if abs(new - old) < 1e-9:
            return
        ratio = new / old
        self.busy = True
        # dialogs that keep the size they were given (help windows, a
        # resized one) are scaled with the UI; the others size themselves
        tops = []
        for h, rec in list(self.wins.items()):
            top = rec["top"]
            try:
                if not top.winfo_exists():
                    continue
                natural = (abs(top.winfo_width() - top.winfo_reqwidth()) <= 2
                           and abs(top.winfo_height() - top.winfo_reqheight()) <= 2)
            except tk.TclError:
                continue
            tops.append((h, rec, natural))
        phases = []

        def mark(name, _t=[t0]):
            now = time.perf_counter()
            phases.append((name, round((now - _t[0]) * 1000.0, 1)))
            _t[0] = now
        n = 0
        parked = ()
        try:
            parked = root.tk.call("::mpdpi::detach", 1 if self.DETACH_ALL else 0)
            n = root.tk.call("::mpdpi::walk", old, new)
            mark("walk")
            themes.rescale(new)              # fonts, styles, element pictures, colours
            mark("themes")
            from . import icons
            icons.rescale()
            mark("icons")
            _run_hooks(ratio)
            for h, rec, natural in tops:
                if h == self.anchor or rec["dpi"] == self.dpi:
                    continue
                if not natural:
                    self._scale_window(rec["top"], h, self.dpi / float(rec["dpi"]))
                rec["dpi"] = self.dpi
            mark("hooks")
            root.update_idletasks()
            mark("layout")
            # sizes that are measured from the laid-out widgets (the page
            # size of the page view ...) - while the pages are still out of
            # their canvas, so each is laid out and drawn once, at its
            # final size, when it goes back in
            _run_hooks(ratio, late=True)
            # the moved window, grown with the scaling, may be larger than
            # its new monitor: keep it on it
            if self.anchor is None:
                self._fit_on_monitor(root, frame_of(root))
            elif self.anchor in self.wins:
                self._fit_on_monitor(self.wins[self.anchor]["top"], self.anchor)
            root.update_idletasks()
            mark("refit")
            root.tk.call("::mpdpi::attach", parked)
            parked = ()
            root.update_idletasks()
            mark("attach")
        except Exception as exc:
            sys.stderr.write(f"[dpi] rescale failed: {exc!r}\n")
        finally:
            self.busy = False
            if parked:
                try:
                    root.tk.call("::mpdpi::attach", parked)
                except tk.TclError:
                    pass
        ms = (time.perf_counter() - t0) * 1000.0
        self.stats.append((old_dpi, self.dpi, round(ms, 1), int(n or 0), phases))
        try:
            from .. import applog
            applog.record(f"[ui] display scaling {round(old_dpi / 0.96)} % -> "
                          f"{round(self.dpi / 0.96)} %: UI rescaled in {ms:.0f} ms")
        except Exception:
            pass

    def _fit_on_monitor(self, top, hwnd):
        """Shrink / move a (normal, not maximised) window that sticks out of
        its monitor's work area back onto it."""
        u32 = _win()
        area = _work_area(hwnd) if hwnd else None
        if area is None or u32 is None:
            return
        try:
            if top.state() != "normal":
                return
            import ctypes
            from ctypes import wintypes as wt
            rc = wt.RECT()
            if not u32.GetWindowRect(hwnd, ctypes.byref(rc)):
                return
        except (tk.TclError, Exception):
            return
        left, top_, right, bottom = area
        w, h = rc.right - rc.left, rc.bottom - rc.top
        nw, nh = min(w, right - left), min(h, bottom - top_)
        x = min(max(left, rc.left), right - nw)
        y = min(max(top_, rc.top), bottom - nh)
        if (x, y, nw, nh) != (rc.left, rc.top, w, h):
            u32.SetWindowPos(hwnd, None, x, y, nw, nh, 0x0004 | 0x0010)

    def _scale_window(self, top, hwnd, f):
        """A dialog with a size of its own: f times that size (same top-left
        corner), kept on its monitor."""
        try:
            w, h = top.winfo_width(), top.winfo_height()
            x, y = top.winfo_rootx(), top.winfo_rooty()
            gx, gy = top.winfo_x(), top.winfo_y()
        except tk.TclError:
            return
        nw, nh = max(1, int(round(w * f))), max(1, int(round(h * f)))
        area = _work_area(hwnd)
        if area is not None:
            left, top_, right, bottom = area
            nw, nh = min(nw, right - left), min(nh, bottom - top_)
            gx = min(max(left, gx), max(left, right - nw - (x - gx)))
            gy = min(max(top_, gy), max(top_, bottom - nh - (y - gy)))
        try:
            top.geometry(f"{nw}x{nh}+{gx}+{gy}")
        except tk.TclError:
            pass


manager = _Manager()


def install(root):
    """Start following the monitors' DPI (main(): after themes.init, before
    the window is shown). A no-op unless enable() made the app per-monitor
    aware."""
    if per_monitor() and _win() is not None:
        manager.install(root)
        return True
    return False


def startup_dpi(rect):
    """The DPI the UI should start at for a window at rect = (left, top,
    right, bottom), when it differs from the one Tk started with (else
    None)."""
    if manager.root is None or rect is None:
        return None
    d = rect_dpi(rect)
    return d if d and d != manager.dpi else None
