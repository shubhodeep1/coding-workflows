<!-- changelog: fixed -->
- **Queued PRs wait less behind older ones in the merge train.** A conflict only in the generated workspace manifest no longer blocks, and an older PR stuck in review stops blocking after 6 hours instead of 24.

`scripts/review_merge_train.sh` already ignored `.ai/.workspace_source_manifest.txt` when deciding whether two PRs edit the same files, because the later merge regenerates it. Its `git merge-tree` conflict check did not, so two PRs that merged cleanly everywhere except that manifest still blocked each other. The check now reports `conflict=ignored` and releases the younger PR when every conflicted path is on the ignore list. Setting `MERGE_TRAIN_IGNORE_PATHS=none` restores blocking on every conflict.

The head-age cap `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` now defaults to 6 hours in the script and in the `review_autofix.yml`, `orchestrate_poll.yml` and `cancel_on_pr_close.yml` callers. An older overlapping PR that is not itself queued and has been under review for longer than that (counted from its newest `ai:merge-queued` removal) stops blocking. A repository variable still overrides it, and `0` still disables it.

| The numbers that matter | Value |
| --- | --- |
| Blocking pairs on the 2026-10-09 23:4x poll that conflicted only in the manifest | 72 of 210 |
| Queued PRs released by this alone on that queue | 2 |
| New audit value | `MERGE_TRAIN_GATE ... conflict=ignored ... action=skip` |
| `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` default | 24 → 6 hours |

What this means for operators: fewer queued PRs wait behind an older PR for a file nobody edits by hand; PRs with real conflicts (most often in `agents.md`) still go one at a time.
