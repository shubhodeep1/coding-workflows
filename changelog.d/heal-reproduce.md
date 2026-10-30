<!-- changelog: added -->
- **New helper `scripts/heal_reproduce.sh` checks whether a heal's failing tests really fail at the failing commit and pass at the last green one.** Nothing calls it yet; the heal intake and the heal editor will call it behind `WORKFLOW_HEAL_REPRODUCE_ENABLED` (off by default).

The helper takes the failing-test list, the commit to test (or an existing worktree) and the last green commit. It fetches what a shallow checkout is missing, adds a temporary worktree and runs the list in the existing credential-free pre-push gate container, which re-runs every failure on the merge-base. It writes a `heal_repro.v1` result: `reproduced` only when every listed test fails at the failing commit and passes at the last green one, `not_reproduced` with `pre_existing` or `passes_at_failing_sha`, and `unverified` with the gate's reason (`timeout`, `no_merge_base`, ...) when it cannot tell. The worktree and temp files are removed on success, failure and timeout.

| The numbers that matter | Value |
| --- | --- |
| Default | off (the callers gate on `WORKFLOW_HEAL_REPRODUCE_ENABLED=false`) |
| Wall time | 10 minutes (`WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES`, 1-60), then `unverified` / `timeout` |
| Fetch depth ladder | 50, 200, 1000 commits, then `no_merge_base` |
| Extra GitHub API calls | 0 |
| Log line | `HEAL_REPRODUCE outcome=<status> reason=<reason> tests=<n>` |

What this means for operators: nothing changes until a caller is wired and the switch is turned on. Test code only ever runs in the network-less, credential-free gate container.
