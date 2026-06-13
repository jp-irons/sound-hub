@echo off
REM Run BirdNET watch-list detection from the ReSpeaker USB mic.
REM Edit the options below to suit your setup.
REM
REM Options:
REM   --device          Device name fragment or numeric index (run with --list-devices to find it)
REM   --channel         Mic channel: 0-3 for a single channel, or "all" to average all four
REM   --threshold       Confidence threshold 0.0-1.0 (default 0.5)
REM   --geo             Add this flag to enable Brisbane species/season filter
REM   --root            Root output folder; CSVs go to <root>\detections\, WAVs to <root>\samples\
REM   --species-config  Path to species_config.yaml (default: same folder as this script)
REM
REM Output layout:
REM   <root>\detections\summary_YYYY-MM-DD.csv     <- all-species daily counts
REM   <root>\detections\incidents_YYYY-MM-DD.csv   <- watched-species incident log
REM   <root>\samples\YYYY-MM-DD\<Species>\mic_....wav
REM
REM To change options, edit the lines below.
REM Make sure every active line except the last ends with ^

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERROR: tools venv not found. Run tools\install-tools.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

python mic_to_birdnet_watch.py ^
    --device "ReSpeaker" ^
    --channel 0 ^
    --threshold 0.5 ^
    --geo ^
    --root "..\detections"

pause
