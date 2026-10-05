[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string[]]$AllowedRoots,
    [string]$Destination = "$env:LOCALAPPDATA\PowerShellService",
    [switch]$AtLogon
)
$ErrorActionPreference = 'Stop'
$Python = (Get-Command $Python -ErrorAction Stop).Source
& $Python -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python validation failed' }
$roots = @($AllowedRoots | ForEach-Object { (Resolve-Path -LiteralPath $_).Path })
if (Test-Path -LiteralPath $Destination) { throw 'Destination already exists; preserve its state and upgrade source files manually.' }
New-Item -ItemType Directory -Path $Destination | Out-Null
$Destination = (Resolve-Path -LiteralPath $Destination).Path
# Restrict the installation before any credentials are written.
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $Destination /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect installation directory' }
Get-ChildItem -LiteralPath $PSScriptRoot -File | Where-Object { $_.Name -ne 'config.json' } | Copy-Item -Destination $Destination
foreach ($folder in @('examples', 'tests')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $folder) -Destination $Destination -Recurse
}
$state = New-Item -ItemType Directory -Path (Join-Path $Destination 'state')
$utf8 = New-Object Text.UTF8Encoding($false)
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
foreach ($name in @('api', 'approval')) {
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    [IO.File]::WriteAllText((Join-Path $state.FullName "$name.token"), [Convert]::ToBase64String($bytes), $utf8)
}
$rng.Dispose()
$config = Get-Content -Raw (Join-Path $Destination 'config.example.json') | ConvertFrom-Json
$config.allowed_roots = $roots
$config.token_file = Join-Path $state.FullName 'api.token'
$config.approval_token_file = Join-Path $state.FullName 'approval.token'
$config.audit_file = Join-Path $state.FullName 'audit.jsonl'
[IO.File]::WriteAllText((Join-Path $Destination 'config.json'), ($config | ConvertTo-Json -Depth 8), $utf8)
[IO.File]::WriteAllText((Join-Path $Destination 'python.txt'), $Python, $utf8)
if ($AtLogon) {
    $action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --config "{1}"' -f (Join-Path $Destination 'service.py'), (Join-Path $Destination 'config.json')) -WorkingDirectory $Destination
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'GeneralPowerShellService' -Action $action -Principal $principal -Trigger $trigger -Settings $settings | Out-Null
}
Write-Output "Installed to $Destination. Run Start.ps1 there. Credentials are in the protected state folder."
