<!-- changelog: fixed -->
- **Release gate Phase 7 no longer fails while the cancel-on-close run waits for a runner.** The 10-minute `PHASE7_WAIT_BUDGET_MINUTES` now starts when the run leaves the queue, and a run that never starts gets its own `queued_timeout` verdict.

In release gate run 44490333814, the `e2e-smoke-test` job failed at "Phase 7: Close PR and verify cancel_on_pr_close fires" with `Cancel-PR .. FAILED (timeout; pre-close unconflict=not_needed)`. The cancel_on_pr_close run 44527274737 was still `queued` with no runner when the budget ran out, so runner starvation failed the gate although nothing was broken. Phase 7 now counts the budget from the first poll that sees the run out of `queued`/`pending`/`waiting`/`requested`. A run that stays queued is waited on for at most the new job env `PHASE7_QUEUE_WAIT_CAP_MINUTES`, then reported as `queued_timeout`. Every Phase 7 deadline also stops `E2E_FINALIZATION_RESERVE_MINUTES` before the job timeout.

| The numbers that matter | Value |
| --- | --- |
| In-progress budget (`PHASE7_WAIT_BUDGET_MINUTES`) | 10 minutes, unchanged |
| Longest queued wait (`PHASE7_QUEUE_WAIT_CAP_MINUTES`, new) | 30 minutes |
| New Phase 7 `status` value | `queued_timeout` |
| `test-and-mark-stable.yml` size | 429,964 → 405,454 bytes |

What this means for operators: a busy runner pool no longer fails the stable release gate in Phase 7. If the cancel run never gets a runner, the results table says `queued_timeout` instead of `timeout`, which points at runner capacity rather than at the cancel workflow.

### For contributors

The Phase 7 run body moved verbatim into `scripts/stable_e2e_step_verify_cancel_on_close.sh` and is sourced the same way as the Phase 4 and 4b step scripts (sparse checkout pinned to `github.sha`, `STABLE_E2E_STEP_SCRIPTS_VERIFIED`). That frees about 24 KB under the 430,000-byte headroom check in `tests/test_test_and_mark_stable_step_scripts_contract.py`. Tests that read the Phase 7 body use the expanded workflow text, and `tests/test_test_and_mark_stable_phase7_queue_wait.py` runs both wait loops with a fake clock.
