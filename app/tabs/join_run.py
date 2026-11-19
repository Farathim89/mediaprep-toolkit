"""Join page (tabs/join.py): running a join - Join / Add to queue / Stop,
the checks before it starts (2+ readable files, none missing, an output
that replaces nothing unasked), the worker and the footer progress
("Joining 2/5: name" + "2/5 · 43.0% ETA 3:10")."""
import os
import re
import threading
import time
from tkinter import messagebox

from ..config import OUTPUT_DIR
from ..engine import join as jengine
from ..engine import recode
from ..engine.files import _same_path
from ..engine.formatting import format_seconds, format_size
from ..i18n import ntr, tr
from ..ui.dialogs import ask_choice
from ..ui.tkthread import _call_tk
from .. import jobs


def safe_name(text):
    """A file name stem Windows accepts (forbidden characters -> _)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text or "")).strip().strip(".")
    return s[:200].strip()


class JoinRunMixin:
    """Expects (JoinTab): self.ordered(), self.join_settings(), self.log(),
    self._refresh(), the vars / buttons built in join.py."""

    # ------------------------------------------------------------ snapshot
    def _snapshot_job(self):
        """What a join needs, taken now (a queued join keeps it): files in
        list order, output folder / name / container, settings. None (and
        a status line) when the list isn't ready."""
        if self._busy:
            self.status_var.set(tr("Busy - Stop the current job first."))
            return None
        rows = self.ordered()
        if len(rows) < 2:
            self.status_var.set(tr("Add at least 2 videos - they are joined in the order of "
                                   "the list."))
            return None
        infos = [it["info"] for _i, it in rows]
        if any(x == "?" for x in infos):
            self.status_var.set(tr("Still reading the files - try again in a moment."))
            return None
        if any(x is None for x in infos):
            self.status_var.set(tr("Some files can't be read - remove them first."))
            return None
        return {"iids": [i for i, _it in rows], "files": [it["path"] for _i, it in rows],
                "out_dir": self.out_dir.get().strip() or OUTPUT_DIR,
                "name": self.name_var.get().strip(), "container": self.container_var.get(),
                "settings": self.join_settings()}

    def _output_for(self, snap):
        """The output path for a snapshot (asks about an existing file:
        Overwrite / Keep both / Skip), or None = don't join."""
        missing = [p for p in snap["files"] if not os.path.isfile(p)]
        if missing:
            messagebox.showerror(tr("Error"), tr("These files are missing:\n{files}",
                                                 files="\n".join(missing[:6])))
            return None
        name = safe_name(snap["name"])
        if not name:
            messagebox.showerror(tr("Error"), tr("Invalid file name."))
            return None
        folder = snap["out_dir"]
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror(tr("Error"), f"{tr('Pick an output folder.')}\n\n{e}")
            return None
        out = os.path.join(folder, f"{name}.{snap['container']}")
        same = [p for p in snap["files"] if _same_path(p, out)]
        if same:
            messagebox.showerror(tr("Error"), tr(
                "The output would replace one of the files to join:\n\n{path}\n\nChoose "
                "another output folder or file name.", path=out))
            return None
        if os.path.exists(out):
            choice = ask_choice(self, tr("Files exist"), tr(
                "The output file exists already:\n{path}\n\nWhat should happen to it?",
                path=os.path.basename(out)),
                [("over", tr("Overwrite")), ("both", tr("Keep both")), ("skip", tr("Skip")),
                 ("cancel", tr("Cancel"))], default="both", cancel="cancel")
            if choice in (None, "cancel"):
                return None
            if choice == "skip":
                self.log(f"[SKIP] {out}: the output exists already")
                self.status_var.set(tr("Skipped - the output exists already."))
                return None
            if choice == "both":
                out = recode.keep_both_path(out)
        return out

    # ------------------------------------------------------------ start
    def start(self):
        snap = self._snapshot_job()
        if snap is None:
            return
        if self.save_hook:
            try:
                self.save_hook()
            except Exception:
                pass
        self._launch(snap)

    def _enqueue(self):
        snap = self._snapshot_job()
        if snap is None:
            return
        n = len(snap["files"])
        jobs.enqueue(ntr("Join - {n} file", "Join - {n} files", n), lambda: self._launch(snap))
        self.log(f"[QUEUE] added: Join - {n} file(s)")
        self.status_var.set(tr("Added to the queue."))

    def _launch(self, snap):
        """Start a join (Tk thread). Returns the job id, or None when nothing
        was started (the queue then moves on)."""
        if self._busy:
            self.log("[QUEUE] Join is busy - not started.")
            return None
        out = self._output_for(snap)
        if not out or self._busy:
            return None
        self.stop_event.clear()
        self._set_busy(True)
        for iid in snap["iids"]:
            it = self.items.get(iid)
            if it is not None:
                it["status"], it["tag"] = tr("Waiting"), ""
        self._refresh()
        self.status_var.set(tr("Joining..."))
        n = len(snap["files"])
        self._jid = jobs.begin(ntr("Join - {n} file", "Join - {n} files", n),
                               stop_event=self.stop_event, tab=self)
        threading.Thread(target=self._worker, args=(snap, out, self._jid), daemon=True).start()
        return self._jid

    def _set_busy(self, on, text=None):
        self._busy = on
        for b in (self.start_btn, self.qbtn, self.rm_btn, self.clear_btn, self.up_btn,
                  self.down_btn, self.sort_btn):
            b.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.status_var.set(text or tr("Idle"))
            self.bar.configure(value=0)
            self.pct_var.set("")

    def stop(self):
        self.stop_event.set()
        self.log("Stopping (the unfinished file is removed)...")

    # ------------------------------------------------------------ worker
    def _mark(self, iids, status, tag=""):
        def _u():
            for iid in iids:
                it = self.items.get(iid)
                if it is not None:
                    it["status"], it["tag"] = status, tag
            self._refresh()
        _call_tk(_u)

    def _worker(self, snap, out, jid):
        files, iids = snap["files"], snap["iids"]
        n = len(files)
        t0 = time.time()
        state = {"k": 0}
        ok, placed = False, None
        self.log(f"=== Join: {n} files -> {out} ===")
        for k, p in enumerate(files, 1):
            self.log(f"   {k}. {p}")

        def on_part(k, total):
            prev, state["k"] = state["k"], k
            if prev:
                self._mark(iids[prev - 1:k - 1], tr("Done"), "ok")
            self._mark(iids[k - 1:k], tr("Joining..."))
            name = os.path.basename(files[k - 1])
            _call_tk(lambda: self.status_var.set(
                tr("Joining {i}/{n}: {name}", i=k, n=total, name=name)))

        def prog(frac, text):
            k = state["k"] or 1
            _call_tk(lambda: (self.bar.configure(value=int(frac * 1000)),
                              self.pct_var.set(f"{k}/{n} · {text.strip()}")))
        try:
            ok, placed = jengine.join(files, out, snap["settings"], progress=prog,
                                      stop_event=self.stop_event, log=self.log, on_part=on_part)
        except Exception as e:          # never leave the page busy
            self.log(f"[FAIL] {e!r}")
        finally:
            stopped = self.stop_event.is_set() and not ok
            took = format_seconds(time.time() - t0)
            if ok:
                size = format_size(os.path.getsize(placed)) if os.path.isfile(placed) else ""
                self._mark(iids, tr("Done"), "ok")
                summary = f"{os.path.basename(placed)} ({size})"
                self.log(f"FINISHED. {placed} - {took}")
                text = tr("Finished: {name} ({size})", name=os.path.basename(placed), size=size)
            elif stopped:
                self._mark(iids, tr("Stopped"), "warn")
                summary = "stopped"
                self.log(f"STOPPED by user - {took}")
                text = tr("Stopped.")
            else:
                self._mark(iids, tr("Failed"), "bad")
                summary = "failed"
                self.log(f"FAILED - {took}")
                text = tr("Join failed - see the Log sub-tab.")
            jobs.end(jid, ok=ok, summary=summary)
            _call_tk(lambda: self._set_busy(False, text))
