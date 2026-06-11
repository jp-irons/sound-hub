@echo off
REM Install dependencies for tools/ scripts into a dedicated tools\venv.
REM Kept separate from the server venv to avoid polluting it with heavy ML deps.
REM Run this once, or again after pulling updates.

cd /d "%~dp0"

if not exist "venv" (
    echo Creating tools venv...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Done. Run tools\run-mic-birdnet.bat to start detection.
