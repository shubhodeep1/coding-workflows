<!-- changelog: fixed -->
- **The review-blocked judge posts its decision comment once per approval request instead of once per review run.** Re-dispatches that reuse a pending trusted-human-approval request no longer add an identical `## Review-Blocked Judge Decision` comment to the PR.

When `scripts/review_rb_judge.sh` finds an unconsumed `REVIEW_BLOCKED_APPROVAL_V1` request for the current head it skips the model call and reuses the recorded decision. It also re-posted the assessment built from that decision, so every 30-minute review-sweep re-dispatch added the same comment again: PR #4079 collected 22 copies in eleven hours while waiting for a maintainer to post `/review-blocked-approve`. The assessment post is now gated on the reuse path, and a new terminal approval request is published only after its assessment succeeds so a transient post failure remains retryable.

| The numbers that matter | Value |
| --- | --- |
| Incident | `shubhodeep1/coding-workflows` PR #4079, runs 34703958674 through 34735874446 |
| Duplicate decision comments before the fix | 22 |
| Regression test | `tests/test_review_rb_judge_pending_approval_comment.py` |

What this means for operators: a PR waiting on human approval shows one decision comment and one approval-request comment; the approval command stays easy to find instead of scrolling past a wall of repeats.
