<!-- changelog: fixed -->
- **The planner is now told about the issue's `files_touched` list, so its plans stop being held for files it did not need to name.** Plans for #9519 and #9520 were both parked in `ai:awaiting-approval` with `plan_files_outside_files_touched`.

The plan approval gate in `scripts/files_touched_scope_guard.py` holds a plan whose `## Files likely to change` section names a file outside the issue's `files_touched` list, but the planner prompt never mentioned that list. The #9519 plan named `agents.d/prepush-gate-passing-change-erased.md`, a fragment without the issue number. The gate always allows `agents.d/9519-<slug>.md`. The #9520 plan listed `tests/test_orchestrator_concurrency_caps.py` "only if" it turned out to be needed. Item 1 of the planner prompt (`scripts/run_plan_codex.sh`, with the `prompts/mode-plan.txt` copies kept in step) now says: name a file off the list only when the plan cannot work without it, never list a file you only might change, and name new fragments `changelog.d/<issue>-<slug>.md` or `agents.d/<issue>-<slug>.md`. The planning context now starts with an `Issue number: <N>` line, so the planner has the number to put in that name; the line is left out when `ISSUE_NUMBER` is unset or not a plain positive number.

| The numbers that matter | Value |
| --- | --- |
| Plan runs held | 43733726102 (#9519, 2 paths), 43733730435 (#9520, 1 path) |
| Planner prompt size | 270 lines, 18,031 to 18,402 bytes |

What this means for operators: fewer plans wait for a maintainer because of an unnumbered fragment or a file the plan only might change. A plan that really needs a file off the list is still held, which is correct, because the implement-time scope guard would refuse that file.
