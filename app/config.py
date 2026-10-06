"""Constants, codec/audio maps, settings persistence and GUI themes."""
import json
import os
import subprocess
import tkinter as tk

# ======================= shared settings =======================
INPUT_DIR = "input"                        # parent folder for the template clips
INTRO_DIR = "input/intro"
CREDITS_DIR = "input/credits"
PREINTRO_DIR = "input/preintro"            # clips that come BEFORE the intro (recaps, logos)
AFTERCREDITS_DIR = "input/aftercredits"    # clips that come AFTER the credits (teasers, previews)
VIDEO_DIR = "videos"
OUTPUT_DIR = "output"
TEMP_DIR = "temp"
AUDIOGAIN_DIR = "Audio Gain"
AUDIOGAIN_INPUT = "Audio Gain/input"
AUDIOGAIN_OUTPUT = "Audio Gain/output"

INTRO_SEARCH_WINDOW = 600
CREDITS_SEARCH_WINDOW = 600

VALID_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]

# ---- video codec selection ----
CODECS = {
    "Auto - CPU (match source)": "auto",
    "Auto - GPU / NVENC (match source)": "auto_gpu",
    "H.264 (libx264) - most compatible": "libx264",
    "H.265 / HEVC (libx265) - smaller files": "libx265",
    "H.264 NVENC (NVIDIA GPU, fast)": "h264_nvenc",
    "H.265 NVENC (NVIDIA GPU, fast)": "hevc_nvenc",
    "AV1 (libsvtav1) - smallest, slow": "libsvtav1",
}

# label used before the CPU/GPU auto split -> its replacement, so old saved
# settings still select a valid dropdown entry (normalized on load).
LEGACY_CODEC_LABELS = {"Auto (match source)": "Auto - CPU (match source)"}

# "Auto - CPU": pick the CPU encoder for the same codec family as the source, so
# the output doesn't balloon (e.g. HEVC source -> libx265).
AUTO_CODEC_MAP = {"hevc": "libx265", "av1": "libsvtav1", "vp9": "libx265"}
# "Auto - GPU": same idea but with the NVENC hardware encoders. NVENC has no AV1
# on most cards, so AV1/VP9 sources fall back to the efficient hevc_nvenc; H.264
# and anything unknown stay on the most-compatible h264_nvenc.
AUTO_GPU_CODEC_MAP = {"hevc": "hevc_nvenc", "h264": "h264_nvenc",
                      "av1": "hevc_nvenc", "vp9": "hevc_nvenc"}
DEFAULT_CODEC_LABEL = "H.264 (libx264) - most compatible"

# audio-track language choices for detection/matching (label -> ISO code or None
# for "all / default"). Used by the Cut / Edit and the Auto-detect tab.
AUDIO_LANG_CHOICES = {
    "All / default track": None,
    "English": "eng",
    "Japanese": "jpn",
    "Spanish": "spa",
    "Portuguese": "por",
    "French": "fre",
    "German": "ger",
    "Arabic": "ara",
}

# NVENC uses p1-p7 presets and -cq instead of -crf
NVENC_PRESET_MAP = {"ultrafast": "p2", "superfast": "p2", "veryfast": "p3",
                    "faster": "p4", "fast": "p4", "medium": "p5",
                    "slow": "p6", "slower": "p7", "veryslow": "p7"}
# SVT-AV1 uses numeric presets 0(slowest)-13(fastest)
SVT_PRESET_MAP = {"ultrafast": "12", "superfast": "11", "veryfast": "10",
                  "faster": "9", "fast": "9", "medium": "8",
                  "slow": "6", "slower": "5", "veryslow": "4"}


BIT_DEPTHS = {"Auto (match source)": "auto", "8-bit": "8", "10-bit": "10"}

# ffmpeg subprocesses must not flash a console window on Windows
POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

AUDIO_RECODE_MAP = {
    "ac3": ("ac3", "640k"),
    "eac3": ("eac3", "640k"),
    # ffmpeg's DTS encoder (dca) is experimental and refuses without -strict -2;
    # E-AC3 is a stable, widely supported surround codec instead
    "dts": ("eac3", "640k"),
    "truehd": ("flac", None),
    "flac": ("flac", None),
    "aac": ("aac", "320k"),
    "mp3": ("libmp3lame", "320k"),
    "opus": ("libopus", "320k"),
    "vorbis": ("libvorbis", "320k"),
    "pcm_s16le": ("pcm_s16le", None),
    "pcm_s24le": ("pcm_s24le", None),
}
DEFAULT_AUDIO_RECODE = ("aac", "320k")

# audio output choices for the Audio Gain tool (None = match the source codec,
# preserving 5.1 / Dolby / DTS and channel layout per track)
AUDIO_OUT_CHOICES = {
    "Match source (keep codec & channels)": None,
    "AAC 320k": ("aac", "320k"),
    "AC3 640k (Dolby Digital)": ("ac3", "640k"),
    "E-AC3 640k": ("eac3", "640k"),
    "FLAC (lossless)": ("flac", None),
    "Opus 320k": ("libopus", "320k"),
}


# ======================= settings persistence =======================
SETTINGS_FILE = "toolkit_settings.json"

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def save_settings(d):
    # write a temp file and swap it in, so a crash mid-write can't leave a
    # truncated (unloadable) settings file behind
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


# ======================= themes =======================
THEMES = {
    "Light": dict(bg="#f0f0f0", fg="#1a1a1a", field="#ffffff", border="#b0b0b0",
                  tab="#dcdcdc", btn="#e1e1e1", btn_active="#d0d0d0",
                  accent="#2f6fd6", text_bg="#ffffff", text_fg="#1a1a1a",
                  hint="#666666"),
    "Dark": dict(bg="#2b2b2b", fg="#e6e6e6", field="#3c3f41", border="#555555",
                 tab="#3c3f41", btn="#444444", btn_active="#555555",
                 accent="#4a88ff", text_bg="#1e1e1e", text_fg="#dcdcdc",
                 hint="#9a9a9a"),
    "High Contrast": dict(bg="#000000", fg="#ffffff", field="#000000", border="#ffffff",
                          tab="#000000", btn="#000000", btn_active="#333333",
                          accent="#ffff00", text_bg="#000000", text_fg="#ffff00",
                          hint="#00ffff"),
}

def apply_theme(root, style, name):
    t = THEMES.get(name, THEMES["Light"])
    style.theme_use("clam")
    style.configure(".", background=t["bg"], foreground=t["fg"],
                    fieldbackground=t["field"], bordercolor=t["border"],
                    lightcolor=t["bg"], darkcolor=t["bg"])
    style.configure("TFrame", background=t["bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["fg"])
    style.configure("Hint.TLabel", background=t["bg"], foreground=t["hint"])
    style.configure("TNotebook", background=t["bg"])
    style.configure("TNotebook.Tab", background=t["tab"], foreground=t["fg"])
    style.map("TNotebook.Tab", background=[("selected", t["bg"])])
    style.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"],
                    insertcolor=t["fg"])
    style.configure("TCombobox", fieldbackground=t["field"], foreground=t["fg"],
                    arrowcolor=t["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", t["field"])],
              foreground=[("readonly", t["fg"])])
    style.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"],
                    arrowcolor=t["fg"], insertcolor=t["fg"])
    style.configure("TButton", background=t["btn"], foreground=t["fg"])
    style.map("TButton", background=[("active", t["btn_active"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, background=t["bg"], foreground=t["fg"])
        style.map(w, background=[("active", t["bg"])])
    style.configure("Horizontal.TProgressbar", background=t["accent"],
                    troughcolor=t["field"])
    style.configure("Treeview", background=t["field"], foreground=t["fg"],
                    fieldbackground=t["field"])
    style.configure("Treeview.Heading", background=t["btn"], foreground=t["fg"])
    style.map("Treeview", background=[("selected", t["accent"])],
              foreground=[("selected", t["fg"])])
    style.configure("TLabelframe", background=t["bg"], bordercolor=t["border"],
                    lightcolor=t["bg"], darkcolor=t["bg"])
    style.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"])
    root.configure(bg=t["bg"])
    # dropdown lists of comboboxes
    root.option_add("*TCombobox*Listbox.background", t["field"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    # plain-tk widgets (Text logs, Canvas) aren't themed by ttk - walk and recolor
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Text):
                c.configure(bg=t["text_bg"], fg=t["text_fg"],
                            insertbackground=t["fg"],
                            selectbackground=t["accent"])
            elif isinstance(c, tk.Canvas):
                c.configure(bg=t["bg"])
            walk(c)
    walk(root)
