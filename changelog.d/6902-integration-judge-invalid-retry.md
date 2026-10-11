<!-- changelog: fixed -->
- **One invalid answer from the integration-conflict judge no longer ends a project's self-heal.** The poller retries the judge on a later tick and logs why the answer was rejected.

Project #6902's final PR #6905 was reported as "Integration self-healing exhausted" after its first and only judge call (run 38107119024): the judge's answer failed validation, which was terminal, and its output and log were deleted unread. An invalid answer (non-zero exit, unparseable JSON, wrong schema, or a diagnosis or guidance over 2000 characters) now spends one slot of `INTEGRATION_CONFLICT_LIFETIME_MAX` and keeps the project in `healing`, so the next poll tick asks again. The run logs `INTEGRATION_JUDGE_INVALID pr=<n> reason=<r>` and prints the last 20 lines of the judge log. A judge that never answers validly still stops at the lifetime cap.

| The numbers that matter | Value |
| --- | --- |
| Judge calls before exhaustion (`INTEGRATION_CONFLICT_LIFETIME_MAX` default) | 10 (was 1 when every answer was invalid) |
| Diagnosis and guidance length limit | 2000 characters |

What this means for operators: a flaky judge answer no longer pages "Manual intervention required", and when the judge does keep failing, the job log says why.
