@echo off
title AutoClipper
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Installing it now...
  winget install --id Python.Python.3.11 -e --silent --accept-source-agreements --accept-package-agreements
  echo.
  echo Python installed. Close this window and double-click start.bat again.
  pause
  exit /b
)
python scripts\easy_start.py
pause
