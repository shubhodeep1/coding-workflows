<!-- changelog: fixed -->
- **The release gate's GitHub reads now wait out a rate limit, and every gate job logs how much of the PAT's hourly allowance it found and left.**

Gate run 46009933351 passed 9 of 10 jobs, then failed after 3 hours 25 minutes in `e2e-smoke-test` step `Phase 7: Close PR and verify cancel_on_pr_close fires`: the runs listing answered "API rate limit exceeded" from 17:22:59 UTC, the step tried three times 5 seconds apart and gave up. Almost every read in the gate was written that way.

The gate's reads now go through `gh_api_retry` from `scripts/gh_helpers.sh`, which waits for the limited bucket's reset and gives up at once when the reset is further away than `GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS`. This covers the Phase 7, deep-verify and Plan-wait step scripts and the inline reads of `e2e-smoke-test`, `orchestrate-decompose-test`, `clarify-rejects-unsolvable-test` and `e2e-alt-model-test`. Phase 7 and deep verify cap each wait at the smaller of 600 s and half the time left before the job's finalization reserve, and deep verify stops its own retry rounds once the helper gives up on a rate limit; a run whose jobs listing cannot be read now fails deep verify instead of passing as a spurious re-trigger. The always() cleanup of `e2e-smoke-test` caps each read's wait at an eighth of the job time left at that moment (its PR-body and dispatch-review reads included), and both cleanup steps fall back to a plain read when the checkout's helper is missing, so cleanup still runs. Writes (closing the test PR, comments, dispatches) keep their current single attempt.

| The numbers that matter | Value |
| --- | --- |
| Attempts per read in a PAT job | 3 (`GH_RETRY_MAX_ATTEMPTS`) |
| Longest single rate-limit wait | 600 s (`GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS`, the helper's default) |
| Raised job limits | `orchestrate-decompose-test` 81 to 101 (on top of #10205's budget), `clarify-rejects-unsolvable-test` 30 to 50, `e2e-alt-model-test` 90 to 110 minutes |
| Extra API calls | 2 `GET /rate_limit` per PAT job (not counted against the allowance) |
| New jobs or workflows | none |

What this means for operators: the `GH_PAT_BUDGET phase=start|end` lines of the seven PAT jobs now show `remaining=<n> reset=<epoch>` (and `used_in_job=<n>` at the end when the reset did not change), so the gate's own share of the 5,000-per-hour allowance can be read from its logs. They fall back to `unknown` only when the read fails. The `validate` job holds no PAT and keeps `unknown`. A rate-limited read now logs `GH_API_RETRY outcome=retry kind=primary ...` before it waits.
