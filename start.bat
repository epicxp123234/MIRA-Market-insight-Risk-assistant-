@echo off
cd /d "%~dp0"
start "Sandbox Agent Lab server" /min python server.py
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8765
