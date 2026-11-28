<!-- changelog: changed -->
- **The merge lease now allows two holders per base branch by default.** The shipped default of `MERGE_LEASE_MAX_HOLDERS` is now `2` (it was `1`) in `scripts/pr_checks_lib.sh` and in every workflow step that passes the setting. Two pull requests whose changed paths are pairwise disjoint (the soft path class applies) may hold a base's merge lease at once; a pull request that shares a path with a holder still waits. An empty or non-integer value now also falls back to `2`. The accepted range stays 1 to 5. A repository that already sets the variable keeps its value. Consumers get the change at the next `@stable` sync. Set `MERGE_LEASE_MAX_HOLDERS=1` to keep the single-holder lease.

Refs #10124
Refs #9781
