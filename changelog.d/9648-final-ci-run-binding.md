<!-- changelog: fixed -->
- **Approved PRs whose only CI run is their final run can auto-merge again.** The required-checks wait now recognises the final CI run, named `CI [pr:<n>]`, as a `CI` run, so its `lint` check counts.

Since #9535 (2026-11-16), a PR head gets a `CI quick` run and one full final run dispatched from review, and that final run is named `CI [pr:<n>]`. The wait before `gh pr merge --auto` (`_pr_wait_for_required_checks` in `scripts/pr_checks_lib.sh`) only accepted a `lint` check from a run named exactly `CI` (`AUTO_MERGE_WAIT_WORKFLOWS`), so on those heads it never found one. It waited the full 45 minutes, logged `ci_check=lint ci_state=absent` and refused auto-merge, even with a green final run. PR #9617 is the observed case: its final run passed at 03:48 UTC and the merge step still timed out at 08:04. A `workflow_dispatch` run named `CI [pr:<n>]` or `CI [pr:<n>] [<mode>]` now counts as a `CI` run for the binding, for the pending count and for the cancelled-run check.

| The numbers that matter | Value |
| --- | --- |
| Wait spent per refused attempt | 45 minutes (`AUTO_MERGE_CHECKS_WAIT_MINUTES` default) |
| Extra API calls | 0 |

What this means for operators: approved PRs stop cycling through 45-minute merge-step timeouts, and each merge attempt frees its runner sooner. `AUTO_MERGE_WAIT_WORKFLOWS` keeps its meaning; a workflow listed there now also matches its own `<name> [pr:<n>]` dispatch runs.
