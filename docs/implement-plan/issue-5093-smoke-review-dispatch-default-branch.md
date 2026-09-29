# Implement-Plan Log — Dispatch the E2E smoke gate's review runs from the default branch, and correlate them with the bait commit

- Plan: docs/plans/issue-5093-smoke-review-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#5093 (https://github.com/shubhodeep1/coding-workflows/issues/5093)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5093-smoke-review-dispatch-default-branch   Final PR: #5107 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5111
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DeJ3bVofrAPjfdXYapCF43   safety net trig_01HaH6KxmNMbExKmVVN1nVe6   hand-back trig_01AEpSHUFmAYrTxQR3gGLNr2
- Last updated: 2026-09-29
- Last note: review round 2 on PR #5111: the one finding (timestamp-shaped log anchor) settled per AD-10 by accepting any single prefix token; first-match rule unchanged

## Phases
1. [ ] Phase 1 — default-branch smoke review dispatch with checked-out-SHA correlation   — PR #5111 open (waiting); review rounds: 2; interventions: 0
   - [x] `scripts/smoke_review_dispatch.sh`: `smoke_review_pr_named_runs`, `smoke_review_checked_out_sha`, `smoke_review_sha_descends_from` (AD-1, AD-2, AD-9)
   - [x] "Validate prerequisites" (`id: prereqs`) outputs `test_repo_default_branch` from its existing `repos/${TEST_REPO}` read (AD-7)
   - [x] Phase 3c dispatches at `REVIEW_DISPATCH_REF`, registers `bug_b_run_id`, and stays fail-soft (AD-3)
   - [x] Phase 4 leg (c): the registered run is a candidate while active, and once completed only after its checked-out SHA equals `PIN_SHA` / `BAIT_SHA` (AD-4)
   - [x] Phase 4b adopts or registers PR-named runs, dispatches at `REVIEW_DISPATCH_REF`, and correlates, emitting `retry_run_unverified` on a definite miss (AD-5, AD-6)
   - [x] Tests: `tests/test_smoke_review_dispatch.py` [new], updated phantom filter test, registered in `ci.yml`
   - [x] `agents.md`, `docs/INVENTORY.md`, `changelog.d/5093-smoke-review-dispatch-default-branch.md`

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header, verified by `.claude/scripts/security_pass_skip.py`.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What runtime metadata correlates a default-branch dispatch run with the bait commit? — Picked: A — the first line-anchored `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` in the run's `codex-agent` job log. Alternatives: B — timing only; C — add a head-SHA artifact or run name to `review_autofix.yml`. Why: written by default-branch code before PR content is reviewed, no review-workflow change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How are the dispatched runs found? — Picked: A — the per-workflow `workflow_dispatch` listing on the default branch, filtered by the ` [pr:<N>]` run-name suffix. Alternatives: B — exact two names; C — reuse `_autofix_pr_named_review_runs`. Why: scoped to the dispatched wrapper and covers renamed consumer wrappers. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] When is Phase 3c's dispatch run registered? — Picked: A — in Phase 3c, 90 s, fail-soft, no dispatch without a baseline. Alternatives: B — lazily in Phase 4; C — dispatch without a baseline. Why: mirrors Phase 4b registration; C starts an uncorrelatable run. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] When does Phase 4 accept the registered run? — Picked: A — active: candidate; completed: only with checked-out SHA equal to `PIN_SHA` / `BAIT_SHA`. Alternatives: B — any descendant of the bait; C — unverified. Why: mirrors leg (a)'s exact pin. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] How is Phase 4b's retry run correlated? — Picked: A — `BAIT_SHA`, `RETRY_DISPATCH_SHA`, or a descendant of `BAIT_SHA`; definite miss → `retry_run_unverified`. Alternatives: B — exact pair only; C — none. Why: registration already tolerates the branch advancing. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Does Phase 4b also adopt active PR-named runs? — Picked: A — yes. Alternatives: B — branch runs only. Why: Phase 3c's own dispatch now appears only there; B would queue a duplicate. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Where does the dispatch ref come from? — Picked: A — `default_branch` from the existing "Validate prerequisites" read. Alternatives: B — a new read per step; C — hardcode `main`. Why: §15; C breaks other default branches. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Is the Phase 6 poller-wrapper dispatch at `ai/issue-<N>` in scope? — Picked: A — no, recorded only. Alternatives: B — move it too. Why: not this finding; separate contract (§5). Applied in: no code change. Status: pending review
- AD-9 [plan, 2026-09-29] Where do the new helpers live? — Picked: A — sourced `scripts/smoke_review_dispatch.sh`. Alternatives: B — inline. Why: one tested implementation for three sites. Applied in: phase 1 PR. Status: pending review
- AD-10 [phase 1/1 — review round 2, 2026-09-29] One reviewer (1 of 6, NIT) re-raised round 1's rejected point: `smoke_review_checked_out_sha` requires an ISO-8601-shaped timestamp before `Captured INITIAL_HEAD_SHA=`, so a changed log timestamp format would fail a genuine run with rc=2. How? — Picked: A — accept any single non-whitespace prefix token, keep exactly one token required and the first match winning, and test other token shapes plus the lines that must still be ignored. Alternatives: B — reject again (fail-closed is correct), which needs the dedicated verdict bot this web session cannot post as, so the PR would block; C — make the prefix optional, which in a timestamp-less log would let `echo Captured …` match. Why: the first-match rule carries the security property, not the timestamp's shape, so A removes a false failure at no security cost (§1) with the smallest change (§5). Applied in: PR #5111 (review round 2 commit). Status: pending review

## Lessons
- [source:intervention] A lookup of default-branch `workflow_dispatch` runs by PR run name must not pass the API's `branch=` filter or require `head_branch == <ref>`: GitHub can report `head_branch` as null on such runs, so keep null or empty head branches and drop only runs on another branch. (files: scripts/smoke_review_dispatch.sh)

## Notes
- Security pass skipped per the plan header (verified automation-produced `ai:security` issue).
- The session started with no `gh` and no GitHub MCP tools; `gh` was installed by running `.claude/hooks/session-start.sh` after the repo was attached, and GitHub writes go through `gh api` via the session proxy.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5093#issuecomment-5887705973 (id 5887705973)
- Phase 1: `tests/test_test_and_mark_stable_review_blocked_budget.py` needed no change (its retry assertions still hold); the phantom filter test now extracts the `jq --argjson extra` form. Local runs also needed `pytest`, `gawk`, and `yamllint` installed in the session container.
- Out of scope (AD-8): the Phase 6 poller-wrapper dispatch still runs at `ai/issue-<N>`.
