<!-- changelog: changed -->
- **Standalone issues are now implemented by Claude Code by default, in every repo.** A new repository variable, `AI_ISSUE_IMPLEMENTER` (default `claude`), picks the implementer, and setting it to `codex` switches a repo back to the clarify → plan → implement pipeline.

Every issue the AI orchestrator does not manage now goes to one "Claude issue dispatcher" routine. `clarify.yml` routes the issue, claims it with the `ai:claude` label, and sends a `claude-issue` dispatch to coding-workflows. The new `claude-issue-intake.yml` checks the repo against `.github/ai/consumer_repos.json` and fires the routine, which starts an Opus session running `/implement-issue-claude`. That session writes a single-phase plan and opens one PR with `Refs #N`, then hands the issue to `/implement-plan-claude` for conformance, security pass, validation, the completion PR (which closes the issue with `Fixes #N`), and verify-activation. Orchestrator issues, tracking / security-audit / retro issues, and `[E2E …]` release-gate fixtures always stay on Codex. Codex `plan.yml`, `implement.yml`, and standalone stall recovery skip Claude-claimed issues, so the two pipelines never work the same issue.

| The numbers that matter | Value |
| --- | --- |
| Routines needed | 1, for every repo (new consumers are covered once listed in `consumer_repos.json`) |
| Per-repo switch | `AI_ISSUE_IMPLEMENTER=codex` |
| Per-issue switch | `ai:codex` or `ai:claude` label, then `/reclarify` |
| New labels | `ai:claude`, `ai:codex`, `ai:claude-handoff-failed`, `ai:claude-blocked` |
| New coding-workflows settings | variable `CLAUDE_ISSUE_ROUTINE_ID`, secret `CLAUDE_ISSUE_ROUTINE_TOKEN` |
| Fire retries | 408 / 429 / 5xx / network errors, backoff 2, 4, 8, 16 s |

CLAUDE.md gains §28: sessions of `/implement-issue-claude` and of every `/implement-plan-claude` project act instead of asking. They take the option they would have recommended and record it as an assumption. They stop only on a hard blocker: an exhausted cap, a failed security or validation run, an ask-first operation, or a missing credential. Then they comment on the issue, label it `ai:claude-blocked`, and send a push notification.

What this means for operators: create the dispatcher routine and set `CLAUDE_ISSUE_ROUTINE_ID` / `CLAUDE_ISSUE_ROUTINE_TOKEN` in coding-workflows before this reaches `@stable` (README, "Claude issue implementer"). Until then, Claude-routed issues get `ai:claude-handoff-failed` and a comment explaining how to retry or switch to Codex. Nothing is dropped silently.

### For contributors

Routing lives in `scripts/claude_issue_route.py`. A routing error falls back to Codex. The handoff and intake drivers are `scripts/claude_issue_handoff.sh` and `scripts/claude_issue_intake.sh`, with stable log prefixes `CLAUDE_ISSUE_HANDOFF` and `CLAUDE_ISSUE_INTAKE`. Tests: `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, and `test_standalone_stall_recovery_skips_claude_claimed_issues`.
