@echo off
rem Purpose: Start the pet in the background without a console window.
cd /d "%~dp0"
start "" pythonw main.py
