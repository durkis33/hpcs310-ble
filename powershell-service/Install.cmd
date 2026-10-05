@echo off
setlocal
if "%~2"=="" (
  echo Usage: Install.cmd "C:\Path\To\python.exe" "C:\AllowedRoot"
  exit /b 2
)
"%~1" "%~dp0lifecycle.py" install --python "%~1" --allowed-root "%~2"
exit /b %errorlevel%
