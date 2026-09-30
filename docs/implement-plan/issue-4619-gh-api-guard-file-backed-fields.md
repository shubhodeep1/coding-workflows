# Implement-Plan Log — gh api guard: prompt for file-backed field values

- Plan: docs/completed/issue-4619-gh-api-guard-file-backed-fields-plan.md
- Source issue: shubhodeep1/coding-workflows#4619
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields   Final PR: #4641 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01SdjPMQ5uLDRMKgrHa86Men   safety net (re-armed each stage)   hand-back (re-armed each stage)
- Last updated: 2026-09-30
- Last note: validation cycle 1 (run 36669567556) passed 10/10 against the project branch; completion PR moves the plan to docs/completed/.

## Phases
1. [x] Phase 1 — file-backed `-F` values and `--input` always prompt in the gh api guard — protected paths: `.claude/hooks/gh_api_write_guard.py` — PR #5004 merged 2026-09-29 (964bc01); twin sync 73ad50e landed 2026-09-29; review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue); re-verified 2026-09-29 with `.claude/scripts/security_pass_skip.py`: `skip: true` (`ai:security: created and labelled by the issue automation`)

## Validation
- Cycle 1 — run 36669567556 2026-09-30 (target_ref: claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 303s)

## Completion
- Completion PR (claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields-complete) open 2026-09-30 — doc moved to docs/completed/issue-4619-gh-api-guard-file-backed-fields-plan.md
- Final PR #4641 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which calls does a file-backed `-F` value make a write? — Picked: A — every call, any method, endpoint, or repository, GraphQL variables included. Alternatives: B — only routine write endpoints. Why: a GET query field or GraphQL variable sends the file off-box too, and the `Bash(gh api repos/*)` allow rule would still approve those. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should file-backed values be allowed from a private generated-content directory (the recommendation's optional half)? — Picked: A — no allowance: every file-backed value prompts, and bodies go through the GitHub MCP tools or an inline `-f body=...`. Alternatives: B — allow `@<path>` that resolves under the session scratchpad. Why: the hook cannot verify a path is private or its contents are safe, and no interactive flow depends on it. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Should `--input` on GET/HEAD, classified read today, also prompt? — Picked: A — yes, `--input` is a write on every method. Alternatives: B — leave it, as outside the finding. Why: same file-read exfiltration path in the same function (CLAUDE.md §12.B). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should `-f`/`--raw-field` values starting with `@` prompt too? — Picked: A — no, they stay as today. Alternatives: B — prompt for them as well. Why: `gh` sends raw fields literally and reads no file. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the existing test fixtures that recorded file-backed calls as allowed or undecided? — Picked: A — keep the observed commands and move them to a new ask expectation. Alternatives: B — delete them. Why: real stage-session commands make the best regression cases. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-09-29] Fix the repeated round-2 nits or reject them? — Picked: A — reject and hold. Alternatives: B — fix them in the hook twin (another twin sync and round). Why: the rejections are correct on the code. Applied in: no code change. Status: pending review
- AD-7 [conformance 1/3, 2026-09-30] /implement-issue-claude step 4 found checker session_01SdjPMQ5uLDRMKgrHa86Men not archived and the log at IN_PROGRESS, but the checker had no pending check-in and the owner's /reclarify asked to continue: stop as "already in progress", or resume? — Picked: A — resume at conformance 1/3. Alternatives: B — stop as already in progress. Why: the log lagged the round-2 BLOCKED stop and nothing was armed, so stopping would stall the project. Applied in: no code change. Status: pending review
- AD-8 [conformance 1/3, 2026-09-30] The sibling path `.claude/scripts/edit_comment.py --body-file` (allowlisted, reads any file into a comment): fix it here or separately? — Picked: A — file it as issue #5452 and keep this project to its plan. Alternatives: B — fix it in a conformance fix PR (twin-first). Why: outside the plan's footprint and non-goals (§5), its own protected path, and its own issue gets its own security pass. Applied in: no code change (issue #5452). Status: pending review

## Lessons
- [source:plan-deviation] A fix to a `.claude/hooks/**` file ships in two steps while Q40 is in force: the stage edits the `workflow-templates/.claude/` twin and its tests, and the tests that load `.claude/` (including template parity) only pass after the supervising session's `[claude-twin-sync]` commit, so verify the twin in a scratch copy with the twin placed at `.claude/` first. (files: workflow-templates/.claude/hooks/gh_api_write_guard.py, tests/test_gh_api_write_guard.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; permission mode auto.
- Resumed 2026-09-29 by the Claude issue dispatcher (`/implement-issue-claude`, session session_01L4i5avJE3z96d5XJoSQCcv, permission mode auto). The earlier stage session is gone; no checker was recorded (`Check-in: none`), so nothing was archived.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29), from the owner's resume comment on #4619 (issuecomment-5882580640): the stage edits only `workflow-templates/.claude/hooks/gh_api_write_guard.py`, opens the phase PR into the project branch, posts a `hold` claim and the `[claude-twin-sync]` blocker on the issue, and stops BLOCKED. `.claude/hooks/**` syncs need the operator's Q62/Q64 approval window.
- Twin verification: in a scratch copy with the twin placed at `.claude/hooks/`, `tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py`, `tests/test_pr_check_in_reminder.py`, and `tests/test_update_workflows_guardrails.py` pass (393 passed). In the real checkout before the sync, 27 guard tests fail as expected: 26 new file-backed cases against the old hook, plus `test_template_parity`.
- Session environment: no `mcp__github__*` tools were attached, so GitHub reads and writes went through `gh` (installed by `.claude/hooks/session-start.sh`) over REST, with inline `-f` fields only.
- Stale Routine sweep 2026-09-29: the Auto-mode classifier denied deleting `trig_01Y1gp9ViVXk3gMqKmWBRUrp` (`dispatch …#4969: deliver`, ended); left for a later sweep.
- Twin sync 73ad50e landed 2026-09-29 (master session): `.claude/hooks/gh_api_write_guard.py` is byte-identical to its twin. `ai:claude-blocked` removed from #4619; Status back to IN_PROGRESS. Checker session_01SdjPMQ5uLDRMKgrHa86Men. Every `check_in_status.py` call passes `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (#5057).
- Review round 1 (2026-09-29, session session_01TCPp9FSuNWJRP8G8cgQYUv): fixed the reason-text test gap for the `--field` spellings (test-only, no `.claude/` edit, so no twin sync needed). Rejected: the "root hook unchanged" task gap (73ad50e synced it), the redundant GraphQL `--input` conjunct (kept as defence in depth), the hardcoded `-F` in the reason (`-F` is the short name of the same flag), and the changelog count (17 new plus 5 moved = 22 entries; the row now says `new`).
- Main sync 2026-09-30: ce8a06d (`[claude-merge-resolve]`, CLAUDE.md §23.H table conflict: kept both the #4619 write row and main's malformed-jq row), then a clean merge d843ca7 at the validation read stage.
- Validation read stage 2026-09-30 (session session_01Rz4Dy2CxU4zUatadVRmXQN): run 36669567556 concluded success, `validation_status.json` status=pass; no validation-fix PR, so no conformance re-run; opened the completion PR.
