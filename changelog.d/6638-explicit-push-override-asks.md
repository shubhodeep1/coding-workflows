<!-- changelog: security -->
- **The merged-PR guard no longer checks the session checkout in place of a push's unresolvable explicit Git override.** A push such as `GIT_DIR+=/path/to/other/.git git push origin HEAD`, or one with an unresolvable `-C`, `env -C`, `GIT_DIR` or `--git-dir` path, now asks for confirmation without querying the session checkout's PR history.

Security finding `appended-git-dir-checks-wrong-checkout` (#6305, re-issued as #6638) showed the gap: Git applies an appended `GIT_DIR+=` value on top of the shell's, which the hook cannot read, yet the hook fell back to the session checkout and, from a clean default-branch checkout, let the push to the other repository's merged branch pass. Commits with an unresolvable override already asked without that fallback; pushes now follow the same rule. A push whose directory is unknown only because of shell control flow or an unresolved `cd` keeps today's behaviour: the session checkout is checked and can block, and otherwise the push asks.

What this means for operators: in an interactive session, a push that carries an explicit Git directory override the hook cannot resolve prompts once; the prompt names the override, not the session checkout. Nothing changes for ordinary pushes.

### For contributors

`_GitInvocation` gains `explicit_directory_unresolved`; `_guarded_git_invocations` sets it for appended selectors and unresolvable `-C` / `env -C` / `GIT_DIR` paths, and `evaluate` routes such pushes to the confirmation reasons before any PR lookup. Both hook copies are byte-identical. Tests: `test_appended_git_override_falls_back_without_using_rhs` (rewritten), `test_appended_git_override_exploit_asks_from_default_branch_checkout`, `test_unresolved_explicit_push_override_asks_without_checking_checkout` in `tests/test_pr_merge_status_guard.py`.
