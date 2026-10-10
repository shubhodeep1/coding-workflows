<!-- changelog: security -->
- **Failure streaks now count only the pipeline's own comments.** This fixes the last open findings from the project security pass (Refs #6664).

The identical-failure cap already skips comments from other authors. The streak counter that decides when a failed review/autofix run is reported to workflow failure heal (`autofix-failure-streak` in `scripts/workflow_failure_heal.py`) did not: a PR commenter posting text that starts like a failure comment or an editor summary could raise or reset the reported streak. It now takes an optional `--author-login`. When that login is set, comments from any other author neither count nor end the streak. `scripts/workflow_failure_heal_autofix_report.sh` passes the login it already receives (`AUTOFIX_FAILURE_MARKER_AUTHOR`) whenever the staged helper supports the flag. Without a login, or with an older staged helper, the count works as before. Both report log lines gain `streak_author_filter=on|off`.

The other findings from the same pass (head-bound poller merges, freshness deferral, and reading the failure marker only from the comment's last line) were already fixed by #6988. A contract test fails CI if any poller `gh pr merge` call loses its `--match-head-commit` binding.

What this means for operators: nothing to change; no new variable.
