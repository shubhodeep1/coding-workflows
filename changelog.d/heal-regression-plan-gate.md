<!-- changelog: added -->
- **Heal plans that only raise a timeout while runs got slower are no longer auto-approved.** Behind `WORKFLOW_HEAL_TREND_ENABLED` (off by default), when a stall or timeout heal issue's `## Run-time trend` says `regression=true`, the plan must name the file or commit that made the runs slower in a `HEAL_REGRESSION: <file or commit>` line, or explain it in a `HEAL_REGRESSION: not a regression: <reason>` line. A plan without that line is held (`regression_line_missing`), and so is a plan that only changes `timeout-minutes`, `*_TIMEOUT*`, `PHASE_TIMEOUT` or `*_BUDGET*` in the failing workflow (`budget_only_plan`), whatever its line says.

The check runs in the plan workflow's automatic approval step, right after the heal counterfactual gate, and posts the same kind of hold comment. The heal intake and heal planner prompts now ask for the line.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_TREND_ENABLED=false`) |
| Hold reasons | `regression_line_missing`, `budget_only_plan`, `heal_regression_check_unavailable` |
| Extra GitHub API calls | none |
| Log line | `AI_PHASE_GATE_V1 phase=plan gate=heal_regression ...` |

What this means for operators: nothing changes until the switch is on; then a held heal plan still goes ahead with a human `/approved`. The orchestrator poller's stall recovery does not apply this check yet.
