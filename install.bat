@echo off
rem Purpose: Install the GUI and system-monitoring Python dependencies.
chcp 65001 >nul
cd /d "%~dp0"
echo 正在安装 PySide6，第一次会有点慢...
python -m pip install -r requirements.txt
echo.
echo 装好了。接下来双击 install-hooks.bat 接上 Claude Code。
pause
