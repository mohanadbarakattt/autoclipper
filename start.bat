@echo off
title AutoClipper
cd /d "%~dp0"
rem "python" may be the Microsoft Store stub, so check that it really runs (3.10+)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is not installed. Installing it now...
  winget install --id Python.Python.3.11 -e --silent --accept-source-agreements --accept-package-agreements
  echo.
  echo Python installed. Close this window and double-click start.bat again.
  pause
  exit /b
)
python scripts\easy_start.py
pause
