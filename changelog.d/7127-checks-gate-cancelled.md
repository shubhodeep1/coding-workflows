<!-- changelog: fixed -->
- **A cancelled required check now blocks merges.** A newer re-run of the same check replaces the cancelled one.

`scripts/pr_checks_lib.sh` checks the check-runs on a PR's head before the orchestrator, the review-blocked judge and the auto-merge paths merge. It counted `cancelled` as passing. On a branch without protection, as on this repository's `main`, a PR whose CI aggregate job `lint` was cancelled could therefore merge. The gate now accepts only `success`, `neutral` and `skipped`. A required check that ended `cancelled`, `timed_out`, `action_required`, `failure` or `startup_failure` blocks.

Re-running a job adds a new check-run with the same name on the same commit. A cancelled check-run now blocks only while it is the newest check-run of its name, so a cancelled run that a later run replaced does not block. The CI aggregate check `lint` shares its name with jobs in two PR-body lint workflows that run again on every PR body edit, so a cancelled `lint` is replaced only by a re-run in the same Actions run (or, in the auto-merge wait, by a newer CI run). A failed or still-running check-run still blocks even when a newer run of the same name exists, because a check-run does not say which workflow produced it and different workflows can share a job name. A cancelled required check with nothing newer counts as finished, so the auto-merge wait refuses at once instead of waiting out its budget, unless another check on the head is still running.

The noop-suspicious force-merge fallback in the poller (Gate D) had its own check-runs query. That query blocked on failed advisory checks and ignored required checks that were still running. Gate D now calls the shared gate, so both paths apply the same rules. It pages once per PR head, like the other force-merge gates, and waits without paging while a check is still running or the check-runs could not be read.

| The numbers that matter | Value |
| --- | --- |
| Conclusions that pass | `success`, `neutral`, `skipped` |
| Check-runs a newer same-name run can replace | cancelled ones only |
| Runs that can replace a cancelled `lint` | a re-run in the same Actions run, or a newer CI run in the auto-merge wait |
| Extra API calls | 1 branch-protection read per Gate D check (noop retry-cap path only) |

What this means for operators: re-run a cancelled required check to unblock its PR. If the newest `review / gate` run on a head was cancelled, the merge waits for the next review run on that head.
