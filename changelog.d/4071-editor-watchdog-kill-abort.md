<!-- changelog: fixed -->
- **A watchdog kill of the review-autofix editor no longer aborts the whole editor step.** After a wall-time or idle kill the editor now moves on to the next attempt (or the fallback summary) instead of the run surfacing as "AI review/autofix produced no output — will retry".

When the editor watchdog killed attempt 1 at the 55-minute wall cap on PR #4071 (AI Review run 34397466777), `scripts/review_apply_fixes.sh` exited 1 a few milliseconds after restoring editor isolation: no attempt 2 or 3, no fallback-model attempt, no fallback summary, and no archived editor stderr. The "Post editor summary comment" step then classified the run as an empty no-op, posted the "will retry" comment, and the review was deferred to the stall poller's next cycle, costing the run's remaining budget and another full reviewer pass. The watchdog subshell exits on its own after it kills the editor, so the parent's `kill "${wd_pid}"` can hit a process that is already gone and return 1; under `set -euo pipefail` that unguarded failure aborted the script. The reap is now `kill ... || true; wait ... || true` at the editor kill site and at both reviewer watchdog kill sites in `scripts/review_run_reviewers.sh`.

| The numbers that matter | Value |
| --- | --- |
| Incident | AI Review run 34397466777, PR #4071, editor attempt 1 killed at 3300s |
| Time from kill to silent exit | ~5 s (the watchdog's TERM, `sleep 5`, KILL sequence) |
| Kill sites fixed | 1 in `review_apply_fixes.sh`, 2 in `review_run_reviewers.sh` |
| Regression test | `tests/test_editor_watchdog_kill_contract.py` |

What this means for operators: an editor that overruns `EDITOR_MAX_WALL` or goes idle now costs one attempt, not the whole review run. The retry loop keeps its remaining attempts (including the `MODEL_EDITOR_FALLBACK` switch on the final one), the editor's stderr is archived as `editor_attempt_<n>.err` in the failure-log artifact, and the fallback summary is written so the downstream steps see a real disposition instead of an empty no-op.

### For contributors

On `main` the race is narrow because the stall guard kills a runner-owned editor process within about a second of the watchdog's TERM, so the parent usually reaps the watchdog while it is still in its `sleep 5`. The `orchestrator/project-3965` branch runs the editor under `sudo -u nobody` isolation, where the runner-owned stall guard cannot signal the editor's process group, the guard survives until the watchdog's KILL five seconds later, and the parent then always reaps an already-exited watchdog. That is why run 34397466777 hit the abort deterministically. The signal-permission gap in the isolated editor path is a separate defect on that branch and is not addressed here.
