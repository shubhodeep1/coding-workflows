<!-- changelog: added -->
- **Check-failure triage can route a fix to the failing PR's own branch (off by default).** Refs #7056, from the unblock operator step for #6856.

A triage issue's fix PR always targeted the default branch, even when the failure belonged to an open PR's branch. A fix that only makes sense on that branch (#6856 needed `ai/issue-6838`) then had no `Target branch:` line to route it, and planning stayed blocked.

With `CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED=true`, `scripts/check_failure_triage.sh` adds one `Target branch: <head ref>` line to a new triage issue, above the diagnosis. It does so only when the PR it already fetched is:
- open,
- headed and based in this repository,
- not headed on the default branch, and
- on a branch whose name passes a strict check (including `git check-ref-format`).

The diagnose step checks the flag and the branch name again. The final body check accepts exactly that line, once, above the first `---`. Model and log text are still neutralized, so they cannot add or change routing. Each run logs `CHECK_TRIAGE routing outcome=emit target_branch=<ref>` or `outcome=skip reason=<reason>`.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 (reuses the existing PR read) |
| Routing lines a triage issue can carry | at most 1, written by the script |
| Behaviour with the flag unset | unchanged |

What this means for operators: set the repository variable to `true` to turn routing on. A triage fix PR merged into a PR branch closes its issue through the poller's close-merged sweep, which accepts a merge into the branch the issue declares.
