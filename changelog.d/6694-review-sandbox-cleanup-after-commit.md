<!-- changelog: fixed -->
- **A failed review sandbox cleanup no longer skips the commit and shows up as `editor_changes_lost`.**

In `review_autofix.yml`, `Clean up isolated review workspace` ran before `Commit changes`. When cleanup exited 1, the commit step's implicit `success()` skipped it, and the `!cancelled()` uncommitted-changes detector then reported the editor's work as lost (PR #6484, run 37666355049). Cleanup now runs immediately after `Commit changes`. It still runs with `always()` and its failure still blocks the push, auto-merge and ready labels. `scripts/review_untrusted_sandbox.sh cleanup` now names the first failing check or the removal cause in a path-free `REVIEW_SANDBOX_CLEANUP reason=...` line, which is appended to the editor's stage stderr so the failure headline and fingerprint carry it. Before failing, it repairs read-only directories inside the validated sandbox root once (no symlinks followed) and tries the removal again.

What this means for operators: a cleanup failure now reports its own cause instead of a misleading changes-lost diagnosis, and unpublished edits stay blocked before push.
