@echo off
rem ===========================================================================
rem  AgentCrew Studio launcher
rem  Double-click, or call from a shortcut / desktop .bat.
rem  Responsibilities:
rem    1. clear ELECTRON_RUN_AS_NODE (inherited env would run Electron as plain
rem       Node and crash with "Cannot read properties of undefined (app)")
rem    2. locate the repo and the Electron binary
rem    3. bootstrap node_modules on first use, then start the GUI
rem ===========================================================================
setlocal

rem Repo root = parent folder of this scripts\ directory
set "REPO=%~dp0.."
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "STUDIO=%REPO%\studio"
set "ELECTRON=%STUDIO%\node_modules\electron\dist\electron.exe"

rem --- 1. sanitize environment -----------------------------------------------
set "ELECTRON_RUN_AS_NODE="

rem --- 2. sanity checks -------------------------------------------------------
if not exist "%STUDIO%\package.json" (
    echo [AgentCrew] Studio not found at: %STUDIO%
    echo [AgentCrew] Move this script under ^<repo^>\scripts\ or edit REPO above.
    pause
    exit /b 1
)

rem --- 3. first-run bootstrap ------------------------------------------------
if not exist "%ELECTRON%" (
    echo [AgentCrew] Electron is not installed yet, running npm install ...
    pushd "%STUDIO%"
    call npm install
    popd
)

if not exist "%ELECTRON%" (
    echo [AgentCrew] Electron still missing.
    echo [AgentCrew] Fix manually:  cd /d "%STUDIO%"  ^&^&  npm install
    pause
    exit /b 1
)

rem --- 4. launch --------------------------------------------------------------
rem start returns immediately, so this console window closes itself.
start "" "%ELECTRON%" "%STUDIO%"
exit /b 0
