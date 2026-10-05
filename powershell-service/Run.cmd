@echo off
setlocal
set /p SERVICE_PYTHON=<"%~dp0python.txt"
if "%SERVICE_PYTHON%"=="" (
  echo ERROR: Missing python.txt. Run Install.cmd first.
  exit /b 1
)
"%SERVICE_PYTHON%" "%~dp0lifecycle.py" %*
exit /b %errorlevel%
