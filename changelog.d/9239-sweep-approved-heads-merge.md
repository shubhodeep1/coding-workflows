<!-- changelog: fixed -->
- **Approved PRs whose final CI passed merge again, and the review sweep stops re-reviewing approved heads it cannot merge.** The auto-merge wait never recognised a dispatched final-CI run, so every approved PR held a runner for 45 minutes and then refused to merge.

`ci.yml` names its dispatched final-CI runs `CI [pr:<n>]`, and the runs API reports that run-name as the run's name. The merge wait in `scripts/pr_checks_lib.sh` matched runs against `AUTO_MERGE_WAIT_WORKFLOWS` (`CI`) by exact name, so the required `lint` from a final-CI run read as `absent`: PR #9603, gate success with a green final CI, logged `outcome=timeout waited_s=2700 ci_check=lint ci_state=absent` and stayed open. The wait now strips the ` [pr:<n>]` and ` [sync-quick]` suffixes before comparing. It also stops at once with `reason=final_ci_dispatch_failed` when the head has no CI run and the final-CI dispatch was refused (a branch whose `ci.yml` predates `workflow_dispatch`, PR #9579), instead of waiting out the budget. `review_autofix_sweep.yml` now reads every open PR's `ai-review/head-gate` state in one GraphQL call per 100 PRs, skips the review dispatch for an approved head whose final CI failed (PR #9239 had 40 gate statuses on one head), and the final-CI safety net checks approved heads first instead of spending its cap on gate-pending ones.

| The numbers that matter | Value |
| --- | --- |
| Runner time held per refused merge wait (before) | 45 min (`AUTO_MERGE_CHECKS_WAIT_MINUTES`) |
| Sweep tick 2026-11-17 08:30, dispatches to approved heads | 5 of 15 |
| Final-CI sweep at the same tick | `candidates=42 checked=10 gate_not_success=10 ensured=0` |
| New API cost per sweep tick | 1 GraphQL call per 100 open PRs, plus 1 runs read per approved head about to be dispatched |
| New repo variable | `SWEEP_SKIP_APPROVED_FINAL_CI_FAILED` (default `true`) |

What this means for operators: approved PRs with a green final CI merge on their next review run, and fewer review runs queue on the shared runner pool. A PR whose final CI failed still needs a fix push or a CI re-run; set `SWEEP_SKIP_APPROVED_FINAL_CI_FAILED=false` to have the sweep re-review those heads again.
