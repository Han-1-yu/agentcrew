@echo off
rem AgentCrew desktop launcher: starts the local backend, then opens Studio.
rem Double-click this file, or run it with arguments (e.g. --port 8766).
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0scripts\desktop.py" %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo AgentCrew failed to start, exit code %CODE%.
  echo See logs\desktop-serve.log for backend output.
  pause
)

exit /b %CODE%
