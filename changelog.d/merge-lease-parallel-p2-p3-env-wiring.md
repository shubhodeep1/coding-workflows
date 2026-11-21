<!-- changelog: changed -->
- **The merge-lease revocation and priority switches now take effect as repository variables.** `review_autofix.yml`, `orchestrate_poll.yml` and `cancel_on_pr_close.yml` now pass `MERGE_LEASE_REVOKE_ENABLED` (default `true`), `MERGE_LEASE_IDLE_MINUTES` (default `60`) and `MERGE_LEASE_PRIORITY_HEAD_PREFIXES` (default `auto/forward-merge-stable-`) beside the existing `MERGE_LEASE_*` switches. The defaults are identical to those in `scripts/pr_checks_lib.sh`, so nothing changes while the variables are unset. The rollbacks are now reachable without a code change: `MERGE_LEASE_REVOKE_ENABLED=false` turns off Phase 2 revocation, and `MERGE_LEASE_PRIORITY_HEAD_PREFIXES=none` (or `off`) turns off Phase 3 forward-merge priority. Known limitation: a repository variable set to an empty string falls back to the default through `||`, so only `none` or `off` turns the priority prefixes off from a repository variable.

What this means for consumer repos: after the next `@stable` sync, setting these repository variables changes the merge lease as documented in the Phase 2 and Phase 3 entries. Nothing needs to be done otherwise.

Refs #9781
