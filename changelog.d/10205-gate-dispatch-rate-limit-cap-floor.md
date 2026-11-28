<!-- changelog: fixed -->
- **Every release-gate dispatch step can now wait up to 1,500 seconds for a GitHub rate-limit reset.**

#10201 made `scripts/dispatch_and_watch_workflow_run.sh` wait for a rate-limit reset, but `test-and-mark-stable.yml` capped that wait at 110 to 410 seconds per step so it fit the existing step limits. Release gate run 45927857867 was refused 505 seconds before the reset, on the step capped at 410 seconds, so the same failure could still happen. Every dispatch step now uses the script default of 1,500 seconds, and the step and job `timeout-minutes` grew to hold it. Completion and registration deadlines are unchanged.

| Step | Cap | Step limit |
| --- | --- | --- |
| validation-refresh | 110 s -> 1500 s | 95 -> 119 min |
| update_workflows | 410 s -> 1500 s | 18 -> 37 min |
| internal-memory-maintenance | 230 s -> 1500 s | 45 -> 67 min |
| orchestrate (no step limit) | 240 s -> 1500 s | none |
| standalone validate | 180 s -> 1500 s | 35 -> 57 min |

Job limits: `orphan-workflows-test` 165 -> 240 minutes, `orchestrate-decompose-test` 60 -> 81, `validate-standalone-test` 45 -> 67. No new jobs or workflows. A new test, `test_gate_dispatch_steps_rate_limit_cap_meets_floor`, fails when a dispatch step sets a cap below 1,500 seconds or when a step or job limit cannot hold its cap plus its watch deadline.

What this means for operators: a primary-limit refusal up to 1,500 seconds before the reset now waits and dispatches instead of failing the gate. A refusal further from the reset (up to an hour) still fails at once with `outcome=cap_reached`. When rate limits do hit, the gate can now take up to about 75 minutes longer in `orphan-workflows-test`.
