<!-- changelog: changed -->
- **A project security pass that stops because the audit rejected its own input now reports `audit_input_rejected` instead of `engine_unavailable`.** The tracking comment says the audit refused its input before any model ran and that this is not an engine outage.

`scripts/orchestrate_poll_process.sh` reads the last `security-audit: phase=` line the audit wrote. When that phase is `oversized-scope`, `diff-scope`, `prior-findings` or `waived-findings`, the pass fails closed as `SECURITY_PASS_FAILED reason=audit_input_rejected`. These four phases run before any model and fail the same way on every poll until the integration branch or the audit code changes. fun-token-multi-chain#579 sat on `engine_unavailable` for a scoped `.env.example` while no engine was involved. Every other failure keeps `engine_unavailable` and its wording.

| The numbers that matter | Value |
| --- | --- |
| Audit phases mapped to `audit_input_rejected` | 4 (`oversized-scope`, `diff-scope`, `prior-findings`, `waived-findings`) |
| New GitHub API calls | 0 |

What this means for operators: an `audit_input_rejected` alert points at the project's content or the audit's rules, not at a model provider, so check the `last audit error` in the tracking comment rather than waiting for an outage to clear. The pass still blocks completion, alerts once per failure streak and retries on every poll. Log searches or alerts that match `reason=engine_unavailable` will not match these failures any more.
