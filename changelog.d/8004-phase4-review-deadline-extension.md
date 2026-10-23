<!-- changelog: fixed -->
- **The release gate's Phase 4 review wait no longer gives up on a review run that is still working.** Once its 90-minute total budget runs out, "Phase 4: Wait for review & autofix to complete" keeps waiting while the watched review run is queued, or in progress and active. The existing inactivity timeout (`REVIEW_TIMEOUT`, 30 minutes by default) still applies, so a run that stays queued with no activity for that long still ends the phase. It never waits past the point that would leave too little time for Phase 4b, Phase 6, Phase 7 and finalization.

Run 39969947939 failed with `Review phase exceeded its 90-minute total wall-clock budget` six minutes after the editor had pushed its fix commit. The run being watched was in its last bookkeeping step, and the job still had about 80 minutes of unused budget. The bait-covering review can wait in the `pr-autofix-<PR>` concurrency group behind the review that ran when the PR opened, so a single Phase 4 can cover two review runs back to back.

| The numbers that matter | Value |
| --- | --- |
| Phase 4 base budget (unchanged) | 90 minutes (`REVIEW_STEP_TIMEOUT` 75 + `REVIEW_PHASE_HANDOFF_BUDGET_MINUTES` 15) |
| Latest point Phase 4 can still be waiting, with defaults | job start + 215 minutes (300 − 25 − 30 − 10 − 20) |
| Job timeout (unchanged) | 300 minutes |
| New GitHub API calls | 0 |

- The ceiling is `E2E_JOB_STARTED_EPOCH + (E2E_JOB_TIMEOUT_MINUTES − EDITOR_RETRY_BUDGET_MINUTES − PHASE_TIMEOUT − PHASE7_WAIT_BUDGET_MINUTES − E2E_FINALIZATION_RESERVE_MINUTES) × 60`, never below the base deadline. This is the job-start cap Phase 4b already uses, and it also reserves Phase 4b's budget. With no recorded job start, nothing is extended.
- Apart from the one handoff poll below, the wait is extended only while the run's ID is numeric, the run is not one already found not to cover the bait commit, and its status is `queued`, `pending`, `waiting` or `requested`, or `in_progress` with activity inside the inactivity window or a current step younger than the per-step stall cap. The inactivity timeout, the per-step stall cap and the bait-coverage checks are unchanged.
- When the loop has just dropped the watched run (it did not review the bait commit, or its coverage could not be confirmed) or moved on from a cancelled run to the new PR head, it gets one more poll to find the next run, as long as the ceiling has not passed. That allowance is used up by the next deadline check, so if that poll finds no eligible run the phase times out. Before this, the check judged the dropped run and timed out before the queued bait-covering run was ever polled.
- A poll past the base deadline starts only when it and its `POLL_INTERVAL` sleep still end before the ceiling.
- The first extension logs `E2E_REVIEW_DEADLINE_EXTENDED run=<id> status=<status> base=<epoch> ceiling=<epoch>` once. The prefix is registered in `agents.d/8004-phase4-review-deadline-extension.md`.

What this means for consumer repos: nothing. This only changes the release gate in coding-workflows, which consumer repos do not run.

### For contributors

The extension logic sits between `# BEGIN/END phase4 review deadline extension (#8004)` in `scripts/stable_e2e_step_wait_review.sh`, the Phase 4 step body that `.github/workflows/test-and-mark-stable.yml` sources (#7991). Tests: new cases in `tests/test_test_and_mark_stable_review_blocked_budget.py` run that block with a fixed clock.
