# General GPT-to-PowerShell Local Service

Status: implemented in [powershell-service](../powershell-service/README.md).
See its [audit and validation notes](../powershell-service/AUDIT.md) for tested
behavior and remaining deployment/hardware checks.

## Purpose

Build a standalone, reusable Windows PowerShell execution service for ordinary ChatGPT to operate local projects. This service is not HPCS-specific.

Target architecture:

ChatGPT -> GPT tool/connector -> local PowerShell service -> PowerShell / local executables / scripts / files / hardware utilities

Codex builds and maintains the service. Ordinary ChatGPT is the operator.

## Minimum tool/API surface

- status()
- exec(command, cwd, timeout)
- exec_script(path, args, cwd, timeout)
- cancel(job_id)
- read_file(path)
- list_files(path)
- get_job(job_id)

## Execution response

Every execution result should include:

- stdout
- stderr
- exit_code
- duration
- working_directory
- invoked command/script
- job/audit id
- service version
- produced/changed files where practical

## Operational requirements

- Windows-first.
- Structured JSON request/response.
- Explicit working directory.
- Correct handling of quoting and paths with spaces.
- Bounded timeout and cancellation.
- UTF-8-safe I/O.
- Append-only audit log.
- Authentication between GPT-facing connector and local service.
- Bind locally/private by default.
- No silent privilege elevation.
- Elevation only as a distinct explicit action requiring user approval.
- Configurable confirmation/deny policy for destructive or high-risk commands.
- Do not hard-code HPCS or LED commands.
- Project-specific logic remains in project scripts.

## Near-term validation use case

1. Set cwd to the hpcs310-ble checkout.
2. Run `py hpcs310.py selftest`.
3. Run `py hpcs310.py flicker --auto -v`.
4. Return stdout, stderr, exit code and identify the produced evidence directory.

## Deliverables

- service source
- install/start/stop instructions
- configuration example
- GPT tool schema / connector contract
- tests
- version command
- security notes
- example HPCS invocation proving the service is general rather than project-specific

Prefer a clean standalone module/folder suitable for moving into its own repository later.
