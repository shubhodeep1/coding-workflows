<!-- changelog: fixed -->
- **Claude-routed issues now get their full issue-mode project again: an hourly pickup session starts the implementation session instead of the routine.** A session that cannot run the chain now stops on the issue instead of shipping a direct edit.

The smoke test on issue #4525 showed that a claude.ai routine run has no claude-code-remote tools (`create_session`, `send_later`, `get_session`, `add_repo`). So the "Claude issue dispatcher" could not start the implementation session. Its fallback implemented the issue in-session as auto-decision AD-1: no plan, no project branch, and no conformance, security or validation pass. `claude-issue-intake.yml` now queues each routed issue as an `ai:claude-issue-queue` issue in coding-workflows. The new `/claude-issue-pickup` session, woken hourly by a trigger bound to itself, starts the Opus `/implement-issue-claude` session for each item and closes it. `claude-issue-queue-watchdog.yml` alerts when an item waits too long. `claude-issue-dispatch.md`, `/implement-issue-claude` and CLAUDE.md §28.C now fail closed with `ai:claude-blocked` when the session tools are missing. The routed comment also reports reason `default` when `AI_ISSUE_IMPLEMENTER` is unset.

| The numbers that matter | Value |
| --- | --- |
| Longest wait before the implementation session starts | about 60 minutes (one pickup wake) |
| Sessions started per pickup wake | at most 10 (the rest wait for the next wake) |
| Watchdog cadence / stale threshold | hourly at :17 / `CLAUDE_ISSUE_QUEUE_STALE_HOURS`, default 3 |
| New labels | `ai:claude-issue-queue`, `ai:claude-issue-queue-stale` |
| Deprecated, unused | `CLAUDE_ISSUE_ROUTINE_ID`, `CLAUDE_ISSUE_ROUTINE_TOKEN`, `CLAUDE_ISSUE_ROUTINE_BETA` |

What this means for operators: start the pickup once by running `/claude-issue-pickup start` in a claude.ai cloud session on coding-workflows that you open from the app, in Auto mode. It refuses to start more than 3 session links deep, because the implementation chain needs 4 more links below it and the session tools stop at 8. If Telegram reports stale queue items, run `/claude-issue-pickup start — restart`. The "Claude issue dispatcher" routine and its variable and secret can be deleted. Consumer repos need no change.

### For contributors

`scripts/claude_issue_route.py` gains `queue-issue`, `queue-pending` (with `--fetch-repo`, one REST read) and `queue-stale`. Only queue issues opened by `github-actions[bot]` for registered repos are acted on. The queue issue is created with `GITHUB_TOKEN`, so neither it nor the pickup's close starts a workflow. `clarify.yml` passes `vars.AI_ISSUE_IMPLEMENTER || ''`. Tests are in `tests/test_claude_issue_route.py` and `tests/test_implement_issue_claude_command.py`; the removal-registry entries are in `docs/scripts-pending-removal.md`.
