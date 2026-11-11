<!-- changelog: added -->
- **Workflow-heal issues held by the counterfactual gate now close themselves once the failing workflow is green again.** A run-sourced heal issue at `ai:awaiting-approval` closes as completed when its workflow has passed on each of the newest completed push runs on the target branch since the issue was last reported (one page of `HEAL_STALE_GREEN_RUNS`+10 runs is read, so on a busy branch older runs since that report are not checked). It needs at least `HEAL_STALE_GREEN_RUNS` (default 5) such runs, and the last report must be at least `HEAL_STALE_MIN_AGE_HOURS` (default 24) old. It also needs no open fix PR.

Until now such an issue waited for a human forever, because a counterfactual hold counts no recovery attempt. If the failure comes back, the heal intake files the next generation as before. New run-sourced heal issues record their workflow file in a body marker, so the check costs one cached runs read per workflow per poller tick. Set the `HEAL_STALE_AUTO_CLOSE_ENABLED` repository variable to `false` to turn it off.

Refs #8808.
