@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 goto nopython
python check.py
echo.
pause
exit /b
:nopython
echo [ 问题 ] 没找到 Python。
echo         去 python.org 装一个，安装时勾上 Add Python to PATH。
echo.
pause
