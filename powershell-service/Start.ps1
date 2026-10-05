[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'config.json'
$config = Get-Content -Raw $configPath | ConvertFrom-Json
$headers = @{ Authorization = 'Bearer ' + (Get-Content -Raw $config.token_file).Trim() }
$uri = "http://127.0.0.1:$($config.port)/status"
try {
    $status = Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 2
    if ($status.ok) { Write-Output 'Service is already running'; return }
} catch { }
$python = (Get-Content -Raw (Join-Path $PSScriptRoot 'python.txt')).Trim()
$process = Start-Process -FilePath $python -ArgumentList ('"{0}" --config "{1}"' -f (Join-Path $PSScriptRoot 'service.py'), $configPath) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $PSScriptRoot 'state\service.stderr.log')
for ($i = 0; $i -lt 40; $i++) {
    if ($process.HasExited) { throw 'Service exited; inspect state\service.stderr.log' }
    try {
        $status = Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 1
        if ($status.ok) { Write-Output "Service ready on port $($config.port)"; return }
    } catch { }
    Start-Sleep -Milliseconds 250
}
throw 'Service did not become ready; inspect state\service.stderr.log'
