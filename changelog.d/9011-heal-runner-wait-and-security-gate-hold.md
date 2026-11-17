<!-- changelog: fixed -->
- **Heal reports no longer expire while the intake job waits for a runner, and an unpushed review commit no longer holds the single-issue security pass.**

On 2026-11-14 six heal reports from coding-workflows were rejected as `identity_stale`. Each `Workflow Failure Heal Intake` job waited about 3h50m for an `ubuntu-latest` runner, but only the heal-queue wait was added to `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS`. `scripts/workflow_failure_heal_intake.sh` now, on an `identity_stale` rejection, credits the time since its own run's `run_started_at` and verifies once more. Separately, the judge-merge gate in `scripts/review_single_issue_security_pass.sh` held PRs #9011 and #9134 with `extensions_unverifiable` because the review job's deferred `[ai-merge-resolve]` commit had moved the checkout past the PR head. That check now runs only when an extension marker needs it. Report mode still refuses to publish when the audit checkout is not at the audited head.

| The numbers that matter | Value |
| --- | --- |
| Heal reports rejected `identity_stale` on 2026-11-14 | 6 |
| Runner wait before the intake job's first step | about 3h50m |
| PRs held by `extensions_unverifiable` with no extension marker | 2 (#9011, #9134) |
| Extra API calls | 1 REST read, only on an `identity_stale` rejection |

What this means for operators: a heal report that arrived fresh is filed even when runners are backed up, and a judge-approved merge is no longer stuck on a security hold that had nothing to verify. A re-run of an old intake run still rejects its report, because a re-run resets `run_started_at`.

### For contributors

The intake logs `WORKFLOW_HEAL report_identity_age_credit arrival_waited_seconds=<n> queue_waited_seconds=<n>` whenever it tries the credit. A failed own-run read leaves the credit at 0, so the rejection stands. The `extensions_unverifiable` log reason is unchanged.
