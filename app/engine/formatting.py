"""Time / size formatting and parsing (UI-free)."""
import re



# ======================= small helpers =======================
def parse_time(text):
    """'HH:MM:SS(.ms)', 'HH:MM:SS:mmm', 'MM:SS' or plain seconds -> float
    seconds, else None."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]?\d)[:.](\d{1,3})", text)
    if m:
        return (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000.0)
    m = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]?\d(?:\.\d+)?)", text)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def format_seconds(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(num_bytes):
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def fmt_time(sec):
    """Display format HH:MM:SS:mmm (milliseconds as a 4th colon group)."""
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}:{ms:03d}"


def format_ffmpeg_timestamp(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:09.6f}"
