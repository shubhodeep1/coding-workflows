<!-- changelog: fixed -->
- **`/implement-plan-claude` fixes from its first end-to-end run: validation verdicts, completion PRs, checker status, and stall handling.** Five problems the dummy two-phase project hit are fixed in the command text.

The validation stage looked for a `VALIDATION_FAILURE_SUMMARY` log line that a passing `validate.yml` run never prints; it now reads `validation_status.json` from the `ai-validation-<run-id>-<attempt>` artifact first, then the record step's `STATUS_VALUE`, and treats the summary line as failure-only. The completion PR now always carries `Refs #N` lines (the tracking issue, or the project's merged phase PRs), because `lint-plan-archival.yml` rejects an archival PR that references nothing. The Haiku checker no longer archives itself after starting the next stage, which cut its turn off and left it shown as FAILED; the next stage archives it. A `— source revision` test run now reads the plan and progress log from `origin/<default>`, where they actually live. And the command, `agents.md`, and the safety net now say that only the chain archives its own sessions: archiving a waiting checker by hand kills the project's only pending check-in.

| The numbers that matter | Value |
| --- | --- |
| Dummy project used to find these | plan PR #4307, phase PRs #4308 and #4310, completion PR #4355 |
| Validation verdict sources, in order | `validation_status.json`, record-step env, `VALIDATION_FAILURE_SUMMARY` |

What this means for operators: `/implement-plan-claude` runs no longer need a hand restart after a passing validation or a stale checker, the session list stops showing healthy checkers as FAILED, and to nudge a stalled project you start its next stage session with a `— resume.` block rather than archiving anything.

### For contributors

`tests/test_implement_plan_claude_command.py` (own `ci.yml` step) pins these rules and the byte-identical `workflow-templates/.claude/commands/implement-plan-claude.md` copy.
