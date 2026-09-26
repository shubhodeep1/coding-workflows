<!-- changelog: changed -->
- **Claude, not GPT, now fixes every `claude/*` pull request, and the session that pushed it is woken to do the fix.** Merge conflicts, failed checks, review findings, and block labels on these PRs reach the pushing Opus session through its §26 check-in, and an hourly catch-all starts a fresh fixer for any PR nobody handled.

`review_autofix.yml` used to run Claude-fixer mode only on `claude/implement-plan-*` heads. Every other Claude PR went through the GPT editor and conflict resolver, and its §26 checker only reported when the PR merged or closed. Now every PR-backed `claude/*` head runs in Claude-fixer mode: the reviewer panel still reviews, and the workflow hands the round to Claude. The hourly Sonnet checker runs `.claude/scripts/check_in_status.py --hand-back` and, when a fix is due, wakes the session that pushed the PR. That session follows the new `/fix-claude-pr` command: it claims the head, fixes, verifies, and pushes. When that session is gone, the checker starts a fresh Opus 5.5 session at high effort instead. The new `claude-pr-catch-all` job in `.github/workflows/review_autofix_sweep.yml` checks this repo and every registered consumer hourly, and starts a fixer through the Claude dispatcher routine for any fix that has been due for 2 hours with no live claim. `/implement-plan-claude` stage, fixer, and `/deploy-activate` sessions now always start on Opus 5.5 at high effort.

| The numbers that matter | Value |
| --- | --- |
| PRs in Claude-fixer mode | every PR-backed `claude/*` head (was `claude/implement-plan-*` only) |
| Checker cadence | hourly, unchanged |
| Catch-all | hourly, cron `17 * * * *`, this repo plus the 13 repos in `.github/ai/consumer_repos.json` |
| Catch-all wait | `CLAUDE_PR_SWEEP_MIN_AGE_HOURS`, default 2 (a `claude/implement-plan-*` failed check waits the chain's 6 hours) |
| Claim lease | `CLAUDE_FIX_CLAIM_LEASE_HOURS`, default 3, ended early by a push |
| Conflict, CI, and block fixes per PR before a hold | `CLAUDE_FIX_HAND_BACK_CAP`, default 3 |
| Stage and fixer sessions | `claude-opus-5-5`, `/effort high` |

What this means for operators: a Claude PR that goes red or conflicts is fixed by Claude within about an hour, usually by the session that wrote it. After three conflict, CI, or block fixes on one PR, the fixer parks it with a hold claim, sends one push notification, and asks you. The catch-all needs the `CLAUDE_ISSUE_ROUTINE_ID` variable and `CLAUDE_ISSUE_ROUTINE_TOKEN` secret the Claude issue implementer already uses; without them it only logs `::warning::` lines. `CLAUDE_FIXER_ENABLED=false` sends every Claude PR back to the GPT path. A Claude PR opened before this change, whose checker still runs the old `--terminal-only` instructions, is picked up by the catch-all.

### For contributors

Claims are PR comments ending in `<!-- ai:claude-fix-claim:v1 head=<sha> kind=<conflict|ci|review|blocked|hold> by=<claimant> -->`, written by `.claude/scripts/claude_fix_claim.py` and read by `read_fix_claims` in `check_in_status.py`. Only owner, member, and collaborator comments count, timed by `created_at`. A PR keeps one §26 checker; a second interested session registers with it through a `PR #<n> status check-in: subscriber` trigger. The dispatcher (`.claude/commands/claude-issue-dispatch.md`) accepts a `claude_pr_fix.v1` payload next to `claude_issue.v1`, and starts every session in two steps (`/effort high` alone, then a one-shot `dispatch <owner>/<repo>#<n>: start` trigger). `stale_routines.py` now also sweeps those ended start triggers. New tests: `tests/test_check_in_status_hand_back.py` and `tests/test_claude_pr_sweep.py`.
