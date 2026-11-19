<!-- changelog: added -->
- **CLAUDE.md §32 makes interactive sessions read production data through the repo's read-only DB bridge themselves.** Sessions now ask the user for data only when the bridge cannot answer.

When a repo's agents file documents a read-only DB bridge (an "Agent DB bridge" section), interactive Claude Code sessions must query it before asking for anything. That covers `/deploy-activate` and `/verify-activation` verification steps, removal preflight checks in `docs/scripts-pending-removal.md`, and investigations. Sessions no longer ask the operator to run a query, open a database client, or paste data, and they never pull database credentials to connect directly. They go to the user only when the bridge's env vars are missing, the correct POST fails, or the data is outside the bridge's scope.

What this means for operators: fewer runbook steps hand you a query to run. A repo with no documented bridge sees no change, and `unattended_system_instructions.md` is unchanged.
