<!-- changelog: security -->
- **`replaced-sessions` no longer archives a session just because a blocked comment names it.** A forged `ai:claude-blocked:v1` comment could get an idle, working session archived on the next `/reclarify` (issue #5063).

When the Claude issue pickup starts the replacement for a `/reclarify`, `scripts/claude_issue_route.py replaced-sessions` picks the old sessions to archive. Before this fix, a collaborator could post a blocked comment naming any idle session of the issue, such as a stage session holding a hand-back Routine, and the named path skipped the blocked-state check. Now the named session needs the same blocked evidence as every other candidate: `status_bucket` `SESSION_STATUS_BUCKET_BLOCKED`, or `BLOCKED` in its title. A blocked comment also names a session only when a trusted author posted it through the Claude GitHub App (`performed_via_github_app.slug` `claude`), which is how Claude sessions post. A hand-written comment can neither name a session nor hide an earlier genuine one.

| The numbers that matter | Value |
| --- | --- |
| Sessions archived per `/reclarify` | at most 5, unchanged |
| New GitHub API calls | 0 |

What this means for operators: a session is archived only when it is idle, blocked, and belongs to the issue. A blocked session that the platform reports as `REVIEW_READY` now stays open until you archive it yourself.
