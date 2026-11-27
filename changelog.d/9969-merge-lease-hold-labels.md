<!-- changelog: fixed -->
- **A PR parked with a hold label no longer gets or keeps the merge lease.** Before, the merge-lease tick could grant the lease to a PR labelled `ai:needs-human` or `ai:security-pass-failed` when its head-gate was an old success, and that PR then held the lease for up to an hour while every other approved PR waited. Now the tick skips such a waiter at grant time, removes its wait label and grants the next one (`MERGE_LEASE ... mode=tick outcome=skipped reason=hold_label`), and releases a holder that carries one at once (`outcome=released reason=revoked_hold_label`, with the usual skip-until-head-changes entry). The labels come from the open-PR listing the tick already reads, so no GitHub API call is added.

The hold labels are set by the new `MERGE_LEASE_HOLD_LABELS` variable. It defaults to `ai:needs-human,ai:security-pass-failed,ai:review-blocked,ai:scope-blocked` in `scripts/pr_checks_lib.sh`, and the default is also wired as a repo variable in `orchestrate_poll.yml` and `cancel_on_pr_close.yml`. Names match exactly, ignoring case, and the merge-lease wait label is never treated as a hold label. The check runs only while `MERGE_LEASE_REVOKE_ENABLED` is on. To restore the old behaviour, set the variable to `none` or `off`. A repo variable set to an empty string falls back to the default.

What this means for consumer repos: after the next `@stable` sync, a PR waiting for a human no longer blocks the merge lease. Nothing needs to be done.

Refs #9969
