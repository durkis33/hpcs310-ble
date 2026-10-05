# General GPT-to-PowerShell Service

A Windows-first local HTTP service that lets a GPT-facing connector execute PowerShell and local scripts in a controlled, auditable way.

This service is intentionally general-purpose. It contains no HPCS-310, LED, or project-specific logic.

## Architecture

```
ChatGPT / tool client
        |
        | HTTP + Bearer token
        v
GPT-to-PowerShell service (localhost by default)
        |
        +--> PowerShell commands
        +--> local scripts/programs
        +--> local files
```

## Requirements

- Windows 10/11
- Python 3.9+
- Windows PowerShell 5.1 or PowerShell 7

No third-party Python packages are required.

## Install

Open PowerShell in this folder and run:

```powershell
.\install.ps1
```

This creates `config.json` with a random authentication token and a local logs directory.

## Start

```powershell
.\start.ps1
```

By default the service listens only on:

```
http://127.0.0.1:8765
```

The bearer token is stored in `config.json`.

## Stop

```powershell
.\stop.ps1
```

## API

- `GET /status`
- `POST /exec`
- `POST /exec-script`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/cancel`
- `POST /read-file`
- `POST /list-files`

All endpoints except `/status` require:

```
Authorization: Bearer <token>
```

`/status` also accepts the token and returns more detail when authenticated.

## Example: execute PowerShell

```json
{
  "command": "Get-Location",
  "cwd": "C:\\Users\\Micro\\Downloads",
  "timeout_seconds": 30
}
```

## Example: HPCS validation

HPCS is only an integration test; it is not built into the service.

```json
{
  "command": "py hpcs310.py selftest",
  "cwd": "C:\\path\\to\\hpcs310-ble",
  "timeout_seconds": 120
}
```

Then:

```json
{
  "command": "py hpcs310.py flicker --auto -v",
  "cwd": "C:\\path\\to\\hpcs310-ble",
  "timeout_seconds": 180,
  "confirmed": true
}
```

## Security model

- Binds to `127.0.0.1` by default.
- Uses a bearer token.
- Does not elevate privileges.
- Supports configurable deny and confirmation regexes.
- Captures stdout, stderr, exit code, duration and working directory.
- Writes an append-only JSONL audit log.
- Does not provide a hidden administrator/elevation path.

Do not bind this service to a public interface without adding a proper private-network/TLS/authentication layer.

## GPT connector

`openapi.yaml` describes the tool surface. A GPT-facing connector still needs a network path to this local service. Keep the service itself local/private; use only an approved connector or private tunnel when adding remote reachability.

## Version

```powershell
python server.py --version
```
