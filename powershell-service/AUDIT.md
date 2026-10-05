# Implementation audit — 2026-10-05

Reviewed against `docs/general-powershell-service.md` at main commit c70f69b.
This is an implementation review and local validation, not an external security
certification or evidence of a deployed ChatGPT connection.

## Findings fixed before submission

| Finding | Resolution | Regression evidence |
| --- | --- | --- |
| Script hashing read arbitrarily large files | Enforce max_file_bytes before hashing | Oversized script rejection test |
| Snapshots bounded files but not empty directories | Stream directory entries and count both directories and files | Empty-directory limit test |
| Unattended submissions at capacity could create inaccessible pending jobs | Check active capacity before allocating a job | No-orphan capacity test |
| HTTP server could allocate unlimited handler threads | Cap at 16, with socket read timeouts | Source review; no load certification |
| A cancelled job could be released through the launch gate | Recheck cancellation before launch and before releasing stdin | Cancellation test |
| Completion audit reported the pre-completion state | Record the terminal state with results | Audit inspection |
| CI temporary paths used Windows 8.3 aliases while the service returned canonical paths | Resolve the test fixture root before comparing paths | Windows CI regression |

## Verified locally on Windows

- 12 automated tests passed: UTF-8 output, explicit cwd and changed files;
  command failure/native exit code; timeout/cancellation; descendant cleanup;
  argument quoting and paths with spaces/apostrophes; script-change detection;
  HTTP authentication and independent approval; file tools/path validation;
  output limits; policy checks; audit failure before launch; capacity and scan
  bounds; HPCS hardware-free integration.
- Install.ps1, Start.ps1 and Stop.ps1 exercised under the normal Windows account
  in a separate test installation. No logon task installed. Credential ACLs
  rejected access from a different Windows account.
- Installed service returned version 1.0.0 and confirm policy through HTTP.
- HPCS selftest ran through that installed service: exit 0, SELFTEST: PASS.
- HPCS flicker --auto -v ran through the installed service: exit 1 with a
  structured ModuleNotFoundError for bleak. No evidence directory was produced.
  The Python launcher was unavailable, so an explicit Python executable was used.
- Test service stopped after validation. PowerShell lifecycle scripts parsed
  without syntax errors.

## Remaining boundaries and deployment checks

- Live BLE acquisition is **not validated**: install HPCS's BLE dependency in the
  chosen project Python environment and repeat with a reachable spectrometer.
- GPT Actions schema and connector are supplied; no public HTTPS gateway,
  custom GPT, or live ChatGPT end-to-end connection was provisioned or verified.
- Optional Task Scheduler registration is implemented but not exercised here.
  This is a per-user background service, not a Windows SCM service.
- No elevation API is provided. Use a non-administrator account. Arbitrary
  approved code has that account's privileges; regex and allowed_roots are not
  an execution sandbox. The approval token is independent, but code already
  approved to run as the service account can access that account's files.
- Audit is append-only in application behavior, not immutable against that
  account. Commands and results can contain secrets. Operator-managed retention
  and protected/off-host audit storage are needed for stronger guarantees.
- File/script validation does not defend against a malicious local account
  racing file replacement or modifying dependencies. Project ACLs and OS
  isolation remain required for that threat model.
- Windows Job Objects contain ordinary descendants, not work delegated to
  pre-existing services. Changed-file reporting is capped, best-effort and
  excludes deletion tracking. Pending/results are memory-only across restart.
- Automated tests used the local Python runtime and Windows PowerShell 5.1.
  The supplied Windows CI workflow also targets Python 3.9. Its first run exposed
  the fixture path-alias issue fixed above; consult the PR checks for final status.
