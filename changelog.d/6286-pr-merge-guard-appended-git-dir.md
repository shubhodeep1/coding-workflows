<!-- changelog: security -->
- **The merged-PR guard no longer uses the value of an appended `GIT_DIR+=` or `GIT_WORK_TREE+=` override.** With `GIT_DIR+=<path> git push`, the guard already treated the working directory as unknown, but it still checked the repository at `<path>`. Bash appends that value to the existing variable, so the guard could check a different repository, and allow a push onto a merged branch.

The guard in `.claude/hooks/pr_merge_status_guard.py` and its `workflow-templates/` copy now ignores the unresolved value. It falls back to the session checkout, as it already did for any other directory it cannot resolve, and blocks a push from a branch whose pull request has merged. `tests/test_pr_merge_status_guard.py` covers both variables.

What this means for consumer repos: the fix reaches them on the next `@stable` sync, and nothing needs configuring.
