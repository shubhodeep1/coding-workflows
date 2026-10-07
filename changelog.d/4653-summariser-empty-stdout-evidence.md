<!-- changelog: fixed -->
- **Reviewer heal evidence now names a summariser that returned nothing.** When the consensus summariser's OpenCode call exits 0 with an empty final message, the `reviewers_failed` evidence says so instead of reducing to `reviewers_failed=true`.

`workflow_failure_heal.py reviewer-failure-evidence` now reads the summariser's `attempt N produced empty stdout` lines. It emits `summariser_exit rc=0`, `summariser_empty_stdout prefix=<prefix>` and `dominant_rc=0`, so the heal report and the failure fingerprint name the failure mode. Attempt counts stay out of the output, so repeated failures fingerprint the same. The `Internal: AI Review & Autofix` / `AI Review` failure-log artifact (`codex-review-autofix-failure-logs-<run>-<attempt>`) now also uploads `summariser_pass1.log` and `summariser_review.log`, which hold each attempt's stderr tail. The summariser's behaviour is unchanged: it still retries 10 times and fails closed on empty output.

| The numbers that matter | Value |
| --- | --- |
| Source failure | PR #4607, run 36317817104: 10 of 10 pass-1 attempts empty |
| Evidence before | `reviewers_failed=true`, `dominant_rc=unknown` |
| Evidence after | `summariser_exit rc=0`, `summariser_empty_stdout prefix=pass1`, `dominant_rc=0` |

What this means for operators: the next heal issue for this failure names the empty summariser output, and its failure-log artifact has the OpenCode stderr needed to find the cause.

### For contributors

A recurrence of this failure class gets a new fingerprint, because the evidence gains lines. The regression test `test_reviewer_failure_evidence_names_summariser_empty_stdout` in `tests/test_workflow_failure_heal.py` fails on the previous parser and passes with this change.
