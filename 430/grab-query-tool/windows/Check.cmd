@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"runtime\node.exe" "selfcheck.mjs"
pause
endlocal
