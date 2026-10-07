"""Convert -> Video and Output sub-tabs (SettingsMixin): the settings form,
form <-> settings dict, crop / interlace detection and the Preview box."""
import os
import tkinter as tk
from tkinter import filedialog, ttk

from ..config import BIT_DEPTHS, VALID_PRESETS
from ..engine import recode
from ..i18n import N_, tr
from ..ui import icons
from ..ui.widgets import KeyedCombobox, add_tooltip, auto_wrap, enable_file_drop, info_icon

# shown label key -> engine value
VCODECS = {
    N_("H.265 / HEVC (x265)"): "libx265",
    N_("H.264 (x264)"): "libx264",
    N_("AV1 (SVT-AV1)"): "libsvtav1",
    N_("H.265 NVENC (GPU)"): "hevc_nvenc",
    N_("H.264 NVENC (GPU)"): "h264_nvenc",
    N_("AV1 NVENC (GPU, RTX 40 or newer)"): "av1_nvenc",
    N_("Plex-ready HEVC"): "plex_hevc",
    N_("Plex-ready H.264"): "plex_h264",
    N_("Copy (no re-encode)"): "copy",
}
RATES = {N_("Constant quality (CRF / CQ)"): "crf", N_("Average bitrate"): "bitrate"}
DEPTHS = {k: v for k, v in BIT_DEPTHS.items()}          # Auto / 8-bit / 10-bit
FPS = {N_("Same as source"): "source", **{f: f for f in recode.FPS_CHOICES[1:]}}
FPS_MODES = {N_("Constant (CFR)"): "cfr", N_("Variable (VFR)"): "vfr"}
HEIGHTS = {N_("Keep size"): 0, **{f"{h}p": h for h in recode.HEIGHTS[1:]}}
CROPS = {N_("Off"): "off", N_("Auto (detect black bars)"): "auto", N_("Manual"): "manual"}
DEINTS = {N_("Off"): "off", N_("Auto (detect)"): "auto", "bwdif": "bwdif", "yadif": "yadif"}
DENOISES = {N_("Off"): "off", N_("Light"): "light", N_("Medium"): "medium",
            N_("Strong"): "strong"}
METHODS = {"hqdn3d": "hqdn3d", N_("nlmeans (slow)"): "nlmeans"}
CONTAINERS = {"MKV": "mkv", "MP4": "mp4"}
# words that stay as they are in every language
_RAW = set(recode.FPS_CHOICES[1:]) | {f"{h}p" for h in recode.HEIGHTS[1:]} | {
    "bwdif", "yadif", "hqdn3d", "MKV", "MP4"}


def _key_of(table, value, default=None):
    for k, v in table.items():
        if v == value:
            return k
    return default if default is not None else next(iter(table))


def _labels(table):
    return [k if k in _RAW else tr(k) for k in table]


class SettingsMixin:
    """Video + Output sub-tabs. Expects self._form_changed(),
    self._shown_job(), self.log(), self._run_bg()."""

    # ------------------------------------------------------------ build
    def _combo(self, parent, key, table, width):
        var = tk.StringVar()
        self._fv[key] = (var, table)
        cb = KeyedCombobox(parent, textvariable=var, values=list(table),
                           labels=_labels(table), state="readonly", width=width)
        var.trace_add("write", lambda *a: self._form_changed())
        return cb

    def _check(self, parent, key, text):
        var = tk.BooleanVar()
        self._fv[key] = (var, bool)
        var.trace_add("write", lambda *a: self._form_changed())
        return ttk.Checkbutton(parent, text=text, variable=var)

    def _spin(self, parent, key, lo, hi, width=6, inc=1):
        var = tk.StringVar()
        self._fv[key] = (var, int)
        var.trace_add("write", lambda *a: self._form_changed())
        return ttk.Spinbox(parent, from_=lo, to=hi, increment=inc, textvariable=var,
                           width=width)

    def _build_video_tab(self, page):
        page.columnconfigure(0, weight=1, uniform="cv")
        page.columnconfigure(1, weight=1, uniform="cv")
        left = ttk.Frame(page)
        left.grid(row=0, column=0, sticky="nwe", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(page)
        right.grid(row=0, column=1, sticky="nwe", padx=(8, 0))
        right.columnconfigure(0, weight=1)

        enc = ttk.LabelFrame(left, text=tr("Video encoder"))
        enc.grid(row=0, column=0, sticky="we")
        enc.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(enc, text=tr("Codec:")).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        cb = self._combo(enc, "vcodec", VCODECS, 28)
        cb.grid(row=r, column=1, columnspan=3, sticky="w", padx=4, pady=3)
        add_tooltip(cb, tr("NVENC = the NVIDIA graphics card: much faster, slightly bigger "
                           "at the same quality; without a usable GPU the CPU encoder of "
                           "the same codec is used. Plex-ready = NVENC (or x265 / x264) "
                           "near-lossless with the bitrate capped below the source. Copy "
                           "keeps the video as it is (filters, crop and resize are off)."))
        r += 1
        ttk.Label(enc, text=tr("Quality:")).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self._combo(enc, "rate", RATES, 28).grid(row=r, column=1, columnspan=3,
                                                 sticky="w", padx=4, pady=3)
        r += 1
        ttk.Label(enc, text=tr("CRF / CQ:")).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self._crf_sp = self._spin(enc, "crf", 0, 63)
        self._crf_sp.grid(row=r, column=1, sticky="w", padx=4, pady=3)
        add_tooltip(self._crf_sp, tr("Lower = better quality and bigger file. Typical: "
                                     "x264 18-23, x265 20-26, SVT-AV1 25-35, NVENC CQ "
                                     "19-28."))
        r += 1
        ttk.Label(enc, text=tr("Bitrate:")).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        bf = ttk.Frame(enc)
        bf.grid(row=r, column=1, sticky="w", padx=4, pady=3)
        self._br_sp = self._spin(bf, "bitrate", 100, 100000, width=7, inc=250)
        self._br_sp.pack(side="left")
        ttk.Label(bf, text="kb/s").pack(side="left", padx=(4, 0))
        r += 1
        ttk.Label(enc, text=tr("Encoder preset:")).grid(row=r, column=0, sticky="w",
                                                        padx=4, pady=3)
        pv = tk.StringVar()
        self._fv["preset"] = (pv, str)
        pcb = ttk.Combobox(enc, textvariable=pv, values=VALID_PRESETS, state="readonly",
                           width=12)
        pcb.grid(row=r, column=1, sticky="w", padx=4, pady=3)
        pv.trace_add("write", lambda *a: self._form_changed())
        add_tooltip(pcb, tr("Speed vs. size: slower presets give smaller files at the same "
                            "quality (x264 / x265 names; NVENC and SVT-AV1 use the nearest "
                            "of their own levels)."))
        r += 1
        ttk.Label(enc, text=tr("Bit depth:")).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self._combo(enc, "depth", DEPTHS, 22).grid(row=r, column=1, columnspan=3,
                                                   sticky="w", padx=4, pady=3)
        r += 1
        cap = self._check(enc, "cap", tr("Never bigger than the source"))
        cap.grid(row=r, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 6))
        add_tooltip(cap, tr("Caps the video bitrate at 95% of the source's (like the "
                            "Plex-ready cut), so a converted file doesn't grow. The log "
                            "warns if the result is bigger anyway."))

        fr = ttk.LabelFrame(left, text=tr("Frame rate"))
        fr.grid(row=1, column=0, sticky="we", pady=(12, 0))
        ttk.Label(fr, text=tr("Frame rate:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self._combo(fr, "fps", FPS, 16).grid(row=0, column=1, sticky="w", padx=4, pady=3)
        fm = self._combo(fr, "fps_mode", FPS_MODES, 16)
        fm.grid(row=0, column=2, sticky="w", padx=4, pady=(3, 6))
        add_tooltip(fm, tr("Constant: every second has exactly that many frames (frames "
                           "are dropped / repeated). Variable: frames keep their own "
                           "timing; a chosen rate is then only the upper limit."))

        pic = ttk.LabelFrame(right, text=tr("Picture size & crop"))
        pic.grid(row=0, column=0, sticky="we")
        ttk.Label(pic, text=tr("Resize to:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        hcb = self._combo(pic, "height", HEIGHTS, 12)
        hcb.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        add_tooltip(hcb, tr("Output height; the width follows the aspect ratio (Lanczos "
                            "scaling)."))
        self._check(pic, "no_upscale", tr("Never upscale")).grid(
            row=0, column=2, columnspan=2, sticky="w", padx=8, pady=3)
        ttk.Label(pic, text=tr("Crop:")).grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ccb = self._combo(pic, "crop", CROPS, 24)
        ccb.grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=3)
        add_tooltip(ccb, tr("Auto measures the black bars at 10 points across the file and "
                            "uses the majority result (detected again for every file when "
                            "it is converted). Manual = the pixels below."))
        self.crop_btn = icons.decorate(ttk.Button(pic, text=tr("Detect now"),
                                                  command=self._detect_now), "detect")
        self.crop_btn.grid(row=1, column=3, sticky="w", padx=4, pady=3)
        add_tooltip(self.crop_btn, tr("Detect the black bars and the interlacing of the file "
                                      "shown now (the result fills the manual crop boxes)"))
        self.crop_info = tk.StringVar(value="")
        ttk.Label(pic, textvariable=self.crop_info, style="Hint.TLabel").grid(
            row=2, column=1, columnspan=3, sticky="w", padx=4)
        mf = ttk.Frame(pic)
        mf.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 6))
        ttk.Label(mf, text=tr("Manual:")).pack(side="left", padx=(0, 6))
        for key, lbl in (("crop_l", tr("Left")), ("crop_r", tr("Right")),
                         ("crop_t", tr("Top")), ("crop_b", tr("Bottom"))):
            ttk.Label(mf, text=lbl).pack(side="left", padx=(6, 2))
            self._spin(mf, key, 0, 2000, width=4, inc=2).pack(side="left")

        flt = ttk.LabelFrame(right, text=tr("Filters"))
        flt.grid(row=1, column=0, sticky="we", pady=(12, 0))
        ttk.Label(flt, text=tr("Deinterlace:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        dcb = self._combo(flt, "deint", DEINTS, 16)
        dcb.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        add_tooltip(dcb, tr("Auto runs ffmpeg's interlace detection (idet) on each file and "
                            "deinterlaces with bwdif only when it is interlaced. bwdif / "
                            "yadif always deinterlace."))
        self.deint_info = tk.StringVar(value="")
        ttk.Label(flt, textvariable=self.deint_info, style="Hint.TLabel").grid(
            row=0, column=2, columnspan=2, sticky="w", padx=4)
        ttk.Label(flt, text=tr("Denoise:")).grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self._combo(flt, "denoise", DENOISES, 16).grid(row=1, column=1, sticky="w",
                                                       padx=4, pady=3)
        mcb = self._combo(flt, "denoise_method", METHODS, 16)
        mcb.grid(row=1, column=2, sticky="w", padx=4, pady=3)
        add_tooltip(mcb, tr("hqdn3d is fast; nlmeans removes more grain but is many times "
                            "slower."))
        self._check(flt, "deblock", tr("Deblock")).grid(row=2, column=0, columnspan=2,
                                                        sticky="w", padx=4, pady=3)
        tm = self._check(flt, "tonemap", tr("HDR → SDR tone-mapping"))
        tm.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(3, 6))
        add_tooltip(tm, tr("Only for HDR10 / HLG sources (detected from the colour "
                           "transfer): converts to standard-range BT.709 with the Hable "
                           "curve, so the picture isn't washed out on SDR screens."))
        self.hdr_info = tk.StringVar(value="")
        ttk.Label(flt, textvariable=self.hdr_info, style="Hint.TLabel").grid(
            row=3, column=2, columnspan=2, sticky="w", padx=4)

    def _build_output_tab(self, page):
        page.columnconfigure(0, weight=1, uniform="cv")
        page.columnconfigure(1, weight=1, uniform="cv")
        box = ttk.LabelFrame(page, text=tr("Container & files"))
        box.grid(row=0, column=0, sticky="nwe", padx=(0, 8))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=tr("Container:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ccb = self._combo(box, "container", CONTAINERS, 8)
        ccb.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        add_tooltip(ccb, tr("MP4 can't hold ASS / PGS subtitles or fonts: text subtitles "
                            "become mov_text, picture subtitles and attachments are "
                            "dropped (each with a log line). MKV keeps everything."))
        ttk.Label(box, text=tr("Output folder:")).grid(row=1, column=0, sticky="w",
                                                       padx=4, pady=3)
        of = ttk.Frame(box)
        of.grid(row=1, column=1, sticky="we", padx=4, pady=3)
        of.columnconfigure(0, weight=1)
        ov = tk.StringVar()
        self._fv["out_dir"] = (ov, str)
        ov.trace_add("write", lambda *a: self._form_changed())
        oe = ttk.Entry(of, textvariable=ov, width=34)
        oe.grid(row=0, column=0, sticky="we")
        enable_file_drop(oe, lambda p: os.path.isdir(p) and ov.set(p))
        icons.decorate(ttk.Button(of, text="...", width=3, command=self._pick_out),
                       "folder").grid(row=0, column=1, padx=(4, 0))
        ttk.Label(box, text=tr("File name:")).grid(row=2, column=0, sticky="w", padx=4, pady=3)
        nf = ttk.Frame(box)
        nf.grid(row=2, column=1, sticky="we", padx=4, pady=3)
        pv = tk.StringVar()
        self._fv["pattern"] = (pv, str)
        pv.trace_add("write", lambda *a: self._form_changed())
        ttk.Entry(nf, textvariable=pv, width=24).pack(side="left")
        info_icon(nf, tr("{name} = the source file name, {codec} = hevc / h264 / av1, "
                         "{res} = the output height (720p). An existing file is never "
                         "silently replaced: Start asks once per run - Overwrite, Keep "
                         "both (adds (2)) or Skip.")).pack(side="left", padx=(6, 0))
        self.name_example = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.name_example, style="Hint.TLabel").grid(
            row=3, column=1, sticky="w", padx=4)
        opts = ttk.Frame(box)
        opts.grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 6))
        srt = self._check(opts, "subs_srt", tr("Convert text subtitles to SRT"))
        srt.pack(anchor="w", pady=1)
        add_tooltip(srt, tr("MKV: ASS / SSA / WebVTT become plain SRT (styles are lost). "
                            "MP4 always converts text subtitles to mov_text."))
        self._check(opts, "attachments", tr("Keep attachments / fonts (MKV)")).pack(
            anchor="w", pady=1)
        self._check(opts, "chapters", tr("Keep chapters")).pack(anchor="w", pady=1)
        self._check(opts, "metadata", tr("Keep metadata")).pack(anchor="w", pady=1)

        pv = ttk.LabelFrame(page, text=tr("Preview & estimate"))
        pv.grid(row=0, column=1, sticky="nwe", padx=(8, 0))
        pv.columnconfigure(1, weight=1)
        ttk.Label(pv, text=tr("Sample from:")).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        sf = ttk.Frame(pv)
        sf.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        self.sample_var = tk.StringVar(value="")
        se = ttk.Entry(sf, textvariable=self.sample_var, width=10)
        se.pack(side="left")
        ttk.Label(sf, text=tr("(mm:ss - empty = the middle)"), style="Hint.TLabel").pack(
            side="left", padx=(6, 0))
        bf = ttk.Frame(pv)
        bf.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(3, 3))
        self.preview_btn = icons.decorate(ttk.Button(bf, text=tr("Preview sample"),
                                                     command=self.preview), "play")
        self.preview_btn.pack(side="left")
        add_tooltip(self.preview_btn, tr(
            "Encode a 10 s sample with the current settings (plus a few short pieces "
            "spread over the file for the estimate), then open source and sample side "
            "by side in Inspect → Dual Player"))
        self.dual_btn = icons.decorate(ttk.Button(bf, text=tr("Open in Dual Player"),
                                                  command=self._open_dual,
                                                  state="disabled"), "compare")
        self.dual_btn.pack(side="left", padx=(6, 0))
        self.preview_info = tk.StringVar(value=tr("No preview yet."))
        auto_wrap(ttk.Label(pv, textvariable=self.preview_info, justify="left"),
                  width=420).grid(row=2, column=0, columnspan=2, sticky="w", padx=4,
                                  pady=(3, 8))

    def _pick_out(self):
        d = filedialog.askdirectory(title=tr("Select folder"))
        if d:
            self._fv["out_dir"][0].set(os.path.normpath(d))

    # ------------------------------------------------------------ form <-> dict
    def _set_form(self, s):
        self._loading = True
        try:
            for key, (var, kind) in self._fv.items():
                v = s.get(key)
                if isinstance(kind, dict):
                    var.set(_key_of(kind, v))
                elif kind is bool:
                    var.set(bool(v))
                else:
                    var.set("" if v is None else str(v))
        finally:
            self._loading = False
        self._update_enables()

    def _get_form(self, base=None):
        s = dict(base or recode.default_settings())
        for key, (var, kind) in self._fv.items():
            try:
                v = var.get()
            except tk.TclError:
                continue
            if isinstance(kind, dict):
                if v in kind:
                    s[key] = kind[v]
            elif kind is bool:
                s[key] = bool(v)
            elif kind is int:
                try:
                    s[key] = int(float(str(v).strip()))
                except ValueError:
                    pass
            else:
                s[key] = str(v).strip()
        return recode.normalize_settings(s)

    def _update_enables(self):
        s = self._get_form()
        copy = s["vcodec"] == "copy"
        bitrate = s["rate"] == "bitrate"
        try:
            self._crf_sp.configure(state="disabled" if copy or bitrate else "normal")
            self._br_sp.configure(state="normal" if bitrate and not copy else "disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------ info labels
    def _show_source_info(self, job):
        """Crop / interlace / HDR hints for the job shown in the form."""
        if job is None:
            self.crop_info.set("")
            self.deint_info.set("")
            self.hdr_info.set("")
            return
        det = job.get("detect") or {}
        v = job["info"]["video"]
        c = det.get("crop")
        if "crop" not in det:
            self.crop_info.set(tr("Source {w}x{h} - not measured yet", w=v["w"], h=v["h"]))
        elif not c or not recode.crop_label(c):
            self.crop_info.set(tr("Detected: no black bars ({w}x{h})", w=v["w"], h=v["h"]))
        else:
            what = {"letterbox": tr("letterbox removed"), "pillarbox": tr("pillarbox removed"),
                    "borders": tr("borders removed")}[recode.crop_label(c)]
            self.crop_info.set(tr("Detected: {w}x{h} ({what})", w=c["w"], h=c["h"], what=what))
        d = det.get("interlace")
        self.deint_info.set("" if d is None else tr("Detected: interlaced") if d["interlaced"]
                            else tr("Detected: progressive"))
        self.hdr_info.set({"smpte2084": tr("Source: HDR10"), "arib-std-b67": tr("Source: HLG")}
                          .get(v["transfer"], tr("Source: SDR")))
