<!-- changelog: fixed -->
- **Semble overflow chunks in the targeted-file-context block are now gated by the remaining budget.** Implement runs whose plan named several large files could build a prompt over the editor model's 1,048,576-character stdin cap and fail every attempt.

`scripts/targeted_file_context.py` pre-loads plan-named files into the editor prompt. When a file was too large to inline, the overflow path asked semble for chunks and rendered them, adding the rendered size to the running total only afterwards. Once the total passed `TARGETED_FILE_CONTEXT_MAX_BYTES`, nothing stopped the next overflow file from rendering another full payload, so the cap bounded the inlined files but not the overflow content rendered alongside them. Overflow representations are now checked against the remaining headroom and rendered only when they fit, so source content in the block stays within the budget; once the headroom is gone the semble subprocess is skipped and the remaining files get the existing "read with read tool" marker. The implement workflow also logs the assembled prompt size and warns when it reaches the stdin cap, so an overshoot is visible instead of surfacing as an opaque model error.

| The numbers that matter | Value |
| --- | --- |
| Budget (`TARGETED_FILE_CONTEXT_MAX_BYTES`) | 102,400 bytes |
| Block emitted before the fix (issue #3990) | ~1,164,000 bytes |
| Overflow content rendered before the fix | 1,281,280 bytes across 10 files |
| Editor stdin cap | 1,048,576 characters |
| Failing run | 33796624872 |

What this means for operators: implement, review-autofix, and conflict-resolver runs on plans that name large files no longer die with `turn/start failed: Input exceeds the maximum length of 1048576 characters` and the downstream "no actionable output" bail. The editor reads those files with its own read tool instead, which is the behaviour the marker path always intended.

### For contributors

The read-fallback branch already computed remaining headroom and emitted a marker at zero; the semble branch simply lacked the same guard. New telemetry reason code `budget-exhausted` on `SEMBLE_FALLBACK` distinguishes a budget rejection from a semble failure. Two contract tests in `tests/test_targeted_file_context.py` pin the source-content bound, the bounded framing overhead, and the skip-the-subprocess-at-zero-headroom behaviour.

- **Stall recovery no longer closes unrelated pull requests that merely mention a stalled issue.** Only the issue's own implementation PR is closed.

When orchestrator stall recovery decides to close and re-issue a stuck task, `close_linked_pr` in `scripts/orchestrate_poll_process.sh` enumerates linked PRs from three sources, one of which is the issue's timeline cross-references. GitHub records a cross-reference for any PR whose body mentions `#<issue>`, and the `Refs #<issue>` linkage this repo requires produces exactly that event, so a rule-following tooling fix that cited a stalled issue was treated as the issue's implementation PR and closed unmerged. Every open candidate now passes through `_linked_pr_is_issue_implementation`: it is closed only when its head branch follows the orchestrator convention (`ai/issue-<n>`, `ai-implement-<n>`) or its body carries a GitHub close keyword for the issue. Cross-reference-only PRs stay open and are logged with a new `close_linked_pr: skipping PR #<pr> … (cross-reference only; …)` line. The check reads head, base, and body from the same `pulls/<n>` request that already read the PR state, so it adds no API calls.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls per candidate PR | 0 |
| Lookup strategies still consulted | 3 (timeline, branch name, body keyword) |
| Branch patterns accepted as implementation PRs | `ai/issue-<n>`, `ai-implement-<n>` |

What this means for operators: a PR on a `claude/…` or feature branch that references an orchestrator issue with `Refs #<n>` survives that issue's stall recovery. PRs the orchestrator opened on `ai/issue-<n>`, and PRs whose body says `Closes #<n>`, are closed exactly as before.

### For contributors

`surface_reissue_closed_without_pr` still consults the broad, unfiltered linked-PR set on purpose: it only surfaces a warning and never blocks recovery, so it does not spend one request per candidate to apply the same filter. Its docstring records that choice. Two tests in `tests/test_orchestrate_poll_process.py` run the real bash functions against a stubbed `gh` and pin the close, skip, unknown-state, and one-request-per-candidate behaviours plus the predicate's accept and reject cases.
