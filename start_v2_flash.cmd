@echo off
setlocal
cd /d "%~dp0"
REM Uses the existing .env; the profile only overrides dataset, Decision model and retrieval mode.
uv run --no-sync python scripts/start_v2_flash.py %*
set "START_EXIT=%ERRORLEVEL%"
if not "%START_EXIT%"=="0" (
    echo TicketMind launch failed. Check the messages above and your local .env / services.
    pause
)
exit /b %START_EXIT%
