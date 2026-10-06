"""Update check: asks GitHub for the latest release and reports when it is newer
than config.APP_VERSION. Nothing is downloaded or installed - the app only
shows a banner that opens the release page. Any failure (offline, timeout,
404 = no release published yet, rate limit) is silent."""
import json
import re
import threading
import time
import urllib.error
import urllib.request

from .config import APP_VERSION, UPDATE_REPO, APP_NAME
from .i18n import tr

API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
TIMEOUT = 5                  # seconds
CHECK_INTERVAL = 24 * 3600   # at most once per day (unless "Check now")


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


def fetch_latest(timeout=TIMEOUT):
    """(tag, html_url) of the latest release, or None (no release / error)."""
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": f"{APP_NAME.replace(' ', '-')}/{APP_VERSION}",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None             # includes HTTPError 404 (no release yet)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or "").strip()
    if not tag or not url.startswith("https://"):
        return None
    return tag, url


def check_async(prefs, on_newer, on_done=None, force=False, save=None):
    """Background check. on_newer(version, url) / on_done(result_text) are
    called from the worker thread - wrap them with a Tk dispatcher.
    Skipped (returns False) when disabled or checked within the last 24 h,
    unless force=True. prefs['update_last_check'] is updated; save() is
    called afterwards to persist it."""
    if not force:
        if not prefs.get("update_check", True):
            return False
        try:
            last = float(prefs.get("update_last_check", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if 0 <= time.time() - last < CHECK_INTERVAL:
            return False

    def work():
        res = fetch_latest()
        prefs["update_last_check"] = time.time()
        if save is not None:
            try:
                save()
            except Exception:
                pass
        if res and is_newer(res[0]):
            ver = res[0][1:] if res[0][:1] in ("v", "V") else res[0]
            try:
                on_newer(ver, res[1])
            except Exception:
                pass
            text = tr("Version {version} is available.", version=ver)
        elif res:
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
