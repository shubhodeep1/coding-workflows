# Implement-Plan Log — Claude issue pickup throughput: catch-up wake, higher limit, resumes first

- Plan: docs/completed/issue-4990-pickup-throughput-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4990 (https://github.com/shubhodeep1/coding-workflows/issues/4990)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4990-pickup-throughput   Final PR: #5030 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (branch claude/implement-plan-issue-4990-pickup-throughput-complete → project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011kZ19gKQj5B1eX3AXqP8r7   safety net: armed by the completion stage (see its report)   hand-back: armed by the completion stage
- Last updated: 2026-09-29
- Last note: validation cycle 1 passed (run 36558347168, 10/10 tests on 87e733f); project branch synced with main f736cad as 58d834c (docs-only, 282 pickup/command tests pass); completion PR moves the plan to docs/completed/

## Phases
1. [x] Phase 1 — pickup throughput: catch-up wake, limit 20 (`CLAUDE_ISSUE_PICKUP_LIMIT`), resumes first, `oldest_waiting`   — PR #5061 merged 2026-09-29 by the master session (merge 2120e61, Q46: A; #4990 Q1: A) after review round 1 (1 finding, rejected); review rounds: 1; interventions: 0 — protected paths: `.claude/commands/claude-issue-pickup.md` (no twin; diff in the sync blocker per Q40)
   - [x] `scripts/claude_issue_route.py`: `QUEUE_PICKUP_LIMIT = 20`, `resolve_pickup_limit`, two-tier ordering, `oldest_waiting_minutes`, `catch_up_due`, `--wake` / `--now`
   - [x] `tests/test_claude_issue_route.py`: limit, ordering, oldest-waiting, catch-up tests
   - [x] `tests/test_implement_issue_claude_command.py`: pickup catch-up assertions (pass after the twin sync)
   - [x] `.claude/commands/claude-issue-pickup.md`: catch-up wake, limit, ordering, report fields (twin sync 3c69d52 by the master, Q1: A, 2026-09-29)
   - [x] `README.md`, `agents.md`, `docs/scripts-pending-removal.md`
   - [x] `changelog.d/4990-pickup-throughput.md`

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security; audited project branch at 6a73383 after merging main aebaa17; 266 pickup tests pass; full-suite failures identical to main, 114 in 18 unrelated files)

## Security pass
- Cycle 1 — run 36551077188 2026-09-29 (ref: claude/implement-plan-issue-4990-pickup-throughput, incremental aebaa17..6a73383, 10 files): clean — tracker=#3576 findings=0 followups_created=0

## Validation
- Cycle 1 — run 36558347168 2026-09-29 (target_ref: claude/implement-plan-issue-4990-pickup-throughput, validated 87e733f): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 289s); no fix PR

## Completion
- Completion PR (this PR, claude/implement-plan-issue-4990-pickup-throughput-complete) open — doc moved to docs/completed/issue-4990-pickup-throughput-plan.md
- Final PR #5030 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does a catch-up wake know it is one? — Picked: A — it carries `— wake. — catch-up`. Alternatives: B — the identical `— wake.` prompt, told apart by a trigger-history read. Why: a catch-up must never schedule another, and an identical prompt cannot tell the two apart. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Where is the catch-up decided? — Picked: A — the script (`queue-pending --wake`, `catch_up_due`). Alternatives: B — the pickup model reads `remaining`. Why: "the script decides", and it is testable. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How is one catch-up per hour enforced? — Picked: A — schedule only when `catch_up_due` and no enabled `Claude issue pickup: catch-up` trigger is bound to the pickup; `stop` deletes it. Alternatives: B — schedule on every hourly wake with work left. Why: a late catch-up could overlap the next. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does the `start` drain count as an hourly wake? — Picked: A — yes. Alternatives: B — never from `start`. Why: a restart during a backlog is when the catch-up helps. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] How are bad `CLAUDE_ISSUE_PICKUP_LIMIT` values handled? — Picked: A — unset/empty/non-integer → 20, out-of-range clamped to 1..30, explicit `--limit` wins, output reports `limit`. Alternatives: B — fail the read. Why: fail-open keeps the queue draining. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Which entries count as resumes? — Picked: A — issue entries with trigger `reclarify`; `opened`, `manual`, `pr_fix` keep queue order after them. Alternatives: B — `pr_fix` also first. Why: the issue names only `reclarify` vs `opened` (§5). Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] What does `oldest_waiting` measure? — Picked: A — every grouped or deferred item at read time, ignored excluded, `none` when empty. Alternatives: B — only items left after the wake. Why: shows the dead time; ignored items would pin it. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Does the binding read window grow with the limit? — Picked: A — yes, 3 × limit (60 at 20), documented. Alternatives: B — cap at 30 targets. Why: keeps stuck items from starving bound ones. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] How does the phase change `.claude/commands/claude-issue-pickup.md`? — Picked: A — twin-first per Q40 as the issue prescribes: diff in the sync blocker, `hold` claim, stop BLOCKED. Alternatives: B — edit `.claude/**` unattended; C — drop the pickup change. Why: §28.C forbids B; C ships no catch-up. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Tests on the phase head (Python 3.12, full `tests/`, 2 pickup-text tests deselected): 4830 passed, 2 skipped, 40 failed; the same 40 (`test_review_reject_verify.py`, `test_review_issue_ledger.py`, `test_review_parse_consolidator.py`, `test_review_pipeline_integration.py`, `test_implement_post_codex_recovery.py`) fail on clean `origin/main` e0725f1 in this environment. With the pickup diff applied the three pickup suites give 266 passed.
- Protected-path approval: phase 1 — twin-first per Q40, as the issue body (OWNER) prescribes (2026-09-29). No `workflow-templates/.claude/**` twin needs a change; the `claude-issue-pickup.md` diff goes in the sync blocker.
- security_pass_skip.py: `{"skip": false, "label": null, "reason": "no skip label"}` → Security pass: run.
- The invoking session installed `gh` by running `.claude/hooks/session-start.sh` by hand: the repository was attached after session start, so the SessionStart hook had not run.
- Project branch synced with main ce1db50 as 87e733f before validation (clean merge; 282 pickup/command tests pass), and with main f736cad as 58d834c at the completion stage (docs-only merge; 282 passed, 1 skipped).
- Issue progress comment id 5883795392. CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1 (#5057).
