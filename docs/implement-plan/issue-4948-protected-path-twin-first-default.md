# Implement-Plan Log — Protected-path phases: apply the twin-first rule automatically (interim until #4785)

- Plan: docs/completed/issue-4948-protected-path-twin-first-default-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4948
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4948-protected-path-twin-first-default   Final PR: #4989 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (into the project branch), then final PR #4989
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Ame2D1ptdLmTxyN2aYG1jw (project checker, reused)   safety net and hand-back: in the validation 1/3 — read result stage report
- Last updated: 2026-09-29
- Last note: Validation cycle 1 (run 36544500911, target_ref project branch at faeef26) passed: status=pass raw_status=pass, 10/10 tests. No validation-fix PR, so no conformance re-run; completion PR opened to move the plan to docs/completed/.

## Phases
1. [x] Phase 1 — interim twin-first default for protected-path phases   — protected paths: `.claude/commands/implement-plan-claude.md` (edited only in its `workflow-templates/.claude/` twin, Q40) — PR #5008 merged 2026-09-29 (merge commit 01b6c30; twin synced in f60a3b1); review rounds: 2 (round 3 clean); interventions: 0
   - [x] Twin `implement-plan-claude.md` step 4: interim paragraph (automatic approval line, twin-only edits, no-twin diff + sha256, hold claim + twin-sync blocker, resume, later stages, the three uncovered cases, sunset) — `workflow-templates/.claude/commands/implement-plan-claude.md:35-45` (root copy synced in `[claude-twin-sync]` f60a3b1)
   - [x] CLAUDE.md §28.C: new bullet naming the interim automatic twin-first default and its sunset (#4785) — `CLAUDE.md:2236-2253`
   - [x] `agents.md` (`:1116-1123`) and `docs/operations/master-session.md` (Q40 row) updated
   - [x] `tests/test_implement_plan_claude_command.py`: 6 new tests (command twin ×3, CLAUDE.md, operator-doc presence, sunset guard)
   - [x] `changelog.d/4948-protected-path-twin-first-default.md`
   - Done: in the twin overlay the command and changelog tests pass (55 passed, 1 skipped); the real tree fails only `test_template_parity` until the twin sync. Full overlay run: 4720 passed, 122 failed — the same 122 fail on a clean `origin/main` copy (missing `jsonschema` / `gawk` in the container)

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Correctness CONCERNS: AD-8) — no fix PR (pre-security)

## Security pass
- Cycle 1 — run 36543305111 2026-09-29 (ref: project branch, audited 1b91d69..17a63d7): clean — tracker=#3576 findings=0 followups_created=0

## Validation
- Cycle 1 — run 36544500911 2026-09-29 (target_ref: project branch, head faeef26 after syncing main aebaa17): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 291s)

## Completion
- Completion PR (this log commit) — doc moved to docs/completed/issue-4948-protected-path-twin-first-default-plan.md
- Final PR #4989 draft (marked ready in the final-merge stage)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the automatic default apply? — Picked: A — only in a repo that has `workflow-templates/.claude/` (coding-workflows). Alternatives: B — every repo that receives the command. Why: consumers have no twin tree, so twin-first cannot run there and the question must stay. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What happens when the log already records a different `Protected-path approval:` answer? — Picked: A — the recorded answer stands and the automatic default never overwrites it; the phase runs under that answer as before, and a `.claude/**` edit that answer cannot carry out still reaches the question. Alternatives: B — ask the A/B/C question whenever a non-twin-first answer is recorded. Why: B re-asks after every human answer, so a `/reclarify` would loop on the same question. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Rewrite the existing stop and §28.C bullet, or add text beside them? — Picked: A — add a new paragraph after the step 4 question and a new §28.C bullet after "Whether to run the chain at all". Alternatives: B — rewrite the lines in place. Why: #4785 rewrites those lines; A merges with it in either order (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the removal trigger enforced? — Picked: A — a test that fails once `scripts/claude_twin_sync.py` (added by #4785) exists while the interim text remains, plus the plan's Rollout note and one comment on #4785. Alternatives: B — the plan note only. Why: a failing check is what makes "#4785 removes it" happen without anyone remembering (§18). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Do later stages of the project (review round, fix PR, intervention) that must change `.claude/**` follow the default? — Picked: A — yes, under the phase's recorded approval; a push that changes a twin posts the hold claim and the twin-sync blocker the same way. Alternatives: B — leave later stages unspecified. Why: without it a review round on such a phase would stop again for the same question. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Update the Q40 row in `docs/operations/master-session.md`? — Picked: A — one sentence saying stages now record the approval themselves and the master answers stop 1 only for the remaining cases. Alternatives: B — leave it. Why: the row would otherwise tell the master to keep answering a question that no longer appears. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is "a phase whose plan explicitly needs a watched session" recognised? — Picked: A — the plan's phase text says the phase must run in a watched session (for example `watched session: required`). Alternatives: B — infer it from the files (for example hooks or `settings.json`). Why: hook and `settings.json` twins can be edited unattended; only their sync needs the operator (Q62/Q64), which stop 2 already covers. Applied in: phase 1. Status: pending review
- AD-8 [conformance 1/3, 2026-09-29] The twin-first hold claim posts claude_fix_claim.py's hand-back-cap sentence ("this PR reached the cap of 3 Claude hand-backs"), which is false for a twin-sync hold (seen on PR #5008, comment 2026-09-29T04:03Z). Fix it in this project? — Picked: A — no code change here; the in-flight claude-fixer-unattended-convergence project owns it (D15, phase 3: `--reason` on hold claims, which /implement-plan-claude passes for every hold it posts). Alternatives: B — add `--reason` in this project's claude_fix_claim.py twin (duplicates D15, conflicts with that phase-3 branch, adds a twin-sync stop); C — add a follow-up explanatory comment to the twin-first paragraph (a twin edit and twin-sync stop for a partial fix). Why: §5 smallest change; D15 already specifies the fix and its caller change. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] A sunset (removal-trigger) test must assert the absence of every interim text the plan's Rollout names, in every file that carries it (docs included), and a paired presence test must pin the same markers while the interim is live so the sunset guard cannot pass vacuously. (files: tests/test_implement_plan_claude_command.py, agents.md, docs/operations/master-session.md)
- [source:intervention] When interim text spans more than one sentence or paragraph, give the sunset test one marker per separately removable piece (the opener and every closing or Sunset sentence), not just the opener, or a partial removal passes with an orphaned sentence. (files: tests/test_implement_plan_claude_command.py)
- [source:conformance] A rule that makes a stage post a `hold` claim for a reason other than the hand-back cap must also fix the hold comment's wording (`claude_fix_claim.py` renders the cap sentence for every hold), or each automated hold posts a false reason on the PR. (files: .claude/scripts/claude_fix_claim.py, .claude/commands/implement-plan-claude.md)

## Notes
- Issue mode: plan written by /implement-issue-claude for #4948 (session session_01KWvixVyQiLvzZBHTrbRFpw, started by the master's dispatch trigger trig_017hVhQvGvbgboSSGrnNqVBF); start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29). Operator context of the dispatch trigger (standing decision Q40: A, `docs/operations/master-session.md`): edit only the `workflow-templates/.claude/**` twins, open the phase PR, post a `hold` claim on its head, and stop BLOCKED for the `[claude-twin-sync]` copy.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Stale Routine sweep: deleted 1 (`trig_015sJatxZ2Cy2xBDGCaZRLP6`, an ended one-shot of issue-4927).
- Twin to sync: `workflow-templates/.claude/commands/implement-plan-claude.md` → `.claude/commands/implement-plan-claude.md`, twin sha256 `83884435a9a1db1dd5f102ac455dd60dd6628a29cac2cd4c6386e704f9bb90a7`. No `.claude/` path without a twin is changed.
- `tests/test_workflow_retro.py` was excluded locally: `scripts/workflow_retro.py` needs Python 3.12 f-string syntax and the container runs 3.11 (unrelated to this change).
- While building the test overlay, a failed `rsync` let one `cp` run in the real checkout and overwrite `.claude/commands/implement-plan-claude.md` with the twin; it was restored with `git checkout` before any commit, and `.claude/` matched HEAD afterwards.
