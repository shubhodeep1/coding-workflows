<!-- changelog: fixed -->
- **Stall recovery no longer clobbers healthy internal review runs, and issues can no longer be auto-closed by a merged PR that merely mentions them.** Two orchestrator-poller guards that existed for exactly these incidents were structurally unable to fire; both are now effective.

On 2026-08-25 the poller re-triggered review for PR #3823 (issue #3816) while its original review run was 137 minutes into a healthy reviewer pass, pushing an empty commit that tripped the stale-base gate and discarded the whole pass. The in-flight review guard's authoritative fallback, `_direct_inflight_review_run_on_branch`, matched workflow names against `AI Review` / `Internal Review` / `Review Autofix` only — but this repo's review workflows are named `Internal: AI Review & Autofix` (`internal-review.yml`) and `Codex PR Self-Healing Semantic Agent` (`review_autofix.yml`), so the guard could never match an upstream review run. The two real names are now recognized by every review-family name matcher in `scripts/orchestrate_poll_process.sh`. The same day, issue #3817 was auto-closed as "merged" although its scope was never implemented: the merged PR #3825 referenced it with a non-closing `Refs #3817`, and the poller's cross-reference linkage adopted that PR as the issue's implementation PR. Merged-state adoption (label reconciliation) and `close_merged_issues_sweep` now verify a candidate through the new `_pr_json_is_issue_implementation_pr` helper — head branch `ai/issue-<n>` or a closing-keyword body reference — before forcing `ai:merged` or closing the issue.

| The numbers that matter | Value |
| --- | --- |
| Review-family name matchers extended | 5 sites in `scripts/orchestrate_poll_process.sh` |
| Workflow names added | `Internal: AI Review & Autofix`, `Codex PR Self-Healing Semantic Agent` |
| Merged-state adoption gates added | label reconciliation + `close_merged_issues_sweep` |
| Extra API cost | 1 `pulls/<n>` fetch per merged cross-referenced sweep candidate |
| Incidents | PR #3823 / issue #3816 (discarded review pass), issue #3817 / PR #3825 (issue closed unimplemented) |

What this means for operators: a `Stall recovery: re-triggered review` warning should no longer appear while a review run is visibly in progress on the PR's branch, and an `ai:orchestrator-managed` issue can only be closed by a PR that actually implements it. A merged PR that merely mentions an issue now shows up as `rejected=not_implementation_pr` in the poll log and, for `ai:merged`-labeled issues with no verifiable implementation PR, falls through to the existing stale-label Telegram alert instead of a silent close.

### For contributors

New helper `_pr_json_is_issue_implementation_pr` (rejects on unverifiable input, since adopting merged state is the destructive act), new structured log keys `LINKED_PR_CROSS_REF_REJECTED` and `CLOSE_MERGED_SWEEP … rejected=not_implementation_pr`, and new contract tests in `tests/test_linked_pr_implementation_guard.py` plus regression cases in `tests/test_retrigger_inflight_direct_fallback.py` and `tests/test_orchestrate_poll_process.py`.
