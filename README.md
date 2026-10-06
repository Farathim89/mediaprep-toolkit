# MediaPrep Toolkit (Intro & Credits Toolkit)

A Windows GUI toolkit for preparing TV episodes for a media server (Plex,
Jellyfin, ...): find and remove intros and credits, add Skip-Intro chapter
markers, export theme songs, normalise loudness, and check/compare the results.
Built on FFmpeg with a Tkinter interface.

> This is the successor of the original single-file *Intro & Credits Toolkit*.
> The code is now split into small modules under `app/`, and it gained
> auto-detection without templates, preview players with sound, and several
> new tools.

## Features

**Template Cutter**
- *Cut template* - open an episode in the built-in frame-accurate preview
  player, scrub to each boundary and click **Set** to capture the exact frame
  for Pre-intro, Intro, Credits and After-credits, then cut template clips.
- *Auto-detect* - point it at a folder of episodes (2+) from the same show and
  it finds the intro and credits **without any template**, by fingerprinting
  the audio and spotting the segment that recurs across episodes. Shows with
  several different openings get one row per opening. Adjustable sensitivity.

**Cut / Edit**
- *Auto-detect* batch - finds the segments in every episode of the `videos`
  folder and processes them in one go.
- *Manual cut* - one video, your own exact cut points.
- *Multi cut* - many files, each with its own times; apply times to several
  files at once, flick through episodes with Up/Down.
- Three run modes: **Cut** (remove the segments), **Inject keyframes** (keep
  everything, just mark the boundaries) or **Add chapter markers** (no
  re-encode; lets Plex/Jellyfin offer *Skip Intro*).
- Codecs: H.264, H.265/HEVC, AV1, NVIDIA NVENC, plus *Auto (match source)* for
  CPU or GPU; 8/10-bit or auto.
- Keeps every audio track (codec untouched), with optional subtitle-language
  filtering. A `[VERIFY]` line after each file confirms no track or resolution
  was lost; finished sources can be moved to `videos\done`.

**Extra tools**
- **Theme Audio** - export just the intro's audio (e.g. `theme.mp3` for Plex).
- **Audio Gain** - gain by dB, two-pass LUFS normalisation or peak matching,
  for one file or a whole season (with a per-file LUFS / peak / gain table).
  Keeps the source audio codec & channels (5.1 AC3 / DTS stay intact) or
  converts to AAC / AC3 / E-AC3 / FLAC / Opus.
- **Dual Player** - two synced previews side by side to compare original vs
  cleaned, each with its own sound.
- **Compare** - track-by-track comparison (flags dropped tracks/languages) and
  visual quality scores (SSIM, PSNR, VMAF when available).
- **Check** - quick or full-decode check of a file or a whole folder for
  corrupt / truncated videos.
- **Log** tab with a live combined log, auto-saved to `logs\`.
- Drag & drop everywhere, keyboard shortcuts in every player, Light / Dark /
  High Contrast themes, all settings remembered between runs.
- **Clean up folders** moves working files to `temp\trash` - nothing is ever
  deleted outright.

## Requirements

- Windows with [Python 3.9+](https://www.python.org/downloads/)
  (tick *Add python.exe to PATH* during install)
- [FFmpeg](https://ffmpeg.org/download.html) (`ffmpeg` and `ffprobe` on PATH)
- Python packages: `pip install -r requirements.txt`

`Install Requirements.bat` does all of this for you (it installs Python and
FFmpeg with winget if they are missing).

## Usage

1. Run `Install Requirements.bat` once.
2. Double-click `Start Toolkit.bat` (windowless). If it doesn't open, use
   `Start Toolkit (debug console).bat` to see the error.
3. Either cut templates on the **Template Cutter** tab, or let **Auto-detect**
   find the intro/credits from a folder of episodes.
4. Put the episodes to process in `videos\`, then press
   **Start batch (auto-detect)** on the **Cut / Edit** tab. Cleaned files
   appear in `output\` with the same file name as the source.

The in-app **Info / Settings** tab explains every setting and lists
recommended combinations for high quality at a small file size.

### Keyboard (all players)

| Key | Action |
|---|---|
| Space | play / pause |
| Left / Right | step 1 frame (Shift: 10 frames) |
| Home / End | first / last frame |
| M | mute |
| Up / Down | previous / next file (Multi cut) |

## Folders

| Folder | Contents |
|---|---|
| `input\intro`, `input\credits`, `input\preintro`, `input\aftercredits` | template clips |
| `videos\` | episodes to process (`videos\done` = finished sources) |
| `output\` | cleaned episodes |
| `Audio Gain\input`, `Audio Gain\output` | Audio Gain batch folders |
| `logs\` | session logs |
| `temp\` | work files and `temp\trash` |

These are created automatically and are ignored by git.

## Project layout

```
intro_credits_toolkit.py   launcher (the .bat files run this)
app/
  config.py      settings, codec/audio maps, themes
  media.py       ffmpeg/ffprobe helpers + detect and cut engines
  player.py      frame-accurate preview player (with sound)
  audio.py       ffmpeg + sounddevice audio playback
  widgets.py     TimeEntry, ScrollFrame, Info/Recommended help tabs
  tabs.py        Template Cutter and Cut / Edit tabs
  audiotools.py  Theme Audio and Audio Gain tabs
  compare.py     Compare tab          checktab.py  Check tab
  dualplayer.py  Dual Player          cleanup.py   Clean up dialog
  applog.py      session log          logtab.py    Log tab
  app.py         main() - builds the window
assets/          window icon
```

## How detection works

- **With templates**: each template's audio is turned into MFCC features and
  cross-correlated against the start/end of every episode; the best match
  above the confidence threshold decides where to cut.
- **Without templates (Auto-detect)**: the audio of several episodes is
  fingerprinted and compared pairwise; a stretch of audio that recurs across
  episodes near the start (intro) or end (credits) is taken as the segment.

## License

MIT - see [LICENSE](LICENSE).
