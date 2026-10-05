$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "service.pid"
if (-not (Test-Path $PidFile)) {
  Write-Host "No service.pid found."
  exit 0
}
$pidValue = Get-Content $PidFile
$p = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
if ($p) {
  Stop-Process -Id $pidValue -Force
  Write-Host "Stopped service PID $pidValue"
}
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
