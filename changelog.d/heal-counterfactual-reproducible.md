<!-- changelog: added -->
- **Heal plans backed by a reproduced failure can now be approved automatically with `[repro-required]`.** A plan may end its counterfactual check with `HEAL_COUNTERFACTUAL: REPRODUCIBLE: <test ids>` when the heal issue's `## Reproduction` section says `status: reproduced`; behind `WORKFLOW_HEAL_REPRODUCE_ENABLED` (off by default) the plan workflow approves it and tags the approval so the heal editor re-runs those tests before the PR opens.

With the switch off, a `REPRODUCIBLE` plan is held for a human with the reason `reproduction_disabled`, the same way an `UNVERIFIED` plan is held. A missing or invalid test list is held as `reproducible_test_list_invalid`. `PASS`, `PARTIAL`, `FAIL` and `UNVERIFIED` behave as before, and every other auto-approval comment is unchanged. Turning the whole counterfactual gate off (`HEAL_COUNTERFACTUAL_GATE_ENABLED=false`) still approves without the tag.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_REPRODUCE_ENABLED=false`) |
| New decision | `approve_pending_repro`, posted as `/approved [auto-approved-by-plan] [repro-required]` |
| Test ids per line | 1-50 pytest node ids (`tests/<path>.py[::name]`) |
| Extra GitHub API calls | 0 |

What this means for operators: nothing changes until the switch is on and a heal issue carries a reproduced `## Reproduction` section.
