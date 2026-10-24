<!-- changelog: changed -->
- **Each review editor attempt now logs one `EDITOR_ATTEMPT_RESULT` line.** It names the model, engine, duration, exit code, stall state, output byte counts, whether the summary was written, and a failure class, so an editor failure no longer shows only an opaque exit code.

Four editor failures in the 2026-09-23 workflow analysis ended with exit code 226 and nothing in the step log saying why, which slowed triage of the repeated `editor_empty_noop` failures. `scripts/review_apply_fixes.sh` now prints, after each attempt's stall-state check and before the exit-code branches:

`EDITOR_ATTEMPT_RESULT attempt=<n>/<max> model=<slug> engine=<claude|codex> duration_secs=<n> exit_code=<n> stall_state=<none|observed|killed> stdout_bytes=<n> stderr_bytes=<n> summary_present=<true|false> failure_class=<token>`

`failure_class` is one of `ok`, `empty_output`, `stall_killed`, `workspace_safety_violation`, `config_bootstrap`, `claude_capacity`, `timeout` or `exit_nonzero`. The line carries counts and fixed tokens only, never editor output, and a model name with unexpected characters prints as `unknown`. An attempt whose sandbox transfer fails stops before this point and prints no line. Exit codes, retries and the model fallback are unchanged.

What this means for consumer repos: after the next `@stable` sync, review/autofix logs show one `EDITOR_ATTEMPT_RESULT` line per editor attempt. Nothing to configure.
