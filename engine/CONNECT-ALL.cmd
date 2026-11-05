@echo off
setlocal
title Memory Suite - connect all agents
cd /d "%~dp0"
echo.
echo  This connects Claude Code, Codex and Claude Desktop to your memory vault
echo  and checks Chat On Steroids.
echo  Every changed file is backed up first. Nothing is deleted.
echo  Please close Claude Code terminal windows before continuing.
echo.
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (
  echo  Python was not found. Install Python 3 from python.org and run this again.
  pause
  exit /b 1
)
%PYEXE% -X utf8 connect_all.py
echo.
echo  Done. Now fully quit Claude Desktop from the tray icon and open it again,
echo  then reopen Codex and Claude Code. The result is in logs\connect-all.log
echo.
pause
endlocal
