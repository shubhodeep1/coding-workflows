<!-- changelog: security -->
- **The stable release workflows' `validate` jobs now require a passing `CI / lint` run on the released commit, and `RELEASE_TAGGING_ENABLED` can stop stable tag writes.** Security finding #6548 (Refs #7029).

The "Verify CI passed on stable" step in `mark-stable.yml` and "Verify CI passed on source branch" in `test-and-mark-stable.yml` read the legacy combined commit status. That status never contains GitHub Actions check-runs, so the steps passed on an empty or pending state. Both now call the existing bounded gate `scripts/release_ci_gate.sh` (#6797): the newest github-actions `lint` check-run from `ci.yml` on the validated commit must be completed with `success`. A missing, failed, cancelled or timed-out run, or unreadable API output, fails the job after the bounded wait. The `release` jobs already ran the same gate; now no status-based check is left in either workflow.

In `gate_only` runs of `test-and-mark-stable.yml` the check does not wait and only warns, because those runs never tag.

What this means for operators:
- A release now waits in `validate` (up to `RELEASE_CI_GATE_WAIT_SECS`, default 30 minutes) for `CI / lint` to finish. The `validate` job timeout went from 5 to 40 minutes, and the job now has read-only `contents`, `checks` and `actions` permissions.
- New repository variable `RELEASE_TAGGING_ENABLED`, default `true`. Any other value skips the changelog commit, the tag pushes, the GitHub Release and the consumer dispatch with a `::warning::`; the CI gate and validation still run and the job ends green. While it is off, `auto-release-stable.yml` keeps re-dispatching the gate every 6 hours.
