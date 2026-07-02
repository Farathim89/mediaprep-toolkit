@echo off
echo Installing Python packages needed by the Batch Remover tab...
python -m pip install --upgrade librosa numpy scipy
echo.
echo Note: ffmpeg must also be installed and on PATH (https://ffmpeg.org).
pause
