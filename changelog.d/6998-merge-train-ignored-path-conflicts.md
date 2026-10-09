<!-- changelog: fixed -->
- **A conflict only in the generated workspace manifest no longer holds a PR in the merge train.** The train's conflict check now applies the same `MERGE_TRAIN_IGNORE_PATHS` list as its path-overlap rule.

`scripts/review_merge_train.sh` already ignored `.ai/.workspace_source_manifest.txt` when deciding whether two PRs edit the same files, because the later merge regenerates it. Its `git merge-tree` conflict check did not, so two PRs that merged cleanly everywhere except that manifest still blocked each other. The check now reports `conflict=ignored` and releases the younger PR when every conflicted path is on the ignore list. Setting `MERGE_TRAIN_IGNORE_PATHS=none` restores blocking on every conflict.

| The numbers that matter | Value |
| --- | --- |
| Blocking pairs on the 2026-10-09 23:4x poll that conflicted only in the manifest | 72 of 210 |
| Queued PRs released by this alone on that queue | 2 |
| New audit value | `MERGE_TRAIN_GATE ... conflict=ignored ... action=skip` |

What this means for operators: fewer queued PRs wait behind an older PR for a file nobody edits by hand; PRs with real conflicts (most often in `agents.md`) still go one at a time.
