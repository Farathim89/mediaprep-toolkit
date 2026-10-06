"""Clean up folders dialog - empties the working folders (templates, videos,
output, Audio Gain in/out, temp) by MOVING their contents into a timestamped
folder under Data\\temp\\trash, so nothing is ever deleted by accident. Reached via
the "Clean up folders..." button in the top bar.

'Empty trash older than N days' is the ONLY permanent delete in the app: it
lists exactly what will go (items / files / size) and needs a Yes first."""
import os
import shutil
import time
import tkinter as tk
from tkinter import ttk, messagebox

from ..config import (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR, VIDEO_DIR,
                      OUTPUT_DIR, AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT, TEMP_DIR, short_path)
from .. import applog
from ..i18n import tr, ntr
from .widgets import help_button
from ..engine.files import TRASH_DIR, move_into_trash

# (English label, shown label, path) - the English label names the trash
# sub-folder and the log line, so it never changes with the language
FOLDERS = [
    ("Intro templates", tr("Intro templates"), INTRO_DIR),
    ("Credits templates", tr("Credits templates"), CREDITS_DIR),
    ("Pre-intro templates", tr("Pre-intro templates"), PREINTRO_DIR),
    ("After-credits templates", tr("After-credits templates"), AFTERCREDITS_DIR),
    ("Episodes (videos folder)", tr("Episodes (videos folder)"), VIDEO_DIR),
    ("Output (cleaned episodes)", tr("Output (cleaned episodes)"), OUTPUT_DIR),
    ("Audio Gain input", tr("Audio Gain input"), AUDIOGAIN_INPUT),
    ("Audio Gain output", tr("Audio Gain output"), AUDIOGAIN_OUTPUT),
    ("Temp files", tr("Temp files"), TEMP_DIR),
]


def _entries(path):
    """Top-level items inside `path` (the trash folder itself is skipped so
    cleaning Temp never eats earlier clean-ups)."""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    out = []
    for n in names:
        full = os.path.join(path, n)
        if os.path.abspath(full) == os.path.abspath(TRASH_DIR):
            continue
        out.append(full)
    return out


def _stats(path):
    """(file_count, total_bytes) for everything under `path`, minus the trash."""
    files, size = 0, 0
    skip = os.path.abspath(TRASH_DIR)
    for root, dirs, names in os.walk(path):
        if os.path.abspath(root) == skip:
            dirs[:] = []
            continue
        for n in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return files, size


def _trash_stats():
    """(top-level items, files, bytes) inside Data\\temp\\trash."""
    items = len(os.listdir(TRASH_DIR)) if os.path.isdir(TRASH_DIR) else 0
    files, size = 0, 0
    for root, _dirs, names in os.walk(TRASH_DIR):
        for n in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return items, files, size


def _trash_item_time(path):
    """When an item was put in the trash: the YYYYMMDD-HHMMSS stamp its name
    starts with (every clean-up folder has one), else its modified time."""
    name = os.path.basename(path)
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        try:
            return os.path.getmtime(path)
        except OSError:
            return time.time()


def _old_trash_items(days):
    """[(path, files, bytes)] for the trash items older than `days` days."""
    if not os.path.isdir(TRASH_DIR):
        return []
    cutoff = time.time() - days * 86400
    out = []
    for n in sorted(os.listdir(TRASH_DIR)):
        p = os.path.join(TRASH_DIR, n)
        if _trash_item_time(p) >= cutoff:
            continue
        if os.path.isdir(p):
            files, size = 0, 0
            for root, _d, names in os.walk(p):
                for fn in names:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
        else:
            files = 1
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
        out.append((p, files, size))
    return out


def _fmt_size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
        b /= 1024.0


class CleanupDialog(tk.Toplevel):
    def __init__(self, master, is_busy=None):
        super().__init__(master)
        self._is_busy = is_busy   # callable -> True while any tab runs a job
        self.title(tr("Clean up folders"))
        self.transient(master)
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text=tr("Tick the folders to empty. Their contents are MOVED into a "
                               "timestamped folder under Data\\temp\\trash - nothing is "
                               "deleted, so you can still recover files."),
                  wraplength=430, justify="left").grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        help_button(frm, "cleanup").grid(row=0, column=2, sticky="ne", padx=(8, 0))

        self._rows = []           # (BooleanVar, (label, shown), path, count_label)
        for i, (label, shown, path) in enumerate(FOLDERS, start=1):
            v = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text=f"{shown}  ({short_path(path)})",
                            variable=v).grid(
                row=i, column=0, sticky="w", pady=1)
            lbl = ttk.Label(frm, text="")
            lbl.grid(row=i, column=1, sticky="e", padx=(20, 0))
            self._rows.append((v, (label, shown), path, lbl))

        # trash size + the one permanent-delete action
        trow = ttk.Frame(frm)
        trow.grid(row=len(FOLDERS) + 1, column=0, columnspan=2, sticky="we", pady=(12, 0))
        trow.columnconfigure(1, weight=1)
        ttk.Label(trow, text=tr("Trash (Data\\temp\\trash):")).grid(row=0, column=0, sticky="w")
        self.trash_lbl = ttk.Label(trow, text="")
        self.trash_lbl.grid(row=0, column=1, sticky="e")
        er = ttk.Frame(trow)
        er.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.empty_btn = ttk.Button(er, text=tr("Empty trash older than"), command=self._empty_trash)
        self.empty_btn.pack(side="left")
        self.days_var = tk.StringVar(value="14")
        ttk.Spinbox(er, from_=0, to=3650, width=5, textvariable=self.days_var).pack(
            side="left", padx=4)
        ttk.Label(er, text=tr("days  (PERMANENT - asks first)")).pack(side="left")

        btns = ttk.Frame(frm)
        btns.grid(row=len(FOLDERS) + 2, column=0, columnspan=2,
                  sticky="we", pady=(12, 0))
        ttk.Button(btns, text=tr("Refresh counts"), command=self._refresh).pack(side="left")
        ttk.Button(btns, text=tr("Open trash folder"), command=self._open_trash).pack(
            side="left", padx=6)
        ttk.Button(btns, text=tr("Close"), command=self.destroy).pack(side="right")
        self.clean_btn = ttk.Button(btns, text=tr("Clean up selected"), command=self._run)
        self.clean_btn.pack(side="right", padx=6)

        self._refresh()
        # centre over the main window
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()

    def _refresh(self):
        for _v, _label, path, lbl in self._rows:
            files, size = _stats(path)
            lbl.configure(text=tr("empty") if files == 0
                          else ntr("{n} file,  {size}", "{n} files,  {size}", files,
                                   size=_fmt_size(size)))
        items, files, size = _trash_stats()
        self.trash_lbl.configure(
            text=tr("empty") if items == 0
            else ntr("{n} item", "{n} items", items) + ",  "
            + ntr("{n} file,  {size}", "{n} files,  {size}", files, size=_fmt_size(size)))

    def _empty_trash(self):
        """Permanently delete trash items older than N days - after an explicit
        confirmation that states exactly what goes."""
        try:
            days = float(self.days_var.get())
            if days < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(tr("Error"), tr("Enter the age in days (0 or more)."),
                                 parent=self)
            return
        if self._is_busy is not None and self._is_busy():
            messagebox.showwarning(tr("A job is running"),
                                   tr("Stop the running job first, then empty the trash."),
                                   parent=self)
            return
        old = _old_trash_items(days)
        if not old:
            messagebox.showinfo(tr("Nothing to delete"),
                                tr("Nothing in Data\\temp\\trash is older than {days:g} days.",
                                   days=days),
                                parent=self)
            return
        files = sum(f for _p, f, _s in old)
        size = sum(s for _p, _f, s in old)
        if not messagebox.askyesno(
                tr("PERMANENTLY delete?"),
                ntr("This will PERMANENTLY delete {n} trash item ({files} files, {size}) "
                    "older than {days:g} days from Data\\temp\\trash.\n\n"
                    "They can NOT be recovered afterwards.\n\nDelete them?",
                    "This will PERMANENTLY delete {n} trash items ({files} files, {size}) "
                    "older than {days:g} days from Data\\temp\\trash.\n\n"
                    "They can NOT be recovered afterwards.\n\nDelete them?",
                    len(old), files=files, size=_fmt_size(size), days=days),
                icon="warning", default="no", parent=self):
            return
        deleted, errors = 0, 0
        for p, _f, _s in old:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                deleted += 1
            except OSError as exc:
                errors += 1
                applog.record(f"[cleanup] could not delete {p}: {exc}")
        applog.record(f"[cleanup] permanently deleted {deleted} trash item(s) older than "
                      f"{days:g} days ({files} files, {_fmt_size(size)})")
        self._refresh()
        msg = ntr("Deleted {n} item ({size}).", "Deleted {n} items ({size}).", deleted,
                  size=_fmt_size(size))
        if errors:
            msg += "\n" + ntr("{n} item could not be deleted (in use?) - see the Log tab.",
                              "{n} items could not be deleted (in use?) - see the Log tab.",
                              errors)
        messagebox.showinfo(tr("Trash emptied"), msg, parent=self)

    def _open_trash(self):
        try:
            os.makedirs(TRASH_DIR, exist_ok=True)
            os.startfile(os.path.abspath(TRASH_DIR))
        except Exception:
            pass

    def _run(self):
        if self._is_busy is not None and self._is_busy():
            messagebox.showwarning(
                tr("A job is running"),
                tr("A cut / detect / audio / check job is still running and may be using "
                   "files in these folders (temp files, source videos).\n\n"
                   "Stop it or wait for it to finish, then clean up."), parent=self)
            return
        chosen = [(label, path) for v, label, path, _l in self._rows
                  if v.get() and _stats(path)[0] > 0]
        if not chosen:
            messagebox.showinfo(tr("Nothing to do"),
                                tr("Tick at least one folder that isn't empty."), parent=self)
            return
        names = "\n".join(f"  - {shown}" for (_label, shown), _p in chosen)
        if not messagebox.askyesno(
                tr("Confirm clean-up"),
                tr("Move the contents of these folders to Data\\temp\\trash?\n\n{names}",
                   names=names),
                parent=self):
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved, errors = 0, 0
        for (label, _shown), path in chosen:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in label).strip()
            dest = os.path.join(TRASH_DIR, stamp, safe)
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError:
                errors += 1
                continue
            for entry in _entries(path):
                try:
                    move_into_trash(entry, dest, record=False)
                    moved += 1
                except Exception as exc:
                    errors += 1
                    applog.record(f"[cleanup] could not move {entry}: {exc}")
            applog.record(f"[cleanup] emptied '{label}' -> Data\\temp\\trash\\{stamp}\\{safe}")
        self._refresh()
        msg = ntr("Moved {n} item to Data\\temp\\trash\\{stamp}.",
                  "Moved {n} items to Data\\temp\\trash\\{stamp}.", moved, stamp=stamp)
        if errors:
            msg += "\n" + ntr("{n} item could not be moved (file in use?) - see the Log tab.",
                              "{n} items could not be moved (file in use?) - see the Log tab.",
                              errors)
        messagebox.showinfo(tr("Clean-up done"), msg, parent=self)
