@echo off
title Stream Clipper
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting up. This takes a minute.
  python -m venv .venv || goto :fail
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
)
.venv\Scripts\python.exe -m clipper %*
if errorlevel 1 goto :fail
exit /b 0
:fail
echo.
echo Stream Clipper stopped with an error. The message above says why.
pause
