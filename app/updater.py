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
        portable   MediaPrep-Toolkit-Portable-<ver>.exe
        zip        MediaPrep.Toolkit.v<ver>.zip     (GitHub turns spaces into dots)
    with the SHA-256 from the asset's "digest" ("sha256:<hex>") field.
  * download(): streams it to Data\\temp\\update\\ (<name>.part, renamed when
    complete), checks size + SHA-256, can be cancelled.
  * installed: run_installer() starts "Setup.exe /S /RUN /D=<this folder>"
    elevated (ShellExecute "runas" - one UAC prompt); the app then closes, the
    installer waits for it, replaces the program files (Media\\ and Data\\
    stay) and /RUN starts the new version.
  * portable: place_portable() moves the new exe next to the current one
    (MEDIAPREP_LAUNCHER, set by the portable launcher) and start_detached()
    starts it; it unpacks its runtime and removes the old one. The new
    version then offers to move the old exe to Data\\temp\\trash.
  * zip / source: the release page is opened (extract the new zip over the
    old folder - Media\\ and Data\\ are not in the zip).
  * write_pending() / take_pending(): a small marker in Data\\temp\\update so
    the next start can say "Updated to X" (and offer the old-exe cleanup).

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
import subprocess
import sys
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
PENDING = "pending.json"

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


def asset_name(kind, version):
    version = strip_v(version)
    return {"installed": f"MediaPrep-Toolkit-Setup-{version}.exe",
            "portable": f"MediaPrep-Toolkit-Portable-{version}.exe",
            "zip": f"MediaPrep.Toolkit.v{version}.zip"}.get(kind)


def pick_asset(release, kind):
    """The asset dict of `release` for install type `kind`, or None."""
    if not release:
        return None
    want = asset_name(kind, release.get("version"))
    if not want:
        return None
    norm = want.lower().replace(" ", ".")
    for a in release.get("assets") or ():
        if str(a.get("name", "")).lower().replace(" ", ".") == norm:
            return a
    return None


def update_dir():
    return os.path.join(C.TEMP_DIR, "update")


# ======================= download + verify =======================
def sha256_file(path, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


def download(asset, dest_dir=None, progress=None, cancel=None, verify=True, timeout=30):
    """Download `asset` into dest_dir (default Data\\temp\\update) and return the
    file path. progress(done_bytes, total_bytes) is called from this thread;
    cancel = a threading.Event. verify=True needs asset['sha256'] and checks
    it (verify=False only when the user agreed to an unverified file).
    Raises Cancelled / UpdateError."""
    url = str(asset.get("url") or "")
    if not (url.startswith("https://") or (_test_api() and url.startswith("http://"))):
        raise UpdateError(tr("The download address is not secure (https): {url}", url=url))
    want = asset.get("sha256")
    if verify and not want:
        raise UpdateError(tr("The release lists no SHA-256 checksum for {name}.",
                             name=asset.get("name", "?")))
    dest_dir = dest_dir or update_dir()
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(str(asset.get("name") or "download"))
    target = os.path.join(dest_dir, name)
    part = target + ".part"
    total = int(asset.get("size") or 0)
    # already downloaded and intact (e.g. a second try) -> reuse
    if verify and os.path.isfile(target):
        try:
            if sha256_file(target, cancel) == want:
                if progress:
                    progress(total or os.path.getsize(target), total or os.path.getsize(target))
                return target
        except OSError:
            pass
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(),
                                               "Accept": "application/octet-stream"})
    h = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as out:
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


def run_installer(setup, install_dir):
    """Start Setup.exe silently and elevated (one UAC prompt). Returns
    (verb, params) on success; raises UpdateError (e.g. UAC declined)."""
    params = installer_params(install_dir)
    verb = "runas"
    if os.environ.get("MEDIAPREP_UPDATE_NOELEVATE", "").strip() not in ("", "0"):
        # test hook: run as the invoker (the installer then installs per user)
        verb = "open"
        os.environ["__COMPAT_LAYER"] = "RunAsInvoker"
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


# ======================= portable: new exe next to the old one =======================
def launcher_path():
    """The portable exe the user started (MEDIAPREP_LAUNCHER from the launcher,
    the one-file exe itself, or a guess in the folder above mediaprep-data)."""
    env = os.environ.get("MEDIAPREP_LAUNCHER", "").strip()
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    if getattr(sys, "mediaprep_portable", False):
        return os.path.abspath(sys.executable)
    folder = os.path.dirname(C.APP_ROOT)
    cand = os.path.join(folder, asset_name("portable", APP_VERSION))
    if os.path.isfile(cand):
        return cand
    try:
        hits = sorted(f for f in os.listdir(folder)
                      if f.lower().startswith("mediaprep-toolkit-portable")
                      and f.lower().endswith(".exe"))
    except OSError:
        hits = []
    return os.path.join(folder, hits[-1]) if hits else ""


def place_portable(downloaded, folder):
    """Move the downloaded portable exe into `folder` (next to the current
    one). An existing file of that name is kept when it is the same file,
    else the new one gets a ' (2)' name. Returns the final path."""
    name = os.path.basename(downloaded)
    target = os.path.join(folder, name)
    if os.path.isfile(target):
        try:
            if sha256_file(target) == sha256_file(downloaded):
                _remove(downloaded)
                return target
        except OSError:
            pass
        stem, ext = os.path.splitext(name)
        n = 2
        while os.path.exists(os.path.join(folder, f"{stem} ({n}){ext}")):
            n += 1
        target = os.path.join(folder, f"{stem} ({n}){ext}")
    try:
        shutil.move(downloaded, target)
    except OSError as exc:
        raise UpdateError(tr("Could not put the new version next to the old one: "
                             "{error}", error=exc))
    return target


def clean_env():
    """Environment for a NEW, independent app process (no PyInstaller /
    portable-runtime variables of this run)."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in list(env):
        if k.startswith("_PYI_") or k == "_MEIPASS2" or k in (
                "MEDIAPREP_PORTABLE_DATA", "MEDIAPREP_LAUNCHER", "__COMPAT_LAYER"):
            env.pop(k, None)
    return env


def start_detached(exe, args=()):
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe), env=clean_env(),
                         close_fds=True, creationflags=flags)
    except OSError as exc:
        raise UpdateError(tr("Could not start the new version: {error}", error=exc))


def move_to_trash(path):
    """Move a file to Data\\temp\\trash (never a hard delete). Returns the new path."""
    os.makedirs(C.TRASH_DIR, exist_ok=True)
    base = os.path.basename(path)
    target = os.path.join(C.TRASH_DIR, base)
    if os.path.exists(target):
        stem, ext = os.path.splitext(base)
        target = os.path.join(C.TRASH_DIR, f"{stem} {time.strftime('%Y%m%d-%H%M%S')}{ext}")
    shutil.move(path, target)
    return target


# ======================= marker for the next start =======================
def write_pending(kind, version, old_exe=""):
    try:
        os.makedirs(update_dir(), exist_ok=True)
        with open(os.path.join(update_dir(), PENDING), "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "from": APP_VERSION, "to": strip_v(version),
                       "old_exe": old_exe, "time": time.time()}, f, indent=2)
    except OSError:
        pass


def clear_pending():
    """Forget the marker (the update did not start)."""
    _remove(os.path.join(update_dir(), PENDING))


def take_pending():
    """The marker of an update started by the previous run (removed), plus
    'done' = this run is that version (or newer). Leftover downloads in
    Data\\temp\\update are removed (a file still in use stays for next time)."""
    d = update_dir()
    info = None
    path = os.path.join(d, PENDING)
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = None
    _remove(path)
    if os.path.isdir(d):
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                _remove(p)
    if not isinstance(info, dict) or not info.get("to"):
        return None
    info["done"] = not is_newer(info["to"])
    return info


def auto_flag():
    """Test hook MEDIAPREP_UPDATE_AUTO=<flag file>: True once (file removed)."""
    flag = os.environ.get("MEDIAPREP_UPDATE_AUTO", "").strip()
    if flag and os.path.isfile(flag):
        _remove(flag)
        return True
    return False
