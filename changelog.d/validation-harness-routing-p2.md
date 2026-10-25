<!-- changelog: changed -->
- **A project held for a harness fix now pages a person after 14 days, and the unblock judge can ask for a hold.** Refs #7913.

A project whose runtime validation failed because of the validation harness waits for a harness fix instead of paging a person. Until now nothing ended that wait if no fix shipped. The poller now ends a hold older than `VALIDATION_HARNESS_HOLD_MAX_DAYS` (default 14): it parks the project in the needs-human digest and sends one CRITICAL alert. A hold with a missing or invalid start time ends the same way. A bounce from the heal intake on the same tick still wins. The limit also applies while `ENABLE_VALIDATION` is off. In coding-workflows itself, whose validation wrapper is pinned to a branch rather than a commit, a harness error is parked as before instead of held.

The poller also honours a hold request from the unblock judge: a `<!-- ai:unblock-harness-hold-request:v1 item=<N> ... fingerprint=<12 hex> ... -->` comment on a failed tracking issue that carries `ai:validation-failed`, from the pipeline login only. The project is held under `fp=judge:<12 hex>` with the same resume caps and time limit, and each request comment is used once. Nothing posts this comment yet; the judge side comes in a later phase.

What this means for operators: a held project never waits more than 14 days without a page. `/revalidate` still resumes a held project at once and keeps the resume counters. Set `VALIDATION_HARNESS_HOLD_MAX_DAYS` (1-365) to change the limit, or `VALIDATION_HARNESS_HEAL_ENABLED=false` to turn the hold off.

### For contributors

- Log `VALIDATION_HARNESS_HOLD` gains `outcome=expired` (`reason=since_invalid` for a bad start time) and `outcome=skip reason=hold_active|not_validation_failure|identity_unavailable`.
- New state field `validation_harness_hold_requests_used`: the last 50 consumed request comment ids.
- The time limit and the request read only the state file and the comments the tick already fetched; they add no GitHub API call.
