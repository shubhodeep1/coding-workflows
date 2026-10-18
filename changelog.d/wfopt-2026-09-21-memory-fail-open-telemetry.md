<!-- changelog: changed -->
- **Failed AI-memory writes now say why they failed.** When recording a run event or a memory candidate fails, the `AI_MEMORY_TELEMETRY` line now also carries the exit code, how long the call took (`duration_ms`) and a failure class (`stderr_category`): `push_rejected`, `auth`, `network`, `validation`, `other`, `none` (no error output) or `unavailable` (the error output could not be captured). The existing fields are unchanged, the warning now ends with `exit_code=<n>`, and the step still carries on as before.

The failure class is the only thing taken from the error output, so remote URLs or tokens in it never reach the telemetry line. The error output itself is still printed to the log, now after the call ends rather than while it runs.

For contributors: `memory_record_run_event` and `memory_record_candidate` in `scripts/memory_helpers.sh` share the new private helper `_memory_record_with_failure_telemetry`, with `_memory_stderr_category` doing the classification. `tests/test_memory_helpers_fail_open_telemetry.py` covers each class, the token-leak case and the `mktemp` fallback.

Source: `analysis/workflow-optimization-2026-09-21-2.md`, Reliability item 4.
