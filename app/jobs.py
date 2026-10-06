"""App-wide job registry + a sequential job queue.

Every long-running tab job (batch cut, detection, gain batch, check, ...)
registers itself here so the status bar, "Stop all", close-window handling and
the done-notifications see every job in one place:

    jid = jobs.begin("Cut / Edit batch", stop_event=self.stop_event, tab=self)
    ...                                   # worker runs
    jobs.end(jid, ok=True, summary="12 files cut, 0 failed")

The QUEUE runs jobs one after another (e.g. several batches overnight):

    jobs.enqueue("Cut / Edit batch - Season 2", start_fn)

start_fn is called on the Tk thread when nothing else is running; it should
start the tab's job, which calls jobs.begin() synchronously inside start_fn
(or start_fn returns the job id). The queue then waits for that job's end()
before it starts the next entry. If start_fn starts nothing (validation
failed, tab busy, ...) the entry is dropped as "not started" and the queue
moves on.

Independent of Tk: subscriber callbacks run through a dispatcher installed
with set_dispatcher(fn) - fn(callable) must run the callable on the Tk thread
(app.py passes a thread-safe queue that the Tk loop drains). Without a
dispatcher callbacks run directly in the calling thread (tests)."""
import itertools
import threading
import time

_LOCK = threading.RLock()
_IDS = itertools.count(1)
_JOBS = {}              # job_id -> dict
_HISTORY = []           # finished jobs, newest last (capped)
_HISTORY_MAX = 50
_SUBS = []
_DISPATCH = None

_QUEUE = []             # [{"qid", "name", "start_fn", "added"}]
_QIDS = itertools.count(1)
_PAUSED = False
_ACTIVE = None          # queue entry currently running: dict(entry, job_id)
_CAPTURE = None         # list collecting job ids begun while a start_fn runs
_PUMP_PENDING = False
_BUSY_CHECK = None      # optional extra "something is busy" probe (app.py)


# ----------------------------------------------------------------- dispatch
def set_dispatcher(fn):
    """fn(callable) runs callable on the GUI thread (None = call directly)."""
    global _DISPATCH
    _DISPATCH = fn


def set_busy_check(fn):
    """Optional fn() -> bool that reports jobs NOT registered here (legacy
    tabs). The queue doesn't start the next entry while it returns True."""
    global _BUSY_CHECK
    _BUSY_CHECK = fn


def subscribe(callback):
    """callback(event, info) on the GUI thread. event is "begin" / "end"
    (info = the job dict) or "queue" (info = None) when the queue changed."""
    with _LOCK:
        if callback not in _SUBS:
            _SUBS.append(callback)


def unsubscribe(callback):
    with _LOCK:
        if callback in _SUBS:
            _SUBS.remove(callback)


def _dispatch(fn):
    d = _DISPATCH
    if d is None:
        fn()
        return
    try:
        d(fn)
    except Exception:
        pass


def _emit(event, info=None):
    with _LOCK:
        subs = list(_SUBS)
    if not subs:
        return

    def run():
        for cb in subs:
            try:
                cb(event, info)
            except Exception:
                pass
    _dispatch(run)


# ----------------------------------------------------------------- registry
def _public(j):
    d = {k: v for k, v in j.items() if k != "stop_event"}
    end = j.get("ended") or time.time()
    d["elapsed"] = max(0.0, end - j["started"])
    return d


def begin(name, stop_event=None, tab=None):
    """Register a running job; returns its id. stop_event (a threading.Event)
    is set by stop_all(). Safe to call from any thread."""
    with _LOCK:
        jid = next(_IDS)
        _JOBS[jid] = {"id": jid, "name": str(name), "tab": tab,
                      "stop_event": stop_event, "started": time.time(),
                      "queued": False}
        if _CAPTURE is not None:
            _CAPTURE.append(jid)
            _JOBS[jid]["queued"] = True
        info = _public(_JOBS[jid])
    _emit("begin", info)
    return jid


def end(job_id, ok=True, summary=""):
    """Mark a job finished (unknown / already ended ids are ignored). Safe to
    call from any thread; a second call for the same id is a no-op."""
    global _ACTIVE
    with _LOCK:
        j = _JOBS.pop(job_id, None)
        if j is None:
            return
        j["ended"] = time.time()
        j["ok"] = bool(ok)
        j["summary"] = str(summary or "")
        j["stopped"] = bool(j.get("stop_event") is not None
                            and j["stop_event"].is_set())
        info = _public(j)
        _HISTORY.append(info)
        del _HISTORY[:-_HISTORY_MAX]
        if _ACTIVE is not None and _ACTIVE.get("job_id") == job_id:
            _ACTIVE = None
    _emit("end", info)
    _schedule_pump()


def running():
    """Snapshot of the running jobs: [{id, name, tab, started, elapsed, queued}]."""
    with _LOCK:
        return [_public(j) for j in _JOBS.values()]


def is_busy():
    with _LOCK:
        return bool(_JOBS)


def history():
    with _LOCK:
        return list(_HISTORY)


def stop_all(pause_queue=True):
    """Set every running job's stop event. By default the queue is paused
    too, so stopping doesn't simply start the next queued entry."""
    global _PAUSED
    with _LOCK:
        evs = [j["stop_event"] for j in _JOBS.values() if j.get("stop_event")]
        if pause_queue and _QUEUE:
            _PAUSED = True
    for ev in evs:
        try:
            ev.set()
        except Exception:
            pass
    if pause_queue:
        _emit("queue")


# -------------------------------------------------------------------- queue
def enqueue(name, start_fn):
    """Add a job to the queue; it starts when nothing else is running.
    Returns the queue entry id."""
    with _LOCK:
        qid = next(_QIDS)
        _QUEUE.append({"qid": qid, "name": str(name), "start_fn": start_fn,
                       "added": time.time()})
    _emit("queue")
    _schedule_pump()
    return qid


def queued():
    """Waiting entries in order: [{qid, name, added}]."""
    with _LOCK:
        return [{"qid": e["qid"], "name": e["name"], "added": e["added"]}
                for e in _QUEUE]


def active():
    """Name of the queue entry currently running, or None."""
    with _LOCK:
        return _ACTIVE["name"] if _ACTIVE else None


def remove(idx):
    """Remove the waiting entry at position idx. Returns True if removed."""
    with _LOCK:
        if not 0 <= idx < len(_QUEUE):
            return False
        del _QUEUE[idx]
    _emit("queue")
    return True


def move(idx, delta):
    """Move a waiting entry up (-1) or down (+1)."""
    with _LOCK:
        j = idx + delta
        if not (0 <= idx < len(_QUEUE) and 0 <= j < len(_QUEUE)):
            return False
        _QUEUE[idx], _QUEUE[j] = _QUEUE[j], _QUEUE[idx]
    _emit("queue")
    return True


def clear():
    with _LOCK:
        _QUEUE.clear()
    _emit("queue")


def pause():
    global _PAUSED
    with _LOCK:
        _PAUSED = True
    _emit("queue")


def resume():
    global _PAUSED
    with _LOCK:
        _PAUSED = False
    _emit("queue")
    _schedule_pump()


def is_paused():
    with _LOCK:
        return _PAUSED


def _schedule_pump():
    global _PUMP_PENDING
    with _LOCK:
        if _PUMP_PENDING:
            return
        _PUMP_PENDING = True

    def run():
        global _PUMP_PENDING
        with _LOCK:
            _PUMP_PENDING = False
        pump()
    _dispatch(run)


def _extra_busy():
    fn = _BUSY_CHECK
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def pump():
    """Start the next queued entry if the queue is idle. Call on the GUI
    thread (the registry calls it itself after end()/enqueue()/resume();
    app.py also calls it from its status poll for legacy busy tabs)."""
    global _ACTIVE, _CAPTURE
    while True:
        with _LOCK:
            if _PAUSED or _ACTIVE is not None or not _QUEUE or _JOBS:
                return
        if _extra_busy():
            return
        with _LOCK:
            if _PAUSED or _ACTIVE is not None or not _QUEUE or _JOBS:
                return
            entry = _QUEUE.pop(0)
            _ACTIVE = {"name": entry["name"], "job_id": None}
            _CAPTURE = []
        ret, err = None, None
        try:
            ret = entry["start_fn"]()
        except Exception as e:      # a broken start_fn must not kill the queue
            err = e
        with _LOCK:
            begun = _CAPTURE or []
            _CAPTURE = None
            jid = ret if (isinstance(ret, int) and not isinstance(ret, bool)) else None
            if jid is None and begun:
                jid = begun[0]
            if jid is not None and jid in _JOBS:
                _JOBS[jid]["queued"] = True
                _ACTIVE = {"name": entry["name"], "job_id": jid}
                started = True
            else:
                _ACTIVE = None
                started = False
                info = {"id": None, "name": entry["name"], "ok": False,
                        "summary": (f"not started: {err}" if err else
                                    "not started (the tab refused or was busy)"),
                        "elapsed": 0.0, "queued": True, "stopped": False,
                        "started": time.time(), "ended": time.time()}
                _HISTORY.append(info)
                del _HISTORY[:-_HISTORY_MAX]
        _emit("queue")
        if started:
            return
        _emit("end", info)
        # entry didn't start -> try the next one


def _reset_for_tests():
    """Drop all state (unit tests only)."""
    global _PAUSED, _ACTIVE, _CAPTURE, _PUMP_PENDING, _DISPATCH, _BUSY_CHECK
    with _LOCK:
        _JOBS.clear()
        _HISTORY.clear()
        _SUBS.clear()
        _QUEUE.clear()
        _PAUSED = False
        _ACTIVE = None
        _CAPTURE = None
        _PUMP_PENDING = False
        _DISPATCH = None
        _BUSY_CHECK = None
