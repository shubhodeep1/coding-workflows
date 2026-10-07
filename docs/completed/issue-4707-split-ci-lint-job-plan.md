# Split the CI lint job into parallel jobs so CI finishes well under 20 minutes

Source issue: shubhodeep1/coding-workflows#4707 (https://github.com/shubhodeep1/coding-workflows/issues/4707)
Base branch: main
Security pass: run

## Summary

`.github/workflows/ci.yml` runs every check in one `lint` job of about 120 sequential steps, which takes 40–45 minutes and was cancelled at its 45-minute limit on `main`. This plan splits that job into parallel jobs (static checks, four area test jobs, and a four-way orchestrate-poll matrix) behind one aggregate `lint` job, so the critical path drops to under 10 minutes with no test dropped and no check weakened.

## Context

- Issue #4707. Stopgap PR #4706 (open) raises the `lint` timeout from 45 to 60 minutes; this plan replaces the need for it.
- Measured on `stable` push run 36374922203 (success, 41.9 minutes of steps):

  | Step range (current order) | Minutes |
  | --- | --- |
  | Checkout, shell-block guard, Python setup, static lint (YAML, drift, actionlint, syntax, ruff) | 0.2 |
  | Orchestrate-poll fast-fail subset | 1.0 |
  | Hook, Claude-command, orchestrator tests up to check-failure triage (#15–#40) | 5.9 |
  | Heal, plan-lint, merge/overlap, validation tests (#41–#53) | 3.4 |
  | Promote cycle, stall, codex, review contract tests (#54–#92) | 7.4 |
  | Release-gate, label, inventory, workflow-log-analysis tests (#93–#116) | 2.7 |
  | JSON schema, prompt checks, shell syntax, ShellCheck (#117–#123) | 1.6 |
  | Orchestrate poll process unit tests (4 local shards) | 19.7 |

- The orchestrate-poll module is already sharded inside one runner by `CI_POLL_TEST_SHARDS` (default 4) with the `NR % total == n` split pinned by `tests/test_ci_poll_test_sharding.py`.
- Check names: no branch protection or ruleset requires a status check. `GET repos/shubhodeep1/coding-workflows/branches/main` and `…/branches/stable` report `required_status_checks.contexts: []`, and the only ruleset ("Copilot review for default branch") has no status-check rule. `scripts/pr_checks_lib.sh` matches required checks by name from branch protection or `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` (`CI,Integration PR readiness check,…`), which never named `lint`. `review_autofix.yml`'s check snapshot reads every check run on the head, so more check runs only change what it waits for, not what it requires.
- `internal-check-failure-triage.yml` triggers on `check_run`, which Actions-created check runs do not fire; it has zero runs in this repo, so an aggregate `lint` job cannot cause duplicate triage.
- Release gates: `test-and-mark-stable.yml`'s `validate-scripts` job took 37 minutes on run 36374918973 (poll 21 minutes, unit tests 14.5 minutes) against `timeout-minutes: 45`. `mark-stable.yml`'s `validate-scripts` carries the same sharded poll step and 45-minute budget. In `test-and-mark-stable.yml` the job runs in parallel with the multi-hour e2e jobs, so it is not on that workflow's critical path.

## Goals

- `ci.yml` runs as parallel jobs: `static-checks`, four `tests-*` jobs, an `orchestrate-poll` matrix of 4 groups, and a final aggregate job whose id stays `lint`.
- Every step of the old `lint` job still runs exactly once per CI run, with its `run` body unchanged, except the orchestrate-poll step, which gains a group-level split, and the setup steps (checkout, Python setup, dependency install), which every job repeats.
- The orchestrate-poll split stays a true partition: group split and local shard split both use `NR % total == n`, pinned by tests.
- `lint` needs every other job, runs even when they fail (`if: always()`), and fails unless every needed job's result is `success`.
- Job timeouts: `static-checks` 15 minutes, every test job and each poll group 20 minutes, `lint` 5 minutes. Measured critical path is recorded in the PR and the changelog fragment.
- Release gates: `validate-scripts` in `mark-stable.yml` and `test-and-mark-stable.yml` goes from 45 to 60 minutes with a comment giving the measured 37 minutes.
- Tests that pin `ci.yml` structure or timeouts are updated, a new contract test pins the aggregate job, and `README.md` / `agents.md` describe the new layout.
- Every `.github/workflows/*.yml` stays under the §27 480,000-byte guard.

## Non-goals

- Automatically re-running cancelled or failed CI (issue's "Not in scope").
- Splitting the release gates' `validate-scripts` job (see AD-5).
- Changing any test, lint rule, or tool invocation inside a step.
- Changing `pr_checks_lib.sh`, `review_autofix.yml`, or the check-failure triage.
- Closing or editing PR #4706.

## Constraints

- §5 minimal change set: move steps between jobs verbatim; no reformatting of step bodies.
- §6 naming: the job id `lint` (check-run name `lint`, referenced as `CI / lint` in README and agents.md) is kept; step names are kept. New identifiers (`static-checks`, `tests-*`, `orchestrate-poll`, `CI_POLL_TEST_GROUP_INDEX`, `CI_POLL_TEST_GROUP_COUNT`) are checked for clashes.
- §9 YAML uses 2-space indentation.
- §15 no new GitHub API calls.
- §20 one `changelog.d/4707-*.md` fragment.
- §27 workflow size guard.

## Approach

Split the one job into seven job definitions, keeping step bodies verbatim:

1. `static-checks` (15 min): checkout, shared shell-block guard, Python setup, dependency install, guard contract tests, YAML lint, drift check, actionlint, Python syntax, ruff, JSON schema validation, prompt checks, shell syntax, `memory_helpers.sh` export check, ShellCheck, script-workflow cross-reference.
2. `tests-hooks-and-orchestrator` (20 min): old steps #15–#40.
3. `tests-heal-plan-and-validation` (20 min): old steps #41–#53.
4. `tests-promote-stall-and-review` (20 min): old steps #54–#92.
5. `tests-release-and-log-analysis` (20 min): old steps #93–#116 and the semantic cache tests.
6. `orchestrate-poll` (20 min, matrix `poll-group: [0, 1, 2, 3]`, `fail-fast: false`): derive subsets, fast-fail subset (group 0 only), then the sharded step. It first selects its group's tests with `awk -v n="${poll_group}" -v total="${poll_groups}" 'NR % total == n'`, then shards those locally exactly as today. The three single-file modules run in group 0 only.
7. `lint` (5 min): `needs` all six, `if: always()`, and exits 1 unless every `needs.*.result` is `success`.

Estimated critical path: the promote/review test job at about 7.5 minutes plus about 30 seconds of setup, with the poll groups at about 5–6 minutes.

Alternatives considered: `pytest -n` (the poll module runs through its own argv runner, and `pytest-xdist` is not installed); a larger matrix (more runners for little gain); splitting the release `validate-scripts` job (see AD-5).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the split must land atomically, because moving steps out of `lint` without the new jobs, or adding jobs without removing the steps, either drops tests or runs them twice.

1. **Phase 1 — split `ci.yml` into parallel jobs, raise the release gates' budget, update tests and docs.**
   - Files: `.github/workflows/ci.yml`, `.github/workflows/mark-stable.yml`, `.github/workflows/test-and-mark-stable.yml`, `tests/test_ci_poll_test_sharding.py`, `tests/test_ci_shared_shell_block_guard.py`, `tests/test_ci_job_split_contract.py` [new], `README.md`, `agents.md`, `changelog.d/4707-split-ci-lint-job.md` [new].
   - Done when: every old step name appears exactly once across the new jobs (checked against `origin/main`'s `ci.yml`), each moved body is byte-identical, all tests that read `ci.yml` pass locally, `actionlint` and `yamllint` pass, and the PR's own CI run finishes with the critical path under 20 minutes.
   - Rollback: revert the PR; the old single `lint` job returns intact.

## Implementation Steps

1. `.github/workflows/ci.yml`: replace the single `lint` job with the seven jobs above. Every job starts with `actions/checkout@v5`, `actions/setup-python@v6` (3.12), and the existing "Install Python CI dependencies" step, except `lint`, which checks out nothing.
2. `ci.yml` `orchestrate-poll`: add `CI_POLL_TEST_GROUP_INDEX: ${{ strategy.job-index }}` and `CI_POLL_TEST_GROUP_COUNT: ${{ strategy.job-total }}` to the sharded step's `env`, select the group's slice before local sharding, gate the fast-fail step and the three single-file modules to group 0, and keep the `CI_POLL_TEST_SHARDS` handling unchanged.
3. `ci.yml` `lint`: aggregate job with `needs`, `if: always()`, and a step that prints each need's result and fails on anything but `success`.
4. `mark-stable.yml`, `test-and-mark-stable.yml`: `validate-scripts` `timeout-minutes` 45 → 60 with a comment citing run 36374918973.
5. Tests: update `tests/test_ci_poll_test_sharding.py` (load the poll job, pin the group split and the two-level partition, new budgets), `tests/test_ci_shared_shell_block_guard.py` (read `static-checks`), and add `tests/test_ci_job_split_contract.py` (aggregate job wiring and failure predicate; every job has a timeout; no step name appears twice across test jobs). Wire the new test into `ci.yml`.
6. Docs: `README.md` and `agents.md` sections on `CI / lint` and `CI_POLL_TEST_SHARDS`; `changelog.d/4707-split-ci-lint-job.md`.

## Files & Modules

- `.github/workflows/ci.yml`
- `.github/workflows/mark-stable.yml`
- `.github/workflows/test-and-mark-stable.yml`
- `tests/test_ci_poll_test_sharding.py`
- `tests/test_ci_shared_shell_block_guard.py`
- `tests/test_ci_job_split_contract.py` [new]
- `README.md`
- `agents.md`
- `changelog.d/4707-split-ci-lint-job.md` [new]

## Tests

- Unit/contract: the three test files above, plus every test that reads `ci.yml` (`tests/test_ci_inventory_parity_order_contract.py`, `tests/test_plan_scope_mode_contract.py`, `tests/test_workspace_safety_check.py`, `tests/test_gh_api_write_guard.py`, `tests/test_permission_prompts.py`, `tests/test_pr_check_in_reminder.py`, `tests/test_pr_watch_guard.py`, `tests/test_stale_routines.py`, `tests/test_opencode_live_smoke_workflow.py`, `tests/test_phase_skip_gate_telemetry_contract.py`, `tests/test_review_autofix_terminal_same_head_gate.py`, `tests/test_review_synthesise_smoke.py`, `tests/test_workflow_file_size_limit.py`) run locally.
- Step parity: a local script compares the old job's steps (from `origin/main`) with the new jobs' steps by name and body.
- Lint: `yamllint -s` and `actionlint` on `ci.yml` and the two release workflows.
- End to end: the phase PR's own CI run shows the new jobs, and its wall-clock is recorded.

## Risks & Mitigations

- A step relied on a side effect of an earlier step in the same job (a `/tmp` file, a tool put on `PATH`, an env var). Mitigation: audit each moved step for `/tmp/`, `GITHUB_PATH`, `GITHUB_ENV`, and tool use before moving; keep dependent steps in the same job.
- A cold runner makes one group slower than measured. Mitigation: 20-minute budgets are about 3x the estimate.
- More runners per CI run. ACCEPTED: about 10 runners for under 10 minutes instead of 1 for 45.
- PR #4706 edits the same `timeout-minutes` line and test assertion. ACCEPTED: whichever merges second resolves the conflict; this plan's values win because the split removes the need for 60 minutes.

## Rollout

No flag. The change takes effect on the PR that carries it. Rollback is a revert. `ci.yml` is not a consumer template, so no consumer sync is involved; the release-gate timeout change reaches the next release run.

## References

- Issue #4707, PR #4706.
- CI runs 36374922203 (step timings), 36367681221 and 36368393442 (cancelled at 45 minutes).
- Release run 36374918973 (`validate-scripts` 37 minutes).
- `tests/test_ci_poll_test_sharding.py`, `scripts/pr_checks_lib.sh`.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Keep an aggregate check named `lint` although nothing requires it? — Picked: A — keep a final `lint` job that needs every other job, runs with `if: always()`, and fails unless all succeeded. Alternatives: B — rename the old job to the static-checks job and keep the name there; C — drop the aggregate and let each job report alone. Why: the issue asks for one aggregate status, docs name `CI / lint`, and B would repurpose an identifier (§6). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to split the orchestrate-poll module across runners? — Picked: A — a 4-group matrix, each group running the existing `CI_POLL_TEST_SHARDS` local shards, both splits `NR % total == n` with the group index and count from `strategy.job-index` / `strategy.job-total`. Alternatives: B — `pytest -n` (needs `pytest-xdist` and a runner change); C — one job per local shard with no in-runner parallelism. Why: keeps the pinned partition and the existing shard variable, and adds no repo variable. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Where do the fast-fail subset and the three single-file poll modules run? — Picked: A — only in poll group 0. Alternatives: B — every group (runs them four times); C — a separate job. Why: each still runs exactly once, with no extra runner. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How to group the remaining test steps? — Picked: A — four jobs over contiguous ranges of the current step order, balanced by measured time. Alternatives: B — one job per step; C — regroup steps by topic across the file. Why: keeps order-dependent steps (inventory parity before the log collector gate) together and keeps the diff reviewable. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] The release gates' `validate-scripts` jobs take 37 minutes against 45: split or raise? — Picked: A — raise both to 60 minutes with a note. Alternatives: B — apply the same job split to both release workflows. Why: the smallest safe change (§5); in `test-and-mark-stable.yml` the job runs beside multi-hour e2e jobs, so splitting would not shorten the release. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] New job budgets? — Picked: A — 15 minutes for `static-checks`, 20 for each test job and poll group, 5 for `lint`. Alternatives: B — 30 for every job; C — 10 for every job. Why: about 3x the measured runtime per job, and the whole run still ends well under the old 45. Applied in: phase 1 PR. Status: pending review
