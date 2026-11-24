<!-- changelog: fixed -->
- **A PR the review-blocked judge decided to merge now gets the merge lease when the lease is busy.** Before this, the judge waited for the lease before it posted `ai-review/head-gate`, and the lease refused a waiter without head-gate success. The PR dropped out of the queue every time, and stall recovery started the same cycle again (#9956 on 2026-11-23).

When the judge decides `merge`, `merge_with_followup` or the terminal fix merge and has to wait for the lease, it posts an `ai-review/judge-merge-pending` success status on the judged head. The lease tick counts that status for a waiter in place of a pending head-gate, when the pipeline login created it on that exact head. A head-gate failure still makes the waiter ineligible. On the grant the tick dispatches the judge (the review wrapper with `force_rb_judge=true`, whose `[pr:<n>]` run name keeps the lease with the PR while the judge runs, or `review_rb_judge_dispatch.yml` when no wrapper accepts that input) instead of a review run, and the judge merges through its usual gates in the usual order: branch update, security merge gate, head-gate, required checks and final CI.

What this means for operators: the `MERGE_LEASE ... outcome=granted` line ends with ` gate=judge_decision` when a grant rested on the judge's decision, and the judge logs `JUDGE_MERGE_PENDING`. `MERGE_LEASE_REVOKE_ENABLED=false` turns this off together with the rest of merge-lease Phase 2. No new variable, job or workflow.

### For contributors

Code: `_pr_judge_merge_pending_mark` and `_merge_lease_pr_eligible` (`MERGE_LEASE_ELIGIBLE_GATE`) in `scripts/pr_checks_lib.sh`, `rb_judge_mark_merge_pending` in `scripts/review_rb_judge.sh`, and `_mt_dispatch_rb_judge` in `scripts/review_merge_train.sh`. Tests: `tests/test_merge_lease.py`, `tests/test_review_merge_train.py` and `tests/test_review_rb_judge_head_binding.py`.
