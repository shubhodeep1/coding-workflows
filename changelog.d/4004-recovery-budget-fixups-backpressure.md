<!-- changelog: fixed -->
- **A converging orchestrator project no longer ends in "Manual intervention required" because of its own bookkeeping.** The poller now files the judge's fix-up issues even when the recovery budget is exhausted, only charges that budget on a repeated finding, and stops counting its own `chore: sync main` merges toward integration backpressure.

Project tele-funtoken-msg-scoring#3928 fixed a different real defect on each of five consecutive judge cycles and was still stopped by `orchestrate_poll.yml`: the recovery-exhausted branch posted `## Project Failed` without creating the two fix-up issues the judge had just produced, `MAX_RECOVERY_ATTEMPTS` had been consumed by findings that were never repeated, and the tracking issue carried `ai:integration-backpressure` because 15 sync-merge commits were counted as drift. Operators had to re-derive the judge's diagnosis by hand and would have been blocked from merging the resulting fix even after `/judge_resume`. All three behaviours are corrected in `scripts/orchestrate_poll_process.sh`; the exhausted branch now lists the filed issues in the `## Project Failed` comment and `/judge_resume` picks them up with no further diagnosis.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `RECOVERY_COUNT_DISTINCT_FINDINGS` (default `false`) |
| Recovery budget charged when | the judge fingerprint is already in the project's `judge_failed_fingerprints` ledger, or is empty |
| Backpressure figure | non-merge commits ahead of default (raw `ahead_by` fallback when the compare commit list is missing or exceeds 250) |
| #3928 shape now evaluated as | 11 work commits against a threshold of 25, instead of 26 |
| Loop breaker unchanged | `JUDGE_REPEAT_FINGERPRINT_MAX` (default `2`) |
| Absolute cap unchanged | `MAX_JUDGE_CYCLES` |

What this means for operators: a project that keeps fixing distinct findings keeps running until `MAX_JUDGE_CYCLES`, identical-finding loops still trip the repeat-fingerprint breaker, and a project that does exhaust recovery leaves its fix-up issues filed and tracked so `/judge_resume --reset-recovery` resumes work immediately. Set `RECOVERY_COUNT_DISTINCT_FINDINGS=true` on a consumer repo to restore the previous every-failed-verdict accounting.

### For contributors

`_integration_branch_ahead_of_default` accepts an optional OUTVAR third argument and sets `INTEGRATION_AHEAD_BY_WORK_COMMITS` from the same compare call; `compute_cycle_integration_ahead_by` exposes `CWS_WORK_AHEAD_BY` and `CWS_BACKPRESSURE_AHEAD_BY`, and only the backpressure gate reads the latter. `BACKPRESSURE_TRIGGERED` and `BACKPRESSURE_CLEARED` log lines carry `raw_ahead_by=` and `work_ahead_by=`; each failed verdict logs a `RECOVERY_BUDGET_ACCOUNTING` line. The judge prompt (`prompts/mode-orchestrate-poll-judge.txt`) now requires the judge to quote the plan sentence a contradiction violates before flagging it.
