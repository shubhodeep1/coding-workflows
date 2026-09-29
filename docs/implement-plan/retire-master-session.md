# Implement-Plan Log — Retire the master session: projects resolve their own escalations, resumes and guard syncs

- Plan: docs/plans/retire-master-session-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-retire-master-session   Final PR: pending (opened after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/4
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Project branch opened from main at f736cad; phase 1 (escalation judge) starts under the automatic twin-first default.

## Phases
1. [ ] Phase 1 — Escalation judge   — protected paths: `.claude/commands/escalation-judge.md` (new), `.claude/scripts/escalation_ledger.py` (new), `.claude/commands/implement-plan-claude.md`, `.claude/settings.json`
   - [ ] `workflow-templates/.claude/scripts/escalation_ledger.py` (`fingerprint`, `allowed`, `record`; exit 2 on a malformed log line), allowlisted in the `settings.json` twin (plain and `PYTHONDONTWRITEBYTECODE=1`)
   - [ ] `workflow-templates/.claude/commands/escalation-judge.md` (inputs, steps 1–8, never-list)
   - [ ] `implement-plan-claude.md` twin: the ten §28.C failure-escalation stops carry `kind=escalation stop=<id>` and hand the checker an `escalation` wait; `escalation` wait type in the checker instructions; `## Escalations` in the log template; "Never auto-decided" recap and Issue Mode bullet updated
   - [ ] CLAUDE.md §28.G (new) and a §28.C pointer sentence
   - [ ] `agents.md` ("Interactive slash-command model selection", "Interactive post-push PR status check-in")
   - [ ] Tests: `tests/test_escalation_ledger.py`, `tests/test_escalation_judge_command.py` (own `ci.yml` steps); `tests/test_claude_md_section_numbers.py` extended for §28.G
   - [ ] Changelog fragment
   - Done: every §28.C escalation stop hands off to the judge; `escalation_ledger.py` passes its tests; template parity passes after the twin sync
2. [ ] Phase 2 — Blocked-issue sweep and automatic resume   — protected paths: `.claude/scripts/claude_blocked_sweep.py` (new), `.claude/commands/claude-issue-pickup.md` (no twin), `.claude/commands/implement-plan-claude.md`, `.claude/commands/claude-issue-dispatch.md`, `.claude/scripts/dispatch_workflow.py`, `.claude/settings.json`
   - [ ] `claude_blocked_sweep.py` (`scan`, `decide`) with the §15 docstring contract; allowlisted
   - [ ] Pickup step 3c; `claude-issue-intake.yml` in `DISPATCHABLE_WORKFLOWS` and the `gh workflow run` allow rule
   - [ ] Q8 stops write `kind=ask-first|no-tools|depth-limit`; every blocker writes `ai:claude-blocked-session:v1`
   - [ ] Watchdog page step (`scripts/claude_issue_queue_watchdog.sh`, `claude-issue-queue-watchdog.yml`)
   - [ ] Tests (`tests/test_claude_blocked_sweep.py`, dispatch and watchdog tests) with `ci.yml` steps; `agents.md` item 15 and README; changelog fragment
   - Done: an answered blocker gives `wake`/`requeue`; a human-only blocker gives one `page`; `judge` degrades to `page` without `escalation-judge.md`
3. [ ] Phase 3 — Guard-change classifier   — precondition: `scripts/claude_twin_sync.py` on `main` (#4785)
   - [ ] `scripts/claude_guard_change_classifier.py`; call from `scripts/claude_twin_sync.py`; tests; `agents.md`; changelog fragment
   - Done: a tightening-only sync auto-merges; any loosening change keeps the approval label
4. [ ] Phase 4 — Alert policy, poller retirement, operator runbook   — protected paths: per the operator's start-up answer (confirmed against the phase's files when it starts)
   - [ ] CLAUDE.md §28.G alert list and §26 note; `docs/operations/operator-runbook.md` (new) and the stub at `docs/operations/master-session.md`; `agents.md` and README references
   - [ ] Retirement steps (only once phases 1 and 2 are on `main`; otherwise `Poller retirement: waiting on phase <n>`)
   - [ ] Tests pinning the runbook and stub; changelog fragment
   - Done: docs merged; poller archived or the wait recorded

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Escalations

## Auto-decisions

## Lessons

## Notes
- Started by the master session (session_01Qt5nTTqhWxcYA4NciTC6DL) through trigger trig_013zGktPPtmZYGZbgKEos3qv, with start-up answers on the operator's behalf: Auto mode (step 0), plan `docs/plans/retire-master-session-plan.md` on `main` at f736cad (step 1), 4 phases (step 3). Operator decisions Q1–Q14 (2026-09-29) are recorded in the plan. There is no source issue: blockers go in the stage report and on the final PR.
- Phase 3: waiting on #4785 (`scripts/claude_twin_sync.py` is not on `main` at f736cad). The other phases do not wait on it.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- The `settings.json` allow rules count as a guard change under the operator's Q9: A, so their sync goes through the operator (twin-sync blocker says so).
- `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` in every status check and in the checker instructions (#5057).
