"""Clean up folders dialog - empties the working folders (templates, videos,
output, Audio Gain in/out, temp) by MOVING their contents into a timestamped
folder under temp\\trash, so nothing is ever deleted by accident. Reached via
the "Clean up folders..." button in the top bar."""
import os
import shutil
import time
import tkinter as tk
from tkinter import ttk, messagebox

from .config import (INTRO_DIR, CREDITS_DIR, PREINTRO_DIR, AFTERCREDITS_DIR,
                     VIDEO_DIR, OUTPUT_DIR, AUDIOGAIN_INPUT, AUDIOGAIN_OUTPUT,
                     TEMP_DIR)
from . import applog

FOLDERS = [
    ("Intro templates", INTRO_DIR),
    ("Credits templates", CREDITS_DIR),
    ("Pre-intro templates", PREINTRO_DIR),
    ("After-credits templates", AFTERCREDITS_DIR),
    ("Episodes (videos folder)", VIDEO_DIR),
    ("Output (cleaned episodes)", OUTPUT_DIR),
    ("Audio Gain input", AUDIOGAIN_INPUT),
    ("Audio Gain output", AUDIOGAIN_OUTPUT),
    ("Temp files", TEMP_DIR),
]
TRASH_DIR = os.path.join(TEMP_DIR, "trash")


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


def _fmt_size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
        b /= 1024.0


class CleanupDialog(tk.Toplevel):
    def __init__(self, master, is_busy=None):
        super().__init__(master)
        self._is_busy = is_busy   # callable -> True while any tab runs a job
        self.title("Clean up folders")
        self.transient(master)
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Tick the folders to empty. Their contents are MOVED into a "
                            "timestamped folder under temp\\trash - nothing is deleted, "
                            "so you can still recover files.",
                  wraplength=430, justify="left").grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self._rows = []           # (BooleanVar, label, path, count_label)
        for i, (label, path) in enumerate(FOLDERS, start=1):
            v = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text=label, variable=v).grid(
                row=i, column=0, sticky="w", pady=1)
            lbl = ttk.Label(frm, text="")
            lbl.grid(row=i, column=1, sticky="e", padx=(20, 0))
            self._rows.append((v, label, path, lbl))

        btns = ttk.Frame(frm)
        btns.grid(row=len(FOLDERS) + 1, column=0, columnspan=2,
                  sticky="we", pady=(12, 0))
        ttk.Button(btns, text="Refresh counts", command=self._refresh).pack(side="left")
        ttk.Button(btns, text="Open trash folder", command=self._open_trash).pack(
            side="left", padx=6)
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        self.clean_btn = ttk.Button(btns, text="Clean up selected", command=self._run)
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
            lbl.configure(text="empty" if files == 0
                          else f"{files} file{'s' if files != 1 else ''},  {_fmt_size(size)}")

    def _open_trash(self):
        try:
            os.makedirs(TRASH_DIR, exist_ok=True)
            os.startfile(os.path.abspath(TRASH_DIR))
        except Exception:
            pass

    def _run(self):
        if self._is_busy is not None and self._is_busy():
            messagebox.showwarning(
                "A job is running",
                "A cut / detect / audio / check job is still running and may be using "
                "files in these folders (temp files, source videos).\n\n"
                "Stop it or wait for it to finish, then clean up.", parent=self)
            return
        chosen = [(label, path) for v, label, path, _l in self._rows
                  if v.get() and _stats(path)[0] > 0]
        if not chosen:
            messagebox.showinfo("Nothing to do",
                                "Tick at least one folder that isn't empty.", parent=self)
            return
        names = "\n".join(f"  - {label}" for label, _p in chosen)
        if not messagebox.askyesno(
                "Confirm clean-up",
                f"Move the contents of these folders to temp\\trash?\n\n{names}",
                parent=self):
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved, errors = 0, 0
        for label, path in chosen:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in label).strip()
            dest = os.path.join(TRASH_DIR, stamp, safe)
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError:
                errors += 1
                continue
            for entry in _entries(path):
                try:
                    shutil.move(entry, dest)
                    moved += 1
                except Exception as exc:
                    errors += 1
                    applog.record(f"[cleanup] could not move {entry}: {exc}")
            applog.record(f"[cleanup] emptied '{label}' -> temp\\trash\\{stamp}\\{safe}")
        self._refresh()
        msg = f"Moved {moved} item{'s' if moved != 1 else ''} to temp\\trash\\{stamp}."
        if errors:
            msg += (f"\n{errors} item(s) could not be moved (file in use?) - "
                    "see the Log tab.")
        messagebox.showinfo("Clean-up done", msg, parent=self)
