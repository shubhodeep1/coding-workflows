<!-- changelog: fixed -->
- **A pull request whose CI failed can no longer be squash-merged by the orchestrator or the review-blocked judge.** The shared merge gate's default required-checks list now names `lint`, the aggregate job of `.github/workflows/ci.yml`, next to `CI`.

The gate in `scripts/pr_checks_lib.sh` blocks a failed check-run only when its name is in `ORCH_FINAL_MERGE_REQUIRED_CHECKS`. GitHub reports the CI workflow's jobs as check-runs (`lint`, `static-checks`, `tests-hooks-and-orchestrator`, …) and never a check-run called `CI`, so a failing CI job matched nothing in the old default and the judge-approved merge of PR #6643 landed on `main` with red CI on 2026-10-09, breaking every main run until #6839. With `lint` in the list, a red CI blocks the final integration merge and all four review-blocked merge paths; pending check-runs block as before.

| The numbers that matter | Value |
| --- | --- |
| Default list entries | 6 (was 5) |
| Merge paths governed | 5 (final integration merge, four review-blocked paths) |
| Incident PR | #6643 |

What this means for operators: repositories that set `ORCH_FINAL_MERGE_REQUIRED_CHECKS` themselves are unchanged; add the name of your own CI aggregate check-run there. Repositories on the default get the new behaviour on the next `@stable` sync.

### For contributors

`tests/test_pr_checks_lib_required_filter.py` pins the default string in both `scripts/pr_checks_lib.sh` and `scripts/orchestrate_poll_process.sh` and now includes a case where `lint` fails while every other listed check is green.
