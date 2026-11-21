<!-- agents: section="Workflow architecture" -->
Phase 4 runner wait (issue #9480): `scripts/stable_e2e_step_wait_review.sh` does not count the time the watched review run waits for a runner (run status `queued`/`pending`/`waiting`/`requested`, or `in_progress` with no job in progress and at least one job `queued`/`waiting`/`pending` in the jobs JSON already fetched that poll) as inactivity; that wait is bounded by the #8004 deadline ceiling, and when the ceiling is reached during a wait an extra `::error::` names the hosted-runner queue before the unchanged wall-clock error and `status=timeout`. The `e2e-smoke-test` job limit (`timeout-minutes` and `E2E_JOB_TIMEOUT_MINUTES`) is 360, GitHub's hosted-runner maximum.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `E2E_REVIEW_RUNNER_WAIT` (`scripts/stable_e2e_step_wait_review.sh`, the body of `test-and-mark-stable.yml` "Phase 4: Wait for review & autofix to complete": `run= status= outcome=waiting since=<epoch> ceiling=<epoch>` when the watched review run starts waiting for a runner, `run= outcome=started waited_seconds=` when that run gets a runner or completes, `run= outcome=ceiling waited_seconds=` when the deadline ceiling is reached during the wait; #9480)

LOG_PREFIX.name=E2E_REVIEW_RUNNER_WAIT

<!-- agents: section="Pre-push affected-test gate" -->
Correction (#9480): the `e2e-smoke-test` job limit (`timeout-minutes` and `E2E_JOB_TIMEOUT_MINUTES`) is now 360, GitHub's hosted-runner maximum, so it cannot be raised any further. With the defaults, the conservative serial budget is 300/360, not 300/300. A larger `PREPUSH_GATE_STEP_STALL_MINUTES` has to fit inside the 360-minute limit, and the budget check fails closed when it does not.

<!-- agents: section="Workflow file size limit" -->
Phase 4 (`scripts/stable_e2e_step_wait_review.sh`, #9480) does not count the time the watched review run waits for a runner as inactivity; that wait is bounded by the #8004 deadline ceiling.
