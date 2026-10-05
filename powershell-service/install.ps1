$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $Root "config.json"
if (-not (Test-Path $Config)) {
  $Template = Get-Content (Join-Path $Root "config.example.json") -Raw | ConvertFrom-Json
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $Template.auth_token = [Convert]::ToBase64String($bytes)
  $Template | ConvertTo-Json -Depth 8 | Set-Content -Path $Config -Encoding UTF8
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
Write-Host "Installed. Config: $Config"
Write-Host "Start with .\start.ps1"
