@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "runtime\node.exe" (
  echo Missing runtime\node.exe. Extract the ENTIRE ZIP before starting.
  pause
  exit /b 1
)
"runtime\node.exe" "launch.mjs"
if errorlevel 1 (
  echo Startup failed. Check %%LOCALAPPDATA%%\GrabQueryTool\server.log
  pause
  exit /b 1
)
endlocal
