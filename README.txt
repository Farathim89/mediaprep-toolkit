MEDIAPREP TOOLKIT v2.1.0
========================
(intro/credits cutting, chapter markers, convert, theme audio, loudness,
compare & check)

MediaPrep Toolkit prepares TV episodes for a media server such as Plex or
Jellyfin. It finds the intro, credits, pre-intro (recap/logo) and after-credits
(teaser) of every episode - by their sound, their pictures or both - and either
cuts them out, marks them with keyframes, or adds chapter markers so your
player can offer "Skip Intro". It works with template clips you cut once or,
with no templates at all, with a Plex-style scan of the season. Around that it
can re-encode videos HandBrake-style (Convert), export a theme.mp3, even out
the loudness of a season, compare a processed file with its original (tracks
and picture quality) and check folders for broken files. Nothing you work with
is ever deleted: everything "removed" goes to a recoverable trash folder.


GETTING STARTED
---------------
The release page (https://github.com/Farathim89/mediaprep-toolkit/releases)
offers three downloads. Each includes ffmpeg / ffprobe and the mpv video
player - nothing else to install.

A) Installer: MediaPrep-Toolkit-Setup-<version>.exe  (recommended)
  - Installs for all users into C:\Program Files\MediaPrep Toolkit (you can
    pick another folder). Windows asks once for administrator permission.
  - Media\ and Data\ are created next to the app in the install folder. The
    installer lets every user of the PC write to these two folders only - the
    program files themselves stay protected.
  - Start menu and desktop shortcuts (untick either one during setup).
  - Running a newer Setup upgrades in place: a running MediaPrep is closed
    first, and Media\ and Data\ are never touched.
  - Uninstall: Windows Settings -> Apps -> Installed apps -> MediaPrep
    Toolkit -> Uninstall. It asks whether to delete Media\ and Data\ as well -
    the default is No (keep them).

B) Portable: MediaPrep-Toolkit-Portable.exe
  - One single file, nothing is installed - put it anywhere, even on a USB
    stick. The name is the same in every release (the version is in the
    file's Properties -> Details), so shortcuts to it keep working.
  - Your Media\ and Data\ live in a "mediaprep-data" folder next to the exe.
    To move the portable app, move the exe together with that folder.
  - The first start (and the first start of a new version) unpacks the
    program once into mediaprep-data\runtime\, with a small progress window;
    every later start takes less than a second.
  - In a very deep folder (beyond Windows' path-length limit) the program is
    unpacked to %LOCALAPPDATA%\MediaPrep\rt instead; your data still stays in
    mediaprep-data\.

C) Zip: MediaPrep Toolkit v<version>.zip
   (GitHub lists it as MediaPrep.Toolkit.v<version>.zip)
  1. Unzip it anywhere (e.g. D:\MediaPrep Toolkit). Keep the _internal\
     folder next to the exe.
  2. Double-click "MediaPrep Toolkit.exe". Media\ and Data\ are created next
     to it on first start.
SmartScreen may warn the first time you run the Setup or an exe (they are not
code-signed): click "More info" -> "Run anyway".

D) From source (Python 3.10 - 3.12)
  1. Run "Install Requirements.bat" once. It checks for Python (installs
     Python 3.12 via winget if missing), checks for ffmpeg (installs
     Gyan.FFmpeg via winget if missing) and installs the Python packages:
     librosa numpy scipy opencv-python Pillow sounddevice tkinterdnd2.
  2. Double-click "Start Toolkit.bat" (runs without a console window).
  3. Optional: install mpv for smoother previews. The app finds mpv.exe on
     PATH, in %LOCALAPPDATA%\Programs\mpv or in C:\Program Files\MPV Player;
     without it the built-in OpenCV preview is used.
  4. If the app doesn't start, run tools\Start Toolkit (debug console).bat -
     it shows Python errors in a console window.


UPDATES
-------
Once a day at startup the app asks GitHub whether a newer release exists.
Switch this off in Settings -> Updates; "Check now" there (or "Check for
updates" in the Help window) checks right away. When there is a new version, a
bar at the top of the window offers "Update now", "Release notes" and "Later".

"Update now" downloads the matching file for your copy, with a progress window
(Cancel stops it), into a private folder in your own %TEMP%, and checks its
SHA-256 checksum against the one GitHub lists - a damaged or changed file is
deleted and nothing is installed. Then:
  - Installed: the new Setup runs silently over the current install (Windows
    asks once for administrator permission); the app restarts as the new
    version.
  - Portable: the new exe replaces MediaPrep-Toolkit-Portable.exe in place
    (same name and folder, so shortcuts keep working) and the app restarts as
    the new version. The previous exe stays next to it with ".old" added to
    its name until the next start.
  - Zip / source: the release page opens - extract the new zip over the old
    folder (Media\ and Data\ are not in the zip).
In every case Media\ and Data\ (videos, outputs, templates, settings, presets)
are kept. While a job runs the app doesn't update; it offers to update when the
job has finished (the queue waits until then). After an update a bar says
"Updated to version ..."; if a silent install failed, the next start says why.


QUICK WORKFLOWS
---------------

Clean a whole season (with templates)
  1. Templates -> Auto-detect: pick the season folder (2+ episodes) ->
     "Detect intro / credits" -> "Auto-cut all templates".
     (Or cut the templates by hand from one episode on Cut template.)
  2. Put the episodes in Media\videos.
  3. Cut / Edit -> Auto-detect: with "Review first: Match templates" selected,
     press "Review first (detect only)". Nothing is cut - every episode is
     matched and loaded into Multi cut.
  4. Multi cut: fix any row marked with a warning sign (⚠) - use the player,
     Set / Snap, or another match from the row's ▾ candidates list.
  5. "Cut all files" (or "Add to queue"). Cleaned files appear in
     Media\output with the same file name.
  (Sure of your templates? "Start batch (auto-detect)" cuts straight away.)

Clean a whole season without templates (Plex-style scan)
  Put the episodes in Media\videos, select "Review first: Plex-style scan (no
  templates)" on Cut / Edit -> Auto-detect and press "Review first (detect
  only)" - or add the files to Multi cut and press "Plex-style scan (no
  templates)". Then continue with steps 4-5 above.

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

Shrink or re-encode a season
  Convert: add the files or the folder, pick a preset (e.g. Plex-ready HEVC),
  press Preview to see the estimated size, then Start. Output goes to
  Media\convert\output.

theme.mp3 for Plex
  Audio -> Theme Audio: load an episode, Set "Intro from" / "Intro to", choose
  MP3, name it "theme", export, and put theme.mp3 in the show's folder.

Even out the loudness of a season
  Audio -> Audio Gain -> Batch (season): put the files in
  Media\Audio Gain\input, choose Loudness (LUFS), "Analyze all (loudness)",
  then "Normalize folder". Afterwards point Input at the output folder and
  Analyze all again to confirm the files now match.


THE PAGES
---------
The sidebar on the left lists the pages in workflow order: Templates,
Cut / Edit, Convert, Audio, Inspect, Log, Info. Ctrl+1 ... Ctrl+7 jump to them,
and ☰ folds the sidebar to icons. At its bottom: Theme, Clean up folders...,
Settings... and Help. The app remembers the page and sub-tab you were on.

Every page has a "?" button (top-right) with its help; "Help" in the sidebar
lists all of them (and has "Check for updates"). Hover the ⓘ icons and most
controls for a tooltip. Drag & drop works throughout: drop a folder or a file
onto a folder box to use that folder, drop files onto players and lists to
load them.

Pages keep a fixed layout: in a smaller window, scroll bars appear (mouse
wheel = up/down, Shift+wheel = sideways); a larger window shows more
background. The app is sharp on every monitor, also when monitors use
different scaling (e.g. 100 % and 150 %) - moving the window redraws it for
that monitor.

TEMPLATES
  Cut template - cut the intro / credits (and optional pre-intro /
    after-credits) of ONE episode into template clips that Cut / Edit then
    searches for in every episode.
    - Load a video, tick a section, find its start and press Set next to From,
      then its end and Set next to To (Set also ticks the section). A ▶ next
      to a section previews it.
    - From and To are both included: To = the LAST frame to cut. Park the
      player on the last frame of the intro and press Set next to To - that
      frame is part of the clip too.
    - Snap moves a From / To to the nearest natural boundary within +/-1 s:
      a picture cut (scene change), a black-frame edge or a silence edge -
      a picture cut wins when there is one.
    - Auto-detect (next to Load) fills the boxes for you: it compares the
      episode with its neighbours in the folder, takes what recurs and also
      matches the templates you already have (the Log says which kinds are
      "already covered by" a template). The box beside it chooses Audio,
      Visual (the pictures) or Audio + Visual. A lone episode only gets
      credits found by their look - the intro needs 2+ episodes. Each row's
      ▾ lists every candidate with its source and score.
    - Empty boxes: Pre-intro From = file start; Credits / After-credits To =
      file end. Intro needs both times, Credits needs a From.
    - IMPORTANT: Cut / Edit uses the template LENGTH as the cut length, so cut
      the WHOLE segment, not just a recognisable part.
    - Templates are saved as .mkv in Media\templates\intro, \credits,
      \preintro or \aftercredits as "<Show - SxxEyy>_<section>.mkv". If the
      name exists you choose overwrite, keep both (_v2, _v3 ...) or skip.

  Auto-detect - finds the intro / credits that RECUR across a season - no
    template needed. Pick a folder with 2+ episodes of the same show and press
    "Detect intro / credits".
    - Detect by: Audio (the sound that recurs), Visual (the PICTURES that
      recur - finds an opening whose audio differs, e.g. a dub, and recurring
      credits pictures) or Audio + Visual (default, both merged). Pre-intro /
      after-credits always use audio.
    - Settings: search first / last N seconds (default 420), minimum lengths,
      "Scan at most (eps)" (0 = all, recommended), Sensitivity (High, Medium =
      default, Low, Very loose), episode length presets, "Detect on audio"
      (which language track) and the safety margin (see Cut / Edit).
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
    template and removes (or marks) those parts. Folders, Encoding, Detection
    and subtitle settings here are SHARED with Manual cut and Multi cut.
    - Run mode:
        Cut               removes the found segments and joins the rest.
        Inject keyframes  keeps the whole video, only adds keyframes at the
                          boundaries.
        Add chapter markers  no cut, no re-encode - adds chapters for Plex /
                          Jellyfin skip buttons.
    - Detection: Min confidence (0.32 default; raise it if wrong things get
      cut, lower it if segments are missed), segments to use, audio language
      to match on, and "Match templates by": Audio (the template's sound),
      Visual (its pictures - also finds a dubbed opening) or Audio + Visual
      (default; if they disagree, a confident audio match is kept and the row
      gets a ⚠).
    - Safety margin (0-5 frames, default 1): extra frames cut before and after
      a detected intro / credits so no single intro frame flashes at the join.
      "With templates" sets where a template match ends: Follow the template's
      own edges (default), Exact content edges, or Exact + safety margin.
    - Options: move finished sources to videos\done, cut intro from file start
      / credits to file end, skip an episode if an enabled segment isn't found,
      trim to where the audio still matches, or anchor the cut to the
      template's first / last N seconds.
    - "Review first (detect only)": a dry run - nothing is cut; every video is
      checked and loaded into Multi cut with its detected times (weak or
      missing matches get a ⚠ and a note). "Review first:" chooses how:
      Match templates, or Plex-style scan (no templates - see Multi cut).
    - Output must differ from the source folder and keeps the same file name.
      After each file a [VERIFY] line in the log confirms no audio/subtitle
      track or resolution was lost.

  Manual cut - cut ranges you mark yourself out of ONE file, no templates
    needed. Use Set (current frame), Go (jump to the time), ▶ (preview) and
    Snap for each From / To; To is inclusive (the last frame removed). Empty
    sections are skipped; an empty Pre-intro / Intro From means file start,
    an empty Credits / After-credits To means file end. "Auto-detect" (next to
    Load) matches this episode against every template variant and fills the
    best match per section; a row's ▾ Candidates list picks another variant.
    "Cut previewed video (manual)" uses the Encoding settings from Auto-detect
    (run mode and confidence are ignored).

  Multi cut - manual cutting for many files, each with its own ranges. Add
    files (button or drag files/folders onto the list), or let "Review first"
    fill it. Select a file, mark its sections (Set / Go / Snap / ▾ candidates)
    - edits are saved to that file automatically. Up / Down switch file; "Keep
    player time when switching files" reopens the next file at the same
    timestamp. "Apply to selected" / "Apply to ALL files" copy the current
    times. Cells show from-to, 0:00 / end for implied ends, or ? when
    incomplete (that section is skipped).
    - Auto-detect ▾ -> Detect selected / Detect all: matches the templates in
      those files (with the Detection settings of Auto-detect).
    - "Plex-style scan (no templates)": finds intro and credits like Plex
      does. Intro = the opening that recurs across the episodes (several
      openings are fine); credits = an ending that recurs near the end, plus
      a picture check for credits (dark frames with bright or rolling text).
      Scans all files, or only the selected ones when 2+ are selected.
      Options: episode length, search windows, min lengths, sensitivity,
      Intro / Credits detection (Audio, Visual or Audio + Visual) and snapping
      to the nearest silence / black. Notes shows where each result came from.
    - Both ask before changing files that already have times: Overwrite, Only
      fill empty, or Cancel.
    - "Cut all files" cuts each file with its own ranges; two files with the
      same name are refused. "Add to queue" queues the list as it is now. The
      list is restored next session.

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

  Show presets: the row above the sub-tabs. "Save as..." stores the folders,
    encoding, detection (incl. Match templates by and the safety margin) and
    mode options (plus the Templates auto-detect settings) under a name in
    Data\presets; "Load" restores them; "Delete" moves the preset to the
    trash. Presets apply to all three sub-tabs.

  Queue: "Add to queue" captures the CURRENT folders and settings - change the
    folders and add again to line up several seasons. Stop kills the current
    ffmpeg step and discards its partial file; finished files are kept.

CONVERT
  Re-encodes videos HandBrake-style: codec, quality, size, frame rate, crop,
  filters and the audio / subtitle tracks.
  - Queue: add files or folders ("Include sub-folders") or drop them on the
    list. Each file keeps its OWN settings: select a file and the Video, Audio
    & subtitles and Output sub-tabs show and change them (several selected =
    all change; nothing selected = the settings new files start from). "Apply
    to all files" copies them to every file. Start converts the files one
    after another; "Add to queue" runs them after the running jobs.
  - Presets: Plex-ready HEVC / H.264, High quality HEVC (x265 CRF 18 slow),
    Small file HEVC (CRF 24), AV1 (SVT-AV1 CRF 30), Archive (x265 CRF 16
    10-bit) and Fast GPU (NVENC). "Save as..." stores your own in
    Data\presets\convert; Delete moves one to the trash.
  - Video: x264 / x265 / SVT-AV1, the NVENC encoders (CPU fallback without an
    NVIDIA GPU) or Copy; constant quality (CRF / CQ) or average bitrate; preset,
    bit depth, "Never bigger than the source". Frame rate, Resize (keeps the
    aspect ratio), Crop (Auto measures the black bars; "Detect now" shows the
    result), deinterlace, denoise, deblock and HDR -> SDR tone-mapping.
  - Audio & subtitles: one row per source track - keep, codec (Copy, AAC, AC3,
    E-AC3, Opus, FLAC, MP3), bitrate, mixdown, Default, Title; subtitles keep,
    Default, Forced or Burn in (one track).
  - Output: MKV or MP4 (MP4 can't hold ASS / PGS subtitles or fonts), folder
    (default Media\convert\output) and file name ({name}, {codec}, {res}).
    Existing outputs: Overwrite, Keep both or Skip. Files are written as .part
    and renamed when done, then tracks, resolution and duration are verified.
  - Preview encodes a 10 s sample (from the middle, or "Sample from"), shows
    the estimated size and encoding time and opens source and sample side by
    side in Inspect -> Dual Player.

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
  Every page's messages in one place, auto-saved to
  Data\logs\session_YYYYMMDD.log so overnight batches are captured. "Save
  log...", "Clear" and "Open logs folder". The view keeps the last 20,000
  lines. Each tool page also has its own Log sub-tab.

INFO
  Info / Settings explains every setting; Recommended lists encoding combos for
  high quality + small file size.


PLAYER KEYBOARD SHORTCUTS
-------------------------
Every preview player plays sound, with a mute button and a volume slider. It
uses mpv when available (smooth playback and fast frame steps, decoded on the
graphics card), else the simple OpenCV preview - see Settings -> Video player.
Keys work anywhere on the page except while typing in a box (Dual Player:
click a video first).

    Space             play / pause
    Left / Right      step 1 frame
    Shift+Left/Right  step 10 frames
    Home / End        first / last frame
    M                 mute
    Up / Down         previous / next file (Multi cut)
    F2 / Delete       rename / move to trash (Templates manager)
    Ctrl+1 ... Ctrl+7 go to a page (Templates ... Info)

Click or drag the timeline to seek. "Go to:" accepts 21:30, 0:01:05.5 or
00:01:05:500. Time boxes are HH : MM : SS : mmm - an empty box counts as 0.
A To box shows the LAST frame of the range (inclusive); Go jumps to it and ▶
plays through it. "⏏ Unload" releases the file so it can be moved.


SETTINGS
--------
Settings... (bottom of the sidebar), remembered in Data\settings.json:
  - Language: 19 languages (English, Svenska, Espanol, Deutsch, Francais,
    Portugues (Brasil), Italiano, Russian, Japanese, Chinese (Simplified),
    Norsk bokmal, Dansk, Suomi, Polski, Nederlands, Turkce, Korean, Hindi,
    Bahasa Indonesia). English is the default; "Automatic" follows the Windows
    display language (English if it isn't one of these). Takes effect after a
    restart - the app offers to restart right away.
  - Theme: 25 themes, each shown with a colour swatch. Basic: Follow Windows,
    Light, Dark (default), High Contrast. More themes: Catppuccin Latte /
    Mocha, Dracula, Everforest, Forest, GitHub Dark, Gold on Dark, Gruvbox
    Dark / Light, Kanagawa, Midnight Blue, Monokai, Nord, One Dark, Plex, Rosé
    Pine, Sepia, Solarized Dark / Light, Synthwave '84, Tokyo Night.
    Follow Windows switches Light / Dark with the Windows app mode. A change
    applies to every window at once. Quicker: the "Theme: ..." item in the
    sidebar - left-click opens the list, right-click switches between your
    last light and last dark theme.
  - Font size: 90 % to 130 % (100 % = normal); lists and pages grow with it.
    Takes effect after a restart.
  - Video player engine: Auto (mpv if available, default), mpv or OpenCV.
    Shows whether mpv was found. Applies to videos loaded after the change.
  - Performance -> Limit CPU use (recommended, on by default): every ffmpeg /
    ffprobe / mpv the app starts runs at below-normal priority, and encodes,
    decodes and detection use fewer threads, so the PC stays responsive and
    cooler. CPU encodes may take a little longer; NVENC is hardly affected.
    Max encoder threads: Auto (half the cores, at most 8), a fixed number, or
    All (only the lower priority stays). Applies to the next job; the audio
    analysis libraries pick it up after a restart.
  - Notifications: when a job that ran longer than the minimum (default
    1 minute) finishes you get a Windows notification (or a small popup in the
    bottom-right corner), an optional sound, and the taskbar button flashes if
    the window is in the background. "Test notification" tries it.
  - Updates: "Check for a new version at startup (once a day)" and "Check
    now" - see UPDATES above.
  - Logs: session logs older than N days (default 30) are moved to
    Data\temp\trash at startup.


QUEUE & STATUS BAR
------------------
The status bar at the bottom shows what is running ("Idle" or "▶ Running:
...") and how many jobs are queued. Queued jobs run one after another, only
when nothing else is running. "Add to queue" exists on Cut / Edit (Auto-detect
batch, Multi cut), Convert, Audio Gain (Normalize folder), Compare (Quality)
and Check; each entry keeps the files and settings from the moment it was
added. "Queue..." opens the queue: reorder or remove waiting jobs, clear it,
pause/resume, or "Stop all" (stops running jobs and pauses the queue). Each
processing page also has a Stop button by its progress bar.


FOLDERS
-------
Media\ and Data\ are created on first start:
  Installed  in the install folder (C:\Program Files\MediaPrep Toolkit)
  Portable   in mediaprep-data\ next to MediaPrep-Toolkit-Portable.exe
  Zip        next to "MediaPrep Toolkit.exe"
  Source     next to intro_credits_toolkit.py

  Media\                    - your media:
    videos\                   put episodes to process here (done\ = finished)
    output\                   cleaned episodes appear here
    templates\intro\          intro template clips (made on the Templates page)
    templates\credits\        credits template clips
    templates\preintro\       optional pre-intro clips (recaps/logos)
    templates\aftercredits\   optional after-credits clips (teasers/previews)
    convert\input, \output    Convert's default folders (created when used)
    Audio Gain\input\         drop a season here for the Audio Gain batch
    Audio Gain\output\        normalized episodes appear here
  Data\                     - the app's own files:
    settings.json             your settings and theme
    presets\                  per-show Cut / Edit presets (\convert = Convert's)
    logs\                     session logs (auto-saved); the exe also writes
                              Python errors to errors.log here
    temp\                     temporary files; temp\trash\ = everything
                              "deleted" by the app (recoverable)
    backups\                  copies kept just in case (e.g. old settings)

Switching between installer, portable and zip: copy your Media\ and Data\
folders into the new place listed above.

Upgrading from 1.x: the old folders that sat next to the app (videos\,
output\, input\, Audio Gain\, logs\, temp\, presets\, backups\,
toolkit_settings.json) are moved into Media\ and Data\ automatically the first
time the new version starts. Nothing is deleted: folders are merged, a name
clash keeps both files (the older one gets " (old)"), saved folder paths in the
settings and presets are updated (the original settings file is copied to
Data\backups\ first), and the Log page lists every move. Anything that is in
use is left where it is with a warning in the log.


NOTHING IS EVER DELETED
-----------------------
Everything the app "deletes" (trashed templates and presets, old logs, cleaned
folders) is MOVED to Data\temp\trash\<date-time> - you can recover it.

Clean up folders... (sidebar) empties working folders the same way: tick the
template folders, Media\videos, Media\output, Convert in/out, Audio Gain
in/out or Data\temp (each shows its file count and size) and press "Clean up
selected" - it asks first.

"Empty trash older than N days" (default 14) in the same dialog is the ONLY
permanent delete in the app; it shows how much will go and asks first. Both
are blocked while any job is running. Partial output files from a stopped job
are discarded; finished files are kept.


TROUBLESHOOTING
---------------
"ffmpeg / ffprobe not found on PATH" banner (running from source): install
  ffmpeg (Install Requirements.bat does it via winget, or get it from
  ffmpeg.org), then close and reopen the app so the new PATH is picked up.
  The installer, portable and zip versions use their bundled copy.

In Settings: preview stutters or stays black -> Video player engine OpenCV
  (or back to Auto); PC sluggish during long jobs -> keep "Limit CPU use" on
  or pick fewer "Max encoder threads"; text too small / large -> Font size.

NVENC chosen but no NVIDIA GPU: the CPU encoder of the same family is used
  automatically (log line "[WARN] ... not usable here"). Pick a CPU codec to
  silence it.

Intro / credits not found, or found in the wrong place:
  - Templates -> Auto-detect: lower the Sensitivity (Medium -> Low -> Very
    loose), lower the minimum length or enlarge the search window. Check the
    "Detect on audio" language, or try Detect by "Visual" for a dubbed show.
  - Cut / Edit: lower Min confidence if segments are missed, raise it if
    wrong parts get cut. Remove bad or duplicate templates in the Templates
    manager. Use "Review first (detect only)" and fix ⚠ rows in Multi cut
    before cutting.
  - A frame of the intro flashes at the join: raise the Safety margin.
  - Remember a template's LENGTH is the cut length - cut whole segments.

Update failed: the app says why at the next start. You can always download the
  release yourself from the release page; Media\ and Data\ stay untouched.

SmartScreen blocks the Setup or exe: "More info" -> "Run anyway" (the files
  aren't code-signed).

Where are the logs? Data\logs\session_YYYYMMDD.log (Log page -> "Open logs
  folder"). The exe writes Python errors to Data\logs\errors.log; from source,
  use tools\Start Toolkit (debug console).bat to see them.


PROJECT LAYOUT (for editing the code)
-------------------------------------
  intro_credits_toolkit.py  - launcher (the .bat runs it; the exe entry point)
  app\        - the code, split into small modules:
      app.py        main() - builds the window, sidebar and pages
      config.py     folder layout (APP_ROOT, Media\, Data\, portable),
                    settings, codec/audio maps
      migrate.py    moves an old flat folder layout into Media\ + Data\
      i18n.py       translations (tr()); catalogs in app\locales\*.json
      applog.py jobs.py notify.py updater.py presets.py helpdocs.py
                    session log, job queue, notifications, update check +
                    in-app update, per-show presets, the help texts
      selftest.py   GUI-less engine check
                    ("MediaPrep Toolkit.exe" --selftest <dir>)
      engine\       the media engine - ffmpeg/ffprobe/librosa, no GUI code:
          probe.py process.py files.py   ffprobe helpers, running ffmpeg
                        (progress, Stop), .part files / done\ / move-to-trash
          encode.py     encoder/codec choice, NVENC fallback, Plex-ready plan
          detect.py vfp.py  template matching (audio) + picture fingerprints
          recurring.py plexscan.py  template-free detection, Plex-style scan
          refine.py snap.py  frame-accurate boundaries, Snap
          cut.py        the cut engines (batch + manual), keep/drop maths
          recode.py     the Convert engine
          chapters.py subs.py loudness.py formatting.py  chapter markers,
                        subtitle trim/flags, loudness/gain/theme export, times
      ui\           shared GUI pieces: widgets.py (TimeEntry, ScrollFrame,
                    tooltips, help windows), sidebar.py, pageview.py (the
                    scrolling pages), themes.py + themepicker.py, fluent.py +
                    icons.py (the Windows 11 look), dpi.py (per-monitor DPI),
                    player.py + mpvplayer.py + playback.py (preview players),
                    frametime.py (inclusive To), dualplayer.py, cleanup.py,
                    dialogs.py (Queue, Settings), update_dialog.py,
                    tkthread.py, bgmode.py (MEDIAPREP_BACKGROUND=1 tests)
      tabs\         one module per page / sub-tab:
          templates.py (+ templates_detect.py, templates_autodetect.py,
                        templates_manager.py, audition.py)  Templates
          cut_edit.py  (+ cut_auto.py, cut_manual.py, cut_multi.py,
                        cut_scan.py, cut_common.py)         Cut / Edit
          convert.py   (+ convert_settings.py, convert_tracks.py,
                        convert_presets.py)                 Convert
          theme_audio.py, audio_gain.py, audio_gain_batch.py
                        (+ audio_common.py)   the Audio page
          compare.py, check.py, log.py        Compare, Check and Log
          common.py                           helpers shared by the tabs
      assets\       the window icon (icon.ico / icon.png)
      locales\      one JSON catalog per language (+ _template.json)
  tools\        - Start Toolkit (debug console).bat, i18n_extract.py,
                  build_exe.py + MediaPrep.spec (the exe builds: folder build,
                  --portable, --installer), portable_launcher\ (the portable
                  exe's launcher), installer\MediaPrep.nsi (the Setup)

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
The releases bundle unmodified ffmpeg / ffprobe builds (gyan.dev, GNU GPL v3)
and mpv (GNU GPL v2 or later); see _internal\ffmpeg\README-ffmpeg.txt and
_internal\mpv\README-mpv.txt in the program folder.
