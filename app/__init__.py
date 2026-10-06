"""MediaPrep Toolkit - package.

Modules:
    config   - constants, codec/audio maps, settings persistence, themes
    media    - ffmpeg/ffprobe helpers, segment maths, detection + cut engines
    player   - VideoPlayer (frame-accurate OpenCV preview widget with sound)
    audio    - AudioPlayer (ffmpeg + sounddevice playback, volume/mute)
    widgets  - TimeEntry, ScrollFrame + the Info/Recommended help tabs
    tabs     - TemplateTab and RemoverTab (the two working tabs)
    app      - main() that wires the window together

Run the toolkit from the repo root via  intro_credits_toolkit.py.
"""
