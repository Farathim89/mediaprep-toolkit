"""Update check + in-app update.

Check: asks GitHub for the latest release (at most once a day, or "Check
now") and reports when it is newer than config.APP_VERSION. Any failure
(offline, timeout, 404 = no release published yet, rate limit) is silent.

Update now (ui/update_dialog.py drives it, this module has the UI-free parts):
  * install_type(): "installed" (Setup.exe install - the uninstaller sits next
    to the exe, or the registry InstallLocation is this folder), "portable"
    (config.PORTABLE), "zip" (any other frozen folder build), "source"
    (python intro_credits_toolkit.py).
  * pick_asset(): the release file for that type -
        installed  MediaPrep-Toolkit-Setup-<ver>.exe
        portable   MediaPrep-Toolkit-Portable.exe   (the same name in every
                   release; MediaPrep-Toolkit-Portable-<ver>.exe as a fallback)
        zip        MediaPrep.Toolkit.v<ver>.zip     (GitHub turns spaces into dots)
    with the SHA-256 from the asset's "digest" ("sha256:<hex>") field.
  * download(): streams it into a NEW private folder in the user's own
    %TEMP% (MediaPrep-update-xxxx, tempfile.mkdtemp; <name>.part, renamed
    when complete), checks size + SHA-256, can be cancelled. Never into the
    app's Data\\ - in an installed copy every user may write there, and the
    installer is run elevated.
  * installed: run_installer() opens Setup.exe so that nobody can change,
    replace or delete it (share mode read-only, no symlink / junction),
    hashes it AGAIN through that handle and - while it is still held -
    starts "Setup.exe /S /RUN /D=<this folder>" elevated (ShellExecute
    "runas" - one UAC prompt); the app then closes, the installer waits for
    it, replaces the program files (Media\\ and Data\\ stay) and /RUN starts
    the new version. A failed silent install leaves its reason in the
    registry (LastSetupError), reported by the next start.
  * portable: replace_launcher() puts the new exe IN PLACE of the one the
    user started (MEDIAPREP_LAUNCHER, set by the portable launcher - any file
    name, so shortcuts keep working): locked + re-hashed, copied to
    <exe>.new, <exe> -> <exe>.old, <exe>.new -> <exe> (put back on any
    failure). The running app is the unpacked runtime, not that exe, so the
    file is normally free. start_detached() then starts <exe> again
    (MEDIAPREP_WAIT_PID: the new version waits until this process has saved
    its settings and ended); its launcher unpacks the new runtime and removes
    the old one. The next start removes <exe>.old
    (cleanup_launcher_leftovers, from take_pending).
  * zip / source: the release page is opened (extract the new zip over the
    old folder - Media\\ and Data\\ are not in the zip).
  * write_pending() / take_pending(): a small per-user marker
    (%LOCALAPPDATA%\\MediaPrep\\update\\pending-<hash of the app folder>.json)
    so the next start can say "Updated to X"; take_pending() also removes
    leftover download folders and the portable <exe>.old. (A marker of an
    older development build may still name an "old_exe" that was put next to
    the new one - app.py then offers to move it to Data\\temp\\trash.)

Test hooks (never set by the app itself): MEDIAPREP_UPDATE_API = another
release-JSON URL (e.g. a local test server; then http:// download URLs are
allowed too), MEDIAPREP_UPDATE_NOELEVATE=1 = start Setup without the UAC
"runas" verb (as the invoker), MEDIAPREP_UPDATE_AUTO=<flag file> = press
"Update now" automatically once (the flag file is removed)."""
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import config as C
from .config import APP_VERSION, UPDATE_REPO, APP_NAME
from .i18n import tr

API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
TIMEOUT = 5                  # seconds
CHECK_INTERVAL = 24 * 3600   # at most once per day (unless "Check now")
UNINSTALLER = "Uninstall.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MediaPrep Toolkit"
APP_EXE = "MediaPrep Toolkit.exe"
PENDING = "pending.json"            # legacy marker name (Data\\temp\\update)
DL_PREFIX = "MediaPrep-update-"     # the private download folders in %TEMP%
PORTABLE_EXE = "MediaPrep-Toolkit-Portable.exe"     # release asset + build output name
# the portable exe's payload trailer (tools/portable_launcher/Launcher.cs):
# version(16) sha256(32) offset(int64) length(int64) magic(8)
TRAILER_SIZE = 72
TRAILER_MAGIC = b"MPPAYLD1"
SWAP_WAIT = 10.0            # seconds a portable exe in use is waited for (replace_launcher)

# the latest release seen by the check (dict from fetch_release), for "Update now"
_LATEST = {"release": None}


class UpdateError(Exception):
    """A failed step - the message is shown to the user."""


class Cancelled(Exception):
    pass


def parse_version(v):
    """'v2.10.1' -> (2, 10, 1). Non-numeric suffixes ('-beta') are ignored."""
    s = str(v or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    m = re.match(r"^\s*(\d+(?:\.\d+)*)", s)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote, local=APP_VERSION):
    """True if version string `remote` is numerically greater than `local`
    (2.10 > 2.9, 2.0.1 > 2.0, 2.0 == 2.0.0)."""
    a, b = parse_version(remote), parse_version(local)
    if not a:
        return False
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def strip_v(tag):
    tag = str(tag or "").strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag


# ======================= release info =======================
def api_url():
    return os.environ.get("MEDIAPREP_UPDATE_API", "").strip() or API_URL


def _test_api():
    return bool(os.environ.get("MEDIAPREP_UPDATE_API", "").strip())


def _user_agent():
    return f"{APP_NAME.replace(' ', '-')}/{APP_VERSION}"


def parse_release(data):
    """The GitHub 'latest release' JSON -> {version, tag, url, notes, assets:
    [{name, url, size, sha256}]} or None (draft / prerelease / unusable)."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or "").strip()
    if not tag or not (url.startswith("https://") or (_test_api() and url.startswith("http"))):
        return None
    assets = []
    for a in data.get("assets") or ():
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        dl = str(a.get("browser_download_url") or "")
        if not name or not dl:
            continue
        try:
            size = int(a.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append({"name": name, "url": dl, "size": size,
                       "sha256": parse_digest(a.get("digest"))})
    return {"version": strip_v(tag), "tag": tag, "url": url,
            "notes": str(data.get("body") or ""), "assets": assets}


def parse_digest(digest):
    """'sha256:<64 hex>' -> the lower-case hex, else None."""
    m = re.match(r"^\s*sha256:([0-9a-fA-F]{64})\s*$", str(digest or ""))
    return m.group(1).lower() if m else None


def fetch_release(timeout=TIMEOUT):
    """The latest release (parse_release dict) or None (no release / error)."""
    req = urllib.request.Request(api_url(), headers={
        "User-Agent": _user_agent(), "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None             # includes HTTPError 404 (no release yet)
    except Exception:
        return None
    rel = parse_release(data)
    if rel is not None:
        _LATEST["release"] = rel
    return rel


def fetch_latest(timeout=TIMEOUT):
    """(tag, html_url) of the latest release, or None (no release / error)."""
    rel = fetch_release(timeout)
    return (rel["tag"], rel["url"]) if rel else None


def latest_release():
    """The release the last check found (or None)."""
    return _LATEST["release"]


def check_async(prefs, on_newer, on_done=None, force=False, save=None):
    """Background check. on_newer(version, url) / on_done(result_text) are
    called from the worker thread - wrap them with a Tk dispatcher.
    Skipped (returns False) when disabled or checked within the last 24 h,
    unless force=True. prefs['update_last_check'] is updated; save() is
    called afterwards to persist it."""
    if not force and not _test_api():
        if not prefs.get("update_check", True):
            return False
        try:
            last = float(prefs.get("update_last_check", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if 0 <= time.time() - last < CHECK_INTERVAL:
            return False

    def work():
        rel = fetch_release()
        prefs["update_last_check"] = time.time()
        if save is not None:
            try:
                save()
            except Exception:
                pass
        if rel and is_newer(rel["version"]):
            try:
                on_newer(rel["version"], rel["url"])
            except Exception:
                pass
            text = tr("Version {version} is available.", version=rel["version"])
        elif rel:
            text = tr("You have the latest version ({version}).", version=APP_VERSION)
        else:
            text = tr("No release information available (offline, or no release "
                      "published yet).")
        if on_done is not None:
            try:
                on_done(text)
            except Exception:
                pass
    threading.Thread(target=work, daemon=True, name="update-check").start()
    return True


# ======================= install type / assets =======================
def _registry_install_dir():
    """InstallLocation of the Setup.exe install (HKLM, else HKCU), or ''."""
    try:
        import winreg
    except ImportError:
        return ""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as k:
                    v, _t = winreg.QueryValueEx(k, "InstallLocation")
                    if v:
                        return str(v)
            except OSError:
                pass
    return ""


def same_dir(a, b):
    try:
        return os.path.normcase(os.path.abspath(a).rstrip("\\/")) == \
            os.path.normcase(os.path.abspath(b).rstrip("\\/"))
    except (TypeError, ValueError):
        return False


def install_type():
    """'installed' / 'portable' / 'zip' / 'source' (see the module docstring)."""
    if not getattr(sys, "frozen", False):
        return "source"
    if C.PORTABLE:
        return "portable"
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.isfile(os.path.join(exe_dir, UNINSTALLER)):
        return "installed"
    reg = _registry_install_dir()
    if reg and same_dir(reg, exe_dir):
        return "installed"
    return "zip"


def asset_names(kind, version):
    """The release file names for install type `kind`, preferred first:
    the portable exe has the same name in every release (older releases:
    MediaPrep-Toolkit-Portable-<ver>.exe)."""
    version = strip_v(version)
    return {"installed": [f"MediaPrep-Toolkit-Setup-{version}.exe"],
            "portable": [PORTABLE_EXE, f"MediaPrep-Toolkit-Portable-{version}.exe"],
            "zip": [f"MediaPrep.Toolkit.v{version}.zip"]}.get(kind, [])


def asset_name(kind, version):
    names = asset_names(kind, version)
    return names[0] if names else None


def pick_asset(release, kind):
    """The asset dict of `release` for install type `kind`, or None."""
    if not release:
        return None
    assets = release.get("assets") or ()
    for want in asset_names(kind, release.get("version")):
        norm = want.lower().replace(" ", ".")
        for a in assets:
            if str(a.get("name", "")).lower().replace(" ", ".") == norm:
                return a
    return None


def legacy_update_dir():
    """Data\\temp\\update - where versions up to 2.1.0 downloaded to (now
    only cleaned up)."""
    return os.path.join(C.TEMP_DIR, "update")


def new_download_dir():
    """A fresh, private folder for one download in the user's own %TEMP%."""
    try:
        d = tempfile.mkdtemp(prefix=DL_PREFIX, dir=tempfile.gettempdir())
    except OSError as exc:
        raise UpdateError(tr("The download failed: {error}", error=exc))
    if _is_reparse(d):
        raise UpdateError(tr("The download failed: {error}", error=f"{d} is a link"))
    return d


def _is_reparse(path):
    """True for a symlink / junction (or another reparse point)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or stat.S_ISLNK(st.st_mode))


def sha256_file(path, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


# SHA-256 of each finished download (normcased path -> hex), so a file
# downloaded without a published checksum is still re-checked before its start
_DIGESTS = {}


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def download(asset, dest_dir=None, progress=None, cancel=None, verify=True, timeout=30):
    """Download `asset` into dest_dir (default: a new private folder in the
    user's %TEMP%, see new_download_dir) and return the file path.
    progress(done_bytes, total_bytes) is called from this thread; cancel = a
    threading.Event. verify=True needs asset['sha256'] and checks it
    (verify=False only when the user agreed to an unverified file).
    Raises Cancelled / UpdateError (a folder it made is then removed)."""
    url = str(asset.get("url") or "")
    if not (url.startswith("https://") or (_test_api() and url.startswith("http://"))):
        raise UpdateError(tr("The download address is not secure (https): {url}", url=url))
    want = asset.get("sha256")
    if verify and not want:
        raise UpdateError(tr("The release lists no SHA-256 checksum for {name}.",
                             name=asset.get("name", "?")))
    own_dir = not dest_dir
    dest_dir = dest_dir or new_download_dir()
    try:
        return _download_into(asset, url, want, dest_dir, progress, cancel, verify, timeout)
    except BaseException:
        if own_dir:
            shutil.rmtree(dest_dir, ignore_errors=True)
        raise


def _download_into(asset, url, want, dest_dir, progress, cancel, verify, timeout):
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(str(asset.get("name") or "download")) or "download"
    target = os.path.join(dest_dir, name)
    part = target + ".part"
    total = int(asset.get("size") or 0)
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(),
                                               "Accept": "application/octet-stream"})
    h = hashlib.sha256()
    done = 0
    try:
        _remove(part)
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "xb") as out:
            if not total:
                try:
                    total = int(r.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
            last = 0.0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and (now - last > 0.1):
                    last = now
                    progress(done, total)
        if progress:
            progress(done, total)
    except Cancelled:
        _remove(part)
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _remove(part)
        raise UpdateError(tr("The download failed: {error}", error=exc))
    if total and done != total:
        _remove(part)
        raise UpdateError(tr("The download is incomplete ({got} of {total} bytes).",
                             got=done, total=total))
    if verify and h.hexdigest() != want:
        _remove(part)
        raise UpdateError(tr("The downloaded file is damaged or was changed (SHA-256 "
                             "mismatch) - it was deleted. Please try again later."))
    try:
        os.replace(part, target)
    except OSError as exc:
        _remove(part)
        raise UpdateError(tr("The download failed: {error}", error=exc))
    _DIGESTS[_key(target)] = h.hexdigest()
    return target


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ======================= installed: run Setup elevated =======================
def installer_params(install_dir):
    """Setup.exe arguments for a silent in-place update (/D= must be last and
    unquoted, even with spaces)."""
    return f"/S /RUN /D={os.path.abspath(install_dir)}"


class LockedFile:
    """`path` opened for reading with share mode READ only: while it is open
    nobody can write, replace, rename or delete the file, so what was hashed
    is exactly what Windows starts. A symlink / junction / folder is refused
    (UpdateError). A context manager; sha256() hashes through the handle."""

    def __init__(self, path, tries=10):
        self.path, self.fd = path, None
        if os.name != "nt":                     # (tests on other systems)
            self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            return
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Info(ctypes.Structure):          # BY_HANDLE_FILE_INFORMATION
            _fields_ = [("attrs", wintypes.DWORD), ("times", wintypes.DWORD * 6),
                        ("volume", wintypes.DWORD), ("size_hi", wintypes.DWORD),
                        ("size_lo", wintypes.DWORD), ("links", wintypes.DWORD),
                        ("index_hi", wintypes.DWORD), ("index_lo", wintypes.DWORD)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        invalid = ctypes.c_void_p(-1).value
        h = None
        for i in range(tries):
            # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING,
            # FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN
            h = k32.CreateFileW(path, 0x80000000, 0x1, None, 3, 0x00200000 | 0x08000000, None)
            if h not in (None, invalid):
                break
            err = ctypes.get_last_error()
            if err != 32 or i == tries - 1:     # 32 = sharing violation (a scanner)
                raise OSError(err, ctypes.FormatError(err), path)
            time.sleep(0.2)
        info = _Info()
        ok = k32.GetFileInformationByHandle(h, ctypes.byref(info))
        # FILE_ATTRIBUTE_REPARSE_POINT 0x400 | FILE_ATTRIBUTE_DIRECTORY 0x10
        if not ok or info.attrs & (0x400 | 0x10):
            k32.CloseHandle(h)
            raise UpdateError(tr("The downloaded installer is a link or not a normal file "
                                 "- it was not started: {path}", path=path))
        self.fd = msvcrt.open_osfhandle(h, os.O_RDONLY)

    def sha256(self):
        h = hashlib.sha256()
        os.lseek(self.fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(self.fd, 1 << 20)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest()

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def run_installer(setup, install_dir, sha256=None):
    """Start Setup.exe silently and elevated (one UAC prompt). Right before
    that it is locked (LockedFile) and its SHA-256 checked again against
    `sha256` (default: the one measured by its download()); it stays locked
    until Windows has started it. Returns (verb, params) on success; raises
    UpdateError (e.g. UAC declined, the file was changed)."""
    params = installer_params(install_dir)
    want = str(sha256 or _DIGESTS.get(_key(setup)) or "").lower()
    if not want:
        raise UpdateError(tr("Could not start the installer: {error}",
                             error="no SHA-256 to check it against"))
    if _is_reparse(os.path.dirname(os.path.abspath(setup))) or _is_reparse(setup):
        raise UpdateError(tr("The downloaded installer is a link or not a normal file "
                             "- it was not started: {path}", path=setup))
    verb = "runas"
    if os.environ.get("MEDIAPREP_UPDATE_NOELEVATE", "").strip() not in ("", "0"):
        # test hook: run as the invoker (the installer then installs per user)
        verb = "open"
        os.environ["__COMPAT_LAYER"] = "RunAsInvoker"
    try:
        lock = LockedFile(setup)
    except OSError as exc:
        raise UpdateError(tr("Could not start the installer: {error}", error=exc))
    with lock:
        if lock.sha256() != want:
            lock.close()
            _remove(setup)
            raise UpdateError(tr("The downloaded file is damaged or was changed (SHA-256 "
                                 "mismatch) - it was deleted. Please try again later."))
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(None, verb, setup, params,
                                                     os.path.dirname(setup), 1)
        except Exception as exc:
            raise UpdateError(tr("Could not start the installer: {error}", error=exc))
    if rc <= 32:
        if rc == 5:                     # SE_ERR_ACCESSDENIED: UAC prompt declined
            raise UpdateError(tr("The update was cancelled (administrator permission "
                                 "was not given)."))
        raise UpdateError(tr("Could not start the installer: {error}",
                             error=f"ShellExecute {rc}"))
    return verb, params


def last_setup_error(since=0.0):
    """The reason the installer recorded for its last failed silent run
    (LastSetupError in its uninstall key, HKLM or HKCU) when that key was
    written at/after `since` (epoch seconds), else ''."""
    try:
        import winreg
    except ImportError:
        return ""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as k:
                    v, _t = winreg.QueryValueEx(k, "LastSetupError")
                    # last write time of the key: 100 ns units since 1601
                    written = winreg.QueryInfoKey(k)[2] / 1e7 - 11644473600.0
            except OSError:
                continue
            if v and written >= since - 5:
                return str(v)
    return ""


# ======================= portable: replace the exe in place =======================
def launcher_path():
    """The portable exe the user started (MEDIAPREP_LAUNCHER from the launcher,
    the one-file exe itself, or a guess in the folder above mediaprep-data)."""
    env = os.environ.get("MEDIAPREP_LAUNCHER", "").strip()
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    if getattr(sys, "mediaprep_portable", False):
        return os.path.abspath(sys.executable)
    folder = os.path.dirname(C.APP_ROOT)
    for name in asset_names("portable", APP_VERSION):
        cand = os.path.join(folder, name)
        if os.path.isfile(cand):
            return cand
    try:
        hits = sorted(f for f in os.listdir(folder)
                      if f.lower().startswith("mediaprep-toolkit-portable")
                      and f.lower().endswith(".exe"))
    except OSError:
        hits = []
    return os.path.join(folder, hits[-1]) if hits else ""


def _trailer_version(fd):
    """The payload version in the trailer of the open portable exe `fd`
    (tools/portable_launcher/Launcher.cs), or '' when it isn't one."""
    try:
        size = os.fstat(fd).st_size
        if size < TRAILER_SIZE + 2:
            return ""
        os.lseek(fd, 0, os.SEEK_SET)
        head = os.read(fd, 2)
        os.lseek(fd, size - TRAILER_SIZE, os.SEEK_SET)
        t = os.read(fd, TRAILER_SIZE)
    except OSError:
        return ""
    if head != b"MZ" or len(t) != TRAILER_SIZE or t[64:] != TRAILER_MAGIC:
        return ""
    offset, length = struct.unpack("<qq", t[48:64])
    if offset <= 0 or length <= 0 or offset + length > size - TRAILER_SIZE:
        return ""
    return t[:16].rstrip(b"\0").decode("ascii", "replace").strip()


def portable_version(path):
    """The payload version of the portable exe `path`, or '' (not one / unreadable)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return ""
    try:
        return _trailer_version(fd)
    finally:
        os.close(fd)


def _rename(src, dst):
    """Rename `src` to `dst` (same folder; never over an existing `dst`)."""
    if os.name != "nt":
        if os.path.lexists(dst):
            raise FileExistsError(17, "File exists", dst)
        os.rename(src, dst)
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    k32.MoveFileExW.restype = wintypes.BOOL
    if not k32.MoveFileExW(src, dst, 0x8):          # MOVEFILE_WRITE_THROUGH
        err = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(err).strip(), src, err, dst)


def _move_file(src, dst, deadline):
    """_rename(), retried while the file is in use (sharing / lock violation
    or access denied - a virus scanner, a backup tool, a test run) until
    time.monotonic() passes `deadline`. Raises the last OSError."""
    while True:
        try:
            _rename(src, dst)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
        time.sleep(0.25)


def _remove_retry(path, deadline):
    """Remove the file `path` (gone already = fine), retried like _move_file."""
    while True:
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
        time.sleep(0.25)


def _clear_file_slot(path, deadline):
    """Make room for a file at `path`: a stale regular file there (an earlier
    update's leftover) is removed; a folder or link is never touched."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or _is_reparse(path):
        raise OSError(f"a folder or link is in the way: {path}")
    _remove_retry(path, deadline)


def _copy_locked(fd, dest, deadline):
    """Copy the locked download `fd` into the NEW file `dest`, hashing what
    is written. Returns the SHA-256 hex (the caller compares it)."""
    _clear_file_slot(dest, deadline)
    h = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    with open(dest, "xb") as out:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
        out.flush()
        os.fsync(out.fileno())
    return h.hexdigest()


def replace_launcher(downloaded, launcher, sha256=None, wait=SWAP_WAIT):
    """The portable update: put the downloaded portable exe IN PLACE of
    `launcher` (the exe the user started - whatever its name, so shortcuts
    keep working). The running app is the unpacked runtime, not the
    launcher, so that file is normally free.

    The download is opened with LockedFile (nobody can change it), must be a
    portable exe (payload trailer) and is copied through that handle to
    <launcher>.new while its SHA-256 is checked against `sha256` (default:
    the one measured by its download()). Then <launcher> -> <launcher>.old
    and <launcher>.new -> <launcher> (renames in the same folder, retried
    while the file is in use for up to `wait` seconds). Any failure puts
    the old exe back under its name and raises UpdateError - nothing
    changed. Returns the backup path <launcher>.old (removed by the next
    start, see cleanup_launcher_leftovers; undo_replace() restores it).
    start_portable() then starts the new exe."""
    launcher = os.path.abspath(launcher)
    name = os.path.basename(launcher)
    new, old = launcher + ".new", launcher + ".old"

    def failed(error):
        return UpdateError(tr("Could not replace {name}: {error}\n\nNothing was changed.",
                              name=name, error=error))

    want = str(sha256 or _DIGESTS.get(_key(downloaded)) or "").lower()
    if not want:
        raise failed("no SHA-256 to check the download against")
    if _is_reparse(os.path.dirname(os.path.abspath(downloaded))) or _is_reparse(downloaded):
        raise failed(f"{downloaded} is a link or not a normal file")
    if not os.path.isfile(launcher) or _is_reparse(launcher):
        raise failed(f"{launcher} is not a normal file")
    deadline = time.monotonic() + wait
    try:
        lock = LockedFile(downloaded)
    except (OSError, UpdateError) as exc:
        raise failed(exc)
    with lock:
        if not _trailer_version(lock.fd):
            lock.close()
            _remove(downloaded)
            raise failed(f"{os.path.basename(downloaded)} is not a portable "
                         f"{APP_NAME} exe - it was deleted")
        try:
            got = _copy_locked(lock.fd, new, deadline)
        except OSError as exc:
            _remove(new)
            raise failed(exc)
        if got != want:
            lock.close()
            _remove(new)
            _remove(downloaded)
            raise UpdateError(tr("The downloaded file is damaged or was changed (SHA-256 "
                                 "mismatch) - it was deleted. Please try again later."))
    # the swap: the old exe steps aside (an earlier backup still there goes)
    try:
        _clear_file_slot(old, deadline)
        _move_file(launcher, old, deadline)
    except OSError as exc:
        _remove(new)
        raise UpdateError(tr("{name} is in use and could not be replaced: {error}\n\n"
                             "Nothing was changed. Close any program that uses it and "
                             "try again.", name=name, error=exc))
    try:
        _move_file(new, launcher, time.monotonic() + wait)
    except OSError as exc:
        try:                                    # roll back: the old exe goes back
            _move_file(old, launcher, time.monotonic() + wait)
        except OSError as exc2:
            raise UpdateError(tr(
                "Could not replace {name}, and the old version could not be put back: "
                "{error}\n\nRename {old} to {name} by hand.", name=name,
                old=os.path.basename(old), error=exc2))
        _remove(new)
        raise failed(exc)
    _DIGESTS[_key(launcher)] = want
    return old


def start_portable(exe, args=(), wait_pid=None, sha256=None):
    """Start the portable exe that replace_launcher() put in place (see
    start_detached). Like run_installer it is locked (LockedFile) and its
    SHA-256 checked again first, and stays locked until it has started."""
    want = str(sha256 or _DIGESTS.get(_key(exe)) or "").lower()
    if not want:
        raise UpdateError(tr("Could not start the new version: {error}",
                             error="no SHA-256 to check it against"))
    try:
        lock = LockedFile(exe)
    except (OSError, UpdateError) as exc:
        raise UpdateError(tr("Could not start the new version: {error}", error=exc))
    with lock:
        if lock.sha256() != want:
            raise UpdateError(tr("Could not start the new version: {error}",
                                 error=f"{exe} was changed (SHA-256 mismatch)"))
        start_detached(exe, args, wait_pid)


def undo_replace(launcher, backup, wait=SWAP_WAIT):
    """The new exe placed by replace_launcher() could not be started: put
    the old one (`backup`) back under the launcher's name. True when done
    (else the new exe stays in place)."""
    launcher = os.path.abspath(launcher)
    aside = launcher + ".new"
    deadline = time.monotonic() + wait
    try:
        _clear_file_slot(aside, deadline)
        _move_file(launcher, aside, deadline)
    except OSError:
        return False
    try:
        _move_file(backup, launcher, deadline)
    except OSError:
        try:
            _move_file(aside, launcher, time.monotonic() + wait)
        except OSError:
            pass
        return False
    _remove(aside)
    return True


def discard_download(path):
    """Remove a downloaded file and its private download folder (DL_PREFIX)."""
    _remove(path)
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.basename(folder).startswith(DL_PREFIX) and not _is_reparse(folder):
        try:
            os.rmdir(folder)
        except OSError:
            pass


def cleanup_launcher_leftovers(new_grace=600):
    """Portable copy: remove <exe>.old (the previous version, kept by
    replace_launcher until the new one had started) and an abandoned
    <exe>.new (older than `new_grace` seconds) next to the exe the user
    started. Only regular files that are portable exes of this app (payload
    trailer) - never a link or anything else. A file still in use stays
    for the next start. Returns the removed paths."""
    if not C.PORTABLE:
        return []
    exe = launcher_path()
    if not exe:
        return []
    out = []
    for path, grace in ((exe + ".old", 0), (exe + ".new", new_grace)):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or _is_reparse(path):
            continue
        if grace and 0 <= time.time() - st.st_mtime < grace:
            continue
        if not portable_version(path):
            continue
        try:
            os.remove(path)
            out.append(path)
        except OSError:
            pass
    return out


def clean_env():
    """Environment for a NEW, independent app process (no PyInstaller /
    portable-runtime variables of this run)."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in list(env):
        if k.startswith("_PYI_") or k == "_MEIPASS2" or k in (
                "MEDIAPREP_PORTABLE_DATA", "MEDIAPREP_LAUNCHER", "__COMPAT_LAYER",
                "MEDIAPREP_WAIT_PID"):
            env.pop(k, None)
    return env


def start_detached(exe, args=(), wait_pid=None):
    """Start `exe` as an independent process. wait_pid: the new app waits
    (up to 30 s) until that process has ended before it reads the settings
    (MEDIAPREP_WAIT_PID, see intro_credits_toolkit.py)."""
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    env = clean_env()
    if wait_pid:
        env["MEDIAPREP_WAIT_PID"] = str(int(wait_pid))
    try:
        subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe), env=env,
                         close_fds=True, creationflags=flags)
    except OSError as exc:
        raise UpdateError(tr("Could not start the new version: {error}", error=exc))


def move_to_trash(path):
    """Move a file to Data\\temp\\trash (never a hard delete). Returns the new
    path. (app.py: the "old_exe" of a marker from an older build.)"""
    os.makedirs(C.TRASH_DIR, exist_ok=True)
    base = os.path.basename(path)
    target = os.path.join(C.TRASH_DIR, base)
    if os.path.exists(target):
        stem, ext = os.path.splitext(base)
        target = os.path.join(C.TRASH_DIR, f"{stem} {time.strftime('%Y%m%d-%H%M%S')}{ext}")
    shutil.move(path, target)
    return target


# ======================= marker for the next start =======================
def _state_dir():
    """Per-user folder of the update markers (%LOCALAPPDATA%\\MediaPrep\\update)."""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "MediaPrep", "update")


def pending_path():
    """This copy's marker: one per app folder (APP_ROOT), per user."""
    key = hashlib.sha256(_key(C.APP_ROOT).encode("utf-8")).hexdigest()[:8]
    return os.path.join(_state_dir(), f"pending-{key}.json")


def write_pending(kind, version, old_exe=""):
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(pending_path(), "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "from": APP_VERSION, "to": strip_v(version),
                       "old_exe": old_exe, "time": time.time(), "root": C.APP_ROOT},
                      f, indent=2)
    except OSError:
        pass


def clear_pending():
    """Forget the marker (the update did not start)."""
    _remove(pending_path())


def _read_marker(path):
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = None
    _remove(path)
    return info if isinstance(info, dict) else None


def cleanup_downloads(min_age=3600):
    """Remove leftover download folders (%TEMP%\\MediaPrep-update-* older
    than min_age seconds; a Setup.exe still running stays) and the files of
    the old Data\\temp\\update folder."""
    tmp = tempfile.gettempdir()
    try:
        names = [n for n in os.listdir(tmp) if n.startswith(DL_PREFIX)]
    except OSError:
        names = []
    now = time.time()
    for n in names:
        p = os.path.join(tmp, n)
        try:
            if now - os.lstat(p).st_mtime < min_age:
                continue
        except OSError:
            continue
        if _is_reparse(p):
            try:
                os.rmdir(p)                 # the link itself, never its target
            except OSError:
                pass
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    d = legacy_update_dir()
    if os.path.isdir(d) and not _is_reparse(d):
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if os.path.isfile(p) and not _is_reparse(p):
                _remove(p)


def take_pending():
    """The marker of an update started by the previous run (removed), plus
    'done' = this run is that version (or newer) and, when not, 'error' = the
    installer's reason if it recorded one. Leftover downloads and the
    portable <exe>.old are removed (cleanup_downloads,
    cleanup_launcher_leftovers)."""
    info = _read_marker(pending_path())
    legacy = _read_marker(os.path.join(legacy_update_dir(), PENDING))   # <= 2.1.0
    if info is None and legacy is not None:
        info = legacy
        if not C.PORTABLE:
            info["old_exe"] = ""        # only trust a path from the user's own folder
    try:
        cleanup_downloads()
    except OSError:
        pass
    try:
        gone = cleanup_launcher_leftovers()
    except OSError:
        gone = []
    if gone:
        try:
            from . import applog
            applog.record("[update] removed the previous portable exe: " + ", ".join(gone))
        except Exception:
            pass
    if not isinstance(info, dict) or not info.get("to"):
        return None
    info["done"] = not is_newer(info["to"])
    if not info["done"] and info.get("kind") == "installed":
        try:
            since = float(info.get("time") or 0)
        except (TypeError, ValueError):
            since = 0.0
        info["error"] = last_setup_error(since)
        if info["error"]:
            try:
                from . import applog
                applog.record(f"[update] the installer reported: {info['error']}")
            except Exception:
                pass
    return info


def auto_flag():
    """Test hook MEDIAPREP_UPDATE_AUTO=<flag file>: True once (file removed)."""
    flag = os.environ.get("MEDIAPREP_UPDATE_AUTO", "").strip()
    if flag and os.path.isfile(flag):
        _remove(flag)
        return True
    return False
