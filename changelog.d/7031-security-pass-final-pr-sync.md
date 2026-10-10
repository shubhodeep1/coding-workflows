<!-- changelog: fixed -->
- **A final PR that goes unmergeable during the security pass is now synced with main right away.** E2E smoke-fixture projects no longer send Telegram alerts.

Before this change, a conflict between main and a project's final PR that appeared during `security-pass` or `security-pass-fixing` had no owner. The poller's tick-level sync skips those statuses, and the finalizer heals only after the pass. Project #6664's final PR #6667 sat unmergeable this way. The poller now checks the final PR on every security-pass tick and runs the existing main-into-integration sync when GitHub reports `mergeable=false`. The head-bound security pass then re-audits the delta. Separately, tracking issues titled `[E2E ...]` or labelled `e2e-smoke-test` no longer page anyone. The smoke fixture closes its wave PRs on purpose, which used to raise a 🚨 CRITICAL "closed without merge" alert (#7017).

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per security-pass tick | 1 PR read, only while a final PR exists |
| Alert paths silenced for smoke fixtures | `tg_notify` plus 5 direct sends |

What this means for operators: mid-pass conflicts on final PRs resolve without a manual merge, and smoke-test runs stop producing false CRITICAL alerts.

### For contributors

New log prefixes: `SECURITY_PASS_FINAL_PR_SYNC`, `SMOKE_FIXTURE_PROJECT`, `TG_NOTIFY_SMOKE_SILENCED`. Coverage is in `tests/test_security_pass_final_pr_sync.py` and `tests/test_smoke_alert_silencing_contract.py`.
