<!-- changelog: fixed -->
- **A PR whose final CI passes after the merge step stopped waiting is now merged on the next poll tick.** Before this, green integration children sat unmerged once the merge step's checks wait had run out.

When a merge step's 45-minute checks wait times out, the head is marked with an `ai-review/final-ci-merge-pending` status. The merge-train release step, which runs on every poll tick and every PR close, enables head-bound auto-merge as soon as that head's final CI is green. This builds on the per-base merge lease already described for the stable line, and it applies only on `main` of this repository, where the final CI run exists.

| The numbers that matter | Value |
| --- | --- |
| Final CI run time on the congested pool, 2026-11-17 | 1.5 to 2.5 hours |
| Merge-step checks wait (`AUTO_MERGE_CHECKS_WAIT_MINUTES`) | 45 minutes |
| Release step cadence | every poll tick (15 min) and every PR close |
| New jobs or workflows | 0 |

What this means for operators: fewer final CI runs per merged PR. PRs waiting on their final CI show `ai:final-ci-merge-pending`. `FINAL_CI_MERGE_RESUME_ENABLED=false` turns the resume off.

### For contributors

The resume trusts only a status that the pipeline login created on the exact head SHA, together with a pipeline `ai-review/head-gate` success. Code: `_pr_final_ci_merge_pending_mark` in `scripts/pr_checks_lib.sh` and `_mt_final_ci_merge_resume` in `scripts/review_merge_train.sh`. Tests: `tests/test_merge_lease.py`.
