@echo off
REM Run live BirdNET detection from the ReSpeaker USB mic.
REM Edit the options below to suit your setup.
REM
REM Options:
REM   --device      Device name fragment or numeric index (run with --list-devices to find it)
REM   --channel     Mic channel: 0-3 for a single channel, or "all" to average all four
REM   --threshold   Confidence threshold 0.0-1.0 (default 0.5)
REM   --geo         Add this flag to enable Brisbane species/season filter
REM   --output      CSV output filename (default: detections.csv)
REM
REM Uncomment --geo below to enable the Brisbane geo filter.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERROR: tools venv not found. Run tools\install-tools.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

python mic_to_birdnet.py ^
    --device "ReSpeaker" ^
    --channel 0 ^
    --threshold 0.5 ^
    --output detections.csv
    --geo

pause
