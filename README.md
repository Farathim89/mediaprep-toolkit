# Intro & Credits Toolkit

A Windows GUI tool that automatically detects and removes intros and credits
from TV episodes using audio fingerprinting (MFCC matching) - while keeping
cold opens and post-credits scenes. Built on FFmpeg.

## Features

- **Template Cutter tab** - cut an intro/credits template from one episode
  with frame-exact HH:MM:SS time boxes; only the audio is used for matching
- **Batch Remover tab** - scans a whole folder of episodes, finds the intro
  and credits in each by matching your templates, cuts them out and stitches
  the rest back together (or just injects keyframes at the boundaries for
  media-server skip buttons)
- Frame-accurate cuts with clean keyframes at every cut point
- Codec choice: H.264, H.265/HEVC, NVENC (GPU), AV1 - with 8/10-bit selection
  and an Auto mode that matches the source's bit depth
- Live progress bar driven by ffmpeg's frame counter, per-step log,
  before/after file sizes, and an automatic duration check so you can verify
  nothing was lost
- Light / Dark / High Contrast themes; all settings remembered between runs
- Keeps all audio tracks and subtitles (`-map 0`), preserves audio codecs
  (FLAC stays FLAC, DTS stays DTS, etc.)

## Requirements

- Windows with [Python 3.9+](https://www.python.org/downloads/)
  (tick "Add python.exe to PATH" during install)
- [FFmpeg](https://ffmpeg.org/download.html) on PATH
  (type `ffmpeg` in cmd to check)
- Python packages for the Batch Remover tab:
  `pip install -r requirements.txt` (or run `Install Requirements.bat`)

## Usage

1. Double-click `Start Toolkit.bat` (uses windowless pythonw - no console).
   The `intro`, `credits`, `videos` and `output` folders are created
   automatically next to the script.
2. **Template Cutter tab**: pick one episode, enter the intro's From/To times
   (and/or the credits'), press *Cut template(s)*. Cut the WHOLE intro or
   credits - the Remover uses the template's length as the cut length.
3. Drop the episodes to process into the `videos` folder.
4. **Batch Remover tab**: press *Start*. Cleaned episodes appear in `output`
   as `<name>_clean.mkv`.

The in-app **Info / Settings** and **Recommended** tabs explain every setting
(CRF, presets, codecs, bit depth, detection confidence) with suggested combos.

### Recommended settings

| Goal | Codec | CRF | Preset |
|---|---|---|---|
| Closest to original (default) | H.264 | 18 | slow |
| Best size/quality balance | H.265 | 22 | slow |
| Anime / Hi10P sources | H.265 10-bit | 22-23 | slow |
| Fast bulk with NVIDIA GPU | H.265 NVENC | 24 | slow |

## How detection works

Each template's audio is converted to MFCC features and cross-correlated
against the episode's audio (first/last 10 minutes). The best match above the
confidence threshold decides where to cut. Raise the threshold if it
false-matches, lower it if it misses - the log shows every template's score.

## Troubleshooting

- App won't start: run `Start Toolkit (debug console).bat` to see the error.
- "Missing package": run `Install Requirements.bat`.
- Weak matches: make sure the template comes from the same show/season and
  covers the whole intro/credits.

## License

MIT - see [LICENSE](LICENSE).
