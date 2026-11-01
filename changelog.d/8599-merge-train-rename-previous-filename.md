<!-- changelog: fixed -->
- **The merge train now counts a renamed file's old path, so a queued PR that edits it waits behind the PR that renamed it.**

`scripts/review_merge_train.sh` read only the new path (`filename`) of a renamed file from `pulls/<n>/files`. When an older PR renamed `a.sh` to `b.sh` and a younger PR edited `a.sh`, the gate saw no overlap and let the younger PR through. It now also reads `previous_filename` when that is a non-empty string, the same as the diff-based path already did. It still makes one paginated call per PR per run.

What this means for operators: a PR may now show `ai:merge-queued` behind an older PR that renamed a file it edits. Before, the two raced and the second one usually hit a merge conflict.
