<!-- changelog: fixed -->
- **The weekly `AI Security Audit` runs again in consumer repos.** Every scheduled run had been cancelled at startup with zero jobs since the reusable workflow and its consumer wrapper started sharing one concurrency group.

Consumer repos call `security-audit.yml` through the synced `ai-security-audit.yml` wrapper. Both files declared `concurrency.group: security-audit-${{ github.repository }}`, and GitHub treats a called workflow that waits on the group its caller already holds as a deadlock, cancelling the run before any job starts. On `shubhodeep1/tele-funtoken-msg-scoring` runs 34747244353, 34021337015 and 33301125251 all ended that way with the banner "Canceling since a deadlock was detected for concurrency group ... between a top level workflow and 'security-audit'". The reusable workflow now uses `security-audit-reusable-${{ github.repository }}`; the wrapper template is unchanged, so consumers pick the fix up on the next `@stable` sync without a wrapper rewrite.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `.github/workflows/security-audit.yml`, `tests/test_security_audit_workflow_contract.py` |
| Consumer wrapper changes required | 0 |
| Regression test | `test_security_audit_reusable_concurrency_group_differs_from_consumer_wrapper` |

What this means for operators: the Sunday 08:00 UTC audit produces findings issues again instead of a zero-job failure, with no repo-var or wrapper change on the consumer side.

### For contributors

A reusable workflow must never declare the same workflow-level concurrency group as the wrapper that calls it. The contract test pins the two groups apart so a future edit cannot reintroduce the deadlock.
