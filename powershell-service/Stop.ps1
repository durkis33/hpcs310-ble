[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$config = Get-Content -Raw (Join-Path $PSScriptRoot 'config.json') | ConvertFrom-Json
$headers = @{ Authorization = 'Bearer ' + (Get-Content -Raw $config.approval_token_file).Trim() }
Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/shutdown" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 10
