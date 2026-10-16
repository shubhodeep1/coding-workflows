<!-- changelog: fixed -->
- **A failed project security pass now says why.** When the findings-JSON security audit exits with an error, the poller logs its stderr and names the failure on the project.

Before this, `SECURITY_PASS_FAILED reason=engine_unavailable` was the only trace: the audit wrote its error to a file under the runner's temp directory that the job log never showed. tele-funtoken-msg-scoring#3928 failed this way on every poll, 0.2 seconds after the audit prompt was built, with no cause anywhere. The poller now logs `SECURITY_PASS_AUDIT_STDERR tracking_issue=<n> rc=<status> lines=<count>` followed by the last 40 stderr lines. The project's "security pass unavailable" comment and completion status give the exit status and the audit's last `phase` and `error`, or point to the job log when the audit failed before reporting one.

| The numbers that matter | Value |
| --- | --- |
| Stderr lines logged | last 40, each cut to 400 characters |
| Text added to the project comment | at most 300 characters, from the audit's already-redacted failure line |

What this means for operators: a project stuck in `ai:security-pass` with `engine_unavailable` now shows its cause in the next poll's log and on the tracking issue, so it can be fixed instead of retrying unchanged.

### For contributors

The raw stderr tail goes only to the job log, where Actions masks registered secrets, and every line is printable-only with `##[` escaped so it cannot act as a workflow command. The project comment quotes only the `phase` and `error` values of the last `security-audit: phase=... error=...` line, which `security_audit.sh` already passes through `security_audit_sanitize_log_value`.
