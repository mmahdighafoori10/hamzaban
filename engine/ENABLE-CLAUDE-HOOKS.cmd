@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (echo Python not found> "%~dp0logs\enable-claude-hooks.log" & exit /b 1)
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo === %DATE% %TIME% === > "%~dp0logs\enable-claude-hooks.log"
%PYEXE% -X utf8 merge_claude_settings.py >> "%~dp0logs\enable-claude-hooks.log" 2>&1
echo --- apply --- >> "%~dp0logs\enable-claude-hooks.log"
%PYEXE% -X utf8 merge_claude_settings.py --apply >> "%~dp0logs\enable-claude-hooks.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\enable-claude-hooks.log"
endlocal
