# Implement-Plan Log — Protected-path phases: apply the twin-first rule automatically (interim until #4785)

- Plan: docs/plans/issue-4948-protected-path-twin-first-default-plan.md
- Source issue: shubhodeep1/coding-workflows#4948
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4948-protected-path-twin-first-default   Final PR: #F draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened; implementing phase 1 twin-first (operator Q40).

## Phases
1. [ ] Phase 1 — interim twin-first default for protected-path phases   — protected paths: `.claude/commands/implement-plan-claude.md` (edited only in its `workflow-templates/.claude/` twin, Q40)
   - [ ] Twin `implement-plan-claude.md` step 4: interim paragraph (automatic approval line, twin-only edits, no-twin diff + sha256, hold claim + twin-sync blocker, resume, later stages, the three question cases, sunset)
   - [ ] CLAUDE.md §28.C: new bullet naming the interim automatic twin-first default and its sunset (#4785)
   - [ ] `agents.md` and `docs/operations/master-session.md` (Q40 row) updated
   - [ ] `tests/test_implement_plan_claude_command.py`: command-twin, CLAUDE.md, and sunset-guard tests
   - [ ] `changelog.d/4948-protected-path-twin-first-default.md`
   - Done: the new tests pass; the rest of `tests/` passes except `test_template_parity` until the `[claude-twin-sync]` copy

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

## Notes
- Issue mode: plan written by /implement-issue-claude for #4948 (session session_01KWvixVyQiLvzZBHTrbRFpw, started by the master's dispatch trigger trig_017hVhQvGvbgboSSGrnNqVBF); start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29). Operator context of the dispatch trigger (standing decision Q40: A, `docs/operations/master-session.md`): edit only the `workflow-templates/.claude/**` twins, open the phase PR, post a `hold` claim on its head, and stop BLOCKED for the `[claude-twin-sync]` copy.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Stale Routine sweep: deleted 1 (`trig_015sJatxZ2Cy2xBDGCaZRLP6`, an ended one-shot of issue-4927).
