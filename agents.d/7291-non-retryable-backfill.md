<!-- agents: section="Workflow architecture" -->
Non-retryable failure backfill (issue #7291, activation gap of PR #7065):
`scripts/workflow_failure_heal_non_retryable_backfill.sh` runs as the
"Backfill non-retryable autofix failure reports" step of
`review_autofix_sweep.yml` (from the trusted `.ci-rerun-support` checkout) and
as the `non-retryable-backfill` job of the consumer `ai-review.yml` schedule
(`contents: read`, no `id-token`). It trusts only comments by the `GH_PAT`
login (`gh api user`, once per tick) and lists open PRs, least recently
updated first, with their last 100 comments in one paginated GraphQL query
(10 PRs per call, `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS`, default 50;
a cut-off listing logs `reason=listing_truncated`). With more than `MAX_PRS`
open PRs each tick starts at a rotated window (`GITHUB_RUN_NUMBER` modulo the
window count, logged as `reason=window_rotated`; one `totalCount` call plus one
pageInfo-only call per 100 skipped PRs), so idle ineligible PRs cannot hide
later ones. The sweep's `head_ref_filter` input reaches it as `HEAD_REF_FILTER`
(head-branch substring, as in the enumerate step).
`workflow_failure_heal.py non-retryable-backfill-candidates`
(`select_non_retryable_backfill_candidate`) picks a same-repository PR whose
newest trusted current-head `review-autofix-failure:v1` marker names a
`NON_RETRYABLE_FAILURE_REASONS` reason, a 64-hex `fp` and a numeric `run`, is
at least `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES` (60) old, and
has no trusted `ai:workflow-heal-non-retryable:v1` diagnosis for the same
pr/head/reason and no trusted
`<!-- ai:workflow-heal-non-retryable-backfill:v1 pr= head= reason= run= -->`
for the same run; provider-outage markers, other heads and cap markers are
skipped, and an editor summary first means `superseded`. Each candidate (at
most `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK`, default 3) goes
through the unchanged `workflow_failure_heal_autofix_report.sh` with the
marker's run as `GITHUB_RUN_ID` and a PR snapshot read just before and
checked against the selected head (`reason=head_moved` / `head_unreadable` /
`pr_read_failed` otherwise) as `PR_PAYLOAD_FILE`, so the intake's provenance gate checks that
run and its non-retryable routing posts the diagnosis; no heal issue is filed.
After a confirmed dispatch the backfill marker is posted. No OIDC report
identity is attached, so an intake with `WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=true`
skips these reports. Kill switch `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED`
(default `true`; `false` off, any other value off with a warning); every path
exits 0.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL` (`scripts/workflow_failure_heal_non_retryable_backfill.sh`: `pr= head= failure= run= outcome=dispatched|skip|failed reason=` (`marker=posted|failed` on dispatch; skip reasons include `head_moved`, `head_unreadable`, `pr_read_failed`, `tick_cap`, `dry_run`), `pr= outcome=skip reason=<selector reason>`, `outcome=warn reason=listing_truncated fetched= max=`, `outcome=info reason=window_rotated window= windows= total_open=`, `outcome=skip reason=disabled|invalid_switch|workflow_heal_disabled|gh_pat_missing|identity_unavailable|pr_listing_unavailable|selector_failed`, final `outcome=complete scanned= candidates= dispatched= dry_run=`)
LOG_PREFIX.name=WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL
