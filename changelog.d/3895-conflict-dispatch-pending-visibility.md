<!-- changelog: fixed -->
- **Duplicate "merge conflicts. Review workflow dispatched for resolution." Telegram warnings no longer repeat every poll cycle while a conflict resolver is already running.**

During the PR #3895 forward-merge conflict (2026-08-29), operators received 6+ identical conflict warnings and GitHub accumulated 10 cancelled duplicate internal-review dispatches over ~95 minutes, while the one real resolver run completed successfully. Two blind spots caused it: duplicate dispatches held back by the review_autofix concurrency group report status `pending`, which neither the orchestrator poller's `_has_active_autofix_run` guard nor `review_autofix_sweep.yml`'s snapshot counted as active, and `forward-merge-stable-to-main.yml` plus the sweep dispatched `internal-review.yml` without `--ref`, keying those runs to the default branch where the head-branch-keyed guards could not see them. Both guards now count `pending` runs as active, and both dispatchers now dispatch on the PR's head branch (the sweep falls back to the default branch for fork PRs, whose head ref does not exist in the repo).

| The numbers that matter | Value |
| --- | --- |
| Duplicate dispatches during the incident | 10 |
| Duplicate Telegram conflict warnings | 6+ |
| Real resolver runtime (run 33273396616) | ~102 minutes |
| Expected duplicate warnings after the fix | 0 |

What this means for operators: a forward-merge or standalone PR conflict now produces one alert when the fallback PR opens and one resolver run, instead of a stream of repeated warnings and cancelled runs while the resolver works.

### For contributors

The guard/status contracts are pinned by `tests/test_conflict_dispatch_active_run_visibility.py` as text contracts on the shipped files. A `pending` run is never treated as stale by the sweep's wedged-run cutoff — it is bounded by its running peer's 240-minute job timeout.
