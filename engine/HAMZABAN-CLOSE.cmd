@echo off
rem Copy a hamzaban-close block from any chat, then double-click this file.
rem The block is staged in 30-Pending for your approval; nothing becomes a fact by itself.
rem The clipboard is read here (not inside the hamzaban package, which must stay free of subprocesses).
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (echo Python was not found. Install Python 3 from python.org.& pause & exit /b 1)
set "TMPF=%TEMP%\hamzaban-clip-%RANDOM%.txt"
powershell -NoProfile -NonInteractive -Command "[IO.File]::WriteAllText($env:TMPF, [string](Get-Clipboard -Raw), (New-Object Text.UTF8Encoding $false))"
if not exist "%TMPF%" (echo Could not read the clipboard.& pause & exit /b 1)
%PYEXE% -X utf8 hamzaban\cli.py ingest "%TMPF%"
set "RC=%ERRORLEVEL%"
del "%TMPF%" >nul 2>nul
echo.
if "%RC%"=="0" (echo Staged. Open the 30-Pending folder in Obsidian to approve or reject.) else (echo Nothing was staged. Copy the whole hamzaban-close block first, then run this again.)
echo.
pause
endlocal
