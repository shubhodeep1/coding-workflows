<!-- changelog: changed -->
- **When `/reclarify` starts the replacement for a blocked Claude issue session, the Claude issue pickup now archives the old session. Stale sessions no longer sit `IDLE` with a question that was already answered.**

An issue-mode session that stops BLOCKED now writes its own session id in its `ai:claude-blocked:v1` comment, on the line `<!-- ai:claude-blocked-session:v1 id=session_… -->`. The stops that do this are `/implement-plan-claude` issue mode, `/implement-issue-claude` step 0, and CLAUDE.md §28.C. When the pickup (`.claude/commands/claude-issue-pickup.md`) starts a session for a `reclarify` queue item, it runs `scripts/claude_issue_route.py replaced-sessions`, which picks the sessions to archive:
- the session named by the latest trusted blocked comment;
- any other idle, blocked session whose title belongs to the same issue.

The pickup then checks each picked session with `get_session` and archives it only when it is still `SESSION_STATUS_IDLE` under the same title. On 2026-09-28 the supervising session archived 12 such sessions by hand. Until then the master poller kept listing them as attention items.

| The numbers that matter | Value |
| --- | --- |
| Sessions archived per issue per wake | at most 5 |
| Extra GitHub API reads | 1 paginated comment read per started `reclarify` item |
| Session list reads | 1 `list_sessions` (`limit: 100`) per wake, only when a `reclarify` item was started |
| Triggers that archive | `reclarify` only (not `opened`, `manual`, or `pr_fix`) |

What this means for operators: after you answer a blocked issue and comment `/reclarify`, the old session is archived once its replacement starts. These sessions are never archived:
- a checker, `/deploy-activate`, pickup, or poller session;
- a session from another repository;
- the new session;
- a session that is `RUNNING` or `REQUIRES_ACTION`, since a live permission prompt may still be answered.

In a consumer repo the pickup session cannot read the issue's comments, so it matches titles only. A blocked session older than the 100 most recent sessions is left for you to archive by hand.

### For contributors

Selection is pure and unit-tested (`select_replaced_sessions`, `blocked_comment_session`, and `parse_sessions_listing` in `tests/test_claude_issue_route.py`).
- **Named route.** Only a comment from an OWNER, MEMBER, or COLLABORATOR, or from `github-actions[bot]`, can name a session. The named session still has to pass the title whitelist:
  - `issue <repo>#<N> — implement`;
  - `implement-issue-claude — #<N>…`;
  - `implement-plan issue-<N>-…`.
- **Title route.** It also needs blocked evidence: `status_bucket` BLOCKED, or `BLOCKED` in the title.
- **Allow rules.** `.claude/settings.json` gains two `replaced-sessions` allow rules.

This change ships twin-first: the `workflow-templates/.claude/` copies change here, and the supervising session syncs `.claude/` and applies the `claude-issue-pickup.md` edit.
