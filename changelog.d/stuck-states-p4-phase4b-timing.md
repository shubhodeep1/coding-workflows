<!-- changelog: changed -->
- **The release gate's Phase 4b retry no longer spends its editor budget waiting for a runner.** Before, the 25-minute budget for the retried review run started when the run was adopted or dispatched, so a run that sat queued (gate run 37395952357 was queued for about 24 of its 25 minutes) timed out as `retry_timeout` and blamed the editor.

`scripts/stable_e2e_step_verify_bait_removed.sh` now starts `EDITOR_RETRY_BUDGET_MINUTES` at the first poll that sees the run `in_progress` or `completed`. A run still queued after `E2E_RETRY_QUEUED_GRACE_MINUTES` fails with the new status `retry_queued_timeout`. Both deadlines stay inside the job's time budget, and no GitHub API call is added.

| The numbers that matter | Value |
| --- | --- |
| Editor budget after the run starts | 25 minutes (unchanged) |
| Queued cap (`E2E_RETRY_QUEUED_GRACE_MINUTES`) | 30 minutes by default |
| Kill switch | `E2E_PHASE4B_QUEUED_GRACE_ENABLED=false` restores the old extension |

What this means for operators: `FAILED (retry_queued_timeout)` on the Editor Bait line means the runner pool was saturated, not that the editor broke.
