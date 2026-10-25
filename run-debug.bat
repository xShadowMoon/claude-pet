@echo off
rem Purpose: Start the pet with a visible console for troubleshooting.
chcp 65001 >nul
cd /d "%~dp0"
python main.py
pause
