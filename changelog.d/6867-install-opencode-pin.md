<!-- changelog: fixed -->
- **The review and orchestrator-poll workflows now use the OpenCode install action with the registry retry.** Their `install-opencode` pin moves from commit 28f5134 to 0e8d83a, the main commit that carries the bounded retry from #6867.

Both production workflows pin the action to an immutable commit for supply-chain reasons, so #6867 alone only changed the local copy that `opencode-live-smoke.yml` runs; the activation check on that PR reported the production reach as dormant. With the pin moved, a transient npm registry reset (the cause of the heal issue #6818 on 2026-10-09) no longer fails a review or a poll run on the first attempt. The two tests that assert the pin move with it.

| The numbers that matter | Value |
| --- | --- |
| Workflows re-pinned | 2 (`review_autofix.yml`, `orchestrate_poll.yml`) |
| Install attempts before failing | 3 |

What this means for operators: nothing to configure; the retry is live for every review and poll run from the next workflow start on `main`.
