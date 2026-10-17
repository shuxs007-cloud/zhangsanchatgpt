@echo off
setlocal
if not exist "%LOCALAPPDATA%\GrabQueryTool" mkdir "%LOCALAPPDATA%\GrabQueryTool"
start "" "%LOCALAPPDATA%\GrabQueryTool"
endlocal
