<!-- changelog: changed -->
- **Standalone PR security audits are now opt-in.** `SINGLE_ISSUE_SECURITY_PASS_ENABLED` defaults to `false`, so eligible PRs can proceed through the normal review and merge path without a per-PR security audit.

This default applies in this repository and to consumer repositories after their next `@stable` sync. Reviews no longer hold a standalone PR for an existing pending or findings marker while the pass is disabled. Previously filed security findings remain open as issues. Orchestrator projects still use the separate `ENABLE_SECURITY_PASS` gate.

| Security pass | Default |
| --- | --- |
| Standalone PR (`SINGLE_ISSUE_SECURITY_PASS_ENABLED`) | `false` |
| Orchestrator project (`ENABLE_SECURITY_PASS`) | `true` |

What this means for operators: set the repository variable `SINGLE_ISSUE_SECURITY_PASS_ENABLED=true` to require the standalone audit before merge again.
