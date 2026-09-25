<!-- changelog: fixed -->
- **`/implement-plan-claude` fixes from its first end-to-end run: validation verdicts, completion PRs, checker status, stall handling, and prompt-free check-ins.** Five problems the dummy two-phase project hit are fixed in the command text, and every check-in now runs on Sonnet in Auto mode so it stops asking for approval.

The validation stage looked for a `VALIDATION_FAILURE_SUMMARY` log line that a passing `validate.yml` run never prints; it now reads `validation_status.json` from the `ai-validation-<run-id>-<attempt>` artifact first, then the record step's `STATUS_VALUE`, and treats the summary line as failure-only. The completion PR now always carries `Refs #N` lines (the tracking issue, or the project's merged phase PRs), because `lint-plan-archival.yml` rejects an archival PR that references nothing. The checker no longer archives itself after starting the next stage, which cut its turn off and left it shown as FAILED; the next stage archives it. A `— source revision` test run now reads the plan and progress log from `origin/<default>`, where they actually live. And the command, `agents.md`, and the safety net now say that only the chain archives its own sessions: archiving a waiting checker by hand kills the project's only pending check-in.

Every check-in, for `/implement-plan-claude` waits and CLAUDE.md §26 PR check-ins alike, now runs on Sonnet in Auto mode instead of Haiku. The claude-code-remote write tools a checker calls (`send_later`, `create_session`, `archive_session`, the trigger tools) ask for approval on every call outside Auto mode, with only *Deny* / *Allow once* and whatever `permissions.allow` says, and Haiku 4.5 cannot run in Auto mode, so every Haiku re-arm stopped at a prompt. A Sonnet trial checker for PR #4366 ran a first check and a woken re-check with no prompt. The interval stays at 3 hours to keep the Sonnet cost down.

| The numbers that matter | Value |
| --- | --- |
| Dummy project used to find these | plan PR #4307, phase PRs #4308 and #4310, completion PR #4355 |
| Validation verdict sources, in order | `validation_status.json`, record-step env, `VALIDATION_FAILURE_SUMMARY` |
| Checker model | `claude-sonnet-5` in Auto mode (was `claude-haiku-4-5-20251001`) |
| Check-in interval | 180 minutes (unchanged) |
| Sonnet trial cost | about $0.32 for the first check, about $0.13 for a woken re-check |

What this means for operators: `/implement-plan-claude` runs no longer need a hand restart after a passing validation or a stale checker, the session list stops showing healthy checkers as FAILED, check-ins run without you approving each re-arm, and to nudge a stalled project you start its next stage session with a `— resume.` block rather than archiving anything.

### For contributors

`tests/test_implement_plan_claude_command.py` (own `ci.yml` step) pins these rules and the byte-identical `workflow-templates/.claude/commands/implement-plan-claude.md` copy.
