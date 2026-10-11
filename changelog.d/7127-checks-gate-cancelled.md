<!-- changelog: fixed -->
- **A cancelled required check now blocks merges.** A newer re-run of the same check replaces the cancelled one.

`scripts/pr_checks_lib.sh` checks the check-runs on a PR's head before the orchestrator, the review-blocked judge and the auto-merge paths merge. It counted `cancelled` as passing. On a branch without protection, as on this repository's `main`, a PR whose CI aggregate job `lint` was cancelled could therefore merge. The gate now accepts only `success`, `neutral` and `skipped`. A required check that ended `cancelled`, `timed_out`, `action_required`, `failure` or `startup_failure` blocks.

Re-running a job adds a new check-run with the same name on the same commit. The gate now looks only at the newest check-run of each name, so a cancelled run that a later run replaced does not block. A cancelled required check with nothing newer counts as finished, so the auto-merge wait refuses at once instead of waiting out its budget.

The noop-suspicious force-merge fallback in the poller (Gate D) had its own check-runs query. That query blocked on failed advisory checks and ignored required checks that were still running. Gate D now calls the shared gate, so both paths apply the same rules.

| The numbers that matter | Value |
| --- | --- |
| Conclusions that pass | `success`, `neutral`, `skipped` |
| Check-runs evaluated per name | the newest one |
| Extra API calls | 1 branch-protection read per Gate D check (noop retry-cap path only) |

What this means for operators: re-run a cancelled required check to unblock its PR. If the newest `review / gate` run on a head was cancelled, the merge waits for the next review run on that head.
