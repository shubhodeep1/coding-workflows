<!-- changelog: fixed -->
- **The review workflow no longer sends a CRITICAL Telegram alert on every re-dispatch while a review-blocked judge decision waits for human approval.** One alert is sent when the approval request is created; later runs that reuse the pending request log a suppression line instead.

`review_autofix.yml` runs `@main`, but `scripts/review_rb_judge.sh` executes from the PR head's `SCRIPT_REF`. A PR whose branch carries the trusted-human-approval gate for terminal judge decisions therefore reports `judge_action=approval_pending` to a workflow that had no arm for it, so the generic `Review-blocked judge action: approval_pending` message fired on every 30-minute review-sweep re-dispatch. The `Telegram review-blocked judge decision` step now handles `approval_pending`: it alerts on `judge_skip_reason=approval_request_created` and exits quietly on `approval_pending`.

| The numbers that matter | Value |
| --- | --- |
| Incident | `shubhodeep1/coding-workflows` PR #4079, run 34721550146 |
| Duplicate CRITICAL alerts before the fix | 24 in eleven hours (one per sweep tick) |
| Regression test | `tests/test_review_rb_judge_self_run_exclusion.py::test_workflow_suppresses_repeat_approval_pending_alert` |

What this means for operators: a pending approval produces one Telegram alert, not one every half hour, and the alert text now says the recommendation needs trusted human approval rather than echoing the raw action name.

### For contributors

The arm is byte-identical to the one `orchestrator/project-3965` already carries in its `review_autofix.yml`, so the project's next `chore: sync main` merge sees the same insertion on both sides. The branch also suppresses `judge_action=skip`; that arm is deliberately not ported because on `main` a handled `skip` still covers `invalid_pr_number` and `pr_not_open`, whose alerts are unrelated to the approval gate.
