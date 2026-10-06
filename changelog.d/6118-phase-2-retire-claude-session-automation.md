<!-- changelog: removed -->
- **The claude.ai-session Claude automation is gone: no more issue queue, pickup, stage chain, §26 checkers or Claude-fixer hand-offs.** `claude/*` pull requests are now reviewed, fixed and auto-merged by the normal review pipeline like every other PR, and every standalone issue runs clarify → plan → implement.

This is Phase 2 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`. The Claude issue intake and queue watchdog workflows, the `claude-pr-catch-all` sweep job, the Claude routing in `clarify.yml`, the `ai:claude` skip gates in `plan.yml`, `implement.yml` and the stall poller, the implement-plan lessons step in `issue_pr_status.yml`, and the Claude-fixer hand-off in `review_autofix.yml` are removed, together with their scripts, `.claude/` helpers, hooks and tests. `/implement-plan-claude` and `/implement-issue-claude` are now short hand-offs to the Actions pipeline (the Claude-engine label they add takes effect in a later phase), and `/verify-activation` and `/deploy-activate` lose their unattended mode. CLAUDE.md §23.I, §26 and §28 keep their numbers as "Retired" stubs.

Consumer repos are cleaned on the next `@stable` sync. `scripts/ai_labels.py sync-labels` deletes the labels listed in the contract's new `retired_labels` array, and `update_workflows.yml` gains a "Remove retired upstream files" step that deletes each file in `workflow-templates/retired_files.txt` whose sha256 matches a released version, keeping and logging any copy a consumer changed.

| The numbers that matter | Value |
| --- | --- |
| Retired labels deleted by the label sync | 6 (`ai:claude`, `ai:claude-handoff-failed`, `ai:claude-blocked`, `ai:claude-issue-queue`, `ai:claude-issue-queue-stale`, `ai:permission-prompt`) |
| Consumer `.claude/` files the sync removes when unmodified | 11 |
| `review_autofix.yml` size | 442,031 bytes (was 458,436) |
| New GitHub API calls per label sync | 1 `DELETE` per retired label |

What this means for operators: a `claude/*` PR no longer waits for a Claude session; the GPT editor, conflict resolver and review-blocked judge handle it, and auto-merge applies as usual. `claude_fixer_converged_head` is still accepted by `review_autofix.yml` and the review wrapper but ignored, and `CLAUDE_FIXER_ENABLED` is read but unused until the Claude-engine review roles land. The retired repo variables and secrets (`CLAUDE_ISSUE_*`, `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, `CLAUDE_FIXER_VERDICT_BOT_LOGIN`, `CLAUDE_FIX_CLAIM_LEASE_HOURS`, `CLAUDE_FIX_HAND_BACK_CAP`, `CLAUDE_PR_SWEEP_*`, `CLAUDE_REVIEW_STALL_HOURS`, `AI_ISSUE_IMPLEMENTER`) are no longer read and can be deleted from repository settings.

### For contributors

`tests/test_no_session_automation.py` fails CI if `send_later`, `create_session`, `create_trigger`, `claude-issue-queue`, `check_in_status`, `claude_fix_claim`, `stale_routines` or `claude_session_janitor` reappear under `.github/`, `scripts/`, `.claude/` or `workflow-templates/`. `security_pass_skip.py` moved from `.claude/scripts/` to `scripts/` for the single-issue security pass of a later phase. The security dependency hold of #4934 (a generated `ai:security` follow-up waits for the issue named on its `Depends on: #N` line) moved unchanged from the retired Claude issue router to `scripts/security_dependency.py`, with the same `security-dependency` command line; clarify, implement and the stall poller call it. The `claude-fixer-auto-merge` job id is kept but never runs, in case a required check names it.
