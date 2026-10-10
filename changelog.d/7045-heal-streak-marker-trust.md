<!-- changelog: security -->
- **The failure-streak count no longer trusts other people's comments when it cannot tell who wrote them.** This completes the streak part of the project security pass (Refs #6664).

`scripts/workflow_failure_heal_autofix_report.sh` decides when a failed review/autofix run is reported to workflow failure heal. It counted every PR comment that looked like a failure or an editor summary whenever it had no pipeline login, so a PR commenter could raise the streak to force heal dispatches or end it to hide failures. It now takes the login from the identical-failure cap (`AUTOFIX_FAILURE_MARKER_AUTHOR`), then from the gate's resolved login (`AUTOFIX_STREAK_AUTHOR_LOGIN`, which the "Report autofix failure to workflow failure heal" step now passes), then from one `gh api user` read when there are PR comments to count. A login that is not a valid GitHub login is ignored. Without a usable login, or with a staged helper that has no author filter, the streak counts only the current run and the reporter logs `warn reason=streak_author_login_unavailable` (or `streak_author_filter_unavailable`). #7018 described this as working as before; it now fails closed.

The other findings from the same pass were already fixed on the integration branch: every poller merge is bound to its checked head (#6988), an unknown merge-base freshness result defers the merge, and failure markers are read only from a trusted comment's last line.

What this means for operators: nothing to change. `AUTOFIX_STREAK_AUTHOR_LOGIN` is optional and defaults to empty.
