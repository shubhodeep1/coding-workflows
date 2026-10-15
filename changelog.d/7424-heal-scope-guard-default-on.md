<!-- changelog: changed -->
- **The workflow failure heal intake files heal issues again, and stops paging CRITICAL for reports that name no failed run.** `HEAL_INTAKE_SCOPE_GUARD_ENABLED` now defaults to `true`.

Since #6871 the intake composed each heal issue and then dropped it (`WORKFLOW_HEAL skip reason=scope_guard_disabled`) until an operator switched the gate on, so failures were diagnosed but never fixed. The gate is now on by default. A heal issue is filed only after its `ai:workflow-heal-scope:v1` marker verifies, and the existing caps still apply. A report that claimed no failed run, such as a label-only `ai:scope-blocked` report or an `engine_fallback` report, can never get a scope. It is now skipped with `WORKFLOW_HEAL skip reason=scope_unavailable_no_runs` and exit 0, instead of a CRITICAL alert and a failed intake run. A report that named runs and still has no valid scope keeps the CRITICAL refusal.

| The numbers that matter | Value |
| --- | --- |
| Open heal issue cap | 10 (`WORKFLOW_HEAL_MAX_OPEN_ISSUES`) |
| New heal issues per UTC day | 20 (`WORKFLOW_HEAL_MAX_ISSUES_PER_DAY`) |
| Reports in the last sample that would page without this change | 10 of 11 reaching the gate (label-only reports, Oct 12-14) |

What this means for operators: expect `ai:workflow-heal` issues for pipeline failures again, each with a verified scope. Set the repository variable `HEAL_INTAKE_SCOPE_GUARD_ENABLED=false` to stop filing them.
