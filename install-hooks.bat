@echo off
rem Purpose: Install the pet hooks and status line for Claude Code.
chcp 65001 >nul
cd /d "%~dp0"
python install_hooks.py
pause
