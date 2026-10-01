<!-- changelog: security -->
- **A `hold` claim can no longer park an `/implement-plan-claude` project indefinitely.** The project checker waits on a held PR head for at most 24 hours, then hands the PR back as blocked.

Since issue #5667, the project checker's plain `.claude/scripts/check_in_status.py --pr N` mode reports `state: held` for a trusted `hold` claim on the current head of a `claude/implement-plan-*` PR, ahead of blocking labels, review hand-offs, conflicts, and failed checks. That wait had no end: a hold marker posted by the PR's author without a real blocker kept the checker waiting even on a PR labelled `ai:needs-human` or `ai:review-blocked` (security finding #5927). The wait is now bounded by `CLAUDE_FIX_HOLD_MAX_HOURS`. A hold at least that old, or one whose comment time cannot be read, reports `state: blocked`, which routes to `action: hand_back`: the PR goes through the existing blocked-PR intervention (3 per PR, then a `Status: BLOCKED` ask), whose claim replaces the hold.

| The numbers that matter | Value |
| --- | --- |
| New env var | `CLAUDE_FIX_HOLD_MAX_HOURS`, default `24` (empty, non-numeric, or non-positive values fall back to `24`) |
| Hold younger than the limit | unchanged: `{"done": false, "state": "held", "action": "wait"}` |
| Hold at or past the limit, or undatable | `{"done": true, "state": "blocked", "action": "hand_back", "head_sha", "claim"}` |
| Age measured from | the latest trusted hold's comment `created_at` (set by GitHub) |
| Extra GitHub API calls | 0 |
| Modes changed | plain `--pr` only; `--hand-back` and `--terminal-only` are unchanged |

What this means for operators: a twin-sync blocker still parks its PR, but if nobody pushes the sync within 24 hours the project re-raises it through the blocked-PR intervention instead of waiting silently. Set `CLAUDE_FIX_HOLD_MAX_HOURS` in the checker environment to allow longer waits.

### For contributors

Trust rules are unchanged (issue #4622): only an owner, member, or collaborator posting as the PR's author or as `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` can hold a head. Claude sessions post as the PR's author through the session proxy, so a script cannot tell an automation hold from a hand-written one; the bound in time is the control. The §26.H cap hold in `--hand-back` mode still never expires on the same head. Tests are in `tests/test_check_in_status.py`.
