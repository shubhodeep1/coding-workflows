<!-- changelog: added -->
- **Review/autofix stops retrying a failure that repeats identically on one head.** After three review runs on the same PR head fail with the same fingerprint, the gate ends the next run before any model call and escalates the PR to `ai:review-blocked`.

Every failure comment `review_autofix.yml` posts now ends with a hidden `<!-- review-autofix-failure:v1 head=… reason=… fp=… -->` marker. The fingerprint combines the failure reason with the normalised stderr of the editor and "Collect PR metadata" stages, so run ids, SHAs and temp paths do not change it. The `gate` job counts the trailing markers for the current head and, at `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`, skips the run with `skip_reason=fingerprint_cap`. A new `fingerprint-cap-block` job then labels the linked issues `ai:review-blocked` (or the PR when it has none), posts one "AI review/autofix stopped: identical failure repeated" comment, sends an `identical_failure_cap` report to workflow failure heal, and sends a Telegram WARNING. PR #4259 failed seven times this way on one head, spending about 75 minutes of reviewer and consolidator time per run before the editor exited in one second.

| The numbers that matter | Value |
| --- | --- |
| Identical failures that trip the cap (`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`) | 3 |
| Extra GitHub API calls in the gate | 0 when the terminal same-head skip already ran, else one `GET /user` plus one paginated comments call |
| Kill switch | `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false` |
| New log prefixes | `AUTOFIX_FINGERPRINT`, `AUTOFIX_FINGERPRINT_CAP_TRIPPED`, `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED`, `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED` |

What this means for operators and consumer repos: a PR stuck on a deterministic review failure now lands in `ai:review-blocked` after its third identical failure instead of being re-dispatched by the sweep and stall poller for hours. The review-blocked judge still runs, because `force_rb_judge` dispatches bypass the cap, and pushing a fix resets the count. Consumers receive the cap with the next `@stable` release, with no wrapper, secret or variable change.

### For contributors

The marker is computed by `scripts/workflow_failure_heal.py autofix-failure-fingerprint`, and the gate count by `autofix-identical-failure-count`. Only markers written by the account `GH_PAT` authenticates as are trusted, several comments from one run count once, and an editor summary or a failure comment without a marker ends the scan. The gate checks out only `scripts/workflow_failure_heal.py` (sparse) and prefers the copy that carries the subcommand, so self-repo branches forked before this change use the main snapshot. `workflow_failure_heal_autofix_report.sh` now honours an explicit `AUTOFIX_FAILURE_REASON` and carries the marker's fingerprint as the optional `failure_fingerprint` payload field.
