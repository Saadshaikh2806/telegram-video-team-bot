@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run Install Bot.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" setup_bot.py
