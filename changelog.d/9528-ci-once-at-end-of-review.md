<!-- changelog: added -->
- **CI can now run once per PR, at the end of review, instead of on every push.** This release ships the mechanism switched off; a follow-up switches this repository over once the review workflow's `promote-candidate` copy carries it.

`.github/workflows/ci.yml` can be dispatched for a pull request (`workflow_dispatch` with `pr_number`, run name `CI [pr:<n>]`). The run tests the PR's merge ref, and its checks, including the required `lint`, land on the PR head. It can also be called with `mode: quick` to run only `static-checks`. A new helper, `_pr_ensure_final_ci` in `scripts/pr_checks_lib.sh`, starts that run once for the head review finished with, and never twice for the same commit. An `orchestrator/project-*` head is skipped because its push run already covers it. When the final run fails, the new `final-ci-failed` job posts one `<!-- ai:final-ci-failed:v1 pr=<n> head=<sha> -->` marker and sends the PR back to review, so the autofix sees the failing checks. Check-failure triage now also handles these dispatched runs.

| The numbers that matter | Value |
| --- | --- |
| Share of CI runner time in the 24 test shards and other test jobs | about 90% |
| PR CI runs per PR, 2026-11-10 to 2026-11-15 | median 3, 90th percentile 9, max 15 |
| Final runs per reviewed head | at most 1 (none for `orchestrator/project-*` heads) |
| New review workflow input | `final_ci_workflow` (default empty, off) |
| New repository variables | `FINAL_CI_FAILED_REDISPATCH_ENABLED` (default `true`), `FINAL_CI_SWEEP_MAX_PER_TICK` (default 10) |

What this means for operators and consumer repositories: nothing changes yet. Every PR still runs full CI on every push until the switch-over removes `ci.yml`'s `pull_request` trigger and passes `final_ci_workflow: ci.yml` from `internal-review.yml`. Consumers receive the input and the helper on the next `@stable` sync, and they stay inert unless a consumer passes the input.

### For contributors

The hooks are in `_pr_wait_for_required_checks` (first poll, required check absent), `scripts/review_enable_auto_merge.sh` (after the `ai-review/head-gate` success on paths that do not auto-merge) and the review sweep step "Ensure the final CI run on reviewed PR heads" (`scripts/review_final_ci_sweep.sh`). Tests: `tests/test_review_final_ci.py`, `tests/test_ci_concurrency.py`, `tests/test_ci_shallow_checkout_contract.py`, `tests/test_ci_job_split_contract.py`.
