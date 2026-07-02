@echo off
cd /d "%~dp0"
rem Runs WITH a console so you can see Python errors if the app
rem fails to start or crashes. Use this when something goes wrong.
python intro_credits_toolkit.py
pause
