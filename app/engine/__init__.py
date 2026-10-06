"""UI-free media engine (ffmpeg / ffprobe / librosa). Nothing in this
package imports tkinter.

    formatting  time / size formatting and parsing
    process     subprocess runners, ffmpeg with progress, power throttling
    probe       ffprobe helpers + quick / full integrity checks
    files       .part outputs, done/ moves, temp cleanup, move to trash
    encode      encoder / codec args, Plex-ready plan, ffmpeg cut / inject
    detect      template fingerprints + matching (detect_segments)
    recurring   template-free recurring-segment detection
    cut         run_batch / run_manual, keep/drop maths, verify_output
    chapters    chapter timeline + writing
    subs        subtitle trim, disposition carry-over
    loudness    measure / gain / loudnorm / peak normalize / theme export
    snap        snap a cut point to silence / black edges
"""
