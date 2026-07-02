@echo off
cd /d "%~dp0"
rem pythonw = windowless Python: no black console window in the background.
rem "start" + exit makes this cmd window close immediately too.
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw intro_credits_toolkit.py
    exit /b 0
)
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)
rem fallback: normal python (console stays visible)
python intro_credits_toolkit.py
if errorlevel 1 pause
