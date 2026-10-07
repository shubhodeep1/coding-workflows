<!-- changelog: security -->
- **The merged-PR guard asks for confirmation when a push's effective repository cannot be resolved.** With `GIT_DIR+=<path> git push`, Bash may push from a different repository than the session checkout. The guard already discarded the appended value, but could allow that push based on the checkout's branch instead.

Both guard copies still check the session checkout and block if its branch has a merged pull request. Otherwise an unresolved push directory prompts for confirmation, rather than treating the checkout's open or default branch as proof that the pushed branch is safe. `tests/test_pr_merge_status_guard.py` covers both appended variables.

An unresolvable explicit directory override on `git commit` now asks for confirmation without checking another checkout's pull requests. Commits whose directory is uncertain only because of shell control flow retain the existing checkout check and warning behavior.

What this means for consumer repos: the fix reaches them on the next `@stable` sync, and nothing needs configuring.
