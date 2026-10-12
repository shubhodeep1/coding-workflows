<!-- changelog: changed -->
- **Automatic approval of plans that list automation paths is now on by default, and the planner puts its file list under its own `## Files likely to change` heading.**

`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` now defaults to `true` in `plan.yml`, `orchestrate_poll.yml`, `scripts/orchestrate_poll_process.sh` and `scripts/files_touched_scope_guard.py` (an unset or empty value counts as on). A plan that lists `.github/`, `.claude/`, `scripts/`, `prompts/` or `workflow-templates/` paths is approved automatically when every one of those paths has an exact `files_touched` entry in an issue written by the pipeline account or a repository member. Plans without that grant are still held with one hold comment, now with reason `open_grant_not_exact` or the missing paths instead of `automation_path_plan_auto_approval_disabled`.

The planner prompts (`prompts/mode-plan.txt`, its template and `scripts/run_plan_codex.sh`) now ask for the file list under `## Files likely to change`, with the next item under its own `##` heading. The approval gate, `plan.yml`'s template-owned path guard and the targeted file context all end the list at that heading, so paths mentioned later in the plan are not read as files to change.

What this means for operators: set `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` to `false` to go back to holding every automation-path plan for a human `/approved`.
