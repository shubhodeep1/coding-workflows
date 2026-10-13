<!-- agents: section="Implement scope-lock label" -->
`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` defaults to `true` everywhere it
is read; `files_touched_scope_guard.py` treats an unset or empty
`--flag-enabled` as on and any value outside `1|true|yes|on` as off. The
planner prompts ask for the file list under `## Files likely to change` with
the next item under its own `##` heading, which every plan parser
(`extract_plan_automation_paths`, `plan.yml`'s template-owned path guard and
`targeted_file_context.extract_paths_from_plan`) treats as the end of the list.
