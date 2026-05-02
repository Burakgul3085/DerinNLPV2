@echo off
cd /d "%~dp0"
echo [DerinNLP] Klasor: %CD%
npm run dev -- --host 127.0.0.1 --port 5173
pause
