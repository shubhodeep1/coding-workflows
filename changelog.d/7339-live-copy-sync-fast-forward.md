<!-- changelog: fixed -->
- **The `.claude` live-copy sync can refresh an existing sync branch again.** Refs #7137.

`Sync live .claude copies` rebuilt its branch from the pushed `main` commit and force-pushed it. The repository ruleset refuses non-fast-forward pushes and branch deletion on every branch, so once `ai/sync-claude-live-copies-held` existed (since 9 October), every later sync of a held change failed at the push. Run 38364113936 for #7146 failed that way, and `main` CI stayed red until the copy was made by hand. The refresh commit now has two parents, the old branch tip and the pushed `main` commit: the push fast-forwards, and the pull request still shows only the copied live files. A failed push now logs git's own error text.

| The numbers that matter | Value |
| --- | --- |
| Script | `scripts/sync_claude_live_copies.py` |
| New log lines | `CLAUDE_LIVE_SYNC fast_forward branch= previous= after=`, `error reason=push_failed branch= detail=` |

What this means for operators: template changes to `workflow-templates/.claude/` reach this repository's own `.claude/` through the sync PR again, including held ones, without anyone deleting the old sync branch.
