# Implement-Plan Log — Deflake the stall-guard observe-only tests on loaded CI runners

- Plan: docs/plans/issue-5119-deflake-stall-guard-observe-test-plan.md
- Source issue: shubhodeep1/coding-workflows#5119 (https://github.com/shubhodeep1/coding-workflows/issues/5119)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5119-deflake-stall-guard-observe-test   Final PR: pending (opened right after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from main at f736cad; phase 1 implementing

## Phases
1. [ ] Phase 1 — deterministic observe-only stall tests (`tests/test_codex_stall_guard_scripts.py`)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the observe-only contracts test stop racing the guard? — Picked: A — the child waits (bounded, 15 s) for the guard's `state=observed` status file, then exits. Alternatives: B — `time.sleep(1.3)` → `time.sleep(2.5)` as the issue's example; C — a larger fixed sleep such as 4 s. Why: the guard's idle clock starts at the read of the child's last output, so a stall delays both ends and any fixed sleep keeps a load threshold (B failed 2 of 8 runs under simulated 1.2 s stalls); A is deterministic, keeps every assertion, and shortens the suite. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Does `test_codex_stall_guard_observe_only_records_event_idle_without_killing_child` (child `time.sleep(2.4)` against a 1 s stall timeout) get the same fix? — Picked: A — yes, the same status-file wait. Alternatives: B — leave it. Why: it is the same sleep-versus-timeout pattern the issue asks to fix and failed 1 of 15 runs under simulated 1.2 s stalls. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is `test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present` (child `time.sleep(2.4)` against the 1 s heartbeat interval) fixed? — Picked: A — widen the sleep to 3.5 s. Alternatives: B — leave it; C — make it deterministic by having the guard write heartbeats to a file. Why: heartbeats go only to the guard's stderr, so there is nothing for the child to wait on without a guard change (§5); 3.5 s widens the window from 1.4 s to 2.5 s for 1.1 s more runtime. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Update the #5119 rows in `docs/operations/master-session.md`? — Picked: A — no; the master session owns that doc and marks rows fixed after merge. Alternatives: B — mark the rows fixed in the phase PR. Why: B would claim a fix before it merges and conflict with the master's frequent edits (§5). Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5119: deliver`); invoking session session_013Yp7ahUmRsRttvAvzppXjB.
- The invoking session started with no `gh` CLI and no GitHub MCP tools (the repository was attached after start, so the SessionStart hook had not run). It ran `.claude/hooks/session-start.sh` to install `gh`; PR and comment writes use `gh api` REST (§23.H routine writes) in place of the `mcp__github__*` tools.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
