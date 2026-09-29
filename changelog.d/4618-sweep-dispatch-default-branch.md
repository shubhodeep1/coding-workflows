<!-- changelog: security -->
- **The review autofix sweep now runs `internal-review.yml` from the default branch only, never from a pull request's unmerged head branch.** Security finding #4618 (`review-dispatches-unmerged-workflow`, critical) is closed.

Every 30 minutes, `review_autofix_sweep.yml` dispatches `internal-review.yml` for each open non-draft PR. For same-repository PRs it passed `--ref <PR head branch>`, so the scheduled sweep ran that branch's own copy of `internal-review.yml`, with `secrets: inherit` and write permissions, before any review had approved it. The sweep now dispatches without `--ref` and passes only the PR number, which it checks is a positive integer (`AUTOFIX_SWEEP_SKIP … reason=invalid_pr_number` otherwise). `review_autofix.yml` still checks out the PR head from the PR's metadata, and both concurrency groups are keyed by PR number, so reviewers still read the PR head. One difference: a sweep-dispatched review of a same-repository PR builds its optional Semble index and README static context from the default-branch tree, as fork PRs always have.

The head-ref dispatch existed so the duplicate-run guards could see sweep runs (the PR #3895 incident). They now find them by name: `internal-review.yml` names every `workflow_dispatch` run `Internal: AI Review & Autofix [pr:<N>]`, the sweep's active-run snapshot keys those runs by PR, and the poller's `_has_active_autofix_run` looks the name up when its head-branch lookups find nothing. The §26 checker (`.claude/scripts/check_in_status.py --hand-back`) accepts a Claude-fixer hand-off whose review run is such a default-branch dispatch for the same PR, and counts an active one as a running review, so a `claude/*` PR whose hand-off came from a sweep run no longer waits forever. The `/implement-plan-claude` project checker (`check_in_status.py --pr N`) counts an active one too, so a PR with a failed check past the 6-hour stuck window is not handed back as `stuck` while a sweep review of it is still running.

| The numbers that matter | Value |
| --- | --- |
| Sweep dispatches that run an unmerged branch's workflow file | 0 (was: every same-repo PR, every 30 minutes) |
| New GitHub API calls in the sweep | 0 |
| New calls in `_has_active_autofix_run` | 1 `gh run list`, only when no head-branch run was found |
| New calls in `check_in_status.py` (`--hand-back`, and `--pr N`'s stuck check) | 1 REST read of `internal-review.yml` dispatch runs, only when no head-branch run was found |

What this means for operators: sweep-dispatched review runs now show `main` as their branch in the Actions list, with the PR in the run name (`Internal: AI Review & Autofix [pr:<N>]`). Pull request and push runs keep their usual names. Nothing changes for consumer repos.

### For contributors

The orchestrator's `_dispatch_review_for_conflicts`, `scripts/review_merge_train.sh`, and the `forward-merge-stable-to-main.yml` fallback still dispatch at a head ref. The forward-merge branch is cut from `stable` by the workflow itself. The other two are the same pattern as this finding and are out of this fix's scope.
