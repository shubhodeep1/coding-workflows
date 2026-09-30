<!-- changelog: fixed -->
- **The review workflow no longer auto-merges a `claude/*` PR that is on hold or missing its `.claude/` twin sync.** `review_autofix.yml` now re-reads the PR's claims just before it enables auto-merge. It refuses the merge when the head carries a live `hold` claim, or when the PR changed a `workflow-templates/.claude/` twin without the matching `.claude/` copy.

A twin-first `/implement-plan-claude` phase edits only the twins, then posts a `hold` claim and waits for a `[claude-twin-sync]` commit. The review run for that head often started before the hold was posted. When it came back clean it enabled head-bound auto-merge, and the PR merged without its `.claude/` copy. PR #5301 merged 13 minutes after its hold was posted, with six `.claude/` files missing. PR #5182's twin sync landed on a branch that had already merged. The new gate, `scripts/claude_merge_hold_gate.py`, runs in `scripts/review_enable_auto_merge.sh` and in the `deterministic-skip-merge` job. It uses the same claim trust rules as `.claude/scripts/check_in_status.py`: only claims posted by the PR author or `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` count, and the latest claim on the current head decides.

| The numbers that matter | Value |
| --- | --- |
| New log key | `AUTOFIX_AUTO_MERGE_SKIPPED pr=<n> head_sha=<sha> reason=hold_claim\|twin_parity\|gate_unavailable` |
| PRs checked | `claude/*` heads only |
| Extra API calls per `claude/*` merge | 1 per 100 PR comments, 1 compare, +2 tree reads only when a twin changed |
| Extra API calls for other PRs | 0 |
| Failure mode | fail closed: a read the gate cannot complete refuses the merge |

What this means for operators: a held `claude/*` PR, or a twin-first phase PR whose stage died before posting its hold, stays open instead of merging. The push that lifts the hold, or that adds the `.claude/` copy, starts a new review run, and that run merges the PR once the gate passes. A hold on an older head, or a hold lifted by a newer claim, does not block.

### For contributors

The twin check compares blob ids from two recursive `git/trees` reads, at the merge base and at the head. It refuses only for a pair the PR took out of parity, so the intentionally divergent consumer command twins never block. The review-blocked judge's merges are not gated because that judge never runs in Claude-fixer mode. `tests/test_claude_merge_hold_gate.py` covers the gate, the helper, and the deterministic-skip step, and runs in the `ci.yml` Claude-fixer claims step.
