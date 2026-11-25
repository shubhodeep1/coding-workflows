<!-- changelog: fixed -->
- **A failed `lint` no longer blocks a PR's merge forever once a later CI run on the same head passed.** The required CI check now uses the newest CI run on the head when a newer run of the same check completed with success.

The merge gate (`_pr_checks_completed` and `_pr_wait_for_required_checks` in `scripts/pr_checks_lib.sh`) counted every check-run of the required CI check (`lint` in this repository) on the head and failed closed, so one failed run outranked a later success. Before this change only a cancelled run could be superseded by a newer one. PR #9839 hit this. Its `lint` failed at 17:15 on 2026-11-20 because `orchestrate-poll (3)` was cancelled, then passed at 18:01 on the same head. Every review-blocked judge "merge" after that logged `AUTOFIX_AUTO_MERGE_CHECKS ... ci_state=failure` and withheld the merge. The head gate stayed `pending`, and the review-round cap parked the PR. Now a completed failure is dropped when a newer run of the same check, bound to the CI workflow on the same head, completed with `success`.

| The numbers that matter | Value |
| --- | --- |
| Checks affected | only the configured CI check (`AUTO_MERGE_REQUIRED_CI_CHECK`, default `lint` here) |
| When it applies | both runs bound to CI workflow run ids (`PR_CHECKS_CI_RUN_IDS`), newer run completed with `success` |
| Still blocking | a newer failure, a still-running run, unbound name-only matching, any other required check |
| Extra API calls | none |

What this means for operators: a PR whose CI was re-run green after an infrastructure failure, such as a cancelled shard, merges on that head without a new push. A real failure still blocks unless a newer CI run on the same head passed. The newer-run order uses the check-run id, then its start time, as for cancelled runs.
