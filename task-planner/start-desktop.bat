@echo off
REM ============================================================
REM  Task Planner - Desktop launcher
REM  Double-click this file to open the app in a native window.
REM  (ASCII-only on purpose: non-ASCII content in .bat files
REM   gets garbled by the Windows console codepage.)
REM ============================================================

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [ERROR] "uv" was not found in PATH.
    echo  Install it first: https://docs.astral.sh/uv/
    echo.
    pause
    exit /b 1
)

echo Starting Task Planner...
echo (first run may take a while: it syncs Python dependencies)
echo.

uv run task-planner-desktop
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo  [ERROR] The app exited with code %EXITCODE%
    echo.
    pause
)

exit /b %EXITCODE%
