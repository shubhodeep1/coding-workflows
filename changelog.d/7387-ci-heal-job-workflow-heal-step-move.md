<!-- changelog: changed -->
- **The CI step `Workflow failure heal contract and driver tests` now runs its largest test module in four parallel shards.** The job `tests-heal-plan-and-validation` took 15m12s of its 20-minute timeout on `main` (run 38584572151, budget-watch issue #7387), and a job that hits its timeout turns CI red with no code fault. Almost all of that time is this step, and almost all of the step is `tests/test_workflow_failure_heal.py` (297 tests).

  The first attempt moved the step unchanged to `tests-promote-stall-and-review`. In this PR's CI run (38602731355) that job had already used 12.8 minutes when the step started and was cancelled at its 20-minute timeout with the step half done. No test job had room for the whole step, so it stays in `tests-heal-plan-and-validation` under the same name, and the module is split by test function across four local `pytest` processes, the way the `orchestrate-poll` step runs its largest module. Each test builds its own temp directories, so the shards share no state. Every collected test function runs exactly once, parametrized cases stay in one shard, and a failing shard fails the step and prints its last 40 log lines outside the collapsed log groups. The other heal and provider-outage test files still run as before.

  No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged.

  What this means for operators: nothing to change; no new variable.
