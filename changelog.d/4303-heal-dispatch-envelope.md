<!-- changelog: fixed -->
- **Workflow failure heal reports now reach the intake.** Both heal reporters send the report enveloped under `client_payload.report`, and a rejected dispatch logs why.

GitHub's `repository_dispatch` API accepts at most 10 top-level `client_payload` properties, and the heal report has 20 (issue reporter) or 23 (review/autofix reporter). Every report so far was answered with HTTP 422 and ended as `WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=dispatch_denied` or `WORKFLOW_HEAL_REPORT error dispatch_failed`, so `workflow-failure-heal-intake.yml` never ran from a `repository_dispatch`. `scripts/workflow_failure_heal_report.sh` and `scripts/workflow_failure_heal_autofix_report.sh` now build the body with `workflow_failure_heal.py wrap-dispatch` (`{schema_version, report}`), the intake's "Materialize the report payload" step unwraps it with `unwrap-dispatch`, and a flat payload from a reporter staged at an older release is still accepted. A failed dispatch now appends `detail=` with the first 300 characters of the API error to the existing log line.

| The numbers that matter | Value |
| --- | --- |
| `client_payload` top-level keys before / after | 20 or 23 / 2 |
| GitHub limit (`DISPATCH_CLIENT_PAYLOAD_MAX_KEYS`) | 10 |
| Rejection detail logged | first 300 characters of stderr |

What this means for operators: after the next `@stable` release, a review/autofix run that fails `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` times in a row on one PR, and every human-needed escalation label, produce an intake run in coding-workflows. If a dispatch is still refused, the reporter's log line names the cause. No variable, secret or wrapper change is needed.
