<!-- changelog: fixed -->
- **A judge-created issue no longer stops at planning just because its `files_touched` list is too narrow** (Refs #9503).

#9503 waited about 6 hours at planning, and held its project's integration PR, because the planner needed a script and its test that the judge had left out of `files_touched`. Now:

- Judge additions and judge fix-ups get the same `tests/**`, `changelog.d/*` and `agents.d/*` companions as plan-derived scopes. The judge prompt also asks for every script an existing job runs when the issue reuses that job.
- When the planner's only blocker is missing scope, it lists the files on `SCOPE_MISSING:` lines. The blocked comment then carries a trusted guard-rejection marker and the issue gets `ai:scope-blocked`, so the unblock judge can widen the scope with `override_guard` under its usual path bans. A grant before any plan exists restarts the issue with `/reclarify`.
- Every other planning block behaves as before.

The change adds no GitHub API calls. Set `JUDGE_FILES_TOUCHED_COMPANIONS_ENABLED=false` or `PLAN_SCOPE_BLOCK_ROUTING_ENABLED=false` to switch either part off; both are on by default.
