<!-- changelog: fixed -->
- **The planner no longer puts directory names in the notes of its `## Files likely to change` list, so a note cannot hold an otherwise approvable plan.**

The plan for #7724 listed `docs/INVENTORY.md` with the note "(one added `scripts/` bullet)". The plan approval gate in `scripts/files_touched_scope_guard.py` reads every `scripts/`, `.github/`, `.claude/`, `prompts/` and `workflow-templates/` token in that section, read the bare `scripts/` as a directory entry, and withheld automatic approval with `path_not_granted`, although every file the plan listed was grantable. The plan prompts (`scripts/run_plan_codex.sh`, `prompts/mode-plan.txt` and `prompts/_templates/mode-plan.txt`) now say that a bullet's note may not name another path or a directory, and that the reason for a change belongs in a later section. The gate itself is unchanged and still holds a plan that lists a directory or glob.

What this means for operators: fewer plans wait for a manual `/approved` because of wording in the file list. A plan that really lists a directory still waits for a human.
