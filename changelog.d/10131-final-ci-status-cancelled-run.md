<!-- changelog: fixed -->
- **A final CI run cancelled as a whole no longer posts `ci/final` = `failure` (Refs #10131).**

When a final CI run (`CI [pr:N]`) was cancelled before its `lint` job started, `lint` still ran (`always()`), its aggregate failed on the cancelled jobs, and `scripts/ci_final_status.sh` posted `ci/final=failure` over an earlier `success` on the same head (runs 45686395594 and 45561623483). The run tested nothing, so the red status was false.

Before posting `failure`, the script now checks whether the run was cancelled as a whole: the needed jobs include a `cancelled` job and no `failure` job, the run is attempt 1, and `scripts/ci_cancelled_rerun.py classify-cancelled-run` finds a cancelled job that never started, or only shutdown/cancel annotations on the cancelled jobs. Then it posts nothing and logs `CI_FINAL_STATUS sha=<sha> outcome=skipped reason=run_cancelled`. A job cut off by its own `timeout-minutes` still posts `failure` and is still handed back through `final-ci-failed`, and so does any failed read or unclear answer.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls | one jobs read plus at most 10 annotation reads, only on the cancelled-and-no-failure path |
| New repo variables | `CI_FINAL_STATUS_RUN_CANCELLED_SKIP_ENABLED` (default `true`; `false` restores the old behaviour) |
| New permissions | `checks: read` on the `lint` job (annotation reads) |
| New jobs, workflows or runs | none |

What this means for operators: a dispatched final CI run uses the PR branch's own `ci.yml` and script, so a PR branch keeps the old behaviour until it is updated from `main`. Statuses already posted are not cleared; the next run on the head replaces them.
