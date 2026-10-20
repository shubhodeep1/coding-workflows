<!-- changelog: fixed -->
- **Standalone stall recovery no longer adds `ai:merged` for PRs merged outside the default or integration branch.** This stops the repeating "carries the `ai:merged` label but no linked merged PR could be verified" warning for check-triage issues.

When a standalone issue's linked PR had merged, stall recovery in `scripts/orchestrate_poll_process.sh` added `ai:merged` whatever branch the PR merged into. `close_merged_issues_sweep` only accepts merges into the default branch or the issue's declared integration branch, so it refused the close and sent a stale-label WARNING on every poll. Check-triage fixes always hit this, because their PRs merge into the failing PR's head branch (`multi-user-ai-agent` issue #300, fix PR #301 into `ai/issue-298`). Stall recovery now applies the same target-merge rule before adding the label. On a non-target merge it logs `STALL_SKIP ... reason=merged_non_target_base`. For an `ai:check-triage` issue it then runs `scripts/check_triage_resolution.py gate`, which closes the issue with an evidence comment once the failing PR has merged and its check is green on the default branch.

| The numbers that matter | Value |
| --- | --- |
| Extra calls for any merged linked PR with a known base | at most 1 `repos/<repo>` read per poller run (none when `CWS_DEFAULT_BRANCH` is set) |
| Extra calls for a non-target `ai:check-triage` merge | 1 `issues/<n>` read plus the gate's own reads, each tick until it closes |
| Off switch for the gate | `CHECK_TRIAGE_RESOLVED_GATE_ENABLED=false` |

What this means for operators: stale-label warnings for check-triage issues stop, and those issues close themselves once the fix is verified on the default branch. A standalone issue whose PR merged into some other branch stays open without `ai:merged`. Close it by hand, or merge its work into the default branch.

### For contributors

`_fetch_linked_pr_status_graphql` now returns `base_ref` from the same query. When the base ref or default branch is unknown, stall recovery adds `ai:merged` as before. The managed-child stall path is unchanged. `orchestrate_poll.yml` stages `check_triage_resolution.py` as an optional support script; without it the gate logs `reason=support_missing`.
