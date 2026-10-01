<!-- changelog: fixed -->
- **The `/implement-plan-claude` project checker now waits on a held PR head instead of starting another review round.** A twin-sync blocker is no longer answered by a fresh Opus stage session that re-derives the same blocker.

A twin-first stage ends by posting a `hold` claim on the head it pushed and a twin-sync blocker on the issue. The review workflow can still review that held head and post a hand-off for it. The project checker runs `.claude/scripts/check_in_status.py --pr N` (plain PR mode), which never read claims, so the hand-off counted as a review round and the checker started another stage while the blocker was open. On PR #5173 a hand-off landed on the held head `25db3d6` 11 minutes before its twin sync. Plain PR mode now reads the same trusted claims `--hand-back` mode reads, and a `hold` on the current head of a `claude/implement-plan-*` PR reports `state: held`, `action: wait`.

| The numbers that matter | Value |
| --- | --- |
| New plain-mode verdict | `{"done": false, "state": "held", "action": "wait", "head_sha", "claim"}` |
| What outranks a hold | merged or closed (labels, hand-offs, conflicts, and failed checks do not); a hold at least `CLAUDE_FIX_HOLD_MAX_HOURS` old (default 24) reports `blocked` instead (issue #5927) |
| What lifts a hold | any push that moves the head, or a newer trusted claim on the same head (a stage resuming on an answer) |
| Heads covered | `claude/implement-plan-*` (other PRs never read comments in plain mode) |
| Extra GitHub API calls, `claude/implement-plan-*` PR without a blocking label | 0 (its one comment listing now comes before the label check and is reused for the hand-off check) |
| Extra GitHub API calls, `claude/implement-plan-*` PR with a blocking label and no hold | 1 per 100 PR comments per check-in (the hold must be ruled out before the label hands the PR back; this path used to return before reading comments) |
| Extra GitHub API calls, any other PR | 0 (plain mode reads no comments for it) |

What this means for operators: a project whose phase or fix PR waits on a `[claude-twin-sync]` copy stays parked until someone pushes the sync. It no longer piles up blocked review-round sessions for the same head.

### For contributors

Trust rules are unchanged (issue #4622): a hold counts only when an owner, member, or collaborator posts it as the PR's author or as `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, and the latest trusted claim on the head decides. `--hand-back` and `--terminal-only` modes, the routing table, and every existing field are unchanged. The command's twin-first rule now says the hold goes on the pushed head in the same step as the blocker. Separately, a project checker was seen re-arming its check-in after starting a stage, against its prompt; that is recorded on issue #5667 and not changed here. Tests are in `tests/test_check_in_status.py`.
