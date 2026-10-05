@echo off
if "%~1"=="" (
  echo Usage: Approve.cmd JOB-ID
  exit /b 2
)
call "%~dp0Run.cmd" approve "%~1"
exit /b %errorlevel%
