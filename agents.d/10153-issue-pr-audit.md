<!-- agents: section="Workflow architecture" -->
- **Daily issue/PR audit (#10153).** `workflow-log-analysis.yml` `collect-logs` (06:00 UTC schedule, or `workflow_dispatch` with `issue_pr_audit=true`; switch `ISSUE_PR_AUDIT_ENABLED`, default `true`) ends with the audit steps, after the PAT budget and stage switch steps; no new job, job timeout 100 minutes, `id-token: write` for the Claude pool. Step bodies: `scripts/issue_pr_audit_run.sh collect|scrub-credentials|analyze|deliver|telegram`, logic in `scripts/issue_pr_audit.py`. It audits every non-archived repository `GH_PAT` lists via `GET /user/repos` plus `GITHUB_REPOSITORY`, from per-repository cursors in `analysis/issue_pr_audit_state.json` (24-hour first lookback, 7-day clamp; a failed repository keeps its cursor). Tier 1 is rule-based (waste signals, creator attribution, blocked items by label or stall, new versus known by fingerprint); tier 2 is engine role `ISSUE_PR_AUDIT` (read profile, codex read-only fallback) on flagged items, a run-id-seeded clean sample and new blocked items, with only trusted authors' text wrapped in `<untrusted_external_data>` and no `GH_PAT`, Telegram or signing secret in the model step (the checkout's persisted token is removed first). The script writes the report's first-line marker `<!-- issue-pr-audit: window_start= window_end= run= repos= -->` and its `## Metrics Appendix`; `scripts/check_issue_pr_audit_report.py` checks it (one retry). A valid report and the state file reach the default branch through `auto/issue-pr-audit-report-<run>-<attempt>` and `scripts/workflow_log_analysis_deliver_report.sh` (new inputs `WLA_DELIVER_BRANCH_PREFIX`, `WLA_DELIVER_DOC_PATTERN`, `WLA_DELIVER_EXTRA_FILES`; defaults keep the workflow-log-analysis delivery unchanged), skip the reviewer panel as generated chore kind `issue_pr_audit_report`, and are handed to the orchestrator by `scripts/apply_analysis_on_main.sh` through `APPLY_ANALYSIS_DOC_GLOB_EXTRA` (default `analysis/issue-pr-audit-*.md`, `none` in the hand-off step's environment disables; the promote-cycle workflows do not forward it as a repository variable yet). When the host repository is public, private repositories appear in the report, state file and log only as `private:<sha256(repo)[:8]>` (`ISSUE_PR_AUDIT_REDACT_PRIVATE_IN_PUBLIC`). One plain-text blocked-items Telegram report per UTC day (cache marker `issue-pr-audit-tg-<day>`).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `ISSUE_PR_AUDIT_START` (`scripts/issue_pr_audit_run.sh collect`, #10153): `run=<id> event=<name>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_START
- `ISSUE_PR_AUDIT_REPOS` (`scripts/issue_pr_audit.py`, #10153): `discovered=<n> audited=<n> skipped_archived=<n> excluded=<n> truncated=<n> [fallback=self]`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_REPOS
- `ISSUE_PR_AUDIT_CURSOR` (`scripts/issue_pr_audit.py`, #10153): `repo=<owner/repo|private:<hash>> cursor=<ISO> clamped=<true|false> source=<state|initial>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_CURSOR
- `ISSUE_PR_AUDIT_REPO` (`scripts/issue_pr_audit.py classify`, #10153): `repo=<owner/repo|private:<hash>> items=<n> flagged=<n> blocked=<n> outcome=<ok|read_failed>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_REPO
- `ISSUE_PR_AUDIT_API` (`scripts/issue_pr_audit.py collect`, #10153): `rest=<n> graphql=<n> failed=<n>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_API
- `ISSUE_PR_AUDIT_REPORT` (`scripts/issue_pr_audit_run.sh analyze`, #10153): `outcome=<valid|retried|invalid> attempts=<n>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_REPORT
- `ISSUE_PR_AUDIT_REPORT_CHECK` (`scripts/check_issue_pr_audit_report.py`, #10153): `outcome=<ok|invalid|error> marker= missing_headings= order_ok= missing_urls= dangling_aud= bytes= too_small=`; never echoes report text.
LOG_PREFIX.name=ISSUE_PR_AUDIT_REPORT_CHECK
- `ISSUE_PR_AUDIT_PURGE` (`scripts/issue_pr_audit.py purge`, #10153): `doc=<path> outcome=purged`, `doc=<path> reason=<unprocessed|in_flight_project>`, or `reason=<processing_report_unavailable|in_flight_unavailable>` (nothing purged).
LOG_PREFIX.name=ISSUE_PR_AUDIT_PURGE
- `ISSUE_PR_AUDIT_TG` (`scripts/issue_pr_audit_run.sh telegram`, #10153): `outcome=sent parts=<n>` or `outcome=skipped reason=<already_sent_today|empty|no_digest|silenced|telegram_not_configured|send_failed|render_failed>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_TG
- `ISSUE_PR_AUDIT_END` (`scripts/issue_pr_audit_run.sh telegram`, #10153): `outcome=<completed|degraded|failed>`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_END
- `ISSUE_PR_AUDIT_SKIPPED` (`scripts/issue_pr_audit_run.sh`, #10153): `reason=<disabled|token_missing|collect_failed|no_digest|report_invalid|run_context_invalid> [stage=<sub>]`.
LOG_PREFIX.name=ISSUE_PR_AUDIT_SKIPPED
