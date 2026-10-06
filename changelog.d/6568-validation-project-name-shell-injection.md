<!-- changelog: security -->
- **Validation no longer runs a repository's `slots.project_name` as shell code, and generated validation tests run without the job's credentials.** Security finding `validation-project-name-shell-injection` (re-issued from #6194).

`scripts/render_validation_templates.py` copied `slots.project_name` from `.ai/validate.yml` unquoted into every family's `tests/10_family_marker.sh`. A value such as `x$(curl …)` therefore ran on the runner inside the job that holds `GH_PAT`. The renderer now rejects a shell-active `project_name` before rendering, whatever schema the caller passes (`ERROR: Manifest safety check failed: /slots/project_name …`; the value is never echoed). It also rejects shell-active `canary_tools` entries, a non-integer `tap_plan`, and an out-of-range `port`. A rejected manifest fails validation as `harness_error`. Every manifest value a generated shell test interpolates is quoted through a `shell_quote` filter. The manifest schema carries the same `project_name` pattern.

Generated tests also no longer run in the credentialed process. `scripts/validate_process.sh` now runs the harness through `scripts/validation_harness_sandbox.sh`:
- The harness runs as a separate user (`ai-validation`) with its own rootless Docker daemon.
- It gets a screened copy of the workspace and an allowlisted environment, with no `GH_PAT`, `GH_TOKEN`, model keys or git auth.
- Before each run, a self-check fails closed if credentials, the host Docker socket or runner directories are reachable.
- Logs come back only as bounded regular files.

On every caller, each test is also started with credential variables and `GIT_CONFIG_*` removed.

| The numbers that matter | Value |
| --- | --- |
| Shell templates with an unquoted manifest value | 0 (was: 13) |
| Credential variables visible to a generated test | 0 |
| Fallback to host execution when the sandbox is unavailable | none (fail-closed `harness_error`) |
| New GitHub API calls | 0 |

What this means for operators:
- Validation now needs passwordless `sudo`, `uidmap`, `docker-ce-rootless-extras` and unprivileged user namespaces on the runner. Provisioning installs or enables them when it can.
- Otherwise validation reports `harness_error` (`VALIDATION_HARNESS_SANDBOX phase=provision outcome=fail reason=…`) and does not run the tests.
- `validation-refresh` and the nightly self-test still run the driver on the host. There they get only the per-test credential scrub.
