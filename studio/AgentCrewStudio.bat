@echo off
rem AgentCrew Studio one-click launcher (double-click to start, console closes itself)
cd /d "%~dp0"
start "" "%~dp0node_modules\electron\dist\electron.exe" .
