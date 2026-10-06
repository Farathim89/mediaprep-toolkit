#!/usr/bin/env python3
"""MediaPrep Toolkit - launcher.

The code now lives in the app/ package:
    app/config.py   - constants, codec/audio maps, settings, themes
    app/media.py    - ffmpeg/ffprobe helpers, segment maths, detect + cut engines
    app/player.py   - VideoPlayer (frame-accurate OpenCV preview)
    app/widgets.py  - TimeEntry + the Info/Recommended help tabs
    app/tabs.py     - TemplateTab and RemoverTab
    app/app.py      - main() that wires the window together

This thin launcher just starts the GUI, so the .bat files and the way you run
the tool stay exactly the same. The old single-file version is kept alongside
as intro_credits_toolkit_legacy.py for reference.
"""
import os
import sys

# make sure the app/ package next to this file is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.app import main

if __name__ == "__main__":
    main()
