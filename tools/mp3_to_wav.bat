@echo off
REM Convert an MP3 file to a 48kHz mono WAV suitable for BirdNET analysis.
REM Usage: mp3_to_wav.bat input.mp3 output.wav

if "%~1"=="" (
    echo Usage: mp3_to_wav.bat input.mp3 output.wav
    pause
    exit /b 1
)

if "%~2"=="" (
    echo Usage: mp3_to_wav.bat input.mp3 output.wav
    pause
    exit /b 1
)

ffmpeg -i "%~1" -ar 48000 -ac 1 "%~2"
