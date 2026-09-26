<!-- changelog: fixed -->
- **A Claude issue session that cannot run the issue-mode chain now stops and says so on the issue, instead of shipping a direct edit.** The routing comment also reports reason `default` when `AI_ISSUE_IMPLEMENTER` is unset.

The smoke test on issue #4525 showed that the "Claude issue dispatcher" routine run has no claude-code-remote tools (`create_session`, `send_later`, `get_session`, `add_repo`). Its fallback then implemented the issue in-session as auto-decision AD-1: no plan, no project branch, and no conformance, security or validation pass. `.claude/commands/claude-issue-dispatch.md` step 3 and `/implement-issue-claude` step 0 now fail closed. They post one `<!-- ai:claude-blocked:v1 -->` comment naming the blocker and the options, add `ai:claude-blocked`, and end. CLAUDE.md §28.C now lists running the chain, its security pass and its validation as never auto-decided away. `/implement-plan-claude` issue mode no longer uses the local-session fallbacks.

| The numbers that matter | Value |
| --- | --- |
| Smoke-test issue | #4525 (dispatcher session `session_015RqYR4nvFgjzaLLtyshGnb`) |
| Label on a stopped issue | `ai:claude-blocked` |
| Routing reason when the variable is unset | `default` (was `repo_var`) |

What this means for operators: until a session-start path with `create_session` is in place, a routine-dispatched issue waits with `ai:claude-blocked` rather than being merged without its security and validation passes. Start `/implement-issue-claude <issue url>` from a claude.ai cloud session in Auto mode, or add `ai:codex` and comment `/reclarify`.

### For contributors

`clarify.yml` now passes `vars.AI_ISSUE_IMPLEMENTER || ''`, so `scripts/claude_issue_route.py` applies its own `claude` default. Contract tests are in `tests/test_implement_issue_claude_command.py`.
