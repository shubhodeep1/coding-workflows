<!-- changelog: fixed -->
- **Stall recovery no longer closes unrelated pull requests that merely mention a stalled issue, and standalone re-triggers of a failed implementation now actually run.** Only the issue's own implementation PR is closed, and the standalone `retrigger_implement` action swaps the phase label before posting `/approved`.

When orchestrator stall recovery decides to close and re-issue a stuck task, `close_linked_pr` in `scripts/orchestrate_poll_process.sh` enumerates linked PRs from three sources, one of which is the issue's timeline cross-references. GitHub records a cross-reference for any PR whose body mentions `#<issue>`, and the `Refs #<issue>` linkage this repo requires produces exactly that event, so a rule-following tooling fix that cited a stalled issue was treated as the issue's implementation PR and closed unmerged. Every open candidate now passes through `_linked_pr_is_issue_implementation`: it is closed only when its head branch follows the orchestrator convention (`ai/issue-<n>`, `ai/<n>`, `ai-implement-<n>` or `ai-<n>`, optionally with a non-word suffix such as `ai/<n>-<slug>`) or its body carries a GitHub close keyword for the issue. Cross-reference-only PRs stay open and are logged with a new `close_linked_pr: skipping PR #<pr> … (cross-reference only; …)` line. The check reads head, base, and body from the same `pulls/<n>` request that already read the PR state, so it adds no API calls.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls per candidate PR | 0 |
| Lookup strategies still consulted | 3 (timeline, branch name, body keyword) |
| Branch patterns accepted as implementation PRs | `ai/issue-<n>`, `ai/<n>`, `ai-implement-<n>`, `ai-<n>` (plus a non-word suffix such as `ai/<n>-<slug>`) |
| Label swaps before a `retrigger_implement` `/approved` (both arms) | 1 `gh issue edit` call |

The second fix is in the same script. The standalone stall-recovery loop (`run_standalone_stall_recovery`) re-triggered a stalled implementation by posting `/approved` while the issue still carried `ai:implementing` from the failed run, so `implement.yml`'s precheck skipped every re-trigger with `reason=already_implementing` and the run finished in seconds with conclusion `success`, no PR and no diagnostics. The orchestrator-managed loop already swapped the label first. Both arms now call one shared helper, `_reset_implementing_to_awaiting_approval_for_retrigger`, which moves the issue back to `ai:awaiting-approval` with a single `gh issue edit` call before the comment is posted; if the swap fails it logs a `::warning::` and the `/approved` comment is still posted.

What this means for operators: a PR on a `claude/…` or feature branch that references an orchestrator issue with `Refs #<n>` survives that issue's stall recovery. PRs the orchestrator opened on `ai/issue-<n>`, and PRs whose body says `Closes #<n>`, are closed exactly as before. A standalone issue whose implementation run failed is retried by stall recovery instead of burning its stall budget on no-op runs and being closed and re-issued.

### For contributors

`surface_reissue_closed_without_pr` still consults the broad, unfiltered linked-PR set on purpose: it only surfaces a warning and never blocks recovery, so it does not spend one request per candidate to apply the same filter. Its docstring records that choice. Two tests in `tests/test_orchestrate_poll_process.py` run the real bash functions against a stubbed `gh` and pin the close, skip, unknown-state, and one-request-per-candidate behaviours plus the predicate's accept and reject cases. Two further tests pin the label-swap helper's contract (one call, warning and return code 1 on failure) and that both `retrigger_implement` arms call it before posting.
