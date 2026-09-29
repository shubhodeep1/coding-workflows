# Implement-Plan Log — Split the CI lint job into parallel jobs so CI finishes well under 20 minutes

- Plan: docs/plans/issue-4707-split-ci-lint-job-plan.md
- Source issue: shubhodeep1/coding-workflows#4707
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4707-split-ci-lint-job   Final PR: #4874 draft
- Status: IN_PROGRESS
- Stage: conformance 3/3
- Activation: not started
- Waiting on: conformance fix PR from `claude/implement-plan-issue-4707-split-ci-lint-job-conformance-fix-3` (next stage `conformance 3/3 — fix check`)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker session_01BWX5wabwbkpvBkn9CqZcd6 (reused; trigger ids in the stage report)
- Last updated: 2026-09-29
- Last note: conformance 3/3 (last run the cap allows): CONFORMANT (Step 3 COMPLETE, Step 4 CONCERNS) — one EVIDENCE-BASED doc finding fixed in the conformance fix PR: `ci.yml`'s job comments said static-checks "About 3 minutes measured" and the test jobs "measured 3-8 minutes", but the five split-layout CI runs measured 1.4-1.9 and 2.4-8.2 minutes. The fix PR gets a fix check, not a fourth run.

## Phases
1. [x] Phase 1 — split `ci.yml` into parallel jobs behind an aggregate `lint`, raise release `validate-scripts` budget, update tests and docs   — PR #4884 merged 2026-09-29 (5f6dd77); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Step 3 COMPLETE; Step 4 CONCERNS: 2 EVIDENCE-BASED doc findings, 0 BLOCKER) — fix PR from `claude/implement-plan-issue-4707-split-ci-lint-job-conformance-fix-1` (pre-security). Checks: 16 CI-reading test modules, yamllint, actionlint (pinned CI version), §27 size (max 351,391 bytes), step parity vs `origin/main` (125 steps, all present once; only the poll steps changed). End to end: CI run 36523765261 on 5f6dd77 via final PR #4874, success, 9.0 minutes wall-clock, critical path `orchestrate-poll (0)` 8.7 minutes.
- Run 2 — 2026-09-29: CONFORMANT (Step 3 COMPLETE; Step 4 CONCERNS: 2 EVIDENCE-BASED doc findings, 0 BLOCKER) — fix PR from `claude/implement-plan-issue-4707-split-ci-lint-job-conformance-fix-2` (pre-security). Re-audited after #5052 on the project branch synced with `main` (e8110b9): PR #5052's figures match run 36523765261's job records (run 8m58s, `orchestrate-poll (0)` 8m43s, `tests-promote-stall-and-review` 8m09s); `main` has not touched the three workflows since 5f6dd77. Checks: 16 CI-reading test modules, yamllint, actionlint 1.7.12, step parity vs current `origin/main` (125 steps, each once; only the two poll steps changed), `assemble_changelog.py assemble --dry-run` (9 fragments, #4706 and #4707 in the same release).
- Run 3 — 2026-09-29: CONFORMANT (Step 3 COMPLETE; Step 4 CONCERNS: 1 EVIDENCE-BASED doc finding, 0 BLOCKER) — fix PR from `claude/implement-plan-issue-4707-split-ci-lint-job-conformance-fix-3` (pre-security; last run the cap allows, so its merge is followed by `conformance 3/3 — fix check`). Re-audited after #5078 on the project branch synced with `main` (aa8f74b; `main`'s #4797 added `tests/test_check_in_session_targeting.py` to the §26 hand-back step, which merged cleanly into `tests-promote-stall-and-review`). Finding: `ci.yml` job comments claimed measured runtimes (static-checks "About 3 minutes", test jobs "3-8 minutes") that CI runs 36523765261, 36527182237, 36530680439, 36533369764, and 36535570251 contradict (static-checks 1.4-1.9 minutes, test jobs 2.4-8.2); the orchestrate-poll "about 5 minutes" claim holds (groups 1-3: 3.5-6.3 minutes). Checks: 17 CI-reading test modules (incl. the new `test_check_in_session_targeting.py`), yamllint, actionlint 1.7.12 over every workflow and template, step parity vs current `origin/main` (125 steps, each once; only the two poll steps changed), cross-job side-effect scan (no step reads a `/tmp` file, `GITHUB_ENV`, or tool written by another job), §27 size (max 351,422 bytes).

## Security pass

## Validation

## Completion
- Final PR #4874 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Keep an aggregate check named `lint` although nothing requires it? — Picked: A — keep a final `lint` job that needs every other job, runs with `if: always()`, and fails unless all succeeded. Alternatives: B — rename the old job to the static-checks job and keep the name there; C — drop the aggregate and let each job report alone. Why: the issue asks for one aggregate status, docs name `CI / lint`, and B would repurpose an identifier (§6). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to split the orchestrate-poll module across runners? — Picked: A — a 4-group matrix, each group running the existing `CI_POLL_TEST_SHARDS` local shards, both splits `NR % total == n` with the group index and count from `strategy.job-index` / `strategy.job-total`. Alternatives: B — `pytest -n` (needs `pytest-xdist` and a runner change); C — one job per local shard with no in-runner parallelism. Why: keeps the pinned partition and the existing shard variable, and adds no repo variable. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Where do the fast-fail subset and the three single-file poll modules run? — Picked: A — only in poll group 0. Alternatives: B — every group (runs them four times); C — a separate job. Why: each still runs exactly once, with no extra runner. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How to group the remaining test steps? — Picked: A — four jobs over contiguous ranges of the current step order, balanced by measured time. Alternatives: B — one job per step; C — regroup steps by topic across the file. Why: keeps order-dependent steps (inventory parity before the log collector gate) together and keeps the diff reviewable. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] The release gates' `validate-scripts` jobs take 37 minutes against 45: split or raise? — Picked: A — raise both to 60 minutes with a note. Alternatives: B — apply the same job split to both release workflows. Why: the smallest safe change (§5); in `test-and-mark-stable.yml` the job runs beside multi-hour e2e jobs, so splitting would not shorten the release. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] New job budgets? — Picked: A — 15 minutes for `static-checks`, 20 for each test job and poll group, 5 for `lint`. Alternatives: B — 30 for every job; C — 10 for every job. Why: about 3x the measured runtime per job, and the whole run still ends well under the old 45. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — review round 1, 2026-09-29] The reviewers flag `_lint_steps` in `tests/test_ci_shared_shell_block_guard.py`, which now reads the `static-checks` job: how to fix the name? — Picked: A — add `_guard_job_steps()`, switch both callers to it, and keep `_lint_steps` as an alias. Alternatives: B — keep the name and only add a docstring; C — rename in place. Why: fixes the misleading name without removing an existing identifier (§6); B leaves the name the reviewers flagged. Applied in: PR #4884. Status: pending review
- AD-8 [phase 1/1 — review round 2, 2026-09-29] The reviewers flag the orchestrate-poll shard judgment loop (`[ -f poll_shard_N.log ] || continue`), which skips a shard that had tests but left no log; the loop predates this PR (moved verbatim) but sits in the step the PR edits. Fix or reject as pre-existing? — Picked: A — fix it here in `ci.yml` and both release ports: skip only shards with an empty test list, count a shard with tests but no log as failed, and add behavioural tests. Alternatives: B — reject as out of scope (pre-existing on main); C — fix `ci.yml` only. Why: a verifiable false-green in the same flow the PR changes (§12.B), a guard-sized change; C would break the release ports' parity with `ci.yml` that the tests pin. Applied in: PR #4884. Status: pending review

## Lessons
- [source:conformance] A plan goal that asks for a measured runtime can only be met after the first real run; write pre-merge numbers as estimates and record the measured value (with the run id) once that run exists. (files: agents.md, changelog.d/4707-split-ci-lint-job.md)
- [source:plan-deviation] When splitting a long CI job, move steps as verbatim text blocks and verify parity (every old step name exactly once, bodies byte-identical) against the base branch's workflow before editing anything else; YAML round-trips reformat run blocks. (files: .github/workflows/ci.yml)
- [source:intervention] When a CI step moves to another job, rename (alias, §6) the test helpers and constants that name the old job in the same change; reviewers read a stale job name as a missing guard. (files: tests/test_ci_shared_shell_block_guard.py)
- [source:intervention] A parallel-shard judge must decide "did this shard have work" from its input (the non-empty test list), never from an output file: skipping on a missing log turns a shard that died before its runner started into a silent pass. (files: .github/workflows/ci.yml, .github/workflows/mark-stable.yml, .github/workflows/test-and-mark-stable.yml)
- [source:conformance] When a change replaces a value that other text still describes (a "mirrors X's budget" comment, a stopgap PR's unreleased changelog fragment), grep for every mention of the old value and the old job before merging; fragments that ship in the same release must not contradict each other. (files: .github/workflows/mark-stable.yml, .github/workflows/test-and-mark-stable.yml, changelog.d/4707-split-ci-lint-job.md)

## Notes
- The plan's "measured critical path is recorded in the PR" goal: PR #4884 merged without it (its CI could not run on a PR into the project branch); the final-merge stage puts run 36523765261's timings in final PR #4874's body (step 11a).
- Issue mode; security pass: run (`security_pass_skip.py`: no skip label).
- PR #4706 (stopgap, 45 → 60 min on `lint`) merged 2026-09-28, before this project's phase PR; its fragment `changelog.d/4706-ci-lint-timeout-60.md` ships in the same release as this project's.
