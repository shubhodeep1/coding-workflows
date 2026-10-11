<!-- changelog: fixed -->
- **The orchestrator stops re-dispatching a validation run that keeps ending without a verdict.** After 3 such runs in one validation cycle it fails validation as a harness error, so the heal path takes over.

binance-blessings#249 re-ran a pinned `validate.yml` every hour for days: each run ended without `ai:validated` or `ai:validation-failed`, and the poller's stale-run check re-dispatched the same ref each time. `dispatch_validation_if_needed` in `scripts/orchestrate_poll_process.sh` now counts dispatches per validation cycle. When `MAX_VALIDATION_DISPATCHES_PER_CYCLE` runs have ended without a verdict, it labels the project `ai:validation-failed` and `ai:harness-broken` (`raw_status=harness_error`), naming the last run. That does not spend `MAX_VALIDATION_RECOVERY_ATTEMPTS`.

| The numbers that matter | Value |
| --- | --- |
| `MAX_VALIDATION_DISPATCHES_PER_CYCLE` default | 3 |
| Stale-run re-dispatch interval | 1 hour |
| Log line | `VALIDATION_DISPATCH_CAP outcome=redispatch\|exhausted` |

What this means for operators: a broken validation harness now surfaces as `ai:harness-broken` within about 3 hours instead of burning a run every hour. Fix the harness, then `/revalidate` to start counting again.
