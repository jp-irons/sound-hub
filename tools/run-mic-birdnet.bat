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
REM   --save-dir    Folder to save WAV chunks that contain detections (omit to discard)
REM
REM To enable geo filter or save-dir, remove the REM from those lines.
REM Make sure every active line except the last ends with ^

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
    --output detections.csv^
    --geo ^
    --save-dir ../detections_audio

pause
