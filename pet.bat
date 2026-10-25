@echo off
rem Purpose: Forward command-line arguments to the pet remote controller.
chcp 65001 >nul
cd /d "%~dp0"
python pet.py %*
