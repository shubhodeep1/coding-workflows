<!-- changelog: fixed -->
- **The release gate no longer fails `Cancel-PR .. FAILED (no_run)` when the smoke-test PR has become conflicted with its base.** Phase 7 of `Test & Mark Stable Release` now makes the throwaway PR mergeable before closing it, so the close exercises the real `pull_request.closed` trigger.

Run 35672590166 closed smoke PR #4252 75 seconds after the forward-merge of `stable` into `main` had rewritten the same hunk of `.ai/.workspace_source_manifest.txt`, so the PR was conflicted at close time. GitHub does not run `pull_request` workflows for a conflicted PR, no cancel-on-close run appeared, and the gate blocked the nightly promotion. The scheduled cleanup sweep added for #4258 covers such closures in production, but its observed cadence is longer than the 10-minute Phase 7 budget, so the gate would still have failed. `.github/workflows/test-and-mark-stable.yml` now merges the base into `ai/issue-N` through the merges API before the close; on a conflict it overwrites each PR file the base also changed with the base's version, retries once, waits for GitHub to recompute mergeability, and records the outcome. A PR that still cannot be made mergeable is closed anyway and falls back to the scheduled sweep, with a workflow warning naming the cause.

| The numbers that matter | Value |
| --- | --- |
| Failing run / smoke PR | 35672590166 / #4252 |
| Gap between the conflicting merge and the close | 75 seconds |
| Observed cancel-on-close cron cadence (median, 2026-09-22) | about 12 minutes |
| `PHASE7_WAIT_BUDGET_MINUTES` | 10 (unchanged) |
| Mergeability poll ceiling per check | 90 seconds |

What this means for operators: a `main` commit that lands during the roughly 100-minute gate no longer blocks the nightly `Promote main to stable` cycle through Phase 7. The run log carries `PHASE7_UNCONFLICT_CHECK`, `PHASE7_UNCONFLICT_FILE` and `PHASE7_UNCONFLICT_RESULT` lines and the results table shows `pre-close unconflict=<outcome>`, so a future `no_run` states whether the PR was mergeable when it was closed.

### For contributors

The step only rewrites files on the throwaway smoke branch, with `[E2E Smoke Test]`-prefixed commits (never `[ai-autofix]`, which `review_autofix.yml` treats as self-triggered). API spend is one mergeability poll, one or two `/merges` calls, and on conflict one `/compare`, one `/pulls/N/files` page and up to four calls per overlapping file. `tests/test_test_and_mark_stable_phase7_unconflict.py` pins the ordering, the commit prefix, and the never-fails contract.
