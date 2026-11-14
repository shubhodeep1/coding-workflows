<!-- changelog: fixed -->
- **Outage CI failures and extensionless plan files no longer need a human.** A check that fails on attempt 1 because of an external outage re-runs its failed jobs once instead of filing a triage issue. A triage issue already filed closes itself once a later attempt passes on the same head. A pipeline plan grant no longer drops an exact extensionless file such as `bin/deploy` or `public/CNAME`.

Two stall classes seen in consumer repos still needed a person after #9126 (Refs shubhodeep1/fun-token-multi-chain#765, Refs shubhodeep1/fun-token-multi-chain#771):

- **Outage failure on an open PR.** Check-failure triage now runs a `transient-rerun` stage in its own `GH_PAT` step, between the upstream route and the post step. It sends one `rerun-failed-jobs` POST, only when all of these hold: the diagnosis has no `AFFECTED_FILES` block, a failed job's log tail for that run shows an HTTP 5xx, a timeout or a connection error, the run is attempt 1, and the PR is still open on the triaged head. It then files no issue. Any other case, a read error or a non-201 response files the issue as before. The check-triage resolution gate (`scripts/check_triage_resolution.py`) also resolves an open PR whose head did not move when a later attempt of the triaged run passed, with every earlier-failed job green (`path=open_head_rerun`). Other same-head cases keep the reason `open_head_unchanged` and add `rerun_reason`.
- **Extensionless file left out of a grant.** `scripts/files_touched_scope_guard.py --planning-root PATH` (plan.yml passes its planning checkout) grants a nested extensionless file only when it is a regular file there. It must not be a symlink and must stay inside the root. A directory, a missing path, or a check with no root (the poller) makes a pipeline plan grant hold with `plan_path_not_grantable` and `replannable: true`. The same hold applies when the unverified path is the plan's only exact file or a protected path such as `scripts/deploy`. Only the file a Files line lists counts (a bold, checkbox or comma-joined listing included), so a prose reference such as `origin/main` or `actions/checkout@v5` later on the line never holds a plan. The poller's #8339 flow then re-plans once, and its `/answer` names the unverified path as the cause.

| The numbers that matter | Value |
| --- | --- |
| Automatic re-runs per workflow run | at most 1 (attempt 1 only) |
| Extra REST calls per qualifying triage | 1 read + 1 POST |
| Extra reads per same-head resolution | at most 1 |
| New switches (default `true`) | `CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED`, `PLAN_GRANT_TREE_FILE_CHECK_ENABLED` |

What this means for consumer repos: after the next `@stable` sync, an outage failure on an open PR is re-run once with no issue filed, and a plan that lists an extensionless file either grants it exactly or re-plans once. Set either repository variable to `false` to keep the earlier behaviour.
