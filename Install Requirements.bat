@echo off
setlocal enabledelayedexpansion

REM Usage: "Install Requirements.bat" [--dry-run]
REM   --dry-run  only check what is there - nothing is installed (tests)
REM The toolkit is developed on Python 3.10 and runs on 3.10 - 3.12 (all its
REM packages support 3.12); a missing Python is installed as 3.12 via winget.

set "DRY="
if /i "%~1"=="--dry-run" set "DRY=1"

echo ============================================
echo  MediaPrep Toolkit - Install Requirements
echo ============================================
echo.

REM --- Python 3.10 or newer ---
REM "where python" also finds the Microsoft Store stub (WindowsApps\python.exe),
REM which only opens the Store - so actually run each candidate.
set "PYCMD="
call :try_python python
if not defined PYCMD call :try_python py -3
if defined PYCMD goto :have_python

if defined DRY (
    echo [dry-run] Python 3.10+ not found - would install Python 3.12 via winget.
    goto :check_ffmpeg
)
echo Python 3.10 or newer not found. Installing Python 3.12 via winget...
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install Python automatically.
    echo Please install Python 3.10 or newer from https://www.python.org/downloads/
    echo - tick "Add python.exe to PATH" - and run this script again.
    pause
    exit /b 1
)
echo.
echo Python was just installed. Please close this window, open a NEW
echo terminal and run this script again so the PATH changes take effect.
pause
exit /b 0

:have_python
for /f "delims=" %%V in ('%PYCMD% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%V"
echo Python %PYVER% found ^(%PYCMD%^).
echo.

:check_ffmpeg
REM --- ffmpeg / ffprobe ---
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo ffmpeg found.
    goto :packages
)
if defined DRY (
    echo [dry-run] ffmpeg not found - would install Gyan.FFmpeg via winget.
    goto :packages
)
echo ffmpeg not found. Installing ffmpeg via winget...
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo.
    echo WARNING: Failed to install ffmpeg automatically.
    echo Please install it manually from https://ffmpeg.org and add it to PATH.
) else (
    echo.
    echo ffmpeg installed. Close and reopen your terminal ^(or the Toolkit^)
    echo for the updated PATH to take effect.
)

:packages
echo.
echo Installing Python packages needed by the toolkit...
echo   - Batch Remover: librosa numpy scipy
echo   - Template Cutter preview player: opencv-python Pillow
echo   - Player sound ^(volume/mute^): sounddevice
echo   - Drag-and-drop: tkinterdnd2
echo.
if not defined PYCMD (
    echo [dry-run] no Python - the packages would be installed after it.
    goto :done
)
if defined DRY (
    echo [dry-run] would run: %PYCMD% -m pip install --upgrade pip
    echo [dry-run] would run: %PYCMD% -m pip install --upgrade librosa numpy scipy opencv-python Pillow sounddevice tkinterdnd2
    goto :done
)
%PYCMD% -m pip install --upgrade pip
if errorlevel 1 echo WARNING: could not upgrade pip - continuing with the installed one.
%PYCMD% -m pip install --upgrade librosa numpy scipy opencv-python Pillow sounddevice tkinterdnd2
if errorlevel 1 (
    echo.
    echo ERROR: Installing the Python packages failed - see the messages above.
    echo Check your internet connection and run this script again.
    pause
    exit /b 1
)

:done
echo.
echo Done.
if not defined DRY pause
exit /b 0

REM ---- sets PYCMD when "%*" runs a real Python 3.10+ ----
:try_python
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" <nul >nul 2>&1
if not errorlevel 1 set "PYCMD=%*"
exit /b 0
