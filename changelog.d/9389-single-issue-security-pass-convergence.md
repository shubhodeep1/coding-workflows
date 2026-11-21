<!-- changelog: fixed -->
- **Single-issue security audits no longer cancel each other, and an audit that never reports no longer burns a cycle.** Each PR's audit now runs in its own concurrency group, a cancelled audit is retried in about 20 minutes instead of 6 hours, and later cycles audit only what changed since the last clean audit.

`security-audit.yml` and the consumer wrapper `ai-security-audit.yml` used one repo-wide concurrency group. GitHub keeps one waiting run per group and cancels the older one, so an audit for one PR cancelled the queued audit of another. From 2026-11-16, 39 of 91 dispatched audits ended `cancelled` without posting a result. Each affected PR then waited out `SECURITY_PASS_PENDING_STALE_HOURS` (6 hours) on its pending marker, and every lost audit used one of its `MAX_SECURITY_PASS_CYCLES`. PR #9389 reached "head attempt 2 of 2" with only 3 of 8 audits completed, all of them clean. The group is now per PR (`security-audit-reusable-<repo>-pr-<n>`, wrapper `security-audit-<repo>-pr-<n>`, `default` without a PR). The gate in `scripts/review_single_issue_security_pass.sh` caps how many audits run at once, retries an audit whose run ended without a result, counts only audits that reported toward the cycle budget, and passes the PR's last clean-audited ancestor head as `SECURITY_AUDIT_DIFF_SINCE`.

| The numbers that matter | Value |
| --- | --- |
| Cancelled single-issue audits, 2026-11-16 to 2026-11-17 | 39 of 91 (43%) |
| Wait before a lost audit is retried | 6 h → about 20 min (`SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES`, default `20`) |
| Audits running at once | 1 (shared group) → up to `SECURITY_PASS_MAX_CONCURRENT_AUDITS` (default `3`) |
| Dispatch cap per PR | new `SECURITY_PASS_MAX_AUDIT_ATTEMPTS`, default twice `MAX_SECURITY_PASS_CYCLES` (`10`) |
| Extra GitHub API calls | at most one audit-runs listing per gate run; two reads per audit for delta scope |

What this means for operators: standalone PRs held by the security pass should converge within one or two sweep ticks after their audit finishes, instead of stalling for 6 hours per cancelled audit and ending in `ai:security-pass-failed` with no completed audit. A held PR may now show `hold_reason=audit_throttled` while three audits are already running; it clears on its own and does not page. Consumer repositories get the per-PR group and the run title on the next wrapper sync. Until then the cancelled-audit check finds no matching run and the 6-hour backstop applies as before.

### For contributors

- New settings, all with defaults: `SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES` (`0` = off), `SECURITY_PASS_MAX_AUDIT_ATTEMPTS`, `SECURITY_PASS_MAX_CONCURRENT_AUDITS` (`0` = off), `SECURITY_PASS_DELTA_AUDIT_ENABLED` (`false` = always full range).
- The marker format is unchanged. `cycle=` still numbers every attempt; the budget is computed by `single_pass_cycle_accounting`, which skips cycles whose only markers are `pending` and still counts gaps with no marker.
- An audit counts as lost only on positive evidence: every run titled `... (PR #<n>)` created since the marker completed at least 10 minutes ago. A missing run, for example under a wrapper that has no `run-name` yet, keeps the hold.
- The new `since` subcommand prints nothing on any doubt (no clean ancestor, failed read, force-push), so `security_audit.sh` keeps the full `merge-base..head` range and never hits its fail-closed non-ancestor check.
