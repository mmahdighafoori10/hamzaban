@echo off
rem Opens the local control room (chats and tags, skills, memory path) in its own window. Everything stays on this computer.
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (echo Python was not found. Install Python 3 from python.org.& pause & exit /b 1)
rem the same window as the Hamzaban shortcut: app_window.py rebuilds the page when it is old and opens it as an app window (no tabs, no
rem address bar, a profile of its own) in Edge or Chrome, or in the default browser if neither is installed
%PYEXE% -X utf8 app_window.py
if errorlevel 1 (echo Could not open the control room. Run "python control_room.py" here to see why.& pause & exit /b 1)
endlocal
