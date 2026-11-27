<!-- agents: section="Stable log prefixes (contractual)" -->
- `MERGE_LEASE_METRIC` (merge-lease parallel merges Phase 1, Refs #9781). `_merge_lease_metric` in `scripts/pr_checks_lib.sh` prints it to stdout right after each `MERGE_LEASE` decision line in `_merge_lease_gate` and `_mt_merge_lease_tick` (`scripts/review_merge_train.sh`). Format: `MERGE_LEASE_METRIC event=<e> base=<ref|-> pr=<n|-> mode=update|merge|tick reason=<token> holder=<n|-> hold_s=<n|-> waiters=<n|-> ts=<epoch|->`.
  - `event` values. Gate: `acquired`, `renewed`, `wait`, `proceed`, `retry` or `unavailable`. Tick: `held`, `released`, `granted`, `no_waiter`, `lost_race`, `write_failed` or `unavailable`.
  - `event=released` lines (tick only) end with ` merged=true|false`: `true` only when the holder-closed read (the existing `pulls/<n>` call, whose `--jq` now returns `.state` and whether `merged_at` is set) shows a merged PR; `expired` and `revoked_*` releases give `false`. This is the plan's K1 wasted-hold share (released with `merged=false` / all released). Every other metric line has no `merged` field.
  - `reason` reuses the `MERGE_LEASE` reason token; `acquired` and `renewed` use their own name, and a failed tick grant uses `grant`.
  - `event` and `reason` are limited to `[a-z_]`; numbers are digits only. Anything else prints as `-`. No path, label or record text is printed.
  - `hold_s` is the time since the record's `acquired=` (`-` when the record has none), and `0` for `acquired` and `granted`.
  - `waiters` (tick only) is the number of open non-draft PRs on the base that carry `MERGE_LEASE_WAIT_LABEL`, counted from the open-PR list already read.
  - A PR's wait time runs from its first `event=wait` `ts` to its `event=acquired` or `event=granted` `ts`. The `granted` `ts` is the moment the lease write succeeded, taken before the wait label is removed and the review is dispatched.
  - A PR that renews its own expired hold gets a new `acquired=`, so it logs `event=acquired hold_s=0`; its `MERGE_LEASE` line still says `outcome=renewed`.
  - `holder` is `-` when the lease is free (`no_waiter`), after a failed tick grant, and for `invalid_input` (no record read).
  - No GitHub API call is added. The helper always returns 0. The tick calls it only when `declare -F _merge_lease_metric` finds it.
  - Kill switch: `MERGE_LEASE_METRICS_ENABLED`, default `true` in the helper (`true`/`1`/`yes`/`on`).
LOG_PREFIX.name=MERGE_LEASE_METRIC
