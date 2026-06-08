@echo off
REM Starts the Vite/React frontend dev server (sound-hub) on port 5173.

cd /d "%~dp0"

echo Starting Vite dev server on http://localhost:5173 ...
npm run dev

pause
