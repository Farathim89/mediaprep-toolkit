"""The app's own taskbar identity on Windows (AppUserModelID).

Windows groups taskbar buttons - and matches a running window to a pinned
shortcut - by the AppUserModelID; without an explicit one it is derived from
the exe's path. The portable build's pinned exe is the small launcher, the
window belongs to the program it starts (mediaprep-data\\runtime\\...\\
MediaPrep Toolkit.exe): two different exes, so the running app got a button
of its own next to the pin. So:

* set_process()  (intro_credits_toolkit.py, before any window) gives the whole
  process the fixed ID APP_ID - every run (portable, installed, zip, source)
  is the same app to the taskbar;
* install(root)  puts the ID on every top-level window together with the
  relaunch properties: pinning the RUNNING window makes a pin that starts the
  right thing (portable: the launcher exe, installed / zip: the app exe,
  source: pythonw + intro_credits_toolkit.py) and that carries the ID, so the
  next start stacks onto it. The properties are removed again when the
  window is destroyed (as SHGetPropertyStoreForWindow asks);
* stamp_shortcut(lnk)  writes the ID into a .lnk - the installer runs
  "MediaPrep Toolkit.exe --set-shortcut-aumid <lnk>..." (elevated, no window)
  for its Start menu / desktop shortcuts, so the running app stacks onto
  them and onto pins made from them.

Plain ctypes COM (IPropertyStore) - nothing extra to bundle. Every call is
best effort: a failure only means Windows' default grouping."""
import os
import sys

APP_ID = "Farathim.MediaPrepToolkit"
CLI_SWITCH = "--set-shortcut-aumid"

_VT_EMPTY, _VT_LPWSTR = 0, 31
# PKEY_AppUserModel_* = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid
_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PID_RELAUNCH_COMMAND, PID_RELAUNCH_ICON, PID_RELAUNCH_NAME, PID_ID = 2, 3, 4, 5
_IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"


class _W:
    api = None
    windows = {}          # Tk path of a top-level window -> its frame hwnd


def _win():
    """ctypes types and functions (None when not on Windows)."""
    if _W.api is None:
        _W.api = False
        if sys.platform == "win32":
            try:
                _W.api = _load()
            except Exception:
                _W.api = False
    return _W.api or None


def _load():
    import ctypes
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]

    class PROPVARIANT(ctypes.Structure):       # 24 bytes on x64, 16 on x86
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]

    def guid(s):
        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    shell32.SHGetPropertyStoreForWindow.argtypes = [wt.HWND, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long
    shell32.SHGetPropertyStoreFromParsingName.argtypes = [
        wt.LPCWSTR, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p)]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wt.LPCWSTR]
    shell32.GetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    hres = ctypes.c_long
    api = dict(ctypes=ctypes, shell32=shell32, ole32=ole32, GUID=GUID, PROPVARIANT=PROPVARIANT,
               PROPERTYKEY=PROPERTYKEY, iid_store=guid(_IID_IPROPERTYSTORE), fmtid=guid(_FMTID))
    # IPropertyStore: QueryInterface, AddRef, Release, GetCount, GetAt,
    # GetValue (5), SetValue (6), Commit (7)
    api["Release"] = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    api["GetValue"] = ctypes.WINFUNCTYPE(hres, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                         ctypes.POINTER(PROPVARIANT))
    api["SetValue"] = ctypes.WINFUNCTYPE(hres, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                         ctypes.POINTER(PROPVARIANT))
    api["Commit"] = ctypes.WINFUNCTYPE(hres, ctypes.c_void_p)
    return api


def _com_init(a):
    # S_OK / S_FALSE (already) / RPC_E_CHANGED_MODE (another apartment kind
    # already set up on this thread) - all fine for these calls
    a["ole32"].CoInitializeEx(None, 0x2)                  # COINIT_APARTMENTTHREADED


def _method(a, store, index, proto):
    ctypes = a["ctypes"]
    vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return a[proto](vtbl[index])


def _key(a, pid):
    return a["PROPERTYKEY"](a["fmtid"], pid)


def _set(a, store, values):
    """values: [(pid, text or None)] -> SetValue each (None = VT_EMPTY), Commit.
    True when all worked."""
    ctypes = a["ctypes"]
    setv = _method(a, store, 6, "SetValue")
    ok = True
    for pid, text in values:
        pv = a["PROPVARIANT"]()
        buf = None
        if text is not None:
            buf = ctypes.create_unicode_buffer(text)
            pv.vt = _VT_LPWSTR
            pv.p = ctypes.addressof(buf)                   # the store copies it
        ok = setv(store, ctypes.byref(_key(a, pid)), ctypes.byref(pv)) >= 0 and ok
    return _method(a, store, 7, "Commit")(store) >= 0 and ok


def _get(a, store, pids):
    ctypes = a["ctypes"]
    getv = _method(a, store, 5, "GetValue")
    out = {}
    for pid in pids:
        pv = a["PROPVARIANT"]()
        if getv(store, ctypes.byref(_key(a, pid)), ctypes.byref(pv)) >= 0:
            out[pid] = ctypes.wstring_at(pv.p) if pv.vt == _VT_LPWSTR and pv.p else None
            a["ole32"].PropVariantClear(ctypes.byref(pv))
    return out


def _window_store(a, hwnd):
    ctypes = a["ctypes"]
    _com_init(a)
    store = ctypes.c_void_p()
    hr = a["shell32"].SHGetPropertyStoreForWindow(hwnd, ctypes.byref(a["iid_store"]),
                                                  ctypes.byref(store))
    return store if hr >= 0 and store.value else None


def _file_store(a, path, write):
    ctypes = a["ctypes"]
    _com_init(a)
    store = ctypes.c_void_p()
    hr = a["shell32"].SHGetPropertyStoreFromParsingName(
        os.path.abspath(path), None, 0x2 if write else 0x0,      # GPS_READWRITE / GPS_DEFAULT
        ctypes.byref(a["iid_store"]), ctypes.byref(store))
    return store if hr >= 0 and store.value else None


def _release(a, store):
    try:
        _method(a, store, 2, "Release")(store)
    except Exception:
        pass


# ---------------------------------------------------------------- process
def set_process():
    """Give this process the app's ID - before any window exists."""
    a = _win()
    if a is None:
        return False
    try:
        return a["shell32"].SetCurrentProcessExplicitAppUserModelID(APP_ID) >= 0
    except Exception:
        return False


def process_id():
    """The process's explicit ID ("" when none) - for checks."""
    a = _win()
    if a is None:
        return ""
    ctypes = a["ctypes"]
    p = ctypes.c_void_p()
    try:
        if a["shell32"].GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p)) < 0 or not p.value:
            return ""
        try:
            return ctypes.wstring_at(p.value)
        finally:
            a["ole32"].CoTaskMemFree(p)
    except Exception:
        return ""


# ---------------------------------------------------------------- windows
def relaunch():
    """(command, icon resource) that start this app again - what a pin of
    the running window starts."""
    if getattr(sys, "frozen", False):
        from . import config as C
        exe = ""
        if C.PORTABLE:
            try:
                from .updater import launcher_path
                exe = launcher_path()
            except Exception:
                exe = ""
        if not exe or not os.path.isfile(exe):
            exe = os.path.abspath(sys.executable)          # installed / zip build
        return f'"{exe}"', f"{exe},0"
    # from source: windowless Python + the launcher script
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "intro_credits_toolkit.py")
    py = os.path.abspath(sys.executable)
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        py = pyw
    return f'"{py}" "{script}"', os.path.join(root, "app", "assets", "icon.ico") + ",0"


def set_window(hwnd):
    """The ID + relaunch properties on top-level frame window `hwnd`."""
    a = _win()
    if a is None or not hwnd:
        return False
    from .config import APP_NAME
    try:
        cmd, icon = relaunch()
        store = _window_store(a, hwnd)
        if store is None:
            return False
        try:
            # the relaunch properties first: the ID makes the taskbar look
            return _set(a, store, [(PID_RELAUNCH_COMMAND, cmd), (PID_RELAUNCH_ICON, icon),
                                   (PID_RELAUNCH_NAME, APP_NAME), (PID_ID, APP_ID)])
        finally:
            _release(a, store)
    except Exception:
        return False


def clear_window(hwnd):
    """Remove the properties again (before the window is destroyed)."""
    a = _win()
    if a is None or not hwnd:
        return False
    try:
        store = _window_store(a, hwnd)
        if store is None:
            return False
        try:
            return _set(a, store, [(PID_ID, None), (PID_RELAUNCH_COMMAND, None),
                                   (PID_RELAUNCH_ICON, None), (PID_RELAUNCH_NAME, None)])
        finally:
            _release(a, store)
    except Exception:
        return False


def window_props(hwnd):
    """{pid: text or None} of the four properties of `hwnd` - for checks."""
    a = _win()
    store = _window_store(a, hwnd) if a is not None and hwnd else None
    if store is None:
        return {}
    try:
        return _get(a, store, (PID_ID, PID_RELAUNCH_COMMAND, PID_RELAUNCH_ICON, PID_RELAUNCH_NAME))
    finally:
        _release(a, store)


def _frame(w):
    from .ui.dpi import frame_of
    return frame_of(w)


def _on_map(e):
    w = e.widget
    if isinstance(w, str):
        return
    try:
        if w.winfo_toplevel() is not w or w.wm_overrideredirect():
            return                        # tooltips, menus, pop-ups
        path = str(w)
        h = _frame(w)
    except Exception:
        return
    if h and _W.windows.get(path) != h and set_window(h):
        _W.windows[path] = h


def _on_destroy(e):
    h = _W.windows.pop(str(e.widget), None)
    if h:
        clear_window(h)


def install(root):
    """Every top-level window gets the ID + relaunch properties (when it is
    first mapped; the main window right away if its frame exists already)
    and loses them when it is destroyed."""
    if _win() is None:
        return False
    try:
        for cls in ("Tk", "Toplevel"):
            root.bind_class(cls, "<Map>", _on_map, add="+")
            root.bind_class(cls, "<Destroy>", _on_destroy, add="+")
        h = _frame(root)
        if h and set_window(h):
            _W.windows[str(root)] = h
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- shortcuts
def stamp_shortcut(lnk):
    """Write APP_ID into shortcut `lnk` (PKEY_AppUserModel_ID). True when
    done."""
    a = _win()
    if a is None or not os.path.isfile(lnk):
        return False
    try:
        store = _file_store(a, lnk, True)
        if store is None:
            return False
        try:
            return _set(a, store, [(PID_ID, APP_ID)])
        finally:
            _release(a, store)
    except Exception:
        return False


def shortcut_id(lnk):
    """The AppUserModelID stored in shortcut `lnk` ("" when none)."""
    a = _win()
    store = _file_store(a, lnk, False) if a is not None and os.path.isfile(lnk) else None
    if store is None:
        return ""
    try:
        return _get(a, store, (PID_ID,)).get(PID_ID) or ""
    finally:
        _release(a, store)


def cli(paths):
    """`--set-shortcut-aumid <lnk>...` (the installer, after it created its
    shortcuts): stamp each existing one. Exit code 0 = all done (missing
    files are skipped), 1 = a shortcut couldn't be written."""
    rc = 0
    for p in paths:
        if os.path.isfile(p) and not (stamp_shortcut(p) and shortcut_id(p) == APP_ID):
            rc = 1
    return rc
