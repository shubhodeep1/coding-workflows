<!-- changelog: changed -->
- **`/implement-plan-claude` now ships a plan phase by phase and drives it through the security and validation gates to completion.** The in-session implement command used to land a whole plan in one PR and stop; it now runs the orchestrator's lifecycle with Claude as the implementer.

Each phase of the plan's *Phases & Merge Strategy* becomes its own PR against the default branch, and the next phase starts only after the previous one merged. Between PR-opened and PR-merged the command waits with a 3-hourly check-in rather than watching the PR: a schedule-less poke Routine bound to the session plus a cron Routine that starts a fresh, read-only `claude-sonnet-5` checker session, which fires the poke only when the PR merged, is blocked, or the awaited workflow run finished. Claude never fixes CI or answers review comments on a healthy PR; it intervenes only on a blocked one (`ai:review-blocked`, `ai:review-autofix-failed`, `ai:needs-human`, closed unmerged, or red / conflicted for six hours with no review run active), under CLAUDE.md §12, at most three times per PR. After the last phase merges the command dispatches `security-audit.yml` (`ai-security-audit.yml` in consumers) and lets the `ai:security` follow-up issues flow through the AI pipeline before re-auditing, then dispatches `internal-validate.yml` (`ai-validate.yml`) in standalone mode and fixes `needs_fixes` diagnoses itself in a validation-fix PR, and only then opens the completion PR that moves the plan doc to `docs/completed/`. Progress lives in `docs/implement-plan/<slug>.md`, so a later session resumes from the recorded stage.

| The numbers that matter | Value |
| --- | --- |
| Check-in cadence | every 3 hours (`0 */3 * * *`), checker model `claude-sonnet-5` |
| Blocked-PR interventions before asking | 3 per PR |
| Security-pass cycles before asking | 5 (matches `MAX_SECURITY_PASS_CYCLES`) |
| Validation cycles before asking | 3 (matches `MAX_VALIDATE_CYCLES`) |
| Stuck-PR threshold | head older than 6 hours, red or conflicted, no review run active |
| Progress log | `docs/implement-plan/<slug>.md` |

What this means for operators: `/implement-plan-claude docs/plans/<slug>-plan.md` is now a fire-and-resume project driver, not a single-PR helper. Expect one PR per phase, then a security audit run, a validation run, and a final completion PR; the session stays idle between check-ins and only wakes on the default model when there is work to do. Re-running the command on the same plan resumes from the log instead of re-implementing merged phases.

### For contributors

The repo-local command and the `workflow-templates/.claude/commands/` copy stay byte-identical, so consumer repos receive the new behaviour on the next `.claude/` sync. `agents.md` records why the Sonnet checker is a fresh-session Routine model rather than a per-command `model:` pin, and `docs/implement-plan/README.md` documents the log directory. Default-branch detection inside the command now uses `gh api repos/<owner>/<repo> --jq .default_branch`; the old `gh repo view … -R` form is rejected by `gh`.
