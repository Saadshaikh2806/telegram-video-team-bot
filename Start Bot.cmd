@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Install Bot.cmd first.
  pause
  exit /b 1
)
if not exist ".env" (
  ".venv\Scripts\python.exe" setup_bot.py
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m video_bot
pause
