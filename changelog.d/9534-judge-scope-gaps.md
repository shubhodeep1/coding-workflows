<!-- changelog: fixed -->
- **Orchestrator judge issues no longer wait hours for a person because their `files_touched` list is too narrow.** Judge-written scopes now get the standard `tests/**`, `changelog.d/*` and `agents.d/*` entries, the judge is told to list the scripts a reused job runs, and a planning block caused only by missing scope goes to the unblock judge's `override_guard`.

On 2026-11-15, #9503 (a judge addition of project #9116) was blocked at planning because its `files_touched` lacked the script its reused `deliver-report` job runs and that script's test. Auto-answer paused, and the issue, and the project's integration PR, waited about 6 hours until a person widened the scope by hand. Now the poller adds the plan-grant companion entries to every judge addition and fix-up that has a `files_touched:` block (`JUDGE_FILES_TOUCHED_COMPANIONS_ENABLED`), and the planner names each missing file on a `SCOPE_MISSING:` line. `plan.yml` turns those lines into a trusted `ai:guard-rejection:v1` marker and the `ai:scope-blocked` label next to `ai:blocked` (`PLAN_SCOPE_BLOCK_ROUTING_ENABLED`). The unblock judge then offers `override_guard` under its existing rules (`.github/`, `.claude/`, `workflow-templates/` and coding-workflows `scripts/` are still never granted; at most 20 paths), and a grant resumes the issue with `/reclarify` when it has no plan yet. Every other planning block, and the existing comment text, labels and log lines, are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Stall on #9503 | about 6 hours |
| New GitHub API calls | 1 label create per routed block, 1 identity read per `/answer` on an `ai:scope-blocked` issue |
| New repo variables (both default `true`) | 2 |

What this means for consumer repos: after the next `@stable` sync, judge-created issues carry the companion scope entries, and a scope-only planning block can be cleared by the unblock judge instead of a person. Set either variable to `false` to keep today's behaviour.
