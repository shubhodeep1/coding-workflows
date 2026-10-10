<!-- changelog: added -->
- **Check-failure triage can now route its fix to the failing PR's own branch, behind a flag that is off by default.**

With `CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED=true`, `scripts/check_failure_triage.sh` adds one ``- **Integration branch:** `<head ref>` `` line to the triage issue, so the pipeline plans and implements the fix on the PR's head branch instead of the default branch. The line is added only when the PR is open, comes from the same repository, has a head SHA equal to the failing check's SHA, and has a valid branch name as its head ref. Any other case logs `CHECK_TRIAGE skip reason=routing_unverified detail=<reason>`, sends a Telegram WARNING, and files no issue. The diagnose step checks the verified ref again before writing the body, and the body validator admits only that one script-generated routing line; text from CI logs or the model is still neutralized.

What this means for operators: nothing changes until you set the repository variable `CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED` to `true`.
