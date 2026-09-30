<!-- changelog: security -->
- **A Claude fixer no longer keeps working under outdated `.claude/` guards when the Claude-asset sync merge conflicts outside `.claude/`.**

The Claude-asset sync merges the default branch's `.claude/hooks/**` and `.claude/settings.json` updates into a working branch before a fixer touches it. Until now, `/fix-claude-pr` aborted that merge when it conflicted only outside `.claude/` and went on fixing and pushing on the unsynced head, still running the guard versions the default branch had fixed (security finding `asset-sync-outside-conflict-fail-open`, issue #5258). `/fix-claude-pr` and the `/implement-plan-claude` review-round and blocked-PR steps now resolve such a conflict inside the sync merge, keeping both sides' intent, and commit it with `git commit --no-edit` under the sync's `[claude-asset-sync]` subject. When the right resolution is not evident, they run `git merge --abort` and stop: `/fix-claude-pr` posts a hold claim, asks which side wins, and sends a push notification. They never continue on the unsynced head.

| The numbers that matter | Value |
| --- | --- |
| Commands changed | 2 (`fix-claude-pr.md` step 5, `implement-plan-claude.md` Claude-asset sync step 5) |
| GitHub API calls added | 0 |
| Report phrase dropped | `claude_assets=stale (conflict outside .claude/)` (never reached `main`) |

What this means for operators: a PR whose sync merge conflicts outside `.claude/` is now fixed under the current guards, because the merged guard files are in the working tree while the conflict is resolved. A conflict the fixer cannot settle parks the PR on a hold with a question instead of shipping a fix made under old guards.

### For contributors

The sync's source is always the PR's base (Claude-asset sync step 3), so a sync conflict is one the PR must resolve before it can merge anyway. For a `conflict` fix, the sync merge is that fix. `tests/test_claude_asset_sync_command.py` pins the rule and runs a scratch repository that stops a merge on an outside conflict, checks that the default branch's hook is already in the working tree, and resolves it with `git commit --no-edit`.
