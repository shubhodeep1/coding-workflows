<!-- changelog: fixed -->
- **The unblock judge no longer resumes an issue that planning found nothing to implement in, and a failed heal issue create now logs why.**

Issue #8470 was a CI-triage issue whose only fix was a CI re-run. Planning stopped with `work is already on the planning ref; nothing to implement`. The unblock judge filed fix-up #8471, and once it merged posted `/answer`, so planning stopped again with the same reason and paged as CRITICAL. Now, when a fix-up is done and the newest pipeline "Planning blocked" comment on a standalone issue gives that reason, `scripts/unblock_judge.sh` closes the issue as completed with `ai:closed` and an evidence comment instead of resuming it. Separately, `scripts/workflow_failure_heal_intake.sh` used to drop the stderr of `gh issue create`: run 41071146143 logged `issue_create_failed` for shubhodeep1/binance-blessings#459 with no reason. The line now carries GitHub's error text and the title and body sizes.

| The numbers that matter | Value |
| --- | --- |
| Planning runs #8470 spent on the same block | 2 |
| New kill switch | `UNBLOCK_NOTHING_TO_IMPLEMENT_CLOSE_ENABLED` (default `true`) |
| Error text kept on a failed heal issue create | last 300 bytes, one line |

What this means for operators: a nothing-to-implement issue closes itself once its fix-up lands, instead of coming back as "Planning blocked: human input required". The next `issue_create_failed` page comes with a reason you can act on. Orchestrator items still resume as before.

### For contributors

The close runs only where the judge would post the resume command, after the protected-path handoff and the operator-step hold. It writes at most three times (comment, label, close) and reads nothing new. A failed write keeps the fix-up wait open, and the trusted marker `<!-- ai:unblock-nothing-to-implement:v1 item=<n> fixup=<f> -->` stops the comment from being posted twice. Log: `UNBLOCK_JUDGE ... op=nothing_to_implement outcome=closed|skip`.
