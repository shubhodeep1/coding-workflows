<!-- changelog: fixed -->
- **The orchestrator no longer marks an integration branch "superseded by main" from stale branch refs.** A project whose last wave PR merged just as a poll run started could be closed without its integration branch ever reaching the default branch.

`evaluate_sync_superseded_by_main` in `scripts/orchestrate_poll_process.sh` reads child-PR state live from the GitHub API, then diffs the child PRs' paths between the default and integration branches using the poll job's local refs. Those refs come from the checkout's fetch at job start. When a wave PR merged after that fetch, the API counted it as terminal while the local integration ref still matched the default branch. The diff came back empty, the branch was marked `superseded-by-main`, and the final integration merge was skipped for good. The function now refetches both branches right before the diff. If that fetch fails, it reports itself not confident and never marks the branch superseded.

| The numbers that matter | Value |
| --- | --- |
| Affected project | `shubhodeep1/fun-token-multi-chain` #739 (wave PR #741) |
| Gap between job-start fetch and the PR merge | 55 seconds (fetch 11:53:39Z, merge 11:54:34Z on 2026-11-08) |
| Extra cost per superseded check | 1 `git fetch` of two branches, no GitHub API calls |

What this means for operators: a project that reports "Integration branch superseded by main" now has its child PRs' changes on the default branch. A project hit by the old bug shows that comment while the compare API still reports the integration branch ahead of the default branch. Its work has to reach the default branch another way, for example the activation-fix issue the pipeline opens when activation grades the project DORMANT.

### For contributors

`tests/test_orchestrate_integration_ahead_by_gate.py` adds three cases that drive the extracted function against a local bare `origin`: a stale clone after the child PR lands on the integration branch (not superseded), a real supersede where the default branch already has the change (still superseded), and an unreachable `origin` (not confident).

The same file also adds `|| true` to the `blocker_stderr` cleanup pipeline in `_runtime_blocker_dispatch_eligible`. That line only builds the text of a fail-open warning, so under `set -euo pipefail` a failing `tr | sed | cut` can no longer abort the poller there. Behaviour is otherwise unchanged.
