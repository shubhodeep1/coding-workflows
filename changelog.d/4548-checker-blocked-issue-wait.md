<!-- changelog: fixed -->
- **The `/implement-plan-claude` checker now stops waiting on a security follow-up whose pipeline gave up.** An issue-list wait ends with `state: blocked` when a waited-on issue is still open and carries `ai:review-blocked`, `ai:review-autofix-failed`, or `ai:needs-human`.

Until now `.claude/scripts/check_in_status.py --issues` only finished once every issue was closed or labelled `ai:merged`. A follow-up whose fix PR was review-blocked never gets there on its own, so the checker kept re-arming every hour and the 24-hour safety net re-ran the same check, with nobody told. This happened with security follow-up #4512 of `heal-deterministic-autofix-failures`, whose fix PR #4516 stayed conflicted and blocked. The checker now starts the `security-pass <k>/5 — blocked` stage, which stops at `Status: BLOCKED` and asks, as step 9 of `.claude/commands/implement-plan-claude.md` now spells out. Closed and `ai:merged` issues still count as resolved first, whatever other labels they carry.

| The numbers that matter | Value |
| --- | --- |
| Labels that end an issue wait as blocked | `ai:review-blocked`, `ai:review-autofix-failed`, `ai:needs-human` |
| Extra GitHub API calls | 0 (the label is read from the existing one-call-per-issue read) |

What this means for operators: a blocked security follow-up now reaches you within about an hour instead of sitting silently until someone notices.

### For contributors

The script and the command file are mirrored to `workflow-templates/.claude/`; `tests/test_check_in_status.py` pins the new verdict, the closed/merged precedence, template parity, and the command-doc wording.
