<!-- changelog: fixed -->
- **Workflow failure heal no longer logs a false `scope_inputs_freeze_failed` warning on reports that name no run.**

Heal reports with no failed run attached, such as a label-only `ai:destructive-blocked` escalation, used to print `jq: invalid JSON text passed to --argjson` and then `WORKFLOW_HEAL warn scope_inputs_freeze_failed` in the `Workflow Failure Heal Intake` log. The frozen heal-scope snapshot is now written with empty run lists instead, and the warning appears only when the freeze really fails. Heal outcomes do not change: a report with no run still gets no heal scope and files no issue, as before.

What this means for operators: `scope_inputs_freeze_failed` in a heal intake log now points to a real problem rather than routine noise.

### For contributors

`scripts/workflow_failure_heal_intake.sh` slurps the `*.verified` run records through `_heal_slurp_verified_runs`, which replaces jq's output with `[]` when jq fails instead of appending it after jq's own `[]`. `tests/test_workflow_failure_heal_scope_tests.py` runs the real freeze block with an empty log dir and with two verified records.
