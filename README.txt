MEDIAPREP TOOLKIT v2.0.1
========================
(intro/credits cutting, chapter markers, theme audio, loudness, compare & check)

MediaPrep Toolkit prepares TV episodes for a media server such as Plex or
Jellyfin. It finds the intro, credits, pre-intro (recap/logo) and after-credits
(teaser) of every episode by their AUDIO and either cuts them out, marks them
with keyframes, or adds chapter markers so your player can offer "Skip Intro" -
without touching the picture. Around that it can export a theme.mp3, even out
the loudness of a season, compare a processed file with its original (tracks
and picture quality) and check folders for broken files. Nothing you work with
is ever deleted: everything "removed" goes to a recoverable trash folder.


GETTING STARTED
---------------

A) The exe release (recommended)
  1. Unzip the release anywhere (e.g. D:\MediaPrep Toolkit). Keep the
     _internal\ folder next to the exe.
  2. Double-click "MediaPrep Toolkit.exe". ffmpeg / ffprobe are included -
     nothing else to install.
  3. Windows SmartScreen may warn on the first run because the exe is not
     code-signed. Click "More info" -> "Run anyway".
  The Media\ and Data\ folders are created next to the exe on first start.

B) From source (Python)
  1. Run "Install Requirements.bat" once. It checks for Python (installs
     Python 3.12 via winget if missing), checks for ffmpeg (installs Gyan.FFmpeg
     via winget if missing) and installs the Python packages: librosa numpy
     scipy opencv-python Pillow sounddevice tkinterdnd2.
  2. Double-click "Start Toolkit.bat" (runs without a console window).
  3. If the app doesn't start or misbehaves, run
     tools\Start Toolkit (debug console).bat - it shows Python errors in a
     console window.


QUICK WORKFLOWS
---------------

Clean a whole season
  1. Templates tab: Auto-detect -> pick the season folder (2+ episodes) ->
     "Detect intro / credits" -> "Auto-cut all templates".
     (Or cut the templates by hand from one episode on Cut template.)
  2. Put the episodes in Media\videos.
  3. Cut / Edit -> Auto-detect: tick "Review first (detect only)" and start.
     Nothing is cut - every episode is matched and loaded into Multi cut.
  4. Multi cut: fix any row marked with a warning sign (⚠) - use the player,
     Set / Snap, or another match from the row's ▾ candidates list.
  5. "Cut all files" (or "Add to queue"). Cleaned files appear in
     Media\output with the same file name.

Clean a single episode
  1. Cut / Edit -> Manual cut: load the episode.
  2. Press "Auto-detect" (next to Load) - it fills the boxes with the best
     template match per section.
  3. If a section is wrong, pick another variant from its ▾ Candidates list,
     then nudge with Set / Snap / the arrow keys (1 frame, Shift = 10 frames).
  4. "Cut previewed video (manual)".

Plex "Skip Intro" without re-encoding
  Cut / Edit -> Auto-detect, Run mode "Add chapter markers". The files are not
  cut and not re-encoded - Intro / Credits chapters are added so Plex or
  Jellyfin can offer skip buttons.

theme.mp3 for Plex
  Audio -> Theme Audio: load an episode, Set "Intro from" / "Intro to", choose
  MP3, name it "theme", export, and put theme.mp3 in the show's folder.

Even out the loudness of a season
  Audio -> Audio Gain -> Batch (season): put the files in
  Media\Audio Gain\input, choose Loudness (LUFS), "Analyze all (loudness)",
  then "Normalize folder". Afterwards point Input at the output folder and
  Analyze all again to confirm the files now match.


THE TABS
--------
The main tabs follow the workflow left to right: Templates, Cut / Edit, Audio,
Inspect, Log, Info. The app remembers the tab and sub-tab you were on. Every
tab has a "?" button (top-right) with its help; "Help" in the status bar lists
all of them. Drag & drop works throughout: drop a folder or a file onto a
folder box to use that folder, drop files onto players and lists to load them.

TEMPLATES
  Cut template - cut the intro / credits (and optional pre-intro /
    after-credits) of ONE episode into template clips that Cut / Edit then
    searches for in every episode.
    - Load a video, tick a section, find its start and press Set next to From,
      then its end and Set next to To. A ▶ next to a section previews it.
    - Snap moves a From / To to the nearest silence or black-frame edge within
      +/-1 s.
    - Empty boxes: Pre-intro From = file start; Credits / After-credits To =
      file end. Intro needs both times, Credits needs a From.
    - IMPORTANT: Cut / Edit uses the template LENGTH as the cut length, so cut
      the WHOLE segment, not just a recognisable part.
    - Templates are saved as .mkv in Media\templates\intro, \credits,
      \preintro or \aftercredits as "<Show - SxxEyy>_<section>.mkv". Only the
      audio matters. If the name exists you choose overwrite, keep both
      (_v2, _v3 ...) or skip.

  Auto-detect - finds the intro / credits that RECUR across a season by
    fingerprinting the audio - no template needed. Pick a folder with 2+
    episodes of the same show and press "Detect intro / credits".
    - Settings: search first / last N seconds (default 420), minimum lengths,
      "Scan at most (eps)" (0 = all, recommended), Sensitivity (High, Medium =
      default, Low, Very loose), episode length presets and "Detect on audio"
      (which language track).
    - One row per found segment; a show with several openings gets several
      rows. Rows already covered by a template are shown green "(have)".
    - "Fill times from selected (review)" / double-click loads the example
      episode into Cut template. Audition plays the segment from up to 3
      episodes spread over the season. "Auto-cut all templates" cuts a template
      for every row not marked "have".
    - Nothing found? Lower the minimum length, loosen the sensitivity or
      enlarge the search window - the Log suggests which.

  Templates (manager) - lists every template clip with kind, length, audio
    language, size and date. Play (or double-click), Rename... (F2), Reveal in
    Explorer, Move to trash (Delete), Import... (or drop videos on the list to
    COPY them into a template folder) and Refresh. Every template in a folder is
    used by Cut / Edit, so trash wrong or duplicate ones - a bad template can
    cause false cuts.

CUT / EDIT
  Auto-detect (batch) - processes every video in the Videos folder: finds each
    template by its audio and removes (or marks) those parts. Folders, Encoding
    and subtitle settings here are SHARED with Manual cut and Multi cut.
    - Run mode:
        Cut               removes the found segments and joins the rest.
        Inject keyframes  keeps the whole video, only adds keyframes at the
                          boundaries.
        Add chapter markers  no cut, no re-encode - adds chapters for Plex /
                          Jellyfin skip buttons.
    - Detection: Min confidence (0.32 default; raise it if wrong things get
      cut, lower it if segments are missed), segments to use, audio language
      to match on.
    - Options: move finished sources to videos\done, cut intro from file start
      / credits to file end, skip an episode if an enabled segment isn't found,
      trim to where the audio still matches, or anchor the cut to the
      template's first / last N seconds.
    - "Review first (detect only)": a dry run - nothing is cut; every video is
      matched and loaded into Multi cut with its detected times (weak or
      missing matches get a ⚠ and a note).
    - Output must differ from the source folder and keeps the same file name.
      After each file a [VERIFY] line in the log confirms no audio/subtitle
      track or resolution was lost.

  Manual cut - cut ranges you mark yourself out of ONE file, no templates
    needed. Use Set (current frame), Go (jump to the time), ▶ (preview) and
    Snap for each From / To. Empty sections are skipped; an empty Pre-intro /
    Intro From means file start, an empty Credits / After-credits To means
    file end. "Auto-detect" (next to Load) matches this episode against every
    template variant and fills the best match per section; a row's ▾
    Candidates list picks another variant. "Cut previewed video (manual)" uses
    the Encoding settings from Auto-detect (run mode and confidence are
    ignored).

  Multi cut - manual cutting for many files, each with its own ranges. Add
    files (button or drag files/folders onto the list), or let "Review first"
    fill it. Select a file, mark its sections (Set / Go / Snap / ▾ candidates)
    - edits are saved to that file automatically. Up / Down switch file; "Keep
    player time when switching files" reopens the next file at the same
    timestamp. "Apply to selected" / "Apply to ALL files" copy the current
    times. Cells show from-to, 0:00 / end for implied ends, or ? when
    incomplete (that section is skipped). "Cut all files" cuts each file with
    its own ranges; two files with the same name are refused. "Add to queue"
    queues the list as it is now. The list is restored next session.

  Encoding (codec, bit depth, CRF/CQ, preset - see Info -> Recommended):
    - Auto - CPU (match source): HEVC -> libx265, AV1 -> SVT-AV1, else libx264.
    - Auto - GPU / NVENC (match source): the matching NVIDIA hardware encoder.
    - Plex-ready HEVC / Plex-ready H.264: near-lossless H.264 or HEVC in an
      MKV, with the video bitrate capped under the source's so files don't
      grow. Uses NVENC when the PC can run it, else the CPU encoder. Audio is
      made Plex-friendly: AAC, AC3, E-AC3 and MP3 are kept, DTS / TrueHD /
      FLAC / Opus become E-AC3. Never AV1 (many Plex clients can't direct-play
      it).
    - H.264 / H.265 (CPU), H.264 / H.265 NVENC (GPU), AV1 (SVT-AV1).
    - NVENC fallback: if an NVENC encoder can't run on this PC (no NVIDIA GPU
      or driver), the CPU encoder of the same family is used instead and the
      log shows a [WARN] line - the batch doesn't fail.

  Subtitles: "Keep only chosen subtitle languages" filters subtitle tracks;
    video and audio tracks always stay.

  Show presets: "Save as..." stores the folders, encoding, detection and mode
    options (plus the Templates auto-detect settings) under a name in
    Data\presets; "Load" restores them; "Delete" moves the preset to the trash.
    Presets apply to all three sub-tabs.

  Queue: "Add to queue" captures the CURRENT folders and settings - change the
    folders and add again to line up several seasons. Stop kills the current
    ffmpeg step and discards its partial file; finished files are kept.

AUDIO
  Theme Audio - exports a stretch of a file's FIRST audio track as MP3,
    M4A/AAC, FLAC or WAV, with optional fade in/out. Set "Intro from" / "Intro
    to", pick the output folder and a file name WITHOUT extension, then
    "Export theme audio". Clips under 3 s get a warning; an existing file with
    the same name is overwritten.

  Audio Gain - Single file: "Analyze loudness" shows LUFS, true peak, mean and
    max volume. Then:
      - change gain by N dB (above +12 dB it may clip),
      - normalize to a LUFS target (default -16, two-pass loudnorm; the
        true-peak ceiling, default -1.5 dBTP, caps peaks - loud parts are
        compressed if needed), or
      - set the sample peak to N dB.
    "Apply to this file" writes NEXT TO the input as <name>..._<target>.<ext>
    (e.g. _-16LUFS, _+3dB, _-1dBpeak). Video is copied untouched. "Output
    audio" keeps the source codec & channels by default (5.1 AC3 / DTS stay
    intact) or converts to AAC / AC3 / E-AC3 / FLAC / Opus; a container that
    can't hold the chosen codec becomes .mkv.

  Audio Gain - Batch (season): makes a whole folder equally loud. Input /
    output default to Media\Audio Gain\input and \output. Match by Loudness
    (LUFS, recommended), Peak, or the same flat Gain for every file.
    "Analyze all (loudness)" fills the table with LUFS, peak, mean, the
    difference to the median and the gain that will be applied; files off by
    more than 1.0 LUFS are highlighted and the Log says CONSISTENT or CHECK.
    A * in Gain means loudnorm will compress that file. Click the check cells
    to tick/untick (header = all); Delete removes rows from the list only.
    "Normalize folder" processes the ticked files; "Add to queue" queues them.
    Note: audio dB counts DOWN from 0, so closer to 0 = louder (-24 is louder
    than -31).

INSPECT
  Dual Player - two players side by side (e.g. original vs. cleaned). The
    "both" buttons play, pause, step and jump both together. With Link ticked
    a seek, step or Go in one player moves the other to the same TIME, so
    files with different frame rates stay aligned. Mute one side to A/B the
    audio.

  Compare - Tracks: checks that a processed file kept all its tracks. Lists
    duration, size, video, cover art, audio (codec, language, channels,
    default/forced), subtitles and attachments (fonts) side by side, paired by
    language. A red X = missing track, changed language / channels / flags, or
    a new file more than 2 s LONGER. A shorter file (after a cut) and codec
    changes are expected and not flagged.

  Compare - Quality: SSIM, PSNR and VMAF (only if your ffmpeg has libvmaf).
    By default a few sample windows are compared; "Full scan" compares every
    frame (slow - "Add to queue" runs it after other jobs). Auto-align finds
    each window in the original by its audio, so CUT files compare correctly
    (not with Full scan). Guide: SSIM 0.98+ or VMAF 90+ = no visible
    difference.

  Check - finds broken video files; files are never changed. Quick = the file
    opens and has valid streams (fast). Full = decodes the whole file and
    reports errors (slow); a full check that times out shows UNKNOWN, which is
    not proof of damage. Check one file, or a folder (sub-folders included) /
    dropped files: tick rows and press "Check ticked files" or "Add to queue".
    Results: OK (green), BROKEN (red), UNKNOWN (amber).

LOG
  Every tab's messages in one place, auto-saved to
  Data\logs\session_YYYYMMDD.log so overnight batches are captured. "Save
  log...", "Clear" and "Open logs folder". The view keeps the last 20,000
  lines. Each tool tab also has its own Log sub-tab.

INFO
  Info / Settings explains every setting; Recommended lists encoding combos for
  high quality + small file size.


PLAYER KEYBOARD SHORTCUTS
-------------------------
Every preview player plays sound, with a mute button and a volume slider.
Keys work anywhere on the tab except while typing in a box (Dual Player:
click a video first).

    Space             play / pause
    Left / Right      step 1 frame
    Shift+Left/Right  step 10 frames
    Home / End        first / last frame
    M                 mute
    Up / Down         previous / next file (Multi cut)
    F2 / Delete       rename / move to trash (Templates manager)

Click or drag the timeline to seek. "Go to:" accepts 21:30, 0:01:05.5 or
00:01:05:500. Time boxes are HH : MM : SS : mmm - an empty box counts as 0.
"⏏ Unload" releases the file so it can be moved.


SETTINGS, QUEUE & STATUS BAR
----------------------------
Settings... (top bar):
  - Language: 19 languages (English, Svenska, Espanol, Deutsch, Francais,
    Portugues (Brasil), Italiano, Russian, Japanese, Chinese (Simplified),
    Norsk bokmal, Dansk, Suomi, Polski, Nederlands, Turkce, Korean, Hindi,
    Bahasa Indonesia). "Automatic" follows the Windows display language
    (English if it isn't one of these). A change takes effect after a restart
    - the app offers to restart right away.
  - Theme: Follow Windows, Light, Dark, High Contrast, Plex, Gold on Dark,
    Midnight Blue, Nord, Dracula, Solarized Light, Solarized Dark, Forest,
    Sepia. Follow Windows switches Light / Dark with the Windows app mode. A
    change applies to every window at once. (Also in the top bar.)
  - Notifications: when a job that ran longer than the minimum (default
    1 minute) finishes you get a Windows notification (or a small popup in the
    bottom-right corner), an optional sound, and the taskbar button flashes if
    the window is in the background. "Test notification" tries it.
  - Updates: "Check for a new version at startup (once a day)" and "Check
    now". The check only asks GitHub for the latest release and shows a banner
    that opens the release page - nothing is downloaded or installed.
  - Logs: session logs older than N days (default 30) are moved to
    Data\temp\trash at startup.
Settings are remembered in Data\settings.json.

Status bar and job queue: the status bar at the bottom shows what is running
("Idle" or "Running: ...") and how many jobs are queued. Queued jobs run one
after another, only when nothing else is running. "Add to queue" exists on
Cut / Edit (Auto-detect batch, Multi cut), Audio Gain (Normalize folder),
Compare (Quality) and Check; each entry keeps the files and settings from the
moment it was added. "Queue..." opens the queue: reorder or remove waiting
jobs, clear it, pause/resume, or "Stop all" (stops running jobs and pauses the
queue). Each processing tab also has a Stop button by its progress bar.


FOLDERS
-------
All created next to the app (the exe, or intro_credits_toolkit.py) on first
start:
  Media\                     - your media:
      videos\                    put episodes to process here (done\ = finished)
      output\                    cleaned episodes appear here
      templates\intro\           intro template clips (made on the Templates tab)
      templates\credits\         credits template clips
      templates\preintro\        optional pre-intro clips (recaps/logos before the intro)
      templates\aftercredits\    optional after-credits clips (teasers/previews)
      Audio Gain\input\          drop a season here for the Audio Gain batch
      Audio Gain\output\         normalized episodes appear here
  Data\                      - the app's own files:
      settings.json              your settings and theme
      presets\                   per-show Cut / Edit presets
      logs\                      session logs (auto-saved); the exe also
                                 writes Python errors to errors.log here
      temp\                      temporary files; temp\trash\ = everything
                                 "deleted" by the app (recoverable)
      backups\                   the old single-file version and settings
                                 backups, kept just in case

Upgrading from 1.x: the old folders that sat next to the app (videos\,
output\, input\, Audio Gain\, logs\, temp\, presets\, backups\,
toolkit_settings.json) are moved into Media\ and Data\ automatically the first
time the new version starts. Nothing is deleted: folders are merged, a name
clash keeps both files (the older one gets " (old)"), saved folder paths in the
settings and presets are updated (the original settings file is copied to
Data\backups\ first), and the Log tab lists every move. Anything that is in
use is left where it is with a warning in the log. To move to the exe, unzip
it and copy your Media\ and Data\ folders next to "MediaPrep Toolkit.exe".


NOTHING IS EVER DELETED
-----------------------
Everything the app "deletes" (trashed templates and presets, old logs, cleaned
folders) is MOVED to Data\temp\trash\<date-time> - you can recover it.

Clean up folders... (top bar) empties working folders the same way: tick
Media\templates, Media\videos, Media\output, Media\Audio Gain in/out or
Data\temp (each shows its file count and size) and press "Clean up selected" -
it asks first.

"Empty trash older than N days" (default 14) in the same dialog is the ONLY
permanent delete in the app; it shows how much will go and asks first. Both
are blocked while any job is running. Partial output files from a stopped job
are discarded; finished files are kept.


TROUBLESHOOTING
---------------
"ffmpeg / ffprobe not found on PATH" banner (running from source): install
  ffmpeg (Install Requirements.bat does it via winget, or get it from
  ffmpeg.org), then close and reopen the app so the new PATH is picked up.
  The exe release uses its bundled copy and doesn't need this.

NVENC chosen but no NVIDIA GPU: the CPU encoder of the same family is used
  automatically (log line "[WARN] ... not usable here"). Pick a CPU codec to
  silence it.

Intro / credits not found, or found in the wrong place:
  - Templates -> Auto-detect: lower the Sensitivity (Medium -> Low -> Very
    loose), lower the minimum length or enlarge the search window. Check the
    "Detect on audio" language.
  - Cut / Edit: lower Min confidence if segments are missed, raise it if
    wrong parts get cut. Remove bad or duplicate templates in the Templates
    manager. Use "Review first (detect only)" and fix ⚠ rows in Multi cut
    before cutting.
  - Remember a template's LENGTH is the cut length - cut whole segments.

SmartScreen blocks the exe: "More info" -> "Run anyway" (the exe isn't
  code-signed).

Where are the logs? Data\logs\session_YYYYMMDD.log (Log tab -> "Open logs
  folder"). The exe writes Python errors to Data\logs\errors.log; from source,
  use tools\Start Toolkit (debug console).bat to see them.


PROJECT LAYOUT (for editing the code)
-------------------------------------
  intro_credits_toolkit.py         - launcher (the .bat runs this; also the exe
                                     entry point)
  Data\backups\intro_credits_toolkit_legacy.py - the previous single-file version
  app\        - the code, split into small modules:
      app.py        main() - builds the window and wires the tabs together
      config.py     folder layout (APP_ROOT, Media\, Data\), settings,
                    codec/audio maps
      migrate.py    moves an old flat folder layout into Media\ + Data\
      i18n.py       translations (tr()); catalogs in app\locales\*.json
      applog.py jobs.py notify.py updater.py presets.py helpdocs.py
                    session log, job queue, notifications, update check,
                    per-show presets, the help texts
      selftest.py   GUI-less engine check
                    ("MediaPrep Toolkit.exe" --selftest <dir>)
      engine\       the media engine - ffmpeg/ffprobe/librosa, no GUI code:
          probe.py      ffprobe helpers (durations, streams, audio tracks, checks)
          process.py    running ffmpeg (progress, Stop, power throttling)
          files.py      output .part files, done\ moves, move-to-trash
          encode.py     encoder/codec choice, NVENC fallback, Plex-ready plan
          detect.py     template fingerprints + matching (detect_segments)
          recurring.py  template-free intro/credits detection across episodes
          cut.py        the cut engines (batch + manual), keep/drop maths
          chapters.py   chapter markers      subs.py      subtitle trim/flags
          loudness.py   loudness, gain, normalize, theme export
          snap.py       snap a cut point to silence / black frames
          formatting.py time / size formatting
      ui\           shared GUI pieces:
          widgets.py    TimeEntry, ScrollFrame, tooltips, help windows
          themes.py     the colour themes (one palette each) + Follow Windows
          player.py     the video preview player (with sound)
          playback.py   ffmpeg + sounddevice audio playback (volume/mute)
          dualplayer.py the Dual Player   cleanup.py  the Clean up dialog
          dialogs.py    the Queue and Settings windows   tkthread.py  Tk-thread calls
      tabs\         one module per tab / sub-tab:
          templates.py (+ templates_detect.py, templates_manager.py,
                        audition.py)          the Templates tab
          cut_edit.py  (+ cut_auto.py, cut_manual.py, cut_multi.py,
                        cut_common.py)        the Cut / Edit tab
          theme_audio.py, audio_gain.py, audio_gain_batch.py
                        (+ audio_common.py)   the Audio tab
          compare.py, check.py, log.py        Compare, Check and Log
          common.py                           helpers shared by the tabs
      assets\       the window icon (icon.ico / icon.png)
      locales\      one JSON catalog per language (+ _template.json)
  tools\        - Start Toolkit (debug console).bat, build_exe.py +
                  MediaPrep.spec (the exe build), i18n_extract.py

Translations: all UI and help strings are marked with tr() / N_(). After
changing texts, run (from the project root)
    python tools\i18n_extract.py --check
It rewrites app\locales\_template.json and lists per language how many keys
are missing / obsolete and any placeholder mismatches (add -v to print the
missing keys).


LICENSE
-------
MediaPrep Toolkit is free software, released under the GNU General Public
License v3.0 or later (GPL-3.0-or-later) - see LICENSE / LICENSE.txt.
Source code: https://github.com/Farathim89/mediaprep-toolkit
The exe releases bundle unmodified ffmpeg / ffprobe builds (gyan.dev, GNU GPL
v3) and mpv (GNU GPL v2 or later); see _internal\ffmpeg\README-ffmpeg.txt and
_internal\mpv\README-mpv.txt.
