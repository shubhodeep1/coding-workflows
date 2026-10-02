<!-- changelog: security -->
- **Claude sessions no longer push after the Claude-asset sync merges a `.claude/settings.json` they cannot confirm they loaded.** The work continues in a fresh session instead.

The Claude-asset sync merges the default branch's hook fixes and hook wiring into long-running Claude branches. Claude Code's file watcher normally reloads a changed `settings.json`, but it can miss a change, so a new `PreToolUse` guard could be merged while the running session kept pushing without it (security finding `asset-sync-settings-not-active`, issue #5259). A new hook, `.claude/hooks/settings_load_recorder.py`, records the sha256 of the `settings.json` each session loaded, at session start and at every change the watcher detects. After a sync merge that changes `settings.json`, `/implement-plan-claude` stages and `/fix-claude-pr` run `.claude/scripts/loaded_settings_check.py`. When the merged file is not the one the session loaded, the session writes nothing more for that work and starts a fresh session for the same stage or fix. A second miss stops the project with an `ai:claude-blocked:v1` blocker, or a hold in `/fix-claude-pr`.

| The numbers that matter | Value |
| --- | --- |
| New hook and helper | `.claude/hooks/settings_load_recorder.py`, `.claude/scripts/loaded_settings_check.py` |
| Record | `~/.claude/loaded-settings/<session id>.json`, outside the repository |
| Wait for a late watcher reload, per check | up to 10 s (`LOADED_SETTINGS_CHECK_WAIT_SECONDS`, `--wait-seconds`) |
| Restarts per stage or fix | 1, then a §28.C escalation |
| GitHub API calls added | 0 |

What this means for operators: a guard registration that reaches a working branch through the asset sync is active before that branch's next push, or the push happens in a fresh session that loaded it. A session with no record, such as a local CLI session, fails closed and asks for a new session. A branch cut before this change has no recorder in its own `settings.json`, and Claude Code runs `ConfigChange` with the hooks loaded before a change, so its first sync that changes `settings.json` cannot be confirmed: it ends in the escalation once, with a reason that says so, and a human confirms the merged wiring and pushes the sync merge.

### For contributors

The recorder is wired under `SessionStart` and under `ConfigChange` with matcher `project_settings`. It records nothing on `clear` and `compact`, prints nothing, never blocks, and must stay the only `ConfigChange` hook, because a hook that blocked a change would leave a record for a change that was never applied. The sync runs the check as `loaded_settings_check.py --before HEAD^1`, which adds `before_recorder_wired` (whether the branch's `settings.json` before the merge wired the recorder) and never changes the verdict. `--before` must be a plain revision name: a value that `git` could read as an option (a leading `-`, a `:`, whitespace) exits 2 before any `git` runs, and `git show` gets `--end-of-options` before the revision. When the sync merge stops on a conflict outside `.claude/`, the check runs as `--before HEAD` before anything is resolved, so no conflict work runs under unconfirmed wiring. Step 6 of the Claude-asset sync no longer says the harness reads `settings.json` only at session start. Tests: `tests/test_settings_load_recorder.py` and `tests/test_claude_asset_sync_command.py`.
