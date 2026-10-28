@echo off
cd /d "%~dp0"
start "Sandbox Agent Lab server" /min python server.py
timeout /t 2 /nobreak >nul
start "" https://mira-market-insight-risk-assistant.onrender.com
