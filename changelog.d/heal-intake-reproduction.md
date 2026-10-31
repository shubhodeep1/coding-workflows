<!-- changelog: added -->
- **The heal intake can now check whether a CI failure's failing tests really reproduce, and shows the answer in the heal issue.** Behind `WORKFLOW_HEAL_REPRODUCE_ENABLED` (off by default), a self-repo `workflow_run` report whose job logs name failing test files runs them through `scripts/heal_reproduce.sh` at the failing commit against the last green run of the same workflow on the same branch.

The heal issue gets a `## Reproduction` section, marked as untrusted evidence, with `status:`, `reason:`, `failing_at:`, `passing_at:` and one line per test. Only `reproduced` changes anything: a diagnosis that called the failure `transient` is filed as `inconclusive` instead, and the `status: reproduced` line lets the planner pick `HEAL_COUNTERFACTUAL: REPRODUCIBLE`. `pre_existing`, `passes_at_failing_sha` and `unverified` (with its reason, for example `no_baseline` or `timeout`) are shown and change nothing. The last-green lookup is a reusable `last_green_run` helper in `scripts/workflow_failure_heal.py`.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_REPRODUCE_ENABLED=false`) |
| Extra GitHub API calls | at most 1 per report (the last-green lookup), none when no failing tests were extracted |
| Wall time | 10 minutes (`WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES`, clamped 1-20 by the intake) |
| Intake job timeout | 156 minutes (was 130), to cover the reproduction wall |
| Log line | `WORKFLOW_HEAL reproduction outcome=<status> reason=<reason> tests=<n> [baseline=<reason>]` |

What this means for operators: with the switch off the heal issue body and the API calls are byte-identical to before. Test code only runs in the network-less, credential-free gate container.
