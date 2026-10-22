@echo off
rem 桌宠通知器（Windows）。Claude Code 的 hook 会调它，把事件原样转给桌宠。
rem   用法: claude-pet-notify.cmd <事件名> [端口] [wake]
rem 第三个参数是 wake 的时候，桌宠没开就顺手把她叫起来（data\no-autowake 存在就不叫）。
rem 不管成功失败都返回 0，桌宠没开的时候 Claude Code 也不会报错。
setlocal
set "EVT=%~1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=47828"
curl -s -m 2 --noproxy 127.0.0.1 -X POST -H "Content-Type: application/json" --data-binary @- -o NUL "http://127.0.0.1:%PORT%/e/%EVT%?src=claude-pet"
if errorlevel 1 if /i "%~3"=="wake" if not exist "%~dp0data\no-autowake" if exist "%~dp0wake.vbs" start "" wscript.exe "%~dp0wake.vbs"
endlocal
exit /b 0
