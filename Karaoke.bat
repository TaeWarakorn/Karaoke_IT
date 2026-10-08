@echo off
title KaraokeIT
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"
if not exist ".venv\installed.ok" python -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('flask','demucs','yt_dlp')) else 1)" >nul 2>&1 && set "PY=python" && set "PYW=pythonw"
if not "%PY%"=="python" if not exist ".venv\installed.ok" start "Karaoke setup" /wait powershell -NoProfile -ExecutionPolicy Bypass -File "setup.ps1"
if not "%PY%"=="python" if not exist ".venv\installed.ok" (
  echo.
  echo Setup did not finish. Please open KaraokeIT again.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "shortcut.ps1"
if "%PYW%"=="pythonw" (where pythonw >nul 2>&1 || set "PYW=")
if not "%PYW%"=="pythonw" if not exist "%PYW%" set "PYW="
if defined PYW (
  start "" "%PYW%" karaoke_app.py
  exit /b 0
)
echo KaraokeIT is running. Keep this window open while you sing.
"%PY%" karaoke_app.py
if errorlevel 1 pause
