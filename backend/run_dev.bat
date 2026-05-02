@echo off
cd /d "%~dp0"
echo [DerinNLP] Klasor: %CD%
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
pause
