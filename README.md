# MediaPrep Toolkit

A Windows app for getting TV episodes and anime ready for **Plex / Jellyfin**:
find and remove intros and credits (or mark them with *Skip Intro* chapters),
export theme songs, even out loudness across a season, and check that nothing
was lost or broken along the way. Built on FFmpeg.

> Successor of the original single-file *Intro & Credits Toolkit*. Version 2.0
> is a near-complete rewrite: template-free detection, a review-before-encoding
> workflow, Plex-ready presets, audio tools, a job queue, 13 themes and
> 19 languages.

**Download:** grab the latest `MediaPrep Toolkit` zip from
[Releases](../../releases), unzip anywhere and run `MediaPrep Toolkit.exe`.
FFmpeg is included - nothing else to install.

## Features

### Templates
- **Cut template** - frame-accurate preview player (with sound), **Set** the
  current frame as a boundary, **Snap** it to the nearest silence / black frame,
  cut pre-intro / intro / credits / after-credits template clips.
- **Auto-detect** - point it at a season (2+ episodes): it finds the intro and
  credits **without any template** by spotting audio that recurs across
  episodes. Shows with several openings/endings (anime!) get one row per
  variant. **Audition** a row across episodes before cutting it.
- **Templates manager** - list, play, rename, import, reveal or trash templates.

### Cut / Edit
- **Auto-detect batch** - match every episode in `Media\videos` against all
  template variants and process the whole folder.
- **Review first (detect only)** - dry run: every episode lands in the Multi cut
  list with its detected times, weak matches flagged ⚠, so you can fix them
  before hours of encoding.
- **Manual cut** with **Auto-detect**: fills the boxes from the best template
  match, a ▾ list offers the other candidates (other OP/ED variants), then
  nudge to the exact frame with Set / Snap / arrow keys.
- **Multi cut** - many files, each with its own times; Up/Down flicks between
  episodes at the same timestamp.
- Run modes: **Cut**, **Inject keyframes**, or **Add chapter markers**
  (stream copy - no re-encode - Plex/Jellyfin offer *Skip Intro*).
- Codecs: **Plex-ready HEVC / H.264** (near-lossless, bitrate capped below the
  source so files never grow, Plex-friendly audio, MKV), H.264, H.265, AV1,
  NVIDIA NVENC (automatic CPU fallback without an NVIDIA GPU), or *Auto*.
- Seamless joins: audio is built in one pass over all kept pieces, so there is
  no dropout at the cut points and sound stays in sync.
- Every audio track kept (5.1 stays 5.1, DTS → E-AC3), subtitles kept with
  their forced/default flags (optional language filter), a `[VERIFY]` check
  after each file, finished sources moved to `videos\done`.
- **Per-show presets** and **Add to queue** for overnight runs.

### Audio
- **Theme Audio** - export the intro as `theme.mp3` for Plex, with fades,
  optional -16 LUFS normalisation and a preview.
- **Audio Gain** - gain by dB, two-pass LUFS (per track, true-peak ceiling) or
  peak, for one file or a whole season with a LUFS / peak / gain table,
  per-language track picker and "only fix outliers".

### Inspect
- **Dual Player** - original vs cleaned side by side, linked seeking.
- **Compare** - tracks (dropped tracks/languages, flags, attachments) and
  picture quality (SSIM, PSNR, VMAF) with auto-alignment.
- **Check** - quick or full-decode check of files/folders for corruption.

### App
- Job queue with status bar, Windows notifications when long jobs finish,
  update check (GitHub releases), per-tab **?** help.
- 13 themes incl. *Follow Windows*, Plex, Gold on Dark, Nord, Dracula,
  Solarized; all pass WCAG 4.5:1 text contrast.
- 19 languages: English, Svenska, Español, Deutsch, Français, Português (BR),
  Italiano, Русский, 日本語, 简体中文, Norsk bokmål, Dansk, Suomi, Polski,
  Nederlands, Türkçe, 한국어, हिन्दी, Bahasa Indonesia.
- Nothing is ever deleted outright - "delete" means `Data\temp\trash`.

## Folders

Created next to the app on first start:

| Folder | Contents |
|---|---|
| `Media\videos` | episodes to process (`done\` = finished sources) |
| `Media\output` | cleaned episodes |
| `Media\templates\{intro,credits,preintro,aftercredits}` | template clips |
| `Media\Audio Gain\{input,output}` | Audio Gain batch |
| `Data\` | `settings.json`, `presets\`, `logs\`, `temp\` (+ `trash\`), `backups\` |

Upgrading from 1.x: the old folders are moved into `Media\` / `Data\`
automatically on first start (nothing is deleted).

## Running from source

Requires Windows, [Python 3.10+](https://www.python.org/downloads/) and
[FFmpeg](https://ffmpeg.org/download.html) on PATH.

```bat
Install Requirements.bat
Start Toolkit.bat
```

`tools\Start Toolkit (debug console).bat` shows Python errors in a console.
Build the exe with `python tools\build_exe.py` (PyInstaller).

### Project layout

```
intro_credits_toolkit.py   launcher
app/
  app.py  config.py  i18n.py  migrate.py  jobs.py  notify.py  updater.py
  presets.py  helpdocs.py  applog.py  selftest.py
  engine/   ffmpeg/ffprobe work, no UI: probe, process, files, encode,
            detect, recurring, cut, chapters, subs, loudness, snap
  ui/       widgets, player, playback, dual player, dialogs, themes, cleanup
  tabs/     templates*, cut_*, theme_audio, audio_gain*, compare, check, log
  locales/  one JSON catalog per language (tools/i18n_extract.py --check)
  assets/   icon
tools/      build script, PyInstaller spec, i18n extractor, debug launcher
```

### Translations

UI strings are English keys wrapped in `tr()`. Run
`python tools\i18n_extract.py --check` to see missing keys per language;
corrections to `app/locales/<lang>.json` are welcome.

## License

MIT - see [LICENSE](LICENSE). The release build bundles FFmpeg (gyan.dev full
build, GPLv3) - see `_internal\ffmpeg\README-ffmpeg.txt` in the release for
its license and source links.
