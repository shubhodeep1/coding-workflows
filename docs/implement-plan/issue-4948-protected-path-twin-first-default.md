# Implement-Plan Log — Protected-path phases: apply the twin-first rule automatically (interim until #4785)

- Plan: docs/plans/issue-4948-protected-path-twin-first-default-plan.md
- Source issue: shubhodeep1/coding-workflows#4948
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4948-protected-path-twin-first-default   Final PR: #4989 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5008 (phase 1): review round 2
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Ame2D1ptdLmTxyN2aYG1jw (project checker, reused)   safety net and hand-back: in the review-round stage report
- Last updated: 2026-09-29
- Last note: The `[claude-twin-sync]` copy landed in f60a3b1 and `ai:claude-blocked` was removed on 2026-09-29. Review round 1 on f60a3b1 (ledger `f8e2625c…`): both task gaps valid and fixed in one `[claude-autofix]` commit; the sunset test now also covers the agents.md sentence and the master-session.md Q40 clause, and a presence test keeps those markers from drifting. No `.claude/**` file changed, so no twin sync is needed.

## Phases
1. [ ] Phase 1 — interim twin-first default for protected-path phases   — protected paths: `.claude/commands/implement-plan-claude.md` (edited only in its `workflow-templates/.claude/` twin, Q40) — PR #5008 open (twin synced in f60a3b1); review rounds: 1; interventions: 0
   - [x] Twin `implement-plan-claude.md` step 4: interim paragraph (automatic approval line, twin-only edits, no-twin diff + sha256, hold claim + twin-sync blocker, resume, later stages, the three uncovered cases, sunset) — `workflow-templates/.claude/commands/implement-plan-claude.md:35-45` (root copy: pending twin sync)
   - [x] CLAUDE.md §28.C: new bullet naming the interim automatic twin-first default and its sunset (#4785) — `CLAUDE.md:2236-2253`
   - [x] `agents.md` (`:1116-1123`) and `docs/operations/master-session.md` (Q40 row) updated
   - [x] `tests/test_implement_plan_claude_command.py`: 5 new tests (command twin, CLAUDE.md, sunset guard)
   - [x] `changelog.d/4948-protected-path-twin-first-default.md`
   - Done: in the twin overlay the command and changelog tests pass (55 passed, 1 skipped); the real tree fails only `test_template_parity` until the twin sync. Full overlay run: 4720 passed, 122 failed — the same 122 fail on a clean `origin/main` copy (missing `jsonschema` / `gawk` in the container)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the automatic default apply? — Picked: A — only in a repo that has `workflow-templates/.claude/` (coding-workflows). Alternatives: B — every repo that receives the command. Why: consumers have no twin tree, so twin-first cannot run there and the question must stay. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What happens when the log already records a different `Protected-path approval:` answer? — Picked: A — the recorded answer stands and the automatic default never overwrites it; the phase runs under that answer as before, and a `.claude/**` edit that answer cannot carry out still reaches the question. Alternatives: B — ask the A/B/C question whenever a non-twin-first answer is recorded. Why: B re-asks after every human answer, so a `/reclarify` would loop on the same question. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Rewrite the existing stop and §28.C bullet, or add text beside them? — Picked: A — add a new paragraph after the step 4 question and a new §28.C bullet after "Whether to run the chain at all". Alternatives: B — rewrite the lines in place. Why: #4785 rewrites those lines; A merges with it in either order (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the removal trigger enforced? — Picked: A — a test that fails once `scripts/claude_twin_sync.py` (added by #4785) exists while the interim text remains, plus the plan's Rollout note and one comment on #4785. Alternatives: B — the plan note only. Why: a failing check is what makes "#4785 removes it" happen without anyone remembering (§18). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Do later stages of the project (review round, fix PR, intervention) that must change `.claude/**` follow the default? — Picked: A — yes, under the phase's recorded approval; a push that changes a twin posts the hold claim and the twin-sync blocker the same way. Alternatives: B — leave later stages unspecified. Why: without it a review round on such a phase would stop again for the same question. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Update the Q40 row in `docs/operations/master-session.md`? — Picked: A — one sentence saying stages now record the approval themselves and the master answers stop 1 only for the remaining cases. Alternatives: B — leave it. Why: the row would otherwise tell the master to keep answering a question that no longer appears. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is "a phase whose plan explicitly needs a watched session" recognised? — Picked: A — the plan's phase text says the phase must run in a watched session (for example `watched session: required`). Alternatives: B — infer it from the files (for example hooks or `settings.json`). Why: hook and `settings.json` twins can be edited unattended; only their sync needs the operator (Q62/Q64), which stop 2 already covers. Applied in: phase 1. Status: pending review

## Lessons
- [source:intervention] A sunset (removal-trigger) test must assert the absence of every interim text the plan's Rollout names, in every file that carries it (docs included), and a paired presence test must pin the same markers while the interim is live so the sunset guard cannot pass vacuously. (files: tests/test_implement_plan_claude_command.py, agents.md, docs/operations/master-session.md)

## Notes
- Issue mode: plan written by /implement-issue-claude for #4948 (session session_01KWvixVyQiLvzZBHTrbRFpw, started by the master's dispatch trigger trig_017hVhQvGvbgboSSGrnNqVBF); start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29). Operator context of the dispatch trigger (standing decision Q40: A, `docs/operations/master-session.md`): edit only the `workflow-templates/.claude/**` twins, open the phase PR, post a `hold` claim on its head, and stop BLOCKED for the `[claude-twin-sync]` copy.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Stale Routine sweep: deleted 1 (`trig_015sJatxZ2Cy2xBDGCaZRLP6`, an ended one-shot of issue-4927).
- Twin to sync: `workflow-templates/.claude/commands/implement-plan-claude.md` → `.claude/commands/implement-plan-claude.md`, twin sha256 `83884435a9a1db1dd5f102ac455dd60dd6628a29cac2cd4c6386e704f9bb90a7`. No `.claude/` path without a twin is changed.
- `tests/test_workflow_retro.py` was excluded locally: `scripts/workflow_retro.py` needs Python 3.12 f-string syntax and the container runs 3.11 (unrelated to this change).
- While building the test overlay, a failed `rsync` let one `cp` run in the real checkout and overwrite `.claude/commands/implement-plan-claude.md` with the twin; it was restored with `git checkout` before any commit, and `.claude/` matched HEAD afterwards.
