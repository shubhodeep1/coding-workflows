<!-- changelog: fixed -->
- **A PR whose description quotes the skip-AI marker is now reviewed, every review-gate skip says why, and a `claude/*` PR whose review never happened is handed to a fixer after 2 hours.** Before, PR #4807 sat unreviewed for about 16 hours and nothing noticed.

The review gate in `.github/workflows/review_autofix.yml` skipped a review whenever `[skip ai]` appeared anywhere in the PR title or body. PR #4807 (phase 1 of #4785) only quoted the marker in its description, so run 36414886805 skipped it with `skip_reason=skip_ai_marker`. The skip logged no reason and left no comment or label. `check_in_status.py --hand-back` saw no hand-off, block, conflict, or failed check, so the checkers reported "waiting" every hour.

Now the marker counts only when it is intentional: in the PR title, or on a description line holding nothing but the marker, outside a code fence. The gate, `review_autofix_sweep.yml`, and the §26.H catch-all (`scripts/claude_pr_sweep.py`) share this rule, and one test holds all three copies to the same cases. Every gate skip logs `AUTOFIX_GATE_SKIP reason=<skip_reason> pr=<n> head_sha=<sha>`. An open `claude/*` PR skipped for the marker, or for `pr_skip_ai=true` on a non-draft, gets one comment per head saying how to undo it. `check_in_status.py --hand-back` reports a new `review-stalled` state for a `claude/*` head that has no review trace after `CLAUDE_REVIEW_STALL_HOURS`. A review trace is a hand-off, a skip notice, auto-merge, the marker, a draft, `ai:merge-queued`, or an active workflow run. `/fix-claude-pr` answers `review-stalled` by re-dispatching the review once.

| The numbers that matter | Value |
| --- | --- |
| Incident | PR #4807, about 16 hours unreviewed (run 36414886805) |
| Stall window (`CLAUDE_REVIEW_STALL_HOURS`) | 2 hours after the head commit |
| Skip notices per head | at most 1 |
| Re-dispatches per stalled head before a hold | 1 |
| New API calls for the stall check | none before its no-call checks; then at most the head-commit read and active-run reads the hand-back mode already budgets |
| Workflows `dispatch_workflow.py` allows | 7 (adds `internal-review.yml`) |

What this means for operators: a PR description can now document the marker without losing its review. A deliberate title marker still skips, and the skip is now visible in the log and, on `claude/*` PRs, as a comment. A `claude/*` PR whose review silently never ran now reaches a fixer within about 2 to 3 hours with no human involved. To change the window, set the repository variable `CLAUDE_REVIEW_STALL_HOURS` for the hourly catch-all, and the same name in the §26 checker and fixer session environments, which read it from there (default 2). The `.claude/settings.json` and `gh_api_write_guard.py` allow lists gain `internal-review.yml`, and consumer repos receive the change with the next `.claude/` sync.

### For contributors

The Python rule is `has_skip_ai_marker` in `.claude/scripts/check_in_status.py`, and the workflows carry an identical `SKIP_AI_BODY_AWK` program; `tests/test_skip_ai_marker_rule.py` checks the three copies against one case table and runs the gate's notice block in bash against a fake `gh`. The notice ends in `<!-- ai:claude-fixer-review-skipped:v1 reason=<reason> head=<sha> -->`, which the existing `gate_fetch_marker_comments` lookup returns, so dedupe costs no extra call. No label is added, because `ai:review-skipped` means the deterministic doc-only or size skip. `review-stalled` reuses the `review` claim kind. The verdict's `stall_redispatched` field tells a second fixer that the head's review was already re-dispatched, so it holds instead of looping. The stall check is off when `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` is unset. The project checker's plain `--pr` mode is unchanged; the hourly catch-all covers `/implement-plan-claude` PRs.
