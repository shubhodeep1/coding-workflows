<!-- changelog: changed -->
- **Failed AI-memory writes now say why they failed.** A failed run-event or memory-candidate write used to log only that it failed.

When `record-run-event` or `record-candidate` fails, its `AI_MEMORY_TELEMETRY` line now also carries the exit code, the time the memory call took (`duration_ms`) and a failure class (`stderr_category`). The class is one of `push_rejected`, `auth`, `network`, `validation`, `other`, `none` (no error output) or `unavailable` (the error output could not be captured). Only the class is taken from the error output, so remote URLs or tokens in it never reach the telemetry line. The existing fields are unchanged, the warning now ends with `exit_code=<n>`, and the step still carries on as before.

What this means for consumer repos: the AI Memory Health section of workflow optimization reports can now name the cause of a fail-open memory write. The error output is still printed to the run log, now after the call ends rather than while it runs.

### For contributors

`memory_record_run_event` and `memory_record_candidate` in `scripts/memory_helpers.sh` share the new private helper `_memory_record_with_failure_telemetry`, and `_memory_stderr_category` does the classification. `tests/test_memory_helpers_fail_open_telemetry.py` covers each class, the token-leak case and the `mktemp` fallback. Source: `analysis/workflow-optimization-2026-09-21-2.md`, Reliability item 4.
