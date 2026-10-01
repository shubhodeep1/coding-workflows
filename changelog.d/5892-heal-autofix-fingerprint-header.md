<!-- changelog: fixed -->
- **Workflow failure heal no longer files unrelated review/autofix failures under one fingerprint.** The intake now ignores the reporter's own header lines when it fingerprints an `autofix_failure` report, and the reporter puts the run's first error into the evidence.

`scripts/workflow_failure_heal_autofix_report.sh` opens every evidence file with `failure_reason=`, `finalize_reason=`, `consecutive_failed_runs=` and a `flags: AUTOFIX_REVIEWERS_FAILED=…` line. That `flags:` line matched the `*_FAILED` signature pattern on every report, so any failure whose evidence had no `::error::` line got the same signature. Three heal issues about one resolver bug (#4411, #4447, #4459) carried fingerprint `218ad70d…`, and #4465 continued that lineage through its `root=` marker. The unrelated forward-merge failure on PR #5892 got the same fingerprint, so its first report continued the lineage of #4459 and escalated at generation 4 without any heal attempt. `scripts/workflow_failure_heal_intake.sh` now passes `--strip-autofix-header` to `workflow_failure_heal.py error-signature`, and the reporter writes `AUTOFIX_FAILURE_FIRST_ERROR` (the `**First error:**` of the PR failure comment) into the evidence as an `::error::` line, which the signature ranks first.

| The numbers that matter | Value |
| --- | --- |
| Heal issues that carried the header fingerprint | 3 (#4411, #4447, #4459) |
| Generation PR #5892's first report escalated at | 4 (cap 3) |
| Header lines ignored by the signature | 4 |

What this means for operators: a review/autofix failure now starts its own heal lineage unless it is the same error, or the same pull request (the `source=` marker, unchanged). Fingerprints of `autofix_failure` reports change once, so an open heal issue filed before this release is still matched for its own PR, but a new PR with the same error opens a new issue instead of an occurrence comment on it.

### For contributors

`strip_autofix_evidence_header()` removes only the leading run of header lines; the same text later in the evidence is kept. Older reporters still pinned in consumer repos send no first-error line, so their signature comes from the evidence tail instead of the header. Tests: `tests/test_workflow_failure_heal.py` (`test_strip_autofix_evidence_header_keeps_reporter_flags_out_of_signature`, `test_intake_autofix_fingerprint_ignores_reporter_header_lines`, `test_autofix_report_puts_first_error_into_evidence`).
