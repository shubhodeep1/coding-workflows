<!-- changelog: fixed -->
- **A final-CI repair no longer replaces an approved head whose final CI has turned green.**

When a final CI run failed, two automations acted on the same failed attempt. The `final-ci-failed` hand-back dispatched a review run, and the sweep re-ran the cancelled job. In PR #9725 the re-run passed and the head was approved. The hand-back's review run then started more than an hour later. It still selected a final-CI repair, committed to the branch and replaced the green head, so the PR needed a new review and a new final CI run.

The review gate now reads the head's `ci/final` status from the statuses response it already fetches. If the newest `github-actions[bot]` `ci/final` is a `success` newer than the hand-back marker, the run ends with `skip_reason=final_ci_now_green`: no repair and no reviewer panel. The repair step checks again just before it would push. If the newest `ci/final` is a `success` newer than the hand-back marker, it drops any repair commit, pushes nothing and posts no marker. Both cases log `FINAL_CI_REPAIR ... outcome=skipped reason=final_ci_now_green`. When the hand-back defers to the sweep's re-run of abandoned cancelled jobs, it now also logs `REVIEW_FINAL_CI_FAILED ... outcome=deferred reason=cancelled_jobs_rerun`.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls in the gate | 0 (reuses its one `GET commits/<head>/statuses`) |
| Extra API calls per final-CI repair run | 1 (`GET commits/<head>/statuses`) |
| Extra API calls in the hand-back | 0 |
| New repo variables, jobs or workflows | none |

What this means for operators: an approved head that goes green while a hand-back review is still queued stays as it is and can merge. If a status read fails, the repair and hand-back run as before.
