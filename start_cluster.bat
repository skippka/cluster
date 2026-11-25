@echo off
chcp 65001 >nul
title Cluster LAN
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" run.py
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 run.py
    exit /b 0
)
start "" pythonw run.py
exit /b 0
