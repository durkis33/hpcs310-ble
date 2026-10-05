[CmdletBinding()]
param([Parameter(Mandatory)][string]$JobId)
$ErrorActionPreference = 'Stop'
$config = Get-Content -Raw (Join-Path $PSScriptRoot 'config.json') | ConvertFrom-Json
$body = @{ job_id = $JobId } | ConvertTo-Json
$api = @{ Authorization = 'Bearer ' + (Get-Content -Raw $config.token_file).Trim() }
$job = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/get_job" -Method Post -Headers $api -ContentType 'application/json' -Body $body
$job.result | ConvertTo-Json -Depth 10 | Write-Host
if ((Read-Host 'Review the exact invocation above. Type APPROVE to run it') -cne 'APPROVE') { throw 'Not approved' }
$admin = @{ Authorization = 'Bearer ' + (Get-Content -Raw $config.approval_token_file).Trim() }
Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/approve" -Method Post -Headers $admin -ContentType 'application/json' -Body $body
