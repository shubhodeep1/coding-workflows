<!-- changelog: fixed -->
- **Three pipeline alerts that asked for a human without needing one now resolve on their own.** The heal intake no longer drops a valid `engine_fallback` report's reporting run as stale when its job waited for a runner, the review editor's sandbox-excluded escalation no longer fires on an explanatory sub-bullet, and the review-blocked judge's security hold on its own unpushed merge-resolve commit no longer pages.

On 2026-11-13 the heal intake job sat 1h54m to 3h25m waiting for a GitHub-hosted runner (runs 43439048054, 43496779526, 43498856864), and only the heal queue wait (a few seconds) was credited against the 3600-second report age limit. PR #9516 already credits that wait for a report that carries an identity, on its `identity_stale` retry. `scripts/workflow_failure_heal_intake.sh` now credits it for an `engine_fallback` report's reporting-run age check too, measured from the time its own run attempt started, so the limit applies when the report arrived. PRs #9453 and #9390 were labelled `ai:needs-human` with `review_editor_sandbox_path_excluded` although the editor declined nothing for that reason: an indented sub-bullet that explained a sandbox-only test failure ended with "a sandbox-excluded path". `scripts/review_autofix_step_post_editor_summary.sh` now counts top-level "Ignored suggestions" bullets only. PRs #9338 and #9283 paged "will not clear on its own (extensions_unverifiable)" because the conflict resolver's `[ai-merge-resolve]` commit was still unpushed when the judge's merge gate ran; `scripts/review_single_issue_security_pass.sh` now holds those as `head_unpushed`, which the judge alert treats as self-clearing.

| The numbers that matter | Value |
| --- | --- |
| Runner wait that caused `identity_stale` | 1h54m to 3h25m (limit 3600 s) |
| Extra API calls per heal report | 1 run-attempt read, only for an `engine_fallback` `repository_dispatch` report |
| Runner-wait credit cap | 86400 s |
| New gate `hold_reason` | `head_unpushed` |

What this means for operators: fewer CRITICAL and WARNING pages that need no action. A security hold still blocks the merge of an unaudited head; only the alert changes, and the push that follows starts the review that audits it. A report that was already older than `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS` when it arrived is still rejected.

### For contributors

The intake logs `WORKFLOW_HEAL report_age_credit outcome=applied|not_needed|unavailable` (`not_needed` when the queue wait already covers the elapsed time) with `attempt_elapsed_seconds=`, `queue_waited_seconds=` and `max_age=`. A failed run read keeps the old queue-only credit. A report that carries an identity is not read up front; the `identity_stale` retry (`report_identity_age_credit`) adds the arrival wait for it, and is skipped when the up-front read already credited the wait, so the runner wait is credited once. The gate logs `SINGLE_ISSUE_SECURITY_PASS mode=gate ... checkout=<sha> outcome=hold reason=head_unpushed`, and `status` mode reports `unverifiable` as before. `head_unpushed` applies only when an extension marker needs the checkout at the PR head; with no marker an unpushed local commit still does not hold the gate (#9011). A checkout that does not descend from the PR head still holds as `extensions_unverifiable`.
