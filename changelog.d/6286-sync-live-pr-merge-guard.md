<!-- changelog: fixed -->
- **The §21 merged-PR guard that coding-workflows sessions run now matches the copy consumers receive.** `.claude/hooks/pr_merge_status_guard.py` had fallen behind `workflow-templates/.claude/hooks/pr_merge_status_guard.py` after #6133, and five guard tests failed on `main`.

Sessions in this repository now ignore redirect targets and file-descriptor digits when reading push refspecs, and keep the working directory after `cd dir || exit`. They also treat `GIT_DIR+=` as unknown, read every positional as a refspec when `--repo` is given, check the checked-out branch for `git push origin HEAD`, and resolve the push source to a commit SHA before checking it. Consumer repos are unchanged; they already had this version.

What this means for operators: the `Merged-PR commit guard hook tests` step in `tests-hooks-and-orchestrator` passes on `main` again, and the review autofix stops trying to repair this drift inside unrelated pull requests.
