<!-- changelog: fixed -->
- **The §21 merged-PR guard now reads `--no-tags` the way git does, so `git push --tags --no-tags origin` is checked like `git push origin`.** Before this fix, a later `--no-tags` let a push that strands work on a merged branch skip the check.

The guard in `.claude/hooks/pr_merge_status_guard.py` treats `git push --tags <remote>` with no refspec as a tags-only push and judges no branch. It only looked for `--tags`. git applies the last of `--tags` / `--no-tags`, and `--tags --no-tags` pushes the checked-out branch. The guard now does the same, wherever the two words sit among the push arguments. A cancelled `--tags` is judged on the current branch, and in a directory the guard cannot resolve it falls back to the session checkout with a warning. Found by the security audit (issue #6089).

| The numbers that matter | Value |
| --- | --- |
| Spellings that cancel `--tags` | `--no-tags`, `--no-tag`, `--no-ta` (git refuses `--no-t` as ambiguous with `--no-thin`) |
| Still not judged | a push whose last word of the pair is `--tags` (or `--tag`, `--ta`) and that names no refspec |
| GitHub API calls | unchanged: at most 1 per `(slug, branch)` pair judged |

What this means for operators and supervising sessions: adding `--no-tags` to a tags push no longer gets past the merged-PR check. Plain `git push --tags origin` behaves as before.
