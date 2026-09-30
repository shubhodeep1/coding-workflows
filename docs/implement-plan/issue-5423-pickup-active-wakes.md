# Implement-Plan Log — Claude issue pickup: 15-minute active wakes while projects are active

- Plan: docs/plans/issue-5423-pickup-active-wakes-plan.md
- Source issue: shubhodeep1/coding-workflows#5423
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5423-pickup-active-wakes   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened; phase 1 starting (twin-first, protected paths).

## Phases
1. [ ] Phase 1 — pickup active wakes: `queue-pending` hot/idle decision (`active_work`, `self_wake`, `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES`), `--wake active`, the stale sweep's pickup one-shot names, docs, changelog — protected paths: `.claude/commands/claude-issue-pickup.md` (no twin; diff in the sync blocker), `.claude/scripts/stale_routines.py` (via its `workflow-templates/.claude/scripts/` twin)
   - [ ] `scripts/claude_issue_route.py`: `resolve_active_wake_minutes`, `fetch_active_work`, `decide_self_wake`, `active` wake kind, new `queue-pending` fields
   - [ ] `tests/test_claude_issue_route.py`: parser, activity reads and call counts, self-wake decision, CLI
   - [ ] `workflow-templates/.claude/scripts/stale_routines.py` + `tests/test_stale_routines.py`: ended pickup one-shots deleted, `… hourly` kept
   - [ ] `tests/test_implement_issue_claude_command.py`: pickup active-wake assertions (pass after the twin sync)
   - [ ] `.claude/commands/claude-issue-pickup.md`: active wake, one-pending rule, external wakes, report fields (diff in the sync blocker)
   - [ ] `README.md`, `agents.md`, `CLAUDE.md` + `workflow-templates/CLAUDE.md` (§26.G, §26.H), `docs/scripts-pending-removal.md`
   - [ ] `changelog.d/5423-pickup-active-wakes.md`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where does the pickup read active work, given the proxy refuses the search API and reaches only attached repositories? — Picked: A — repository-scoped reads in the queue repository only. Alternatives: B — search API across registered repos (403, verified); C — per-repo reads of all registered repos. Why: the only option that works from the pickup session within §15. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which PRs count as an open `claude/implement-plan-*` final PR? — Picked: A — any open PR whose head starts with `claude/implement-plan-`. Alternatives: B — only PRs whose base is not a `claude/implement-plan-` branch. Why: safe superset; B misses issue-mode projects built on another project's branch. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Which queue items count as active work? — Picked: A — any open queue issue from the queue read. Alternatives: B — only bound or deferred items. Why: the issue's wording; pending bindings become work within minutes. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] What does a failed activity read do? — Picked: A — read the remaining sources; hot if any found work, else `unknown` with no active wake. Alternatives: B — treat a failure as hot. Why: fail open to today's behaviour, never a loop. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] How is `CLAUDE_PICKUP_ACTIVE_WAKE_MINUTES` parsed? — Picked: A — unset/empty/non-integer → 15; ≤ 0 → off; else clamped to 5..30. Alternatives: B — no off switch; C — any positive value. Why: never later than the catch-up it replaces; `0` is a rollback without a revert. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] How does the active wake identify itself? — Picked: A — `— wake. — active` and `--wake active`. Alternatives: B — plain `— wake.`. Why: report and future per-hour steps can tell the kinds apart. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] How is "at most one pending self-wake" checked? — Picked: A — an enabled `catch-up` or `active wake` trigger bound to the pickup with `next_run_at` in the future. Alternatives: B — any enabled trigger with those names. Why: the firing one-shot must not block its own successor. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Where is the hot/idle and catch-up choice made? — Picked: A — in the script (`self_wake`), `catch_up_due` unchanged. Alternatives: B — the pickup model combines the fields. Why: "the script decides", testable, §6. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] How are external early wakes handled? — Picked: A — any `— wake.` without a suffix is an hourly wake. Alternatives: B — a separate `— wake. — master` mode. Why: the issue asks for identical behaviour. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-30] Which Routine names does the stale sweep add? — Picked: A — exactly `Claude issue pickup: master wake`, `… catch-up`, `… active wake`, deleted once ended. Alternatives: B — every `Claude issue pickup: …` name. Why: B would match the recurring `… hourly` Routine and triggers other flows own. Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-09-30] How does the phase change the protected files? — Picked: A — twin-first per Q40: sweep twin edited, pickup diff in the sync blocker, hold claim, `BLOCKED`. Alternatives: B — edit `.claude/**` unattended; C — drop the `.claude/` changes. Why: §28.C forbids B; C ships no wake. Applied in: phase 1 PR. Status: pending review
- AD-12 [plan, 2026-09-30] What does the report line show? — Picked: A — add `activity=` and `active_wake=`, and `catch_up=replaced`. Alternatives: B — replace `catch_up=` with `self_wake=`. Why: §6 keeps the existing key. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by `/implement-issue-claude` (session session_01NYcwPTP6EDb17PvLDEwFPi), Auto mode.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
