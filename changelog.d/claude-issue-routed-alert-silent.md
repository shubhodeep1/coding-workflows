<!-- changelog: changed -->
- **The two Telegram pings sent when an issue is routed to Claude are now off by default.** `🔍 DEBUG: Claude issue handoff dispatched …` and `🔍 DEBUG: Claude issue queued …` no longer arrive unless a repo asks for them.

Both pings fired for every standalone issue routed to Claude and again on every `/reclarify`. That made two messages per routing, and they only confirmed that things were working. They are now gated by a new repository variable, `CLAUDE_ISSUE_ROUTED_ALERT_LEVEL`, which defaults to `SILENT`. The handoff ping comes from the `clarify.yml` "Claude issue handoff" step (`scripts/claude_issue_handoff.sh`) in the repo the issue lives in. The queued ping comes from `claude-issue-intake.yml` (`scripts/claude_issue_intake.sh`) in coding-workflows. Failure alerts from the same scripts, `Claude issue handoff FAILED …` and the intake `ERROR`s, still honour the global `ALERT_MSG_LEVEL` and keep arriving.

| The numbers that matter | Value |
| --- | --- |
| Pings silenced by default | 2 per routed issue (`opened` or `reclarify`) |
| New repository variable | `CLAUDE_ISSUE_ROUTED_ALERT_LEVEL` (default `SILENT`) |
| Failure alerts affected | 0 |

What this means for operators: nothing to do if you don't want these pings. To get them back, set `CLAUDE_ISSUE_ROUTED_ALERT_LEVEL=DEBUG`: on the issue's repo for the handoff ping and on coding-workflows for the queued ping. Consumer repos get the new default on the next `@stable` sync.
