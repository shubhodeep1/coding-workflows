<!-- agents: section="Stable log prefixes (contractual)" -->
- `AUTOFIX_COMMIT_SKIPPED` (`scripts/review_commit_changes.sh`: `pr= reason=updater_pr_verify_job_owns_branch`; the review editor's edits on an `auto/update-workflows-<sha12>` branch are discarded, not committed, and `AUTOFIX_UPDATER_COMMIT_WITHHELD=true` keeps the editor-changes-lost detector from reporting them as lost)
- `AUTOFIX_MERGE_CONFLICT_SKIPPED` (`scripts/review_autofix_step_detect_merge_conflicts.sh`: `pr= reason=updater_pr_verify_job_owns_branch`; no conflict resolver runs on a consumer-updater PR)
- `AUTOFIX_PUSH_SKIPPED` (`review_autofix.yml` "Push all pending commits": `pr= reason=updater_pr_verify_job_owns_branch`; no review commit is pushed to a consumer-updater branch)
LOG_PREFIX.name=AUTOFIX_COMMIT_SKIPPED
LOG_PREFIX.name=AUTOFIX_MERGE_CONFLICT_SKIPPED
LOG_PREFIX.name=AUTOFIX_PUSH_SKIPPED
