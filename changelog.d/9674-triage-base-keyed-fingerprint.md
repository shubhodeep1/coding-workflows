<!-- changelog: changed -->
- **A broken default branch now files one check-failure triage issue, not one per pull request, and triage stops filing once 5 triage issues are open.**

While the newest run of a check on the default branch has failed (`failure`, `timed_out`, `cancelled`, `startup_failure` or `action_required`), `scripts/check_failure_triage.sh` keys the triage issue on that branch instead of the PR. The first failing PR files one issue, titled `CI failure: <check> on <branch> (first seen on PR #<n>)`. Every other PR that fails the same check is folded into it (`CHECK_TRIAGE skip reason=base_triage_tracked`). The issue's fix runs on the default branch, not on the example PR's branch: `scripts/resolve_integration_ref.sh` reads the new `<!-- check-failure-triage:base=<branch> -->` header line and keeps the default branch (`reason=base_keyed`).

A new repository variable, `CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES` (default `5`, `0` = no cap), stops new filings once that many `ai:check-triage` issues are open (`CHECK_TRIAGE skip reason=open_issue_cap`, one Telegram WARNING per run). Historical-mode triage and supersede replacements are not capped.

| The numbers that matter | Value |
| --- | --- |
| Triage issues for one default-branch breakage across 17 open PRs | 17 before, 1 now |
| Default open-issue cap | 5 (`0` turns it off) |
| New GitHub API calls | 1, only when a base-keyed issue is about to be filed: the post step lists open triage issues again just before creating it. The collect-stage guards read the listing the duplicate check already fetches |
| Renamed log keys, env names, labels or markers | 0 |

Unchanged: a green or pending base branch, an unknown base result, a base that is not the default branch, integration-branch / PR-head / target-branch routes, the heal-issue skip and historical mode.

What this means for operators: with the branch guard off (the default), nothing to do. To allow more open triage issues, set the repository variable `CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES`. If you run `CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true`, a base-keyed issue has no branch line, so planning blocks it (`ai:blocked`). This includes repositories that also set `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED=true`, where such issues used to pass the guard as `pr_head_routed`. Fix the default branch by hand, or turn the guard off.

Runs for different pull requests are not serialized, so the post step lists open triage issues once more just before it files a base-keyed issue. If another run filed it in the meantime, it files nothing (`CHECK_TRIAGE skip reason=base_triage_tracked ... stage=post`). If that read fails, it files as before (`CHECK_TRIAGE base_keyed recheck=read_failed outcome=file`). Two runs that reach the create within the same few seconds can still both file. The open-issue cap is counted from the collect-stage listing only, so overlapping runs can go slightly past it.
