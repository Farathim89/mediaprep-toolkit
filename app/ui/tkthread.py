"""Thread-safe 'run this on the Tk thread' queue, drained by the Tk loop
(set up in main()). Used by the job registry, notifications and the updater."""
import queue
import tkinter as tk



# thread-safe "run this on the Tk thread" queue, drained by the Tk loop (set
# up in main()). Used by the job registry, notifications and the updater.
_TK_CALLS = queue.Queue()


def _call_tk(fn):
    _TK_CALLS.put(fn)


def _drain_tk_calls(root):
    try:
        while True:
            fn = _TK_CALLS.get_nowait()
            try:
                fn()
            except Exception:
                pass
    except queue.Empty:
        pass
    try:
        root.after(50, _drain_tk_calls, root)
    except tk.TclError:
        pass
