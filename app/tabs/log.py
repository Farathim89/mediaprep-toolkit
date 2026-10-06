"""Log tab - a live, combined view of every tab's activity, with Save / Clear /
Open-folder. All messages are also auto-saved to the logs folder."""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .. import applog
from ..i18n import tr
from ..config import LOGS_DIR
from ..ui.widgets import trim_text_lines, help_button


class LogTab(ttk.Frame):
    def __init__(self, master, logdir=LOGS_DIR):
        super().__init__(master, padding=8)
        self.logdir = logdir
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 4))
        ttk.Button(bar, text=tr("Save log..."), command=self.save).pack(side="left")
        ttk.Button(bar, text=tr("Clear"), command=self.clear).pack(side="left", padx=6)
        ttk.Button(bar, text=tr("Open logs folder"), command=self.open_folder).pack(side="left")
        help_button(bar, "log").pack(side="right")
        ttk.Label(bar, text="  " + tr("(all activity is also auto-saved to the logs folder; "
                                      "the view keeps the last {n:,} lines)",
                                      n=applog.MAX_LINES),
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))

        self.text = tk.Text(self, state="disabled", font=("Consolas", 9), wrap="none")
        self.text.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)

        self._append(applog.all_text())
        applog.add_listener(self._on_line)

    def _on_line(self, line):
        self.after(0, lambda: self._append(line))

    def _append(self, s):
        if not s:
            return
        self.text.configure(state="normal")
        self.text.insert("end", s + "\n")
        trim_text_lines(self.text, applog.MAX_LINES)   # same cap as the in-memory log
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        applog.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def save(self):
        p = filedialog.asksaveasfilename(title=tr("Save log"), defaultextension=".log",
                                         filetypes=[(tr("Log files"), "*.log *.txt"),
                                                    (tr("All files"), "*.*")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(applog.all_text())
            messagebox.showinfo(tr("Saved"), tr("Log saved to:\n{path}", path=p))
        except OSError as e:
            messagebox.showerror(tr("Error"), str(e))

    def open_folder(self):
        try:
            os.makedirs(self.logdir, exist_ok=True)
            os.startfile(os.path.abspath(self.logdir))   # Windows
        except Exception:
            messagebox.showinfo(tr("Logs folder"), os.path.abspath(self.logdir))
