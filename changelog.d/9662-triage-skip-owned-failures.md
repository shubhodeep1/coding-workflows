<!-- changelog: fixed -->
- **Check-failure triage no longer files fix issues for failures another path already handles.** A failure on a commit that is no longer the PR head, and a failed final CI run while the final-CI hand-back is on, are now skipped.

Six triage issues from 2026-11-16 and 11-17 became fix PRs on other PRs' branches (#9597, #9598, #9601, #9603, #9614, #9621). Five came from two cases. Issues #9580, #9581 and #9582 were filed against failing commits that the PR had already moved past, as three near-duplicates within three minutes. Issues #9608, #9618 and #9662 came from failed `CI [pr:<n>]` final runs, which this repository's final-CI hand-back on `main` already sends back to review and autofix; the `stable` line creates no such runs, so for consumers only the stale-head skip applies. `scripts/check_failure_triage.sh` now skips both before any base, run or issue read and before diagnosis, logging `CHECK_TRIAGE skip reason=head_advanced` or `reason=final_ci_repair_owns`.

| The numbers that matter | Value |
| --- | --- |
| Fix PRs from the six measured issues these rules would have prevented | 5 of 6 (plus open issue #9662) |
| Extra API calls | 0 |
| Switches (repo variables) | `CHECK_TRIAGE_SKIP_HEAD_ADVANCED_ENABLED`, `CHECK_TRIAGE_SKIP_FINAL_CI_ENABLED` (both default `true`) |

What this means for operators: fewer `ai:check-triage` issues and fix PRs racing a PR's own review loop. A failing PR head that is still current is triaged as before. Turning the final-CI hand-back off (`FINAL_CI_FAILED_REDISPATCH_ENABLED=false`) brings final CI runs back to triage. Consumers get it on the next `@stable` sync.
