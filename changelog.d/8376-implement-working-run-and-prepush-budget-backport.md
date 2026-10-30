<!-- changelog: fixed -->
- **The stable release gate now follows the Implement run that is doing the work, and gives the pre-push test gate step its own wait limit.** This backports two `main` fixes (#8243, #8259) to `stable`'s `.github/workflows/test-and-mark-stable.yml`.

Release run 40876853022 failed in Phase 3b ("Wait for PR creation") with `Implement phase stalled — no activity for 30 minutes`, nine minutes before the smoke PR existed. Two things went wrong. First, Phase 3b read its step-progress signal from the newest matching Implement run, which was a duplicate held `pending` by the per-issue concurrency group, so it never saw the working run's steps change. Phase 3b now watches the newest `in_progress` matching run (`IMPL_RUN_ID_WORKING`), looks for it on later pages when a full first page holds only a pending duplicate, and falls back to the newest run when none is in progress. Second, the working run's "Pre-push affected-test gate" step ran 30m28s, longer than the 30-minute inactivity limit on its own. While the watched run is in that step, Phase 3b now uses `PREPUSH_GATE_STEP_STALL_MINUTES` instead; every other step keeps the 30-minute limit, and a value at or below it never shortens it. A non-numeric value falls back to the normal limit. The release gate's serial budget check counts the extra minutes, so the default still fits the 300-minute job timeout.

| The numbers that matter | Value |
| --- | --- |
| Normal Phase 3b inactivity limit (unchanged) | 30 minutes |
| `PREPUSH_GATE_STEP_STALL_MINUTES` default (same as `main`) | 35 minutes |
| Pre-push gate step in run 40876853022 | 30m28s |
| Conservative serial budget / job timeout | 300 / 300 minutes |

What this means for operators: a release gate whose smoke implement run spends up to 35 minutes in the pre-push gate step, or that has a queued duplicate Implement run beside the working one, no longer fails Phase 3b. The stall error, `status=timeout` / `status=implement_failed` and the `stall-diag` diagnostics from #8379 are unchanged; when the gate step's limit is the one that fires, the error names it ("(pre-push gate step: limit 35m)"). An Implement run created before the gate's own `/approved` (for example by plan auto-approval) is still outside the watched window; widening that window is separate work.

### For contributors

The identifiers and the default match `main`, so the next `main` to `stable` promotion merges cleanly. Tests: `tests/test_test_and_mark_stable_plan_polling_guard.py` (working-run selection, later-page lookup, the gate step's own limit, the normal limit for other steps, non-numeric values, and a replay of this incident) and `tests/test_test_and_mark_stable_review_blocked_budget.py` (budget validation and the 300-minute serial budget).
