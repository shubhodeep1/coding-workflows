<!-- changelog: fixed -->
- **A workflow-heal plan that changes no file is now approved automatically instead of waiting for a human (#8241).** After PR #8243 delivered the in-scope part of heal issue #8241, the re-plan found nothing left to change and wrote `Files likely to change: None.` The heal plan gate in `scripts/files_touched_scope_guard.py` treated that as unreadable, held it with `heal_scope_plan_paths_missing`, and sent a CRITICAL "plan awaiting approval" alert. The issue then sat until stall recovery posted `/approved` an hour later, and implement closed it as already implemented. The gate now approves such a plan with reason `heal_scope_plan_no_changes`, but only when the Files section explicitly says `None` and names no path, bullet or filename. An empty or prose-only Files section still holds as before.

| The numbers that matter | Value |
| --- | --- |
| Hold that triggered this | `heal_scope_plan_paths_missing` on #8241, 2026-10-28 06:40 UTC |
| Delay before stall recovery approved it | about 68 minutes |
| New approval reason | `heal_scope_plan_no_changes` |

What this means for operators: a heal issue whose fix already merged now closes on its own, with no CRITICAL alert and no `/approved` needed, as long as the plan's `HEAL_COUNTERFACTUAL` line passes. That line is checked by the separate heal counterfactual gate (#8249), which runs first and is unchanged. A plan that names files keeps the existing scope check.
