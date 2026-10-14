<!-- changelog: fixed -->
- **Heal issues filed without a scope marker are now recovered on a schedule, and a non-retryable report no longer skips recovery.** Issue #7333 (activation gaps of PR #7325 for #7082).

PR #7325 let an authenticated report replace a pipeline-authored heal issue that has no `ai:workflow-heal-scope:v1` marker, but nothing revisited such an issue on its own, and a report with a non-retryable sandbox reason (as for #7082) stopped before the recovery check. The heal intake workflow now also runs hourly: `scripts/workflow_failure_heal_recovery_scan.sh` lists open, pipeline-authored heal issues without a scope marker, rebuilds the review/autofix failure report from the issue's own never-edited body, and runs the unchanged intake on it. The intake still binds the pull request, verifies the failed runs, recomputes the fingerprint and requires it to match the issue, and creates the replacement only after its scope marker passes `heal-scope verify`; a replay posts nothing on the source pull request. A failed attempt leaves one marker comment on the heal issue; after three, the scan stops and sends one WARNING. Issues that have used up their attempts are dropped from the list, and a scan is skipped for the hour while another intake run is active, so it cannot replace a report waiting for the shared lock. The scan uses the same AI engine selection as a normal intake. A cap or failed read that clears on its own does not count as an attempt, and an issue already replaced but left open is closed.

A verified report with `conflict_resolver_sandbox_path_unsupported` or `conflict_resolver_sandbox_support_missing` now first gets the recovery check, then the unchanged non-retryable handling on the pull request (diagnosis comment, `ai:needs-human`, WARNING). With no matching heal issue the outcome is the same as before. The host-only stop is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Scan schedule | hourly (`41 * * * *`) |
| `HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK` | default `1`, at most `2` |
| `HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` | default `3` |

What this means for operators: #7082-style heal issues are replaced without a person relabelling or redispatching anything. `HEAL_SCOPE_RECOVERY_ENABLED=false` turns the scan off together with recovery.
