<!-- changelog: changed -->
- **The release gate now checks workflow script references before the live E2E jobs start.** A workflow that names a missing `scripts/` file stops the release in seconds instead of after the live jobs.

Until now, `scripts/check_workflow_script_refs.py` ran only inside `validate-scripts`, which runs in parallel with the live jobs and can take up to 90 minutes. Release run 35802596362 kept its live E2E jobs running for about 98 minutes after that check had already failed. The `resolve-version` job of `test-and-mark-stable.yml` now runs the same check as its "Preflight workflow script references" step. Every live job already waits for `resolve-version`, so a missing reference stops them before they start. The check in `validate-scripts` stays as it was.

| The numbers that matter | Value |
| --- | --- |
| Switch | `RELEASE_STATIC_PREFLIGHT_ENABLED` (default on) |
| `false` | turns the preflight off |
| Any other value | turns it off with a warning that does not print the value |
| Extra GitHub API calls | 0 |
| Live E2E time spent after a missing reference in run 35802596362 | about 98 minutes, now 0 |

What this means for operators: a release that names a missing `scripts/` file now fails in `resolve-version` with an error annotation, and no live E2E job starts. Set `RELEASE_STATIC_PREFLIGHT_ENABLED=false` only to turn the early check off; `validate-scripts` still runs it.
