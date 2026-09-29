# Implement-Plan Log — Claude issue pickup throughput: catch-up wake, higher limit, resumes first

- Plan: docs/plans/issue-4990-pickup-throughput-plan.md
- Source issue: shubhodeep1/coding-workflows#4990 (https://github.com/shubhodeep1/coding-workflows/issues/4990)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4990-pickup-throughput   Final PR: #5030 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented (script, tests, docs, changelog); the `claude-issue-pickup.md` change waits on the Q40 twin sync

## Phases
1. [ ] Phase 1 — pickup throughput: catch-up wake, limit 20 (`CLAUDE_ISSUE_PICKUP_LIMIT`), resumes first, `oldest_waiting`   — protected paths: `.claude/commands/claude-issue-pickup.md` (no twin; diff in the sync blocker per Q40)
   - [x] `scripts/claude_issue_route.py`: `QUEUE_PICKUP_LIMIT = 20`, `resolve_pickup_limit`, two-tier ordering, `oldest_waiting_minutes`, `catch_up_due`, `--wake` / `--now`
   - [x] `tests/test_claude_issue_route.py`: limit, ordering, oldest-waiting, catch-up tests
   - [x] `tests/test_implement_issue_claude_command.py`: pickup catch-up assertions (fail until the twin sync)
   - [ ] `.claude/commands/claude-issue-pickup.md`: catch-up wake, limit, ordering, report fields (diff in the sync blocker; waiting on the twin sync)
   - [x] `README.md`, `agents.md`, `docs/scripts-pending-removal.md`
   - [x] `changelog.d/4990-pickup-throughput.md`

## Conformance

## Security pass

## Validation

## Completion
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
- Protected-path approval: phase 1 — twin-first per Q40, as the issue body (OWNER) prescribes (2026-09-29). No `workflow-templates/.claude/**` twin needs a change; the `claude-issue-pickup.md` diff goes in the sync blocker.
- security_pass_skip.py: `{"skip": false, "label": null, "reason": "no skip label"}` → Security pass: run.
- The invoking session installed `gh` by running `.claude/hooks/session-start.sh` by hand: the repository was attached after session start, so the SessionStart hook had not run.
