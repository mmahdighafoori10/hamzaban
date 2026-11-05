@echo off
rem Removes the background task, the Claude Code hooks and the memory-server entries that INSTALL.cmd added. Your Vault and notes are not touched.
setlocal
cd /d "%~dp0"
set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo  Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
%PYEXE% -X utf8 install.py --uninstall %*
echo.
pause
endlocal
