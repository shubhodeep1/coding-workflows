<!-- changelog: security -->
- **A `hold` claim now stops a `claude/*` PR from getting `ai:ready-to-merge` labels and from being merged by the orchestrator poller.** Before this, the merge hold gate ran only when the review workflow enabled auto-merge itself.

With the `ENABLE_AUTO_MERGE` repo variable off, or on the `e2e-smoke-test` path, `review_autofix.yml` still marked a `claude/*` PR's linked issues `ai:ready-to-merge` without checking claims. Then `scripts/orchestrate_poll_process.sh` merged the ready PR, even when its head carried a `hold` claim or broke `workflow-templates/.claude/` twin parity (issue #5564). Both label paths (`scripts/review_enable_auto_merge.sh` and the `deterministic-skip-merge` job) now run `scripts/claude_merge_hold_gate.py` for `claude/*` heads before they authorize labels. The deterministic-skip job also re-reads the PR before it enables auto-merge, so a branch renamed to `claude/*` after the review's snapshot meets the gate there too. The poller re-reads the PR right before its two ready-to-merge merges, runs the gate for a `claude/*` head, and merges such a PR only with `--match-head-commit` on the head the gate allowed.

| The numbers that matter | Value |
| --- | --- |
| Label paths now gated | 3 (helper with auto-merge off, helper `e2e-smoke-test` exit, deterministic skip with auto-merge off) |
| Poller merges now gated | 2 (current-wave and prior-wave `ai:ready-to-merge` merges) |
| Extra API calls per deterministic skip | 1 `pulls/{n}` read (reused by the gate as `--pr-json`) |
| Extra API calls per poller ready-to-merge attempt | 1 `pulls/{n}` read (reused by the gate as `--pr-json`) |
| New log key | `ORCH_MERGE_HOLD_GATE pr=<n> head_sha=<sha> action=allow\|refuse reason=<…>` |

What this means for operators: a held `claude/*` PR stays unlabelled and unmerged until a push lifts the hold, whatever `ENABLE_AUTO_MERGE` is set to. `orchestrate_poll.yml` now reads `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, so holds count the same way in the poller as in the review workflow. A support checkout without the gate refuses these merges (fail closed). If that re-read fails, or the head moved since the checks ran, the deterministic skip does not enable auto-merge and the poller defers the merge to its next tick.

### For contributors

The poller helper is `_orch_claude_merge_hold_gate_allows`. It reads the gate from `CLAUDE_MERGE_HOLD_GATE_SCRIPT`, else from `.codex-workflow-src/scripts/`, the checkout the poller itself is staged from (`orchestrate_poll.yml` deletes `.codex-workflow-src-main` before the poller runs, so that snapshot is not a fallback), and passes the PR object the poller already fetched as `--pr-json`. The poller's judge, stall, and noop force-merges are not gated. Every gated path decides whether a head is `claude/*` from the head ref it reads together with the fresh head SHA, so a branch renamed to `claude/*` after the review's metadata snapshot still meets the gate.
