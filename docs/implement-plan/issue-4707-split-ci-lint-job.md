# Implement-Plan Log — Split the CI lint job into parallel jobs so CI finishes well under 20 minutes

- Plan: docs/plans/issue-4707-split-ci-lint-job-plan.md
- Source issue: shubhodeep1/coding-workflows#4707
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4707-split-ci-lint-job   Final PR: #4874 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4884
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: review round 2 on PR #4884: fixed the one consensus finding (the orchestrate-poll shard judgment loop skipped a shard with tests but no log, so a shard that died before its runner started could pass) in `ci.yml` and both release ports, with behavioural tests (AD-8).

## Phases
1. [ ] Phase 1 — split `ci.yml` into parallel jobs behind an aggregate `lint`, raise release `validate-scripts` budget, update tests and docs   — PR #4884 open (waiting); review rounds: 2; interventions: 0

## Conformance

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
- [source:plan-deviation] When splitting a long CI job, move steps as verbatim text blocks and verify parity (every old step name exactly once, bodies byte-identical) against the base branch's workflow before editing anything else; YAML round-trips reformat run blocks. (files: .github/workflows/ci.yml)
- [source:intervention] When a CI step moves to another job, rename (alias, §6) the test helpers and constants that name the old job in the same change; reviewers read a stale job name as a missing guard. (files: tests/test_ci_shared_shell_block_guard.py)
- [source:intervention] A parallel-shard judge must decide "did this shard have work" from its input (the non-empty test list), never from an output file: skipping on a missing log turns a shard that died before its runner started into a silent pass. (files: .github/workflows/ci.yml, .github/workflows/mark-stable.yml, .github/workflows/test-and-mark-stable.yml)

## Notes
- Issue mode; security pass: run (`security_pass_skip.py`: no skip label).
- PR #4706 (open stopgap, 45 → 60 min on `lint`) edits the same timeout line and test assertion; whichever merges second resolves the conflict.
