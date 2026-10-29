<!-- changelog: fixed -->
- **The release gate no longer fails validation-refresh runs that waited for a runner.** Its validation-refresh watch now allows 30 minutes of runner queueing on top of the refresh job's own 60 minutes, and the alt-model check posts `/approved` once instead of on every poll.

The gate's validation-refresh watch counts from the moment it dispatches the run, so time spent waiting for a runner used up the same budget as the run itself. In release gate run 40831286935 the run waited 20m22s for a runner, ran for 41m45s and finished with `success`, but the gate had given up 5 seconds earlier and blocked the release. The `orphan-workflows-test` job in `.github/workflows/test-and-mark-stable.yml` now waits up to 5520 seconds for the run, and its step and job time limits were raised to fit. In the same run the `e2e-alt-model-test` wait loop posted about 67 `/approved` comments while the canary issue waited for approval. Each comment starts every workflow triggered by issue comments, and the extra implement runs queued behind each other. The loop now posts once each time the issue enters `ai:awaiting-approval`, retries when posting fails, and posts once more if the issue is still waiting 10 minutes after a post (implement can skip before it changes the label).

| The numbers that matter | Before | Now |
| --- | --- | --- |
| validation-refresh watch window | 3720 s | 5520 s |
| validation-refresh step time limit | 65 minutes | 95 minutes |
| `orphan-workflows-test` job time limit | 135 minutes | 165 minutes |
| `/approved` posts while awaiting approval | one per 20 s poll | one per arrival, plus one per 10 minutes still waiting |

What this means for operators: a release no longer fails because validation-refresh waited in the runner queue for up to about 30 minutes. A run that really hangs is reported after up to 92 minutes instead of 62.

### For contributors

`scripts/dispatch_and_watch_workflow_run.sh` and its other callers are unchanged. Tests: `tests/test_validation_refresh_workflow_contract.py` (the watch window includes the queue allowance) and `tests/test_test_and_mark_stable_alt_model_cleanup_pr_selector.py` (runs the alt-model wait loop against a fake `gh`: one post per arrival, a new post after a re-plan, one retry after a failed post). Not changed yet, because the evidence does not show the cause: the e2e-smoke-test Phase 3b implement-run tracker, the alt-model 4500-second deadline, and a separate "still queued" error in the dispatch helper.
