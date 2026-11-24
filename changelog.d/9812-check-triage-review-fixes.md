<!-- changelog: fixed -->
- **Check-failure triage: the final-CI skip fires, cancelled jobs stop counting as failures, and a release-download re-run touches only the classified job.** Five defects the review of stable backport #9726 found in `scripts/check_failure_triage.sh`, plus one misleading comment in `plan.yml`, are fixed (Refs #9726, #9673, #9125, #9272).

- The `final_ci_repair_owns` skip (#9673) never matched: a `workflow_run` caller passes the workflow name `CI` as the check name, and `CI [pr:<n>]` is only the run's display title. `check_failure_triage.yml` now passes that title to the collect stage as `CHECK_TRIAGE_RUN_TITLE` (for `workflow_dispatch` runs only), and the skip matches it, falling back to the check name when it is empty. The skip line gains `title_source=run_title|check_name`.
- The tracked-base-failure check counted a PR job cancelled by a newer push as failed, so the failure was filed instead of folded into the default branch's heal issue. Only `failure` and `timed_out` jobs count now, on both sides.
- On the `check_run` path the release-download gate classified one job but re-ran every failed job in the run, which suppressed triage for a real failure in a sibling job. It now re-runs only that job (`POST actions/jobs/<id>/rerun`); the `workflow_run` path keeps `rerun-failed-jobs`.
- The gate's jobs and annotations bounds now match `scripts/ci_cancelled_rerun.py` (a page of 100 or more is not classified), and each copy names the other.
- On the `workflow_run` path the PR run's jobs are read once: the tracked-base-failure check reuses the gate's validated read.
- A blocked `ai:check-triage` plan says the unblock judge will close the issue, and pages at WARNING, only while `UNBLOCK_JUDGE_ENABLED` and `CHECK_TRIAGE_RESOLVED_GATE_ENABLED` are both on; otherwise it keeps the human-input text and the CRITICAL page.

What this means for consumer repos: after the next `@stable` sync, failed final-CI runs (main only) no longer file duplicate triage issues, fewer per-PR issues are filed for failures the default branch already tracks, and a release-download re-run no longer re-runs a sibling job that really failed. No variable needs to be set.
