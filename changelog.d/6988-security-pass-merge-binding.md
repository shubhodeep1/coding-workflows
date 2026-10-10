<!-- changelog: security -->
- **Automated merges now merge only the commit that was checked, and wait when base freshness cannot be verified.** Three findings from the project security pass (Refs #6664) are fixed.

The orchestrator poller's merges used to call `gh pr merge` without naming a commit. A PR author could push after the poller checked the head and before it merged, and on a base without branch protection the new, unchecked commit merged. Every poller merge (the final integration merge, standalone stall `attempt_merge`, the backward-scan, the ready-to-merge loop, the review-blocked merge, force-merge and no-fix paths, and the noop-suspicious force-merge) now passes `--match-head-commit` with the head SHA its check-runs and freshness gates evaluated. GitHub refuses the merge when the head moved, and the next poll tick checks the new head. A head that cannot be resolved never reaches `gh`, and a moved head on the final merge does not use up the final-merge attempt budget.

The merge-base freshness gate (`_pr_base_fresh_for_merge` in `scripts/pr_checks_lib.sh`) used to let a merge go ahead when the compare or PR-files read failed. It now defers the merge in that case (`MERGE_BASE_FRESHNESS ... outcome=unknown action=defer`) without requesting a branch update; the next poll tick, review sweep or review round retries. This covers every caller: the poller, the review auto-merge step, the review-blocked judge and the `deterministic-skip-merge` job. Their deferral logs now say `reason=base_freshness_unknown` instead of claiming the base moved.

Review/autofix failure markers (`review-autofix-failure:v1`) are read only from the comment's last line, where the pipeline writes them. An older marker quoted earlier in the comment, for example inside an echoed "First error" line, can no longer hide the real marker, slip past the identical-failure cap, or link an unrelated run to a heal report. The cap comment's "First error" lookup in `review_autofix.yml` uses the same rule.

| The numbers that matter | Value |
| --- | --- |
| New log key | `ORCH_MERGE_HEAD_BOUND pr=<n> head_sha=<sha> mode=auto\|sync outcome=enabled\|merged\|failed\|refused` |
| New freshness action | `action=defer` for `outcome=unknown` |
| New judge skip reason | `judge_skip_reason=base_freshness_unknown` |

What this means for operators: during a GitHub API outage, merges wait instead of going ahead; `MERGE_BASE_FRESHNESS_ENABLED=false` remains the kill switch for the freshness gate.
