<!-- changelog: changed -->
- **A broken default branch now files one check-failure triage issue, not one per pull request, and triage stops filing once 5 triage issues are open.**

While the newest run of a check on the default branch has failed (`failure`, `timed_out`, `cancelled`, `startup_failure` or `action_required`), `scripts/check_failure_triage.sh` keys the triage issue on that branch instead of the PR. The first failing PR files one issue, titled `CI failure: <check> on <branch> (first seen on PR #<n>)`. Every other PR that fails the same check is folded into it (`CHECK_TRIAGE skip reason=base_triage_tracked`). The issue's fix runs on the default branch, not on the example PR's branch: `scripts/resolve_integration_ref.sh` reads the new `<!-- check-failure-triage:base=<branch> -->` header line and keeps the default branch (`reason=base_keyed`).

A new repository variable, `CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES` (default `5`, `0` = no cap), stops new filings once that many `ai:check-triage` issues are open (`CHECK_TRIAGE skip reason=open_issue_cap`, one Telegram WARNING per run). Historical-mode triage and supersede replacements are not capped.

| The numbers that matter | Value |
| --- | --- |
| Triage issues for one default-branch breakage across 17 open PRs | 17 before, 1 now |
| Default open-issue cap | 5 (`0` turns it off) |
| New GitHub API calls | 0: both guards read the open-triage listing the duplicate check already fetches |
| Renamed log keys, env names, labels or markers | 0 |

Unchanged: a green or pending base branch, an unknown base result, a base that is not the default branch, integration-branch / PR-head / target-branch routes, the heal-issue skip and historical mode.

What this means for operators: nothing to do. To allow more open triage issues, set the repository variable `CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES`. Note that with `CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true` the branch guard blocks a base-keyed issue, because it has no branch line.
