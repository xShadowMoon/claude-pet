@echo off
rem 桌宠审批桥（Windows）。Claude Code 要你点"允许"的时候会调它：
rem 把请求交给桌宠，等你在桌宠上点了按钮，再把结果原样交回给 Claude Code。
rem 桌宠没开、等太久、或者你选了"去窗口里处理"，它什么都不输出，Claude Code 就照常问你。
rem   用法: claude-pet-ask.cmd [端口] [最多等几秒]
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=47828"
set "WAIT=%~2"
if "%WAIT%"=="" set "WAIT=600"
curl -s -m %WAIT% --noproxy 127.0.0.1 -X POST -H "Content-Type: application/json" --data-binary @- "http://127.0.0.1:%PORT%/permission"
endlocal
exit /b 0
