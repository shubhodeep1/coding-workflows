<!-- changelog: changed -->
- **Plans for pipeline-written issues that change automation paths are now approved automatically, after the pipeline writes an exact `files_touched` list from the plan into the issue.**

Issues the pipeline writes itself, such as security-pass fixes, activation-gap follow-ups and unblock operator steps, usually carry no `files_touched` list. Their plans were held for a human `/approved` whenever they listed `.github/`, `.claude/`, `scripts/`, `prompts/` or `workflow-templates/` paths. On 2026-10-13 that held #7291, #7295, #7297 and #7302 with reason `open_grant_not_exact`. Now `plan.yml` and the poller's stall recovery append a `files_touched` block to such an issue before they post `/approved`. The block lists every file named in the plan's Files section, plus `tests/**`, `changelog.d/*` and `agents.d/*`. From then on that list is the issue's implement-time scope.

| The numbers that matter | Value |
| --- | --- |
| Companion entries always added | `tests/**`, `changelog.d/*`, `agents.d/*` |
| Most plan files written into one block | 50 |
| API calls per written block | 1 issue PATCH (poller: 1 extra issue read first) |

What this means for operators: a plan that lists a directory or a glob, names more than 50 files, or belongs to an issue a person wrote without `files_touched` still waits for a human `/approved`. The log line `PIPELINE_PLAN_GRANT issue=<n> source=plan|poller outcome=written|failed` records every write. Setting `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED=false` turns this off along with the rest of automatic approval for automation-path plans.
