@echo off
REM Starts the FastAPI backend (sound-hub) with auto-reload on port 8000.
REM Adjust the venv path / module path below if yours differ
REM (e.g. if main.py isn't a "server" package, or your entry module
REM has a different name).

cd /d "%~dp0"

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    echo [warn] venv\Scripts\activate.bat not found - using system Python
)

echo Starting FastAPI backend on http://localhost:8000 ...
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8000

pause
