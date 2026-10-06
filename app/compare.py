"""Compare tab - two sub-tabs:

  Tracks : ffprobe the original and the new file and show a side-by-side of
           their streams (audio/subtitle/video tracks, codecs, languages,
           channels, duration) so you can confirm nothing was dropped. Codec
           changes from a normal re-encode are expected and NOT flagged.
  Quality: objective visual-quality comparison of two frame-aligned videos
           (original vs re-encode) with ffmpeg's SSIM and PSNR filters, plus
           VMAF when the ffmpeg build includes libvmaf. Sample mode compares
           a few short windows (fast); Full scan compares every frame."""
import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import POPEN_FLAGS
from .media import (fmt_time, format_size, parse_time, probe_duration,
                    probe_streams, probe_video_fps, _run_capture_stoppable)
from .widgets import add_tooltip, enable_file_drop

_MEDIA_TYPES = [("Video / audio", "*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.aac *.flac *.wav"),
                ("All files", "*.*")]
_VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")]


class CompareTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=(6, 6))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        tracks = ttk.Frame(nb, padding=10)
        nb.add(tracks, text="  Tracks  ")
        self.quality = _QualityPane(nb)
        nb.add(self.quality, text="  Quality  ")
        self._build_tracks(tracks)
        # let the Quality tab copy the picked files with one click
        self.quality.source_vars = (self.orig_var, self.new_var)

    # ===================== Tracks sub-tab =====================
    def _build_tracks(self, f):
        f.columnconfigure(1, weight=1)
        f.rowconfigure(3, weight=1)

        self.orig_var = tk.StringVar()
        self.new_var = tk.StringVar()
        oe = self._file_row(f, 0, "Original file:", self.orig_var)
        ne = self._file_row(f, 1, "New file:", self.new_var)

        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=3, sticky="we", pady=(4, 4))
        self.cmp_btn = ttk.Button(bar, text="Compare", command=self.compare)
        self.cmp_btn.pack(side="left")
        add_tooltip(self.cmp_btn, "Probe both files and list the differences")
        self.summary_var = tk.StringVar(value="Pick both files and click Compare.")
        ttk.Label(bar, textvariable=self.summary_var, style="Hint.TLabel").pack(side="left", padx=(10, 0))

        tvf = ttk.Frame(f)
        tvf.grid(row=3, column=0, columnspan=3, sticky="nsew")
        tvf.rowconfigure(0, weight=1)
        tvf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tvf, columns=("orig", "new", "ok"), show="tree headings", height=14)
        self.tree.heading("#0", text="Property")
        self.tree.heading("orig", text="Original")
        self.tree.heading("new", text="New")
        self.tree.heading("ok", text="Match")
        self.tree.column("#0", width=150, anchor="w")
        self.tree.column("orig", width=220, anchor="w")
        self.tree.column("new", width=220, anchor="w")
        self.tree.column("ok", width=60, anchor="center", stretch=False)
        self.tree.tag_configure("bad", foreground="#d43a3a")
        self.tree.tag_configure("good", foreground="#2e9e4f")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

        enable_file_drop(oe, lambda p: self._drop(self.orig_var, p))
        enable_file_drop(ne, lambda p: self._drop(self.new_var, p))

    def _file_row(self, f, r, label, var):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=3)
        ent = ttk.Entry(f, textvariable=var)
        ent.grid(row=r, column=1, sticky="we", padx=4, pady=3)
        ttk.Button(f, text="Browse...",
                   command=lambda: self._browse(var)).grid(row=r, column=2, padx=4)
        return ent

    def _browse(self, var):
        p = filedialog.askopenfilename(title="Select file", filetypes=_MEDIA_TYPES)
        if p:
            var.set(p)

    def _drop(self, var, p):
        p = (p or "").strip().strip('"')
        if p and os.path.isfile(p):
            var.set(p)

    def compare(self):
        a = self.orig_var.get().strip().strip('"')
        b = self.new_var.get().strip().strip('"')
        if not a or not os.path.isfile(a) or not b or not os.path.isfile(b):
            messagebox.showerror("Error", "Pick two valid files first.")
            return
        self.summary_var.set("Probing...")
        self.cmp_btn.configure(state="disabled")
        threading.Thread(target=self._worker, args=(a, b), daemon=True).start()

    def _worker(self, a, b):
        ia = probe_streams(a)
        ib = probe_streams(b)
        if ia is None or ib is None:
            self.after(0, lambda: (self.summary_var.set("Could not probe one of the files."),
                                   self.cmp_btn.configure(state="normal")))
            return
        rows = self._build_rows(ia, ib)
        bad = sum(1 for _, _, _, ok in rows if ok is False)
        self.after(0, lambda: self._show(rows, bad))

    @staticmethod
    def _build_rows(a, b):
        rows = []   # (label, orig, new, ok|None)

        def dur(x):
            return fmt_time(x) if x else "?"

        rows.append(("Duration", dur(a["duration"]), dur(b["duration"]), None))
        if a["duration"] and b["duration"]:
            rows.append(("  difference", "", f"{b['duration'] - a['duration']:+.2f} s "
                         "(expected for a cut)", None))
        rows.append(("File size", format_size(a["size"] or 0), format_size(b["size"] or 0), None))

        rows.append(("Video tracks", len(a["video"]), len(b["video"]),
                     len(a["video"]) == len(b["video"])))
        for i in range(max(len(a["video"]), len(b["video"]))):
            va = a["video"][i] if i < len(a["video"]) else None
            vb = b["video"][i] if i < len(b["video"]) else None
            sa = f"{va['codec']} {va['w']}x{va['h']}" if va else "- (missing)"
            sb = f"{vb['codec']} {vb['w']}x{vb['h']}" if vb else "- (missing)"
            ok = bool(va and vb and va["w"] == vb["w"] and va["h"] == vb["h"])
            rows.append((f"  video #{i}", sa, sb, ok))

        rows.append(("Audio tracks", len(a["audio"]), len(b["audio"]),
                     len(a["audio"]) == len(b["audio"])))
        for i in range(max(len(a["audio"]), len(b["audio"]))):
            aa = a["audio"][i] if i < len(a["audio"]) else None
            ab = b["audio"][i] if i < len(b["audio"]) else None
            sa = f"{aa['codec']} {aa['lang']} {aa['channels']}ch" if aa else "- (missing)"
            sb = f"{ab['codec']} {ab['lang']} {ab['channels']}ch" if ab else "- (missing)"
            ok = bool(aa and ab and aa["lang"] == ab["lang"] and aa["channels"] == ab["channels"])
            rows.append((f"  audio #{i}", sa, sb, ok))

        rows.append(("Subtitle tracks", len(a["subtitle"]), len(b["subtitle"]),
                     len(a["subtitle"]) == len(b["subtitle"])))
        for i in range(max(len(a["subtitle"]), len(b["subtitle"]))):
            sa_ = a["subtitle"][i] if i < len(a["subtitle"]) else None
            sb_ = b["subtitle"][i] if i < len(b["subtitle"]) else None
            sa = f"{sa_['codec']} {sa_['lang']}" if sa_ else "- (missing)"
            sb = f"{sb_['codec']} {sb_['lang']}" if sb_ else "- (missing)"
            ok = bool(sa_ and sb_ and sa_["lang"] == sb_["lang"])
            rows.append((f"  subtitle #{i}", sa, sb, ok))
        return rows

    def _show(self, rows, bad):
        self.tree.delete(*self.tree.get_children())
        for i, (label, ov, nv, ok) in enumerate(rows):
            tag = "" if ok is None else ("good" if ok else "bad")
            mark = "" if ok is None else ("OK" if ok else "X")
            self.tree.insert("", "end", iid=str(i), text=label,
                             values=(ov, nv, mark), tags=(tag,) if tag else ())
        if bad == 0:
            self.summary_var.set("All tracks match - nothing was dropped. "
                                 "(Codec/duration changes from re-encoding/cutting are normal.)")
        else:
            self.summary_var.set(f"{bad} mismatch(es) - check the red rows (a track or language may be missing).")
        self.cmp_btn.configure(state="normal")


# ===================== Quality sub-tab =====================
_VMAF_AVAILABLE = None      # None = not checked yet


class _Stopped(Exception):
    """Raised when Stop interrupts a running ffmpeg comparison."""


def _has_vmaf():
    global _VMAF_AVAILABLE
    if _VMAF_AVAILABLE is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=POPEN_FLAGS, timeout=30)
            _VMAF_AVAILABLE = "libvmaf" in (r.stdout or "")
        except Exception:
            _VMAF_AVAILABLE = False
    return _VMAF_AVAILABLE


def _quality_window(new, ref, new_pos, ref_pos, length, ref_wh, use_vmaf,
                    stop_event=None):
    """Measure one aligned window: SSIM + PSNR (one ffmpeg pass) and VMAF
    (second pass, slow). `new` is the re-encode (distorted), `ref` the
    original. Returns {"ssim","psnr","vmaf"|None}; raises RuntimeError, or
    _Stopped if stop_event is set (ffmpeg is killed right away)."""
    scale = f",scale={ref_wh[0]}:{ref_wh[1]}:flags=bicubic" if ref_wh else ""

    def inputs():
        cmd = []
        for pos, path in ((new_pos, new), (ref_pos, ref)):
            cmd += ["-ss", f"{max(0.0, pos):.3f}"]
            if length:
                cmd += ["-t", f"{length:.3f}"]
            cmd += ["-i", path]
        return cmd

    flt = (f"[0:v]setpts=PTS-STARTPTS,format=yuv420p{scale},split=2[d1][d2];"
           f"[1:v]setpts=PTS-STARTPTS,format=yuv420p,split=2[r1][r2];"
           f"[d1][r1]ssim;[d2][r2]psnr")
    cmd = (["ffmpeg", "-hide_banner", "-nostdin"] + inputs()
           + ["-filter_complex", flt, "-f", "null", "-"])
    rc, err = _run_capture_stoppable(cmd, stop_event)
    if stop_event is not None and stop_event.is_set():
        raise _Stopped()
    err = err or ""
    m_ssim = re.search(r"SSIM.*All:\s*([0-9.]+)", err)
    m_psnr = re.search(r"PSNR.*average:\s*([0-9.]+|inf)", err)
    if not m_ssim or not m_psnr:
        tail = "\n".join(err.strip().splitlines()[-4:])
        raise RuntimeError(f"ffmpeg could not compare this window:\n{tail}")
    out = {"ssim": float(m_ssim.group(1)),
           "psnr": 99.0 if m_psnr.group(1) == "inf" else float(m_psnr.group(1)),
           "vmaf": None}
    if use_vmaf:
        flt2 = (f"[0:v]setpts=PTS-STARTPTS,format=yuv420p{scale}[d];"
                f"[1:v]setpts=PTS-STARTPTS,format=yuv420p[r];"
                f"[d][r]libvmaf")
        cmd2 = (["ffmpeg", "-hide_banner", "-nostdin"] + inputs()
                + ["-filter_complex", flt2, "-f", "null", "-"])
        _rc2, err2 = _run_capture_stoppable(cmd2, stop_event)
        if stop_event is not None and stop_event.is_set():
            raise _Stopped()
        m = re.search(r"VMAF score[:=]\s*([0-9.]+)", err2 or "")
        if m:
            out["vmaf"] = float(m.group(1))
    return out


def _verdict_ssim(v):
    if v >= 0.99: return "visually identical"
    if v >= 0.98: return "excellent - very hard to tell apart"
    if v >= 0.95: return "good - minor differences in stills"
    if v >= 0.90: return "fair - visible differences if you look"
    return "poor - clearly visible quality loss"


def _verdict_vmaf(v):
    if v >= 95: return "visually identical"
    if v >= 90: return "excellent"
    if v >= 80: return "good"
    if v >= 70: return "fair - noticeable on a big screen"
    return "poor - clearly visible quality loss"


class _QualityPane(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.columnconfigure(1, weight=1)
        self.stop_event = threading.Event()
        self.source_vars = None          # set by CompareTab (Tracks entries)

        self.ref_var = tk.StringVar()
        self.new_var = tk.StringVar()
        re_ = self._file_row(0, "Original (reference):", self.ref_var)
        ne = self._file_row(1, "Re-encoded (new):", self.new_var)
        enable_file_drop(re_, lambda p: self._drop(self.ref_var, p))
        enable_file_drop(ne, lambda p: self._drop(self.new_var, p))

        orow = ttk.Frame(self)
        orow.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(orow, text="Start at - original:").pack(side="left")
        self.ref_off = tk.StringVar(value="0")
        e1 = ttk.Entry(orow, textvariable=self.ref_off, width=9)
        e1.pack(side="left", padx=(4, 12))
        add_tooltip(e1, "Where to start comparing in the ORIGINAL (e.g. 1:30 or 90).\n"
                        "Use the offsets to line up a cut file with its source.")
        ttk.Label(orow, text="new:").pack(side="left")
        self.new_off = tk.StringVar(value="0")
        e2 = ttk.Entry(orow, textvariable=self.new_off, width=9)
        e2.pack(side="left", padx=(4, 12))
        add_tooltip(e2, "Where to start comparing in the NEW file")
        ttk.Label(orow, text="for:").pack(side="left")
        self.span_var = tk.StringVar(value="")
        e3 = ttk.Entry(orow, textvariable=self.span_var, width=9)
        e3.pack(side="left", padx=(4, 16))
        add_tooltip(e3, "How much to compare from the offsets (e.g. 15:00). Empty = to the end.\n"
                        "For a CUT file, keep the whole compared stretch inside ONE kept\n"
                        "segment - past a cut the timelines no longer line up.")
        cpy = ttk.Button(orow, text="Use files from Tracks", command=self._copy_from_tracks)
        cpy.pack(side="left")
        add_tooltip(cpy, "Copy the two files picked on the Tracks sub-tab")

        srow = ttk.Frame(self)
        srow.grid(row=3, column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.full_var = tk.BooleanVar(value=False)
        fc = ttk.Checkbutton(srow, text="Full scan (every frame - slow)", variable=self.full_var)
        fc.pack(side="left", padx=(0, 14))
        add_tooltip(fc, "Compare the whole video instead of a few sample windows. "
                        "Takes roughly as long as a re-encode.")
        ttk.Label(srow, text="Sample:").pack(side="left")
        self.win_count = tk.Spinbox(srow, from_=1, to=10, width=3)
        self.win_count.delete(0, "end"); self.win_count.insert(0, "3")
        self.win_count.pack(side="left", padx=(4, 2))
        ttk.Label(srow, text="windows of").pack(side="left")
        self.win_len = tk.Spinbox(srow, from_=5, to=120, width=4)
        self.win_len.delete(0, "end"); self.win_len.insert(0, "20")
        self.win_len.pack(side="left", padx=(4, 2))
        ttk.Label(srow, text="seconds").pack(side="left", padx=(0, 14))
        self.align_var = tk.BooleanVar(value=True)
        ac = ttk.Checkbutton(srow, text="Auto-align (audio)", variable=self.align_var)
        ac.pack(side="left", padx=(0, 14))
        add_tooltip(ac, "Line the two files up by their audio before scoring, so a CUT "
                        "output (intro/credits removed) still compares correctly - each "
                        "window finds its matching moment in the original. Turn off only "
                        "if the files are already the same timeline.")
        self.vmaf_var = tk.BooleanVar(value=False)
        self.vmaf_chk = ttk.Checkbutton(srow, text="VMAF (checking...)", variable=self.vmaf_var,
                                        state="disabled")
        self.vmaf_chk.pack(side="left")
        add_tooltip(self.vmaf_chk, "Netflix's perceptual metric - the best match for what eyes "
                                   "see, but slower. Needs an ffmpeg build with libvmaf.")

        rrow = ttk.Frame(self)
        rrow.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.run_btn = ttk.Button(rrow, text="Compare quality", command=self.start)
        self.run_btn.pack(side="left")
        add_tooltip(self.run_btn, "Measure how close the new file looks to the original "
                                  "(SSIM/PSNR, optional VMAF)")
        self.stop_btn = ttk.Button(rrow, text="Stop", width=8, command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(rrow, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.rowconfigure(5, weight=1)
        self.out = tk.Text(self, height=14, font=("Consolas", 9), wrap="word", state="disabled")
        self.out.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.grid(row=5, column=3, sticky="ns", pady=(8, 0))
        self._print("Pick the original and the re-encoded file, then Compare quality.\n\n"
                    "The two videos must show the SAME pictures on the same timeline\n"
                    "(e.g. an episode and its re-encode).\n\n"
                    "For a CUT file (intro/credits removed), compare ONE kept stretch:\n"
                    "  - cold open:  offsets 0 / 0, 'for:' = time until the intro starts\n"
                    "  - main part:  original offset = where the intro ENDS, new offset =\n"
                    "    where the intro STARTED, 'for:' = time until the credits\n"
                    "Past a cut point the timelines drift, so scores there mean nothing.")

        threading.Thread(target=self._detect_vmaf, daemon=True).start()

    # ---- small helpers ----
    def _file_row(self, r, label, var):
        ttk.Label(self, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=3)
        ent = ttk.Entry(self, textvariable=var)
        ent.grid(row=r, column=1, sticky="we", padx=4, pady=3)
        ttk.Button(self, text="Browse...", command=lambda: self._browse(var)).grid(
            row=r, column=2, padx=4)
        return ent

    def _browse(self, var):
        p = filedialog.askopenfilename(title="Select video", filetypes=_VIDEO_TYPES)
        if p:
            var.set(p)

    def _drop(self, var, p):
        p = (p or "").strip().strip('"')
        if p and os.path.isfile(p):
            var.set(p)

    def _copy_from_tracks(self):
        if self.source_vars:
            self.ref_var.set(self.source_vars[0].get())
            self.new_var.set(self.source_vars[1].get())

    def _detect_vmaf(self):
        ok = _has_vmaf()
        def apply():
            if ok:
                self.vmaf_chk.configure(text="VMAF (perceptual)", state="normal")
            else:
                self.vmaf_chk.configure(text="VMAF (not in this ffmpeg build)", state="disabled")
        self.after(0, apply)

    def _print(self, txt, clear=True):
        self.out.configure(state="normal")
        if clear:
            self.out.delete("1.0", "end")
        self.out.insert("end", txt + "\n")
        self.out.see("end")
        self.out.configure(state="disabled")

    def _append(self, txt):
        self._print(txt, clear=False)

    # ---- run ----
    def stop(self):
        self.stop_event.set()

    def start(self):
        ref = self.ref_var.get().strip().strip('"')
        new = self.new_var.get().strip().strip('"')
        if not ref or not os.path.isfile(ref) or not new or not os.path.isfile(new):
            messagebox.showerror("Error", "Pick two valid video files first.")
            return
        ref_off = parse_time(self.ref_off.get().strip() or "0")
        new_off = parse_time(self.new_off.get().strip() or "0")
        if ref_off is None or new_off is None:
            messagebox.showerror("Error", "The start offsets must be times like 90, 1:30 or 0:01:30.5.")
            return
        span = None
        if self.span_var.get().strip():
            span = parse_time(self.span_var.get().strip())
            if span is None or span <= 0:
                messagebox.showerror("Error", "'for:' must be a time like 300 or 15:00 (or empty).")
                return
        try:
            n = max(1, min(10, int(self.win_count.get())))
            length = max(5, min(600, int(self.win_len.get())))
        except ValueError:
            n, length = 3, 20
        self.stop_event.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.bar.configure(value=0)
        self._print("Comparing...")
        threading.Thread(target=self._worker,
                         args=(ref, new, ref_off, new_off, span, self.full_var.get(),
                               n, length, self.vmaf_var.get(), self.align_var.get()),
                         daemon=True).start()

    def _worker(self, ref, new, ref_off, new_off, span, full, n, length, use_vmaf, auto_align):
        try:
            self._run_quality(ref, new, ref_off, new_off, span, full, n, length,
                              use_vmaf, auto_align)
        except _Stopped:
            self.after(0, lambda: self._append("\n[STOPPED]"))
        except Exception as exc:
            self.after(0, lambda e=exc: self._append(f"\n[FAIL] {e}"))
        finally:
            self.after(0, lambda: (self.run_btn.configure(state="normal"),
                                   self.stop_btn.configure(state="disabled")))

    def _run_quality(self, ref, new, ref_off, new_off, span, full, n, length,
                     use_vmaf, auto_align=False):
        dur_ref = probe_duration(ref) or 0
        dur_new = probe_duration(new) or 0
        usable = min(dur_ref - ref_off, dur_new - new_off)
        if span:
            usable = min(usable, span)
        if usable <= 1:
            self.after(0, lambda: self._append("[FAIL] Nothing to compare - check the offsets."))
            return
        info_ref = probe_streams(ref)
        info_new = probe_streams(new)
        if not info_ref or not info_new or not info_ref["video"] or not info_new["video"]:
            self.after(0, lambda: self._append("[FAIL] Could not probe a video stream in both files."))
            return
        vr, vn = info_ref["video"][0], info_new["video"][0]
        ref_wh = (vr["w"], vr["h"]) if (vr["w"], vr["h"]) != (vn["w"], vn["h"]) else None

        head = [f"Original:  {os.path.basename(ref)}",
                f"New:       {os.path.basename(new)}"]
        if ref_wh:
            head.append(f"[note] resolutions differ ({vn['w']}x{vn['h']} vs {vr['w']}x{vr['h']}) - "
                        "the new file is upscaled for the comparison.")
        fps_r, fps_n = probe_video_fps(ref), probe_video_fps(new)
        if fps_r and fps_n and abs(fps_r - fps_n) > 0.01:
            head.append(f"[warn] frame rates differ ({fps_n:.3f} vs {fps_r:.3f} fps) - "
                        "frames won't align, scores are unreliable!")
        # different lengths after the offsets = one file was cut (intro/credits
        # removed). Comparing at the same timestamps then measures unrelated
        # scenes - auto-align fixes it; otherwise warn and suggest the offset.
        diff = (dur_ref - ref_off) - (dur_new - new_off)
        if abs(diff) > 2.0 and ref_off == 0 and new_off == 0 and not auto_align:
            shorter, longer = ("New", "Original") if diff > 0 else ("Original", "New")
            head.append(
                f"[warn] {shorter} is {fmt_time(abs(diff))} shorter than {longer} - it "
                f"looks CUT (intro/credits removed), so the timelines don't line up and "
                f"these scores are meaningless. Tick 'Auto-align (audio)' to fix this "
                f"automatically, or set the {longer} offset to ~{fmt_time(abs(diff))}.")

        if full:
            # always bound the scan to the usable length: without -t the
            # shorter input ends first and ssim/psnr keep comparing against
            # its repeated last frame, skewing the score
            windows = [(ref_off, new_off, usable)]
            head.append(f"Mode: full scan ({fmt_time(usable)})")
        else:
            length = min(length, max(5, int(usable)))
            if usable <= n * length:
                windows = [(ref_off, new_off, min(length, usable))]
            else:
                fracs = [0.08, 0.5, 0.88] if n == 3 else [
                    (i + 0.5) / n for i in range(n)]
                windows = [(ref_off + f * (usable - length),
                            new_off + f * (usable - length), length) for f in fracs[:n]]
            head.append(f"Mode: {len(windows)} sample window(s) of {int(length)}s")

        # audio auto-alignment: re-derive each window's position in the ORIGINAL
        # from where its (new-file) audio actually occurs, so a cut output still
        # compares against the right moment. Full-scan can't be re-aligned as one
        # block (mid cuts), so it's skipped there.
        if auto_align and not full:
            try:
                from .media import audio_fingerprint, align_window
                M_ref, fps_a = audio_fingerprint(ref)
                M_new, _ = audio_fingerprint(new)
                if M_ref is None or M_new is None:
                    head.append("[note] auto-align skipped - could not read audio; "
                                "using the timestamps as given.")
                else:
                    fv = 1.0 / fps_r if fps_r else 1.0 / 24.0
                    aligned, weak = [], 0
                    for rp, np_, ln in windows:
                        arp, score = align_window(M_ref, fps_a, M_new, np_, ln or length)
                        if arp is None or score < 0.15:
                            aligned.append((rp, np_, ln))   # keep original guess
                            weak += 1
                            continue
                        # audio lands within ~1 frame; micro-search +-2 video
                        # frames on a short probe to make it frame-exact
                        best_rp, best_s = arp, -1.0
                        for k in (-3, -2, -1, 0, 1, 2, 3):
                            cand = max(0.0, arp + k * fv)
                            try:
                                s = _quality_window(new, ref, np_, cand, 3, ref_wh, False,
                                                    self.stop_event)["ssim"]
                            except RuntimeError:
                                continue
                            if s > best_s:
                                best_s, best_rp = s, cand
                        aligned.append((best_rp, np_, ln))
                    windows = aligned
                    msg = "Auto-align: matched each window by audio + frame refine"
                    if weak:
                        msg += f" ({weak} weak - left as-is)"
                    head.append(msg)
            except ImportError:
                head.append("[note] auto-align needs numpy/scipy/librosa - "
                            "install them or set offsets manually.")
        elif auto_align and full:
            head.append("[note] auto-align works with sample windows, not full scan - "
                        "switch off Full scan to align a cut file.")
        self.after(0, lambda t="\n".join(head): self._print(t))

        results = []
        total = len(windows)
        hdr = f"\n{'window':<16}{'SSIM':>8}{'PSNR':>10}" + (f"{'VMAF':>8}" if use_vmaf else "")
        self.after(0, lambda: self._append(hdr))
        for i, (rp, np_, ln) in enumerate(windows, 1):
            if self.stop_event.is_set():
                self.after(0, lambda: self._append("\n[STOPPED]"))
                return
            res = _quality_window(new, ref, np_, rp, ln, ref_wh, use_vmaf, self.stop_event)
            results.append(res)
            at = fmt_time(rp).rsplit(":", 1)[0]
            line = (f"{'@ ' + at:<16}{res['ssim']:>8.4f}{res['psnr']:>8.1f}dB"
                    + (f"{res['vmaf']:>8.1f}" if res.get("vmaf") is not None else
                       ("     n/a" if use_vmaf else "")))
            pct = int(i * 100 / total)
            self.after(0, lambda l=line, p=pct: (self._append(l), self.bar.configure(value=p)))

        ssim = sum(r["ssim"] for r in results) / len(results)
        psnr = sum(r["psnr"] for r in results) / len(results)
        vmafs = [r["vmaf"] for r in results if r.get("vmaf") is not None]
        lines = ["", f"{'average':<16}{ssim:>8.4f}{psnr:>8.1f}dB"
                 + (f"{sum(vmafs) / len(vmafs):>8.1f}" if vmafs else ""), ""]
        lines.append(f"SSIM {ssim:.4f}: {_verdict_ssim(ssim)}")
        if vmafs:
            v = sum(vmafs) / len(vmafs)
            lines.append(f"VMAF {v:.1f}: {_verdict_vmaf(v)}")
        lines.append("(guide: SSIM 0.98+ / VMAF 90+ = you won't see a difference "
                     "in normal viewing)")
        self.after(0, lambda t="\n".join(lines): self._append(t))
