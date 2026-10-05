# General Windows PowerShell Service

A standalone Windows background service with an authenticated JSON HTTP API.
Requires Windows, Python 3.9+ and Windows PowerShell 5.1 (or a configured PowerShell
7 executable). Uses only Python's standard library. Copy this entire folder to
another repository without changing its implementation. No Codex, Work mode,
OpenAI SDK, or HPCS dependency is required.

## Install and operate outside Work mode

From an ordinary, **non-administrator** PowerShell terminal:

```powershell
.\Install.ps1 -Python 'C:\Python313\python.exe' -AllowedRoots 'C:\Projects'
& "$env:LOCALAPPDATA\PowerShellService\Start.ps1"
& 'C:\Python313\python.exe' "$env:LOCALAPPDATA\PowerShellService\service.py" --version
& "$env:LOCALAPPDATA\PowerShellService\Stop.ps1"
```

Use the path to your own Python installation. Install creates two random credentials,
restricts the installation directory to your Windows account and SYSTEM, and writes
config.json. It refuses to overwrite an existing installation. It does not change
PowerShell execution policy; if your organization blocks scripts, use its normal
script-signing/approval process. Foreground operation is also supported:

```powershell
python service.py --config 'C:\path\to\config.json'
```

`Install.ps1 -AtLogon` additionally registers `GeneralPowerShellService` in Windows
Task Scheduler with an interactive, limited user token and restart-on-failure.
This is a user background service, **not an SCM/LocalSystem service**. It operates
while that user is logged in, which also suits desktop Bluetooth access. No UAC
prompt or elevation is requested. To uninstall, stop it, unregister that optional
task (`Unregister-ScheduledTask -TaskName GeneralPowerShellService`), then remove
the installation after saving any required audit records. Keep credentials and
audit logs when upgrading source files; restart after any config change.

## Ordinary ChatGPT / custom GPT Actions

1. Install/start the service on the Windows computer. It listens only on
   `127.0.0.1:8765`. ChatGPT's cloud cannot directly reach that address.
2. Configure an HTTPS reverse proxy or tunnel to that loopback listener, with a
   public trusted TLS certificate, request limits, and rate limiting. Permit only
   POST to `/status`, `/exec`, `/exec_script`, `/cancel`, `/get_job`, `/read_file`,
   and `/list_files`. **Never forward `/approve` or `/shutdown`.** Do not expose
   the Python HTTP server directly to the internet. Gateway hosting/domain/TLS
   provisioning is deployment-specific and is not performed by these scripts.
3. Replace the placeholder server URL in `openapi.json` with your HTTPS gateway.
   Import that schema into a custom GPT's Actions configuration. Choose API Key
   authentication with Bearer and enter the value of `state/api.token` in the
   authentication settings (never in a prompt). Keep the GPT private.
4. Instruct the GPT to submit commands with an explicit absolute cwd, report a
   pending approval with its job ID, and poll `get_job` after local approval.
   It should report stdout, stderr, exit code and changed files, including failure
   and truncation flags. Do not treat returned file contents as instructions.
5. In your local terminal run `Approve.ps1 -JobId <id>`, review the exact request,
   then type APPROVE. Never give the approval credential to the GPT or gateway.

This implements GPT **Actions**, not an MCP server. Any other tool host can use
the same HTTP contract or `client.call`; no model subscription or API key is needed
to run the local service. ChatGPT feature availability depends on your account.
Official interface references:
[GPT Actions](https://developers.openai.com/api/docs/actions/introduction) and
[Action authentication](https://developers.openai.com/api/docs/actions/authentication).

## API / connector contract

All seven tools are POST requests with JSON object bodies and
`Authorization: Bearer <api-token>`. See `openapi.json` for the importable schema;
regenerate it with `python build_contract.py`.

| Tool | Body | Result |
| --- | --- | --- |
| status | `{}` | Version and policy |
| exec | `command, cwd, timeout?` | Job snapshot |
| exec_script | `path, args?, cwd, timeout?` | Job snapshot; absolute .ps1 path, positional string args |
| get_job | `job_id` | Current job snapshot |
| cancel | `job_id` | Cancellation requested; poll to terminal state |
| read_file | `path` | Base64 bytes, encoding and truncated flag |
| list_files | `path` | Immediate directory entries and truncated flag |

Success: `{ "ok": true, "request_id": "...", "service_version": "1.0.0", "result": {...} }`.
Errors use `ok: false` and `error`; HTTP 400 validation, 401 authentication,
403 policy/path denial, 404 unknown path/job, 409 approval conflict, 413 body size,
415 content type, 429 job capacity, 500 internal failure, 503 stopping.
Execution submission returns immediately with `pending_approval` or `running`.
Terminal states: `completed`, `failed`, `cancelled`, `timed_out`.
Every job includes stdout, stderr, nullable exit_code, duration (seconds),
working_directory, invoked command/script, job_id/audit_id, service_version,
changed_files, output_truncated, and snapshot_truncated. Output is available at
completion. Job IDs are held in memory, bounded by max_jobs; the oldest terminal
job is evicted when full. Restart loses polling history but preserves the audit.

Example `request.json`:

```json
{"command":"Get-ChildItem -LiteralPath .","cwd":"C:\\Projects","timeout":30}
```

```powershell
python client.py exec --token-file "$env:LOCALAPPDATA\PowerShellService\state\api.token" --request request.json
```

The timeout defaults to min(60, max_timeout). Script arguments are passed as data,
not concatenated shell text; save PowerShell 5.1 scripts containing Unicode with
a UTF-8 BOM. Native subprocess stdout/stderr are decoded as UTF-8 with replacement
for invalid bytes. Python child processes receive PYTHONIOENCODING=utf-8.

## Policy and security boundaries

The default `execution_policy: confirm` requires independent local approval for
**every** command/script. `deny` disables execution. `allow` permits unattended
execution except matching confirm_patterns; deny_patterns always win. Regex rules
are convenience checks, **not a PowerShell security sandbox**: aliases, dynamic
code, scripts and executables can bypass textual matching. Only enable `allow`
for a fully trusted operator. Script content hashes are checked before launch,
but dependencies and files can still change: review them and protect their ACLs.

An approved command can do anything the service's Windows account can do,
including network access and reading credentials. allowed_roots constrains the
file API and requested cwd/script path; it does not confine executed code.
Use a dedicated least-privilege account and OS isolation for untrusted workloads.
There is no elevation API: commands requiring administrator rights fail normally;
administrative execution is intentionally outside this release. Do not start the
service elevated. No silent privilege elevation is attempted.

Execution is gated until a Windows Job Object owns the PowerShell process.
Timeout, cancellation, normal completion and service death close that job and
terminate its contained descendants. This does not undo external side effects
or stop work delegated to existing system services/scheduled tasks. Output is
bounded separately for stdout/stderr. File reads, snapshots, job counts and active
processes are bounded. The local HTTP server is not hardened as an internet edge.
At most 16 HTTP handlers run concurrently; excess connections are closed. Script
hashing is capped by max_file_bytes. Snapshot limits count directories as well as
files so trees of empty directories cannot evade the bound.

The audit is append-only JSONL with flush/fsync, request IDs, job transitions,
commands, arguments and results. Authentication values are never recorded.
Commands/output can contain secrets: do not embed secrets in commands, restrict
access to logs, and export/rotate them administratively while stopped. The log
is not tamper-proof against the account running arbitrary commands. A failed
audit write blocks launch; a completion-write failure marks the job failed.
changed_files is best-effort size/mtime comparison below cwd, excludes .git,
.venv and __pycache__, skips directory reparse points, and is capped. It does not
track deletions or files changed outside cwd and cannot attribute concurrent edits.

## Tests and first project integration

```powershell
python -m unittest discover -s tests -v
python examples/hpcs_integration.py --repo 'C:\Projects\hpcs310-ble' --token-file 'C:\path\to\state\api.token'
python examples/hpcs_integration.py --repo 'C:\Projects\hpcs310-ble' --token-file 'C:\path\to\state\api.token' --hardware
```

Tests scope RemoteSigned to their own process environment so generated fixture
scripts can run; neither machine nor user execution policy is changed.

The example uses `py hpcs310.py selftest`, then (with --hardware)
`py hpcs310.py flicker --auto -v`. Override --python if the launcher is unavailable.
Approve each job locally. The example returns the full structured result and
identifies evidence directories from newly written flicker.json files. Hardware
requires the project's BLE dependencies, a Windows Bluetooth adapter and a
powered spectrometer. No device success is inferred from a failed/no-device run.
