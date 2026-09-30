# Implement-Plan Log — Workflow failure heal: catch deterministic review/autofix failures

- Plan: docs/completed/heal-deterministic-autofix-failures-plan.md (moved from docs/plans/ in the completion PR)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Status: COMPLETE
- Stage: activation
- Activation: deploy-activate started (session_016P7FeK9zr1XzmHTkH6eUxe)
- Waiting on: `@stable` promotion by docs/deploy-activation/pr-4443.md step 9 (runbook docs/deploy-activation/plan-heal-deterministic-autofix-failures-plan.md)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01F2ssEbNUkDaVFUvyMLKZEz   safety net and hand-back: see the completion-stage report (session_01LEgTnawM6DVbtZQkHVQgNL)
- Last updated: 2026-09-27
- Last note: verify-activation cycle 1 DORMANT (live in coding-workflows; `@stable` lacks P2–P4 while promotion is held); /deploy-activate runbook started and waits for the pr-4443 step 9 promotion.

## Phases
1. [x] Phase 1 — Heal dispatch envelope and visible rejection — PR #4303 merged 2026-09-23; interventions: 0
2. [x] Phase 2 — Identical-failure fingerprint cap — PR #4327 merged 2026-09-23; interventions: 0
3. [x] Phase 3 — Self-inflicted classification and routing — PR #4365 merged 2026-09-24; interventions: 0
4. [x] Phase 4 — Editor preflight and main-pinned divergence check — PR #4375 merged 2026-09-24; interventions: 0

## Conformance
- Run 1 — 2026-09-24 (session_01EPN5a3t2tnV79nwuyDjRx5): CONFORMANT — no fixes (pre-security)

## Security pass
- Cycle 1 — run 35996690244 2026-09-24 (ref: default): follow-ups #4398, #4399, #4400 — all closed ai:merged
- Cycle 2 — run 36076664580 2026-09-25 (ref: default): follow-ups #4431, #4432 — all closed ai:merged
- Cycle 3 — run 36110448412 2026-09-25 (ref: default): follow-ups #4451, #4452, #4453, #4454 — all closed ai:merged
- Cycle 4 — run 36141193439 2026-09-25 (ref: default): follow-ups #4469, #4470, #4471 — all closed ai:merged
- Cycle 5 — run 36214760369 2026-09-26 (ref: default): follow-ups #4511, #4512 — all closed ai:merged (#4512 last, via PR #4516 merged 2026-09-26T11:04Z)
- Cycle 5 is final: 5-cycle cap reached, no sixth audit dispatched (user decision Q18: A, 2026-09-26). Every run concluded success.

## Validation
- Cycle 1 — run 36248291165 2026-09-26 (target_ref: default, main @ f15db7a): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 271s)

## Completion
- Completion PR from claude/implement-plan-heal-deterministic-autofix-failures-complete — doc moved to docs/completed/heal-deterministic-autofix-failures-plan.md

## Activation
- Verify cycle 1 — 2026-09-27 (session_01LD7P1fqjrZXU7g1TmsvSVn, scope activation, unattended): DORMANT — no fix PR, 0 auto-decisions. Live in coding-workflows on main 6e00a6e (review run 36314357232, intake run 36316962977); `@stable` v1.29.12 has P1 but not P2–P4; promotion paused by `PROMOTE_CYCLE_ENABLED=false` (docs/deploy-activation/pr-4443.md step 8b).
- deploy-activate started 2026-09-27 (session_016P7FeK9zr1XzmHTkH6eUxe): runbook docs/deploy-activation/plan-heal-deterministic-autofix-failures-plan.md; waits for the pr-4443 step 9 promotion, then verifies `stable` content and one consumer review run.

## Auto-decisions
- none

## Lessons
- [source:security] Workflows that run PR- or branch-controlled code (support scripts, build files, workspace hooks, Python imports) in a step that also holds GH_PAT or other secrets were the dominant finding class across all five audit cycles; run such code tokenless and without network, or pin it to the default branch, before exposing a credential in the same job. (files: .github/workflows/review_autofix.yml, .github/workflows/validate.yml, .github/workflows/security-audit.yml, scripts/run_workspace_hook.sh, scripts/validate_process.sh)
- [source:security] Fixes to one credential-exposure path kept surfacing an adjacent one in the next cycle (hooks, then hook replay, then preflight imports, then import shadowing), so the pass ran into the 5-cycle cap; when a finding touches a trust boundary, audit every other entry point that crosses the same boundary in the same fix. (files: .github/workflows/validate.yml, scripts/validate_process.sh)
- [source:activation] A project that edits reusable workflows is live in coding-workflows on merge (internal wrappers call @main) but reaches consumers only through @stable promotion; check the promotion kill switch PROMOTE_CYCLE_ENABLED and stable's file content, not commit ancestry, because promotions are non-ancestral. (files: .github/workflows/promote-main-to-stable.yml, .github/workflows/review_autofix.yml)

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
- 2026-09-24: phase 4 resumed from checker session_01WZAzPjEn2HGD4joLA2ZdZq in session_0174Kdk3odzxZxX9Nmm72cfH; previous stage and checker archived, safety net trig_01Pt2eeqYKwxGJs38xCVDATC deleted.
- 2026-09-24: phase 4 plan-vs-main differences: `review_apply_fixes.sh` on main has no `: "${VAR:?…}"` guard (the #4259 guard lived only on that PR's branch), so `preflight_required_vars` starts empty and the contract test keeps it complete; `CODEX_HELPERS_PATH` is referenced nowhere, so the preflight checks it only when set (requiring it would fail every run).
- 2026-09-24: phase 4 additions beyond the plan text, all additive: (a) the preflight skips with `reason=editor_not_scheduled` in Claude-branch review mode or a terminal resume, where the editor never runs, and logs `reason=disabled` under the kill switch; (b) the preflight runs after the file checks, so the existing missing-file failures and alerts are unchanged; (c) `derive_autofix_failure_reason` in `workflow_failure_heal.py` gains the same `EDITOR_PREFLIGHT_FAILED` precedence as the reporter, because "Assemble failure evidence" exports its reason as `AUTOFIX_FAILURE_REASON` (parity is test-pinned); (d) the main-pinned test fetches `origin/main` only when the ref is missing and uses `--depth=1` only on an already-shallow checkout, so it never makes a full local clone shallow.
- 2026-09-24: phase 4 predicted PR 4374 for the changelog fragment; GitHub assigned #4375, fragment renamed.
- 2026-09-24: `tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` fails locally only without `gawk` (container lacked it); passes once installed. Unrelated to phase 4.
- 2026-09-26: security cycle 5 is final — the 5-cycle cap was reached and no sixth audit was dispatched (user decision Q18: A, 2026-09-26).
- 2026-09-26: log staleness corrected in the completion PR. The copy on main still read `Stage: phase 4/4`, `Waiting on: PR #4375`, because legacy mode commits the log only with a PR in flight and the conformance, security and validation stages opened none. Verified against GitHub on 2026-09-27: phases 1–4 merged (#4303 2026-09-23, #4327 2026-09-23, #4365 2026-09-24, #4375 2026-09-24); every security follow-up above is closed with `ai:merged`.
- 2026-09-26: stall — the cycle-4 stage session hit the claude-code-remote lineage depth limit (8); PR #4513 introduced one checker per project; the project was restarted by hand in session_018qF9QsireLdhXGCKhpw3kS, which created project checker session_01F2ssEbNUkDaVFUvyMLKZEz.
- 2026-09-26: validation cycle 1 (run 36248291165) was dispatched 2026-09-26T14:22Z by session_01RLUEDB5zg9C7ySjiGc1oTD against main in legacy mode (tracking_issue=0, pr_number=0, no target_ref).
- 2026-09-27: validation read in session_01LEgTnawM6DVbtZQkHVQgNL from `validation_status.json` in artifact `ai-validation-36248291165-1`: status=pass, raw_status=pass. No validation-fix PR merged after conformance run 1, so no conformance re-run; straight to the completion PR. README.md and agents.md already document all three kill switches (phases 2–4), so the completion PR ships no doc update besides the move and the repointed status note in `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md`.
