# Implement-Plan Log — Workflow failure heal: catch deterministic review/autofix failures

- Plan: docs/plans/heal-deterministic-autofix-failures-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Status: IN_PROGRESS
- Stage: phase 3/4
- Activation: not started
- Waiting on: PR #4365
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: see the phase 3 PR report (Haiku checker session + safety net)
- Last updated: 2026-09-24
- Last note: Phase 2 merged (PR #4327, 2026-09-23). Phase 3 implemented and verified locally (77 heal tests incl. 14 new; ruff, yamllint, actionlint, shellcheck clean); phase PR opened.

## Phases
1. [x] Phase 1 — Heal dispatch envelope and visible rejection — PR #4303 merged 2026-09-23; interventions: 0
2. [x] Phase 2 — Identical-failure fingerprint cap — PR #4327 merged 2026-09-23; interventions: 0
3. [ ] Phase 3 — Self-inflicted classification and routing — PR #4365 open (waiting); interventions: 0
4. [ ] Phase 4 — Editor preflight and main-pinned divergence check

## Security pass
- not started

## Validation
- not started

## Completion
- not started

## Notes
- 2026-09-23: phase branches follow /implement-plan-claude naming (`claude/implement-plan-heal-deterministic-autofix-failures-phase-<n>`), one fresh branch per phase from origin/main.
- 2026-09-23: changelog fragment filenames use the predicted PR number; phase 1 predicted 4303 and GitHub assigned #4303.
- 2026-09-23: phase 2 resumed from a checker poke (session_01Q4tMZ3UfdGwyibECCi1ps2); no `implement-plan heal-deterministic-autofix-failures` Routines existed, so none needed deleting.
- 2026-09-23: phase 2 additions beyond the plan text, all additive: the failure marker also carries `run=<id>` so two failure comments of one run count once; the gate's comment filter keeps failure texts and editor summaries (clipped) as well as both markers, so the scan-ending rules of D3 can be evaluated; `force_rb_judge` dispatches bypass the cap (the judge is the cap's intended consumer, D4); `fingerprint-cap-block` skips with `reason=head_moved` when a push landed after the gate. The plan's "same support-source checkout the deterministic-skip-merge job uses" does not exist (that job checks nothing out), so the gate and the cap job check out only the files they need (sparse) at the support ref plus the main snapshot, preferring the copy that carries the new subcommands.
- 2026-09-23: phase 2 predicted PR 4324 for the changelog fragment; GitHub assigned #4327, fragment renamed.
- 2026-09-24: the phase 2 session ran the pre-#4304 command and armed Sonnet Routines (trig_01PYNNqBUeEdTNtNSrQLZQ9i, trig_018dZziPdrKU8eumzW9u3jzK) that could not act; they were deleted by hand and phase 3 was restarted by hand. The phase 2 session was archived on the user's answer (Q2: A).
- 2026-09-24: PR #4335 (merged after the plan was written) routes self-repo `workflow-defect` autofix heals to the PR head branch. User decision Q1: A — `pr-self-inflicted` follows plan D5 (PR comment, no issue); #4335's head-branch route stays for `workflow-defect` / `inconclusive`.
- 2026-09-24: phase 3 additions beyond the plan text, all additive: (a) the intake only honours a self-inflicted token its computed ownership backs, otherwise logs `classification_remapped … to=workflow-defect reason=<routing_disabled|ownership_*>`; (b) the base diff is a two-dot tree diff (`git diff origin/main origin/<base>`) because the plan's three-dot form has no merge base on depth-1 fetches; (c) the base comparison is skipped when the base is `main` or `WORKFLOW_HEAL_TARGET_BRANCH` so `stable` is never targeted; (d) the autofix reporter adds the `failure_evidence_tail.txt` stage-stderr tail to its evidence (the crash line lives there, and the P2 tail was not in the report) and sends the ownership flags only when the staged helper advertises `classify-crash-ownership`; (e) `ai:orchestrator-managed` is added only for an `orchestrator/project-<N>` base; (f) `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` is wired into `workflow-failure-heal-intake.yml` env so the repo variable takes effect.
- 2026-09-24: phase 3 predicted PR 4357 for the changelog fragment; GitHub assigned #4365, fragment renamed.
