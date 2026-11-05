@echo off
setlocal
title Memory Suite AutoSync - install
cd /d "%~dp0"
echo.
echo  This installs a small background task that keeps your skills and project
echo  instructions in step between Codex, Claude and Chat On Steroids.
echo  It runs once a minute as you (no admin), and it NEVER deletes anything.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_autosync.ps1" -Action register
if errorlevel 1 (
  echo.
  echo  Something went wrong. Please send a screenshot of this window.
) else (
  echo.
  echo  Done. To check later: double-click this file again, or run
  echo  powershell -ExecutionPolicy Bypass -File manage_autosync.ps1 -Action status
)
echo.
pause
endlocal
