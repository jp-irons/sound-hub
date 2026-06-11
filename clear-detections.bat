@echo off
REM Clear BirdNET detection records from sound_hub.db.
REM
REM Usage examples:
REM   clear-detections.bat --all
REM   clear-detections.bat --older-than 7
REM   clear-detections.bat --source soundscape
REM   clear-detections.bat --all --dry-run
REM
REM Pass arguments on the command line, e.g.:
REM   clear-detections.bat --older-than 30

cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: clear-detections.bat [options]
    echo.
    echo Options:
    echo   --all                  Delete all detections
    echo   --older-than DAYS      Delete detections older than N days
    echo   --source SUBSTRING     Delete detections whose source filename contains SUBSTRING
    echo   --dry-run              Preview what would be deleted without making changes
    echo.
    echo Examples:
    echo   clear-detections.bat --all
    echo   clear-detections.bat --all --dry-run
    echo   clear-detections.bat --older-than 7
    echo   clear-detections.bat --source soundscape
    echo.
    pause
    exit /b 0
)

python tools\clear_detections.py %*

pause
