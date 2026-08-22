@echo off
REM ============================================================
REM  YouTube Automation Tool - double-click launcher
REM
REM  Finds Python, runs launch.py (preflight checks + dashboard),
REM  and keeps this window open if anything goes wrong so you can
REM  actually read the error.
REM ============================================================

title YouTube Automation Tool

REM Work from this script's own folder, whatever directory it was started from.
cd /d "%~dp0"

REM Find a Python: the "py" launcher first (handles multiple installs),
REM then plain "python" from PATH.
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>&1 && set "PYEXE=python"
)

if not defined PYEXE (
    echo.
    echo   [FAIL] Python was not found on this computer.
    echo.
    echo   Install Python 3.9 or newer from:
    echo       https://www.python.org/downloads/
    echo.
    echo   IMPORTANT: tick "Add Python to PATH" in the installer,
    echo   then run this file again.
    echo.
    pause
    exit /b 1
)

if not exist "launch.py" (
    echo.
    echo   [FAIL] launch.py is missing from:
    echo       %~dp0
    echo.
    echo   Keep run.bat in the project folder, next to the tools folder.
    echo.
    pause
    exit /b 1
)

%PYEXE% launch.py %*
set "RC=%ERRORLEVEL%"

REM Only hold the window open on failure. A clean Ctrl+C shutdown just exits.
if not "%RC%"=="0" (
    echo.
    echo   The launcher exited with code %RC%.
    echo.
    pause
)

exit /b %RC%
