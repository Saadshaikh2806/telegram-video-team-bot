@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failure
echo Installation complete. Double-click Start Bot.cmd next.
pause
exit /b 0
:failure
echo Installation failed. Check Python and your internet connection.
pause
exit /b 1
