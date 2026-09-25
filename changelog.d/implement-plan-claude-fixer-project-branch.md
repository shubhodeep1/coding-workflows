<!-- changelog: changed -->
- **`/implement-plan-claude` now builds each project on its own branch and merges it into the default branch only after every stage passed, and on its PRs the Claude session fixes what the review panel finds instead of the GPT editor.**

New `/implement-plan-claude` projects open a project branch `claude/implement-plan-<slug>` with a draft final PR into the default branch, the way the orchestrator uses `orchestrator/project-<N>`. Phase, conformance-fix, validation-fix, and completion PRs target that branch, and the security audit and runtime validation run against it. The final PR is then marked ready, reviewed as a whole, and auto-merged. `review_autofix.yml` gains a Claude-fixer mode for PRs whose head starts with `claude/implement-plan-`. The reviewer panel still reviews every head, but the GPT editor, the GPT conflict resolver, and the review-blocked judge no longer run. The workflow hands findings and conflicts to a fresh Claude stage session, re-reviews each push, and auto-merges once no valid finding is left. The security pass now dispatches with `bypass_weekly_cap`, because the audit's 3-per-week follow-up cap could let a project pass with deferred findings that a later incremental audit never finds again. Check-in sessions, including the CLAUDE.md §26 post-push check-in, now run every hour on Sonnet at low effort instead of every 3 hours.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 60 minutes (was 180) |
| Security follow-ups per UTC week before a finding was deferred | 3; command-dispatched audits now bypass the cap |
| Claude-fixer rounds before the PR is labelled `ai:review-blocked` | `MAX_AUTOFIX_ITERATIONS` (default 5), counting `[claude-autofix]` commits |
| Claude interventions per blocked PR before the command asks | 3 |
| New workflow inputs | `security-audit.yml`: `ref`, `bypass_weekly_cap`; `validate.yml`: `target_ref`; `review_autofix.yml` and both review wrappers: `claude_fixer_converged_head` |
| New repo var | `CLAUDE_FIXER_ENABLED` (default `true`) |

What this means for operators: nothing reaches the default branch from an `/implement-plan-claude` project until conformance, security, validation, and the completion PR have all landed on the project branch and the final PR has cleared review. Projects whose progress log already exists on the default branch finish the old way. Consumer repos need the next `@stable` wrapper sync for `ai-security-audit.yml`, `ai-validate.yml`, and `ai-review.yml` before the command can dispatch the new inputs; until then the command stops with `Status: BLOCKED` rather than auditing or validating the wrong branch. Set `CLAUDE_FIXER_ENABLED=false` to send `claude/implement-plan-*` PRs back through the GPT editor path.

### For contributors

- The hand-off comment is `<!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->`, posted by `scripts/review_autofix_step_claude_fixer_handoff.sh`. The session answers with `<!-- ai:claude-fixer-verdict:v1 head=<sha> -->`. The gate accepts a `claude_fixer_converged_head` dispatch only when the workflow's hand-off and a collaborator's verdict both name the current head. `.claude/scripts/check_in_status.py` reads the same markers and reports `state: review-round` / `state: conflict`.
- `/effort low` is applied only when it is the whole starting prompt of a `create_session` child. That was verified with `get_session` on 2026-09-25, so checkers start with `/effort low` and receive their instructions through a one-shot `create_trigger` two minutes later.
- `/implement-plan-claude` no longer passes `pr_number` to the consumer `ai-validate.yml`, which has no such input and rejected the dispatch.
