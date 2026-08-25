<!-- changelog: fixed -->
- **The editor-changes-lost re-dispatch works again.** Both branch-scoped list-runs probes in `scripts/gh_helpers.sh` now pin `-X GET`, so they stop 404ing and the automated retry is no longer reported as budget-exhausted on a head SHA that was never retried.

`autofix_retrigger_has_inflight_peer` and `autofix_changes_lost_head_retry_consumed` query `/repos/{repo}/actions/runs` with `-f branch=` and `-f per_page=`. `gh api` infers its HTTP method from its arguments: GET by default, but POST as soon as any `-f` / `-F` parameter is present and no method is given. There is no `POST /repos/{repo}/actions/runs` route, so every call 404'd, `_is_gh_permanent_failure` classified the 404 as non-retryable, and both probes returned `reason=api_error` on their first attempt. The peer probe fails open, so its breakage was silent. The budget probe fails closed, so the same 404 reported the per-head-SHA retry budget as consumed and suppressed the `Re-dispatch review on editor-changes-lost` step every single time it was reachable.

The visible effect in consumer repos was a PR that went quiet: the run finished green, posted the CRITICAL `⚠️ Editor changes lost (retry unavailable)` alert and the "retry budget is unavailable or exhausted" comment, blocked auto-merge, and then waited for the orchestrator's generic stall recovery to re-trigger the review roughly two hours later.

| The numbers that matter | Value |
| --- | --- |
| Probes fixed | 2 (`autofix_retrigger_has_inflight_peer`, `autofix_changes_lost_head_retry_consumed`) |
| Automated changes-lost retries previously dispatched | 0 of every occurrence |
| Retry attempts before the fail-closed skip | 1 (404 is non-retryable) |
| Observed stall before generic recovery | 123–137 min, up to attempt 3 (`tele-funtoken-msg-scoring` #3763/#3764, #3761/#3765) |
| Reference run | `tele-funtoken-msg-scoring` 32732281452 |
| New regression tests | 4 in `tests/test_gh_helpers_list_runs_method.py` |

What this means for operators: an editor-changes-lost iteration now re-dispatches its own review immediately instead of dead-ending on a CRITICAL alert and waiting on stall recovery, which removes the two-hour idle window and the repeated stall-recovery escalations on affected PRs. The one-retry-per-head-SHA bound is unchanged — a head that genuinely already consumed its retry still skips, and the probe still fails closed when the API is truly unreachable.

### For contributors

The pre-existing suite could not catch this: its `gh` stub ignores the arguments it is passed, so the method the helper actually requests was never asserted. The new tests add a stub that reproduces gh's method inference, plus a static assertion that both call sites pin GET, so removing the flag fails the suite. `review_autofix_sweep.yml` already used `-X GET` on the same endpoint family; these two helpers were the only REST GETs in `gh_helpers.sh` passing `-f` without a pinned method.
