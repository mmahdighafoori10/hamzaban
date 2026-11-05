@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (echo Python was not found. Install Python 3 from python.org.& exit /b 1)
%PYEXE% -X utf8 hamzaban\cli.py %*
endlocal
