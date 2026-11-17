<!-- changelog: fixed -->
- **Workflow failure heal now diagnoses a review/autofix head stopped as `review_editor_sandbox_path_excluded` instead of rejecting the report.** The sandbox-excluded escalation (#8321) records its failure marker on a review run that ends `success` by design, and the identical-failure cap run that reports it also ends `success`. The intake's provenance gate accepted only `failure` or `timed_out` runs, so every such cap report was rejected with `WORKFLOW_HEAL skip reason=provenance_rejected ... detail=no_verified_runs` and a Telegram WARNING. The PR never got its non-retryable diagnosis comment or `ai:needs-human`. PR #9112 hit this in intake run 42583232747: both cited runs were rejected as `not_failed`, and no PR in this repository has ever received that diagnosis.

`verify_run_provenance` in `scripts/workflow_failure_heal.py` now accepts a completed `success` run for an `autofix_failure` report only when the trusted account's trailing `review-autofix-failure:v1` marker names that run, the report's PR head, and a reason in the new `SUCCESS_RUN_FAILURE_MARKER_REASONS` set. Today that set holds only `review_editor_sandbox_path_excluded`. The workflow-path and PR-link checks still apply. Markers from another author, for another run or head, quoted after other text, or carrying a retryable reason still get `not_failed`, as do `cancelled` runs.

| The numbers that matter | Value |
| --- | --- |
| Reasons that may cite a green run | 1 (`review_editor_sandbox_path_excluded`) |
| Runs rejected in intake run 42583232747 | 2 of 2 (`not_failed`) |
| New GitHub API calls | 0 (uses the comment snapshot the gate already reads) |

What this means for operators: a PR whose only fix needs a path the review editor sandbox does not admit now gets the "cannot be fixed by another run" comment and `ai:needs-human` on its first cap, instead of a `no_verified_runs` WARNING and a PR left with no label telling a person to act. PR #9112 needs that manual change and label now; a new push to it re-runs the review.

Refs #9112.
