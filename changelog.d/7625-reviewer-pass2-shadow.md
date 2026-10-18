<!-- changelog: added -->
- **Two-pass review now logs whether the second pass could have been skipped.** `scripts/review_run_reviewers.sh` prints one `REVIEWER_PASS2_SHADOW` line after pass 1 and before pass 2. This is the shadow step of the adaptive second pass proposed in `analysis/workflow-optimization-2026-09-21-2.md` (Speed item 2). Pass 2 still runs on every review.

The line reads `would_skip=true` only when at least two pass-1 reviewers succeeded, pass 1 reported no high-severity finding, the diff is below `REVIEWER_PASS2_DIFF_LARGE_LOC` (the same count and threshold the pass-2 reasoning gate uses), and no changed path matches `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`. Otherwise `reason=` names the first condition that failed: `pass1_insufficient`, `high_findings`, `large_diff` or `sensitive_paths`. The reviewer contract rates findings `BLOCKER`, `MAJOR` or `NIT`, so `BLOCKER` and `MAJOR` count as high severity next to `high` and `critical`. Rejected pool outputs and the consensus ledger are not counted. Any evaluation error, including a missing or empty changed-file list, logs `would_skip=unknown reason=evaluation_failed` and the review continues.

`REVIEWER_PASS2_SHADOW_ENABLED=false` turns the line off. Unset, empty or `true` keeps it on; any other value turns it off with one warning. The variable is read by the script only, so a repository variable alone does not reach it.

For contributors: nothing reads the decision yet. A later change can use the logged data to decide whether to skip pass 2.
