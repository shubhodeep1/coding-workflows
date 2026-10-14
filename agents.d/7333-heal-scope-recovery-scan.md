<!-- agents: section="Workflow architecture" -->
Heal-scope recovery scan (#7333, activation gaps of PR #7325 for #7082):
`workflow-failure-heal-intake.yml` also runs on `schedule` (`41 * * * *`).
Its `recovery-candidates` job (no shared lock, `issues: read`) runs
`scripts/workflow_failure_heal_recovery_scan.sh --list-only`, which lists, in
up to four GraphQL pages of 50, open `ai:workflow-heal` issues in this
repository written by the token's own `/user` login whose body has no
`ai:workflow-heal-scope` marker, dropping those whose last 30 comments already
hold `HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` scan markers. Only when that count
is above zero, and no other run of this workflow is active (one runs read;
`reason=intake_busy` / `reason=runs_unavailable` write `count=0`), does the
`recovery-scan` job run, holding the job-level `workflow-failure-heal-intake`
concurrency group. A newly queued member of a group replaces its pending one,
so the busy check keeps a scan from displacing a waiting report (a report
queued in the seconds between the check and the job start can still be
displaced). Each candidate's body and `lastEditedAt` are re-read (GraphQL)
right before it is rebuilt, and its source PR is read only while the issue is
unedited. For at most `HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK`
issues (default 1, clamped 1-2) the scan rebuilds the `autofix_failure`
report from the issue's never-edited body (`workflow_failure_heal.py
heal-scope-recovery reconstruct`: pipeline author, `lastEditedAt` null,
composed `fp` / `source` markers, title, Head SHA, Failed run lines and the
evidence block; the live PR with the body's head, and the trusted failure
markers) and runs the unchanged intake with `WORKFLOW_HEAL_REPORT_ORIGIN=recovery_scan`
plus `WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP` / `_ISSUE`. The intake accepts
that origin only on a `schedule` event with recovery on, both expected values
well formed and an `autofix_failure` payload, else
`report_auth=rejected reason=recovery_scan_invalid`. It sets
`REPORT_AUTH=reconstructed`, which only `_heal_scope_recovery_evaluate`
accepts; binding, provenance and the `heal-scope verify` creation guard are
unchanged. The recomputed fingerprint must equal the issue's `fp`
(`heal_scope_recovery outcome=skip reason=fingerprint_mismatch`) and the
budget must be a duplicate of that issue (`reason=decision_mismatch`);
anything but a recovery exits with `reason=not_recovered`. A replay posts
nothing on the source PR (`source_comment=skipped_reconstructed`), records
no occurrence (`occurrence=skipped_reconstructed`) and skips the
non-retryable PR handling (`non_retryable outcome=skip reason=reconstructed`).
A failed attempt, or a permanent reconstruct skip (`HEAL_SCOPE_RECOVERY_SCAN_PERMANENT_REASONS`),
leaves `<!-- ai:heal-scope-recovery-scan:v1 outcome=failed reason=<r> run=<id> -->`
(trusted only from the pipeline login, on the comment's last line); after
`HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` (default 3) the issue is skipped
(`outcome=attempts_exhausted`) and one WARNING was sent. A permanent skip
runs no intake, so it does not use the run's per-tick slot. Other heal kinds log
`reason=kind_unsupported` and still need a fresh verified report.
The scan records the intake's specific skip reason, not its final
`reason=not_recovered` line. `budget_budget_exhausted`, `budget_failed`,
`comments_unavailable`, `identity_unavailable`, `issue_record_missing` and the
intake's `error heal_issue_list_failed` read failure clear on their own: they log
`outcome=deferred`, post no attempt marker and do not count as intake failures; up to
two deferrals per run give their per-tick slot back, so one deferring issue
cannot starve later candidates. An open
issue that already carries a trusted recovery marker (a failed close after
`outcome=replaced`) is closed by the scan (`reason=already_replaced close=done|failed`;
a failed close leaves an attempt marker with `reason=close_failed`, so the
attempt cap bounds the retries and only the last attempt sends a WARNING).
`HEAL_SCOPE_RECOVERY_ENABLED=false` turns the scan off with recovery.

Non-retryable reports and recovery (#7333 gap 2): a verified (OIDC)
`autofix_failure` report routed by `non-retryable-route` with
`conflict_resolver_sandbox_path_unsupported` or
`conflict_resolver_sandbox_support_missing`, while recovery is on, is no longer
stopped at the non-retryable block: it logs
`non_retryable outcome=deferred reason=heal_scope_recovery` and installs an
EXIT trap, so the unchanged PR-side handling (`_non_retryable_route_apply`:
diagnosis comment, `ai:needs-human`, WARNING, pr/head/reason dedup) runs after
recovery on every exit path and keeps the exit code. When no eligible
markerless heal issue matches, the intake exits right after
`_heal_scope_recovery_evaluate` (`heal_scope_recovery outcome=skip
reason=not_eligible_non_retryable`) with today's PR-side outcome and no
occurrence or heal issue. A deferred report whose heal-issue list cannot be
read logs `heal_scope_recovery outcome=skip reason=heal_issue_list_failed` and
exits 0 with the same PR-side outcome; the hourly scan retries recovery. The host-only stop and unauthenticated reports are
never deferred.
