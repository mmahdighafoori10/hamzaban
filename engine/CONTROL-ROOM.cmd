@echo off
rem Opens the local control room (chats and tags, skills, memory path) in its own window. Everything stays on this computer.
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (echo Python was not found. Install Python 3 from python.org.& pause & exit /b 1)
%PYEXE% -X utf8 control_room.py >nul
if errorlevel 1 (echo Could not build the control room page. See logs\control-room.log.& pause & exit /b 1)
rem an app-style window (no tabs, no address bar) in Edge or Chrome; the default browser if neither is installed
powershell -NoProfile -Command "$p=(Resolve-Path '%~dp0..\00-Dashboard\control-room.html').Path; $u=([Uri]$p).AbsoluteUri; foreach($b in 'msedge','chrome'){ try { Start-Process $b -ArgumentList ('--app='+$u) -ErrorAction Stop; exit 0 } catch {} }; Start-Process $p"
endlocal
