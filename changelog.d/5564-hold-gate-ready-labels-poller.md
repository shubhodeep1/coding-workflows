<!-- changelog: security -->
- **A `hold` claim now stops a `claude/*` PR from getting `ai:ready-to-merge` labels and from being merged by the orchestrator poller.** Before this, the merge hold gate ran only when the review workflow enabled auto-merge itself.

With the `ENABLE_AUTO_MERGE` repo variable off, or on the `e2e-smoke-test` path, `review_autofix.yml` still marked a `claude/*` PR's linked issues `ai:ready-to-merge` without checking claims. Then `scripts/orchestrate_poll_process.sh` merged the ready PR, even when its head carried a `hold` claim or broke `workflow-templates/.claude/` twin parity (issue #5564). Both label paths (`scripts/review_enable_auto_merge.sh` and the `deterministic-skip-merge` job) now run `scripts/claude_merge_hold_gate.py` for `claude/*` heads before they authorize labels. The poller runs the gate right before its two ready-to-merge merges, and merges a `claude/*` PR only with `--match-head-commit` on the head the gate allowed.

| The numbers that matter | Value |
| --- | --- |
| Label paths now gated | 3 (helper with auto-merge off, helper `e2e-smoke-test` exit, deterministic skip with auto-merge off) |
| Poller merges now gated | 2 (current-wave and prior-wave `ai:ready-to-merge` merges) |
| Extra API calls for non-`claude/*` PRs | 0 |
| New log key | `ORCH_MERGE_HOLD_GATE pr=<n> head_sha=<sha> action=allow\|refuse reason=<…>` |

What this means for operators: a held `claude/*` PR stays unlabelled and unmerged until a push lifts the hold, whatever `ENABLE_AUTO_MERGE` is set to. `orchestrate_poll.yml` now reads `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, so holds count the same way in the poller as in the review workflow. A support checkout without the gate refuses these merges (fail closed).

### For contributors

The poller helper is `_orch_claude_merge_hold_gate_allows`. It reads the gate from `CLAUDE_MERGE_HOLD_GATE_SCRIPT`, then from `.codex-workflow-src/scripts/`, then from `.codex-workflow-src-main/scripts/`, and passes the PR object the poller already fetched as `--pr-json`. The poller's judge, stall, and noop force-merges are not gated.
