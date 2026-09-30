# Deploy-Activation Log — Workflow failure heal: catch deterministic review/autofix failures

- Reference: docs/completed/heal-deterministic-autofix-failures-plan.md   (+ phase PRs #4303, #4327, #4365, #4375; progress log docs/implement-plan/heal-deterministic-autofix-failures.md)
- Deploy target: shubhodeep1/coding-workflows (+ the 13 consumers in `.github/ai/consumer_repos.json` via `@stable`)
- How it runs: coding-workflows: `pull_request` / `workflow_dispatch` via `internal-review.yml` → `review_autofix.yml@main` (P2, P4), and `repository_dispatch` `workflow-failure-heal` → `workflow-failure-heal-intake.yml` (P1, P3). Consumers: `workflow-templates/ai-review.yml:39` → `review_autofix.yml@stable`, scripts staged from `stable`. P3 routing is self-repo only by design.
- Status: IN_PROGRESS
- Last updated: 2026-09-27
- Last note: 2026-09-27: step 2 done (no opt-outs anywhere). Step 3 waits for docs/deploy-activation/pr-4443.md steps 9a/9; at 12:5x UTC pr-4443 is BLOCKED at step 8 and `stable` is still `d58d7bc` / tag `fade4be9`.

## Runbook
1. [x] Prereqs: Homebrew, git, gh, jq, `gh auth login` (repo, workflow), clone   — done 2026-09-27: operator confirmed `done` (no output pasted)
2. [x] Baseline read (read-only): the three kill switches (`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED` read at `review_autofix.yml:361,374,458`; `REVIEW_EDITOR_PREFLIGHT_ENABLED` at `review_autofix.yml:4081`; `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` at `workflow-failure-heal-intake.yml:77`; all default `true`) in coding-workflows and the 13 consumers, plus `PROMOTE_CYCLE_ENABLED` in coding-workflows   — done 2026-09-27: coding-workflows `PROMOTE_CYCLE_ENABLED=false` (set 2026-09-26T05:16:16Z, pr-4443 hold), none of the three kill switches set; all 13 consumers have no kill switch set (defaults `true`), no read errors
3. [ ] Wait for docs/deploy-activation/pr-4443.md steps 9a/9 (re-enable promotion, promote main → `@stable`); no operator action for this project. Then verify on `stable` content, not ancestry: `autofix-identical-failure-count` and `REVIEW_EDITOR_PREFLIGHT_ENABLED` in `review_autofix.yml`, `--preflight` in `scripts/review_apply_fixes.sh`, on both the `stable` branch and the `stable` tag
4. [ ] Verify LIVE in one consumer: a review run after the promotion logs `AUTOFIX_FINGERPRINT cap=` and `REVIEW_EDITOR_PREFLIGHT result=`

## Notes
- 2026-09-27: verify-activation cycle 1 (session_01LD7P1fqjrZXU7g1TmsvSVn, scope activation, unattended) = DORMANT, no fix PR, 0 auto-decisions. Live in coding-workflows on main `6e00a6e`: review run 36314357232 logged `AUTOFIX_FINGERPRINT cap=not_tripped`, the `AUTOFIX_FINGERPRINT … fp=` marker, `REVIEW_EDITOR_PREFLIGHT result=ok checks=4 failed=0` and `WORKFLOW_HEAL_AUTOFIX_REPORT dispatched`; intake run 36316962977 unwrapped the enveloped `autofix_failure` report with the P3 ownership fields and ran with `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED: true`.
- 2026-09-27: re-checked in this session. `stable` branch `d58d7bc` (v1.29.12, 2026-09-26 13:42 UTC; tag `stable` = `fade4be9`) has none of `autofix-identical-failure-count`, `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`, `REVIEW_EDITOR_PREFLIGHT_ENABLED` in `review_autofix.yml` / `scripts/`, and no `--preflight` in `scripts/review_apply_fixes.sh`. It has P1. Main has all of them.
- Promotion is paused on purpose: `PROMOTE_CYCLE_ENABLED=false` (read at `promote-main-to-stable.yml:138`, default `true`) belongs to docs/deploy-activation/pr-4443.md step 8b (operator Q6: A) and is lifted at its step 9a. This runbook never re-enables it and never runs a manual promotion; it waits for pr-4443's step 9.
- `stable` is not a descendant of main (promotions are non-ancestral), so step 3 checks file content, not commit ancestry.
- No consumer wrapper, secret or variable is needed (plan "Rollout"). Consumers may set the three kill switches to `false` per repository; step 2 reads them so step 4 does not mistake a deliberate opt-out for a failed rollout.
- Not in scope for this runbook: #4512, #4516, #4546.
- Repo variables cannot be read from the web session (the agent proxy refuses Actions variable paths), so step 2 runs on the operator's Mac.
- No auto-decisions (progress log `## Auto-decisions`: none).
