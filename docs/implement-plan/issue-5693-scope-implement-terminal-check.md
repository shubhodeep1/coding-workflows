# Implement-Plan Log — Scope the release smoke test's "Implement finished" decision to the smoke issue's own runs

- Plan: docs/plans/issue-5693-scope-implement-terminal-check-plan.md
- Source issue: shubhodeep1/coding-workflows#5693
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: stable
- Project branch: claude/implement-plan-issue-5693-scope-implement-terminal-check   Final PR: #5705 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5712
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01BjYNqoKcxG8jz4wxY4r8co   safety net and hand-back: see the stage report (re-armed each stage)
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5712: fixed the one consensus finding (unknown walks repeated every poll); 34/34 guard tests pass

## Phases
1. [ ] Phase 1 — confirm the issue's own Implement runs finished before failing `wait-implement`   — PR #5712 opened 2026-09-30 (waiting); review rounds: 1; interventions: 0
   - [x] `summarize_scoped_impl_runs` helper in the `wait-implement` step (`.github/workflows/test-and-mark-stable.yml`)
   - [x] terminal branch: cached active-run read, issue-scoped walk, fail only on a complete walk with no active issue-scoped run; unknown falls through to the inactivity check
   - [x] behavioural tests in `tests/test_test_and_mark_stable_plan_polling_guard.py`
   - [x] `changelog.d/5693-scope-implement-terminal-check.md`
   - Done when: new and existing tests pass, the workflow parses as YAML, and it stays under 480,000 bytes

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue; plan header `Security pass: skip`)

## Validation

## Completion

## Activation
- n/a (base stable): the project ends after the final merge; the final-merge stage closes #5693 and labels it `ai:merged`

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which runs decide that Implement has finished? — Picked: A — keep the unscoped page-1 poll as the progress signal and trigger, and confirm with a paged, issue-scoped walk before failing. Alternatives: B — walk every poll cycle; C — title-filter the page-1 poll only. Why: the issue's suggested fix, and §15 (the walk runs only at the terminal decision); C loses the run as soon as it leaves page 1, which is this incident. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] A complete walk finds no non-skipped Implement run for the smoke issue: what does the step do? — Picked: A — fail with `status=implement_failed` as before, saying no issue-scoped run ran. Alternatives: B — keep waiting. Why: the fix constraints forbid weakening the guard, and B could poll up to the job's 300-minute limit, because unrelated runs keep resetting the inactivity timer; A's only false-failure risk (the run not indexed 60+ seconds after `/approved`) existed before. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] While the smoke issue's Implement run is active, how is the next terminal decision checked? — Picked: A — remember that run's ID and re-read only that run, walking again once it has completed or cannot be read. Alternatives: B — walk again every cycle. Why: §15 cycle-local caches; one call instead of up to 10. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] What does the failure line say? — Picked: A — keep `::error::All <n> implement workflow run(s) completed but no PR was created` and `status=implement_failed`, with `<n>` the issue-scoped count, and append the issue number, the newest issue-scoped run ID, and its conclusion. Alternatives: B — new wording and a new status value. Why: §6 and the issue's fix constraints (never rename log prefixes); the appended detail makes a genuine Implement failure diagnosable. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] How does an unknown walk keep waiting? — Picked: A — fall through to the loop's existing inactivity check and poll sleep. Alternatives: B — `continue` with its own idle check, like `wait-plan`'s `fail_plan_confirm_retry_if_idle`. Why: §5; the same `PHASE_TIMEOUT` bound without a new helper. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which docs change? — Picked: A — a changelog fragment only. Alternatives: B — also add an `agents.md` paragraph. Why: neither `README.md` nor `agents.md` documents the `wait-implement` terminal check, and #4723 made the same call for this step (§5). Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — review round, 2026-09-30] How does the terminal branch avoid repeating an unknown walk on every poll (PR #5712 review round 1)? — Picked: A — keep a capped walk for the rest of the phase (the created=> window only grows, so no later walk can complete) and retry an unreadable page's walk no sooner than 120 seconds later. Alternatives: B — retry both after a fixed interval; C — reject the finding as a NIT bounded by PHASE_TIMEOUT. Why: §15 forbids per-iteration API calls in poll loops; A removes every useless re-walk and still retries a transient read failure. Applied in: PR #5712. Status: pending review

## Lessons
- [source:intervention] A poll loop that walks paged results only at a terminal decision must also remember an inconclusive walk (a page cap, an unreadable page), or the walk repeats on every poll cycle; a cap over a window that only grows is final for the phase. (files: .github/workflows/test-and-mark-stable.yml)

## Notes
- Issue mode: base branch `stable` from the issue's `Target branch:` line. The final PR targets `stable`, so steps 12–13 do not run (`Activation: n/a (base stable)`), and `forward-merge-stable-to-main.yml` carries the fix to `main`.
- Security pass skipped: `security_pass_skip.py` → `{"skip": true, "label": "ai:workflow-heal", "reason": "ai:workflow-heal: created and labelled by the issue automation"}`.
- Issue progress comment: 5912771960.
