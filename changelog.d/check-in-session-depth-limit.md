<!-- changelog: fixed -->
- **A §26 PR status checker is no longer created at the session depth limit, where it could never re-arm.** Sessions too deep to create their own checker now ask the Claude issue pickup to create it.

The claude-code-remote tools refuse `create_session`, `create_trigger`, `update_trigger`, and `send_later` from a session 8 parent links below its root. That includes a session re-arming itself. CLAUDE.md §26.B always created the checker one link below the pushing session and never looked at depth. On 2026-09-27 the pushing session for PR #4601 sat at depth 7, because a stage session of an issue-mode project had started it. Its checker landed at depth 8 and stopped after its first read. The chain began at a pickup running at depth 3. §26.B now has a step 1c depth check: a session at depth 6 or 7 sends the pickup an `— arm-check-in` request, and the pickup creates the checker one link below itself. A session at depth 8 or deeper, or one with no pickup running, arms no checker and sends a push notification. The §26.H sweep still covers its `claude/*` fixes.

| The numbers that matter | Value |
| --- | --- |
| Session lineage limit | 8 parent links |
| Deepest session that creates its own checker | depth 5 (checker at 6, fresh fixer at 7) |
| Depths routed through the pickup | 6 and 7 |
| Pickup start depth, before / after | ≤ 3 / ≤ 1 |
| Depth count per arming | at most 8 `get_session` calls |
| API calls for the pickup to parse a request | 0 (`claude_issue_route.py arm-check-in-request`) |

What this means for operators: restart the Claude issue pickup from a session you open in the app (`/claude-issue-pickup start — restart`). The running pickup sits at depth 3. Its hourly wakes do not check depth, so it keeps working, but the chains it starts keep the old, too-small margin until you restart it. `/implement-plan-claude` stage sessions no longer start side sessions for work outside their stage. They record it in the project log, and a separate fix is filed as an issue.

### For contributors

The pickup's new step 5 writes the request's arguments to a file with the file tool and parses them offline. Only a registered repository, a PR number, and a `session_…` id pass. The requester writes the checker's instructions itself (§26.B step 3), so no free text passes through the pickup. Both new trigger names (`PR #<n> status check-in: arm request` and `…: checker ready`) match the stale Routine sweep's `PR #<n> status check-in…` pattern. §26.C step 5 also notifies and leaves the fix to the sweep when a pre-existing deep checker's fresh fixer is refused. Tests: `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_implement_plan_claude_command.py`.
