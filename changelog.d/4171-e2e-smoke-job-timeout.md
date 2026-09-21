<!-- changelog: fixed -->
- **The release gate's `e2e-smoke-test` job cap is now 300 minutes (was 120), so a healthy but slow smoke run is no longer cancelled while Phase 6 is still inside its own 30-minute window.**

Release v1.29.7 failed on `Test & Mark Stable Release` run 35479338161 with every phase healthy: Phase 4 (wait for review & autofix) spent exactly its 75-minute `REVIEW_STEP_TIMEOUT`, Phase 6 (review-blocked simulation) started at +99m, and the 120-minute job cap cancelled it at idle 1222s of its 1800s `PHASE_TIMEOUT` window. The cancel also skipped `Deep verify`, so `Verify all phases passed` blocked the release on `Internals` even though a Phase 6 timeout is scored as a non-blocking warning. Phase 6 could not finish sooner because its poller dispatch queued behind a scheduled `Internal: AI Orchestrate Poller` run that held the `ai-orchestrate-poll` concurrency group for 40 minutes while judging two live projects, and was then displaced from the single pending slot by later force-tick dispatches. The 300-minute cap in `.github/workflows/test-and-mark-stable.yml` is aligned with the workflow's 295-minute conservative serial-budget guard rather than a typical run, and its timeout comments now match that hard runner limit.

| The numbers that matter | Value |
| --- | --- |
| `e2e-smoke-test` `timeout-minutes` | 120 → 300 |
| Phase 4 per-step cap (`REVIEW_STEP_TIMEOUT`, unchanged) | 75m, clamped at 90m |
| Phase 6 inactivity window (`PHASE_TIMEOUT`, unchanged) | 30m |
| Failing run | 35479338161 (v1.29.7) |
| Parent `promote-main-to-stable.yml` cycle cap (unchanged) | 340m |

What this means for operators: a release-gate run whose review phase legitimately takes the full 75 minutes now gets its review-blocked simulation and deep verification instead of a `cancelled` gate, at the cost of the hard runner cap allowing a genuinely hung run to take up to 180 minutes longer to fail. Phase scoring, phase timeouts, and the promote cycle are unchanged.
