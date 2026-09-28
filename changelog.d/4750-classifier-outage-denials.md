<!-- changelog: fixed -->
- **Auto-mode classifier outages no longer file permission-prompt issues or stall unattended stages.** A tool call the classifier refuses without a verdict (`Classifier unavailable`) is now treated as an outage: it is retried once, then waited out, and it is counted in the stage report instead of being filed.

On 2026-09-28 one classifier outage denied already-allowlisted calls (`get_session`, `git status`, `git fetch`, `grep`) in several `/implement-plan-claude` stages, and `.claude/scripts/permission_prompts.py` filed each denied command shape as its own `ai:permission-prompt` issue, each of which started a full Claude project. The filer now leaves those denials out of its patterns and filing and reports them once under a new `outage_denials` key, shown as `classifier outage: <n> (not filed)` on the report's `Permission prompts:` line. The new CLAUDE.md §23.J tells every session to retry such a call once and, if it is refused again, to arm one `send_later` about 30 minutes out and end the turn instead of asking or stopping at `Status: BLOCKED`. `/implement-plan-claude` resume stages now take their session id from `CLAUDE_CODE_REMOTE_SESSION_ID` and their permission mode from the progress log, so step 0 no longer calls `get_session` on itself.

| The numbers that matter | Value |
| --- | --- |
| Issues filed by the 2026-09-28 outage | 11 (#4750 and the 10 folded into it or filed since) |
| Retries before waiting | 1 |
| Wait after a second refusal | 30 minutes (`send_later`) |
| New allow rules | 0 |

What this means for operators: a classifier outage now costs a delayed stage, not a queue of `ai:permission-prompt` issues and Claude projects. Real denials, and every `PermissionRequest`, are still filed as before.

### For contributors

`permission_prompts.py` recognises an outage with `CLASSIFIER_OUTAGE_REASON_RE`, only on `PermissionDenied` records. Existing summary keys (`total`, `patterns`, `filed`, `commented`, `errors`, `skipped`, `dry_run`) keep their names; `total` now counts only filed-able occurrences. `tests/test_permission_prompts.py` and `tests/test_implement_plan_claude_command.py` cover the split, §23.J, and the step-0 text.
