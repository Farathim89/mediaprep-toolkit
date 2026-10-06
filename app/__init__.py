"""MediaPrep Toolkit - package.

    app.py      main(): builds the window and wires the tabs together
    config      constants, codec/audio maps, settings persistence, themes
    applog, jobs, notify, updater, presets, helpdocs   app-wide services
    engine/     UI-free ffmpeg/ffprobe/librosa engine (probe, encode, detect,
                cut, chapters, subs, loudness, snap ...)
    ui/         shared widgets, video/audio players, dialogs
    tabs/       one module per tab (templates, cut_*, theme_audio, audio_gain,
                compare, check, log)

Run the toolkit from the repo root via  intro_credits_toolkit.py.
"""
