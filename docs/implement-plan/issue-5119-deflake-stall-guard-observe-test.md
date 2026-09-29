# Implement-Plan Log — Deflake the stall-guard observe-only tests on loaded CI runners

- Plan: docs/plans/issue-5119-deflake-stall-guard-observe-test-plan.md
- Source issue: shubhodeep1/coding-workflows#5119 (https://github.com/shubhodeep1/coding-workflows/issues/5119)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5119-deflake-stall-guard-observe-test   Final PR: #5151 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (head `claude/implement-plan-issue-5119-deflake-stall-guard-observe-test-phase-1`)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (full file passes, 20/20 under CPU burners, 20/20 under 1.2 s guard stalls, negative control fails in 15 s); phase PR opened

## Phases
1. [ ] Phase 1 — deterministic observe-only stall tests (`tests/test_codex_stall_guard_scripts.py`)   — PR open (waiting); review rounds: 0; interventions: 0

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
- [source:plan-deviation] A test that asserts a watchdog observed something must not race it with a fixed child sleep: the watchdog's idle clock starts when it reads the last output, so a stalled runner shrinks the window from both ends. Have the child wait for the watchdog's own written verdict, bounded under the subprocess timeout. (files: tests/test_codex_stall_guard_scripts.py, scripts/codex_stall_guard.sh)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5119: deliver`); invoking session session_013Yp7ahUmRsRttvAvzppXjB.
- The invoking session started with no `gh` CLI and no GitHub MCP tools (the repository was attached after start, so the SessionStart hook had not run). It ran `.claude/hooks/session-start.sh` to install `gh`; PR and comment writes use `gh api` REST (§23.H routine writes) in place of the `mcp__github__*` tools.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Draft final PR #5151 opened 2026-09-29.
- Phase 1 verification (2026-09-29): `python3 tests/test_codex_stall_guard_scripts.py` passes in 32.1 s (34.6 s before the fix); 57 assert lines before and after, none changed; ruff E,F clean. Stress harness (local, not committed): 4 `yes` burners plus a helper that SIGSTOPs the guard at random moments. Pre-fix 1.3 s sleep with 0.7 s stalls: 10/10 runs failed with the CI assertion. Issue's 2.5 s sleep with 1.2 s stalls: 2/8 failed. Fix with 1.2 s stalls: 20/20 passed; fix with CPU burners only: 20/20 passed. Negative control (a guard that never writes `state=observed`): the unchanged assertion fails after 15.2 s, no `TimeoutExpired`.
