$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "service.pid"
if (Test-Path $PidFile) {
  $old = Get-Content $PidFile -ErrorAction SilentlyContinue
  if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
    Write-Host "Service already running. PID $old"
    exit 0
  }
}
$p = Start-Process -FilePath "python" -ArgumentList @("server.py") -WorkingDirectory $Root -PassThru -WindowStyle Hidden
$p.Id | Set-Content $PidFile
Start-Sleep -Milliseconds 500
Write-Host "Service started. PID $($p.Id)"
Write-Host "Status endpoint: http://127.0.0.1:8765/status"
