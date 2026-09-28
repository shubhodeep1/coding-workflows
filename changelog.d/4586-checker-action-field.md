<!-- changelog: fixed -->
- **The Sonnet checker sessions now follow one `action` field from `.claude/scripts/check_in_status.py` instead of interpreting `state`.** A review round or a merge conflict can no longer be misrouted into a hand-back.

On 2026-09-27 the `/implement-plan-claude` project checker for PR #4596 read `{"done": true, "state": "review-round"}` and pulled the stage session's hand-back Routine forward, a step meant only for blocked, closed, or stuck PRs. No review-round stage session started, and a human had to notice. The checker instructions were right; the low-effort model picked the wrong branch. The script now adds `action` (and `next_stage`) to every verdict, and both checker prompts route on that field alone: the "Checker prompt" in `.claude/commands/implement-plan-claude.md` with an explicit state → action table, and the CLAUDE.md §26.C steps. `/fix-claude-pr`, the §26.D hand-back read, and the §26 reminder hook (`.claude/hooks/pr_check_in_reminder.py`) name the same values.

| The numbers that matter | Value |
| --- | --- |
| Plain PR mode actions | `wait`, `hand_back` (blocked, closed, stuck), `next_stage` + `success` (merged) or `review` (review-round, conflict) |
| Run and issue-list mode actions | `wait`, `next_stage` + `success` (completed, resolved) or `block` (failed, blocked) |
| `--hand-back` mode actions (§26 checker) | `wait` (open, claimed, held), `hand_back_fixer` (conflict, review-round, ci-failed, blocked), `hand_back_all` (merged, closed) |
| Read failure (exit 2) | `action: retry` |
| Extra GitHub API calls | 0 (derived from the data already read) |

What this means for operators: a review round or a conflict on an `/implement-plan-claude` PR always starts a fresh `… — review round` stage session, and a §26 checker hands a due fix only to the fixer and a terminal PR to every subscriber, without depending on the checker model reading `state` correctly.

### For contributors

The change is additive: `done`, `state`, `reason`, `kind`, `head_sha`, `claim`, the hand-back counts, and every exit code are unchanged, and `scripts/claude_pr_sweep.py`, which calls `check_pr_hand_back` directly, sees no new field. The whole mapping lives in `route_verdict` and `CHECKER_ROUTE_TABLE`; a done state with no row exits 2 with `retry` rather than guessing. After the third consecutive `retry`, the project checker now starts the `<next stage on block>` stage session with reason `checker could not read state: <error>`; before, it was told to "treat it as done" with no `state` to route on. `tests/test_check_in_status.py` and `tests/test_check_in_status_hand_back.py` cover every row, and the command, CLAUDE.md, and hook tests pin the new prompt wording. The script, `implement-plan-claude.md`, `fix-claude-pr.md`, and the hook are mirrored byte-for-byte under `workflow-templates/.claude/`.
