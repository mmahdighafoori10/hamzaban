@echo off
rem Hamzaban installer for Windows: finds a real Python 3.10+ and runs install.py. Extra arguments are passed on (for example --dry-run).
setlocal
cd /d "%~dp0"
set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo  Hamzaban needs Python 3.10 or newer, and it was not found.
  echo  Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo  then double-click INSTALL.cmd again.
  echo  (The Microsoft Store "python" shortcut is not a real Python: install the one from python.org.)
  echo.
  pause
  exit /b 1
)
%PYEXE% -X utf8 install.py %*
echo.
pause
endlocal
