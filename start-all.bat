@echo off
REM Convenience launcher: opens the backend and frontend each in their
REM own window, so you don't have to juggle two terminals manually.

cd /d "%~dp0"

start "sound-hub backend"  cmd /k "start-backend.bat"
start "sound-hub frontend" cmd /k "start-frontend.bat"
