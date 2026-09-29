<!-- changelog: fixed -->
- **Long-running Claude branches now pick up the default branch's `.claude/` guard fixes when a stage or fixer session starts work on them.**

A Claude Code session runs the hooks of the branch it has checked out. A project or PR branch cut before a guard fix therefore kept running the old guard: on 2026-09-29 the session working on PR #4641, whose branch was 43 commits behind `main` and lacked #4704, stopped at a permission prompt on a plain read of `CLAUDE.md`. `/implement-plan-claude` stages, `/implement-issue-claude` resumes, and `/fix-claude-pr` now check each working branch right after checkout. When the default branch holds `.claude/hooks/**` or `.claude/settings.json` changes the branch lacks, they merge it in with a `[claude-asset-sync]` merge commit. A conflict under `.claude/` aborts the merge and stops with an `ai:claude-blocked:v1` blocker that names the files and the commits on each side. `.claude/hooks/session-start.sh` also logs `[session-start] claude_assets=stale behind=<n> files=<list>` when a web session starts on a stale checkout, or `claude_assets=diverged behind=unknown` when shallow history has no merge base and the direction of a difference is unknown.

| The numbers that matter | Value |
| --- | --- |
| Commands changed | 3 (`implement-plan-claude.md`, `implement-issue-claude.md`, `fix-claude-pr.md`) |
| GitHub API calls added | 0 (local git plus one `git fetch`) |
| Hook network calls | at most 2 (`git ls-remote` only when `origin/HEAD` is unset, then 1 fetch), 15 s each, skipped without GNU `timeout`, failures ignored |

What this means for operators: guard fixes on the default branch now reach every in-flight Claude project the next time a session works on its branch, with no manual merge. A project that edits the same guard stops with a precise blocker instead of silently running the old one. A merged `settings.json` change takes effect from the next session, because the harness reads it only at session start.

### For contributors

Only branches that land in the default branch are synced. A branch bound for `stable`, another PR's head, or a non-default issue base is left alone and reported as `claude_assets=stale (base <base>)`. A PR head on a lagging project branch syncs the project branch first, so its PR diff stays limited to its own change. Tests: `tests/test_claude_asset_sync_command.py` and `tests/test_session_start_claude_assets_drift.py`.
