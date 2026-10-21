@echo off
chcp 65001 >nul
title Cluster - Розподілені обчислення
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" goto ready
echo [Cluster] Перший запуск: створення ізольованого середовища Python (.venv)...
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 goto failed
:ready
".venv\Scripts\python.exe" -c "import cryptography, psutil" >nul 2>nul
if not errorlevel 1 goto run
echo [Cluster] Встановлення необхідних бібліотек з requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
:run
".venv\Scripts\python.exe" -X utf8 run.py
pause
exit /b
:failed
echo [Cluster] Помилка запуску. Переконайтеся, що встановлено Python 3.10+ та є доступ до інтернету.
pause
exit /b 1
