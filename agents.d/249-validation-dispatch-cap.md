<!-- agents: section="Workflow architecture" -->
The poller counts the validation runs it dispatches per validation cycle
(state `validation_cycle_dispatch_count`, reset when
`validation_last_dispatch_cycle` changes). A run that ends without a verdict
label is re-dispatched once it is an hour old and no validation run is
active. When `MAX_VALIDATION_DISPATCHES_PER_CYCLE` (default `3`) runs have
ended that way, `dispatch_validation_if_needed` stops and calls
`mark_validation_failed` with `LAST_VAL_RAW_STATUS=harness_error`, so the
project gets `ai:validation-failed` + `ai:harness-broken` and goes to the
heal and unblock paths without spending `MAX_VALIDATION_RECOVERY_ATTEMPTS`.
State written before the counter existed reads as one dispatch.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `VALIDATION_DISPATCH_CAP` (`scripts/orchestrate_poll_process.sh`: `outcome=redispatch|exhausted`, `issue=`, `cycle=`, `dispatches=`, `max=`)
LOG_PREFIX.name=VALIDATION_DISPATCH_CAP
