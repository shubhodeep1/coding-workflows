<!-- changelog: changed -->
- **Automatic plan approval now checks automation-path grants by default.** `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` defaults to `true`, so a plan that lists `.github/`, `.claude/`, `scripts/`, `prompts/` or `workflow-templates/` paths without an exact `files_touched` grant from a trusted author stays in `ai:awaiting-approval` instead of getting an automatic `/approved`.

The gate added in #6838 was off unless a repository set the variable, so the plan step and stall recovery still auto-approved plans that edited automation paths nobody had granted (#6766). The plan step, managed stall recovery and standalone stall recovery now run the check unless the variable is `false`, `0`, `no` or `off` (any letter case). An unset, empty or mistyped value keeps the gate on, and a hold marker no longer decides whether the gate runs. A held issue gets one comment naming the ungranted paths and a WARNING; a missing plan or guard helper holds it with `automation_path_check_unavailable`. Plans without automation paths still auto-approve, and a human `/approved` and the implement commit-time guard are unchanged.

| The numbers that matter | Value |
| --- | --- |
| `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` default | `true` (was `false`) |
| Values that turn the gate off | `false`, `0`, `no`, `off` |
| Log line | `STALL_SKIP ... reason=automation_path_ungranted\|automation_path_check_unavailable` |

What this means for operators: plans that touch automation paths now wait for a `files_touched` grant or a human `/approved`. Set the repository variable `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` to `false` to restore the old behaviour.
