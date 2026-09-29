# Implement-Plan Log — gh api guard: prompt for file-backed field values

- Plan: docs/plans/issue-4619-gh-api-guard-file-backed-fields-plan.md
- Source issue: shubhodeep1/coding-workflows#4619
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields   Final PR: pending
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: twin sync of `.claude/hooks/gh_api_write_guard.py` (Q40) on the phase 1 PR, then `/reclarify` on #4619
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Q40): the fix is in `workflow-templates/.claude/hooks/gh_api_write_guard.py` only; the phase PR is held until the supervising session copies it into `.claude/hooks/` as `[claude-twin-sync]`.

## Phases
1. [ ] Phase 1 — file-backed `-F` values and `--input` always prompt in the gh api guard — protected paths: `.claude/hooks/gh_api_write_guard.py` — phase PR open, held for the twin sync (2026-09-29); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue); re-verified 2026-09-29 with `.claude/scripts/security_pass_skip.py`: `skip: true` (`ai:security: created and labelled by the issue automation`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which calls does a file-backed `-F` value make a write? — Picked: A — every call, any method, endpoint, or repository, GraphQL variables included. Alternatives: B — only routine write endpoints. Why: a GET query field or GraphQL variable sends the file off-box too, and the `Bash(gh api repos/*)` allow rule would still approve those. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should file-backed values be allowed from a private generated-content directory (the recommendation's optional half)? — Picked: A — no allowance: every file-backed value prompts, and bodies go through the GitHub MCP tools or an inline `-f body=...`. Alternatives: B — allow `@<path>` that resolves under the session scratchpad. Why: the hook cannot verify a path is private or its contents are safe, and no interactive flow depends on it. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Should `--input` on GET/HEAD, classified read today, also prompt? — Picked: A — yes, `--input` is a write on every method. Alternatives: B — leave it, as outside the finding. Why: same file-read exfiltration path in the same function (CLAUDE.md §12.B). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should `-f`/`--raw-field` values starting with `@` prompt too? — Picked: A — no, they stay as today. Alternatives: B — prompt for them as well. Why: `gh` sends raw fields literally and reads no file. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the existing test fixtures that recorded file-backed calls as allowed or undecided? — Picked: A — keep the observed commands and move them to a new ask expectation. Alternatives: B — delete them. Why: real stage-session commands make the best regression cases. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A fix to a `.claude/hooks/**` file ships in two steps while Q40 is in force: the stage edits the `workflow-templates/.claude/` twin and its tests, and the tests that load `.claude/` (including template parity) only pass after the supervising session's `[claude-twin-sync]` commit, so verify the twin in a scratch copy with the twin placed at `.claude/` first. (files: workflow-templates/.claude/hooks/gh_api_write_guard.py, tests/test_gh_api_write_guard.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; permission mode auto.
- Resumed 2026-09-29 by the Claude issue dispatcher (`/implement-issue-claude`, session session_01L4i5avJE3z96d5XJoSQCcv, permission mode auto). The earlier stage session is gone; no checker was recorded (`Check-in: none`), so nothing was archived.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29), from the owner's resume comment on #4619 (issuecomment-5882580640): the stage edits only `workflow-templates/.claude/hooks/gh_api_write_guard.py`, opens the phase PR into the project branch, posts a `hold` claim and the `[claude-twin-sync]` blocker on the issue, and stops BLOCKED. `.claude/hooks/**` syncs need the operator's Q62/Q64 approval window.
- Twin verification: in a scratch copy with the twin placed at `.claude/hooks/`, `tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py`, `tests/test_pr_check_in_reminder.py`, and `tests/test_update_workflows_guardrails.py` pass (393 passed). In the real checkout before the sync, 27 guard tests fail as expected: 26 new file-backed cases against the old hook, plus `test_template_parity`.
- Session environment: no `mcp__github__*` tools were attached, so GitHub reads and writes went through `gh` (installed by `.claude/hooks/session-start.sh`) over REST, with inline `-f` fields only.
- Stale Routine sweep 2026-09-29: the Auto-mode classifier denied deleting `trig_01Y1gp9ViVXk3gMqKmWBRUrp` (`dispatch …#4969: deliver`, ended); left for a later sweep.
