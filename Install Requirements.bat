@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  IntroCreditsToolkit - Install Requirements
echo ============================================
echo.

set PYCMD=python

REM --- Check for Python (python or py launcher) ---
where python >nul 2>&1
if errorlevel 1 (
    where py >nul 2>&1
    if errorlevel 1 (
        echo Python not found. Installing Python via winget...
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        if errorlevel 1 (
            echo.
            echo ERROR: Failed to install Python automatically.
            echo Please install it manually from https://python.org and re-run this script.
            pause
            exit /b 1
        )
        echo.
        echo Python was just installed. Please close this window, open a NEW
        echo terminal, and re-run this script so the PATH changes take effect.
        pause
        exit /b 0
    ) else (
        set PYCMD=py
    )
)

echo Python found.
echo.

REM --- Check for ffmpeg / ffprobe ---
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg not found. Installing ffmpeg via winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo WARNING: Failed to install ffmpeg automatically.
        echo Please install it manually from https://ffmpeg.org and add it to PATH.
    ) else (
        echo.
        echo ffmpeg installed. Close and reopen your terminal (or the Toolkit)
        echo for the updated PATH to take effect.
    )
) else (
    echo ffmpeg found.
)

echo.
echo Installing Python packages needed by the toolkit...
echo   - Batch Remover: librosa numpy scipy
echo   - Template Cutter preview player: opencv-python Pillow
echo   - Player sound (volume/mute): sounddevice
echo   - Drag-and-drop: tkinterdnd2
echo.
%PYCMD% -m pip install --upgrade pip
%PYCMD% -m pip install --upgrade librosa numpy scipy opencv-python Pillow sounddevice tkinterdnd2

echo.
echo Done.
pause
