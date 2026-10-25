@echo off
rem Purpose: Remove only the Claude Code integration added by this pet.
chcp 65001 >nul
cd /d "%~dp0"
python install_hooks.py --remove
pause
