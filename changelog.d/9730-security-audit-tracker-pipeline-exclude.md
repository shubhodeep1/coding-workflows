<!-- changelog: fixed -->
- **The poller no longer recovers or unblock-judges the security-audit tracker.** An issue labelled `ai:security-audit` (the long-lived tracker, such as #3576) is now skipped by standalone stall recovery, the unblock scan and the unrouted-blocked-reply detector, as `clarify.yml` already does.

Before, standalone stall recovery auto-answered the tracker's stalled clarification, the plan then blocked with "nothing to implement", and no retry could clear it. Now, when the tracker also carries the `<!-- ai:security-audit-tracker:v1 -->` body marker, the poller removes `ai:blocked` and the pipeline phase labels from it and logs `STALL_RECOVERY_SKIP issue=<n> reason=security_audit_tracker`. It never closes the tracker or edits its body or comments. `scripts/unblock_scan.py select` skips it too (skip reason `security_audit_tracker`). `SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED` (default `true`) is the off switch: `false` restores the old behaviour.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads per tick | 0 |
| Label edits per tracker per tick | at most 1 |
