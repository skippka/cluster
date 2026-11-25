@echo off
chcp 65001 >nul
title cluster - Build EXE & Installer
cd /d "%~dp0"

echo [cluster] Installing dependencies and PyInstaller...
python -m pip install -r requirements.txt pyinstaller pillow pywin32
if errorlevel 1 goto failed

echo [cluster] Building standalone dist\cluster.exe...
python -m PyInstaller --noconfirm --onefile --windowed --name cluster --icon assets\logo.ico --add-data "examples;examples" --add-data "assets;assets" run.py
if errorlevel 1 goto failed

echo [cluster] Building minimalist installer dist\cluster-setup.exe...
python -m PyInstaller --noconfirm --onefile --windowed --name cluster-setup --icon assets\logo.ico --add-data "dist\cluster.exe;payload" --add-data "examples;payload\examples" --add-data "assets;payload\assets" --add-data "README.md;payload" installer.py
if errorlevel 1 goto failed

echo.
echo [cluster] Done!
echo   Standalone app: dist\cluster.exe
echo   GitHub Setup:   dist\cluster-setup.exe
pause
exit /b 0

:failed
echo [cluster] Build failed.
pause
exit /b 1
