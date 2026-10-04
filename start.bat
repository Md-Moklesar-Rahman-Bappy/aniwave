@echo off
REM ---------------------------------------------------------------------------
REM AniWave Telegram Publishing Bot - Windows launcher
REM
REM This script contains NO secrets. The bot token is read from your local .env
REM file at runtime.
REM
REM IMPORTANT: only ONE polling instance may run per bot token. Telegram rejects
REM a second getUpdates from the same token, so close any other running window
REM before starting this one.
REM ---------------------------------------------------------------------------

setlocal

REM Always start in this script's own directory, whatever it was launched from.
cd /d "%~dp0"

echo ============================================================
echo  AniWave Telegram Publishing Bot
echo  Working directory: %CD%
echo ============================================================
echo.

REM Prefer the project virtual environment when it exists.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    echo Using virtual environment: .venv
) else (
    set "PYTHON=python"
    echo No .venv found - using the system python.
    echo Tip: create one with:  python -m venv .venv
)

REM Fail early with a clear message if .env is missing.
if not exist ".env" (
    echo.
    echo ERROR: .env was not found in this folder.
    echo Copy the template and fill it in:
    echo     copy .env.example .env
    echo.
    pause
    exit /b 1
)

echo.
echo Starting bot... (press Ctrl+C to stop)
echo.

"%PYTHON%" bot.py
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo The bot exited with code %EXITCODE%.
    echo Review the messages above. Common causes:
    echo   - Invalid configuration in .env
    echo   - Invalid or revoked bot token
    echo   - Another instance is already polling with the same token
) else (
    echo The bot stopped normally.
)

REM Keep the window open so errors stay readable.
echo.
pause
endlocal