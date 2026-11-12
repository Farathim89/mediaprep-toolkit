"""Thread-safe 'run this on the Tk thread' queue, drained by the Tk loop
(set up in main()). Used by the job registry, notifications and the updater."""
import queue
import tkinter as tk
import traceback

# thread-safe "run this on the Tk thread" queue, drained by the Tk loop (set
# up in main()). Used by the job registry, notifications and the updater.
_TK_CALLS = queue.Queue()
_MAX_LOGGED = 100            # failing calls logged per session (no log flood)
_errors = {"n": 0}


def _call_tk(fn):
    _TK_CALLS.put(fn)


def _log_failure(fn):
    """Record a queued call that raised in the session log."""
    _errors["n"] += 1
    if _errors["n"] > _MAX_LOGGED:
        return
    try:
        from .. import applog
        name = getattr(fn, "__qualname__", None) or repr(fn)
        msg = f"[ui] queued call {name} failed:\n{traceback.format_exc().rstrip()}"
        if _errors["n"] == _MAX_LOGGED:
            msg += "\n[ui] (further failing queued calls are not logged)"
        applog.record(msg)
    except Exception:
        pass


def _drain_tk_calls(root):
    """Run the queued calls, in order. The next drain is scheduled FIRST: a
    call that opens a modal message box runs a nested event loop, and the
    queue keeps draining inside it (job progress, other notifications)
    instead of stalling until the box is closed."""
    try:
        root.after(50, _drain_tk_calls, root)
    except tk.TclError:
        return                  # the window is gone
    while True:
        try:
            fn = _TK_CALLS.get_nowait()
        except queue.Empty:
            return
        try:
            fn()
        except Exception:
            _log_failure(fn)
