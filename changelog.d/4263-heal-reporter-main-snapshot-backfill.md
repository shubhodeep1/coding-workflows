<!-- changelog: fixed -->
- **Repeated review/autofix failures on older PR branches now reach the workflow-heal intake.** The review workflow's failure path skipped its heal report with `reason=reporter_missing` whenever the PR branch predated the reporter script, so those PRs never opened a heal issue.

`review_autofix.yml` stages its support scripts with the `stage_workflow_support.sh` from the PR branch, and a branch forked before #4208 does not know `workflow_failure_heal.py` or `workflow_failure_heal_autofix_report.sh`. Run 35685250882 on PR #4259 was the second consecutive `editor_empty_noop` failure on that PR and should have been reported, but the `Report autofix failure to workflow failure heal` step found no reporter and logged `WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=reporter_missing`. The `Stage workflow support files` step now backfills the pair from the main snapshot, the same way it already backfills the preflight-checked scripts, using the new `REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS` env list. A branch copy still wins when present, and a miss on both refs only logs a warning.

| The numbers that matter | Value |
| --- | --- |
| Failing run | 35685250882 on PR #4259 |
| Scripts backfilled | `workflow_failure_heal.py`, `workflow_failure_heal_autofix_report.sh` |
| Extra GitHub API calls | 0 (files come from the already checked-out main snapshot) |

What this means for operators: a PR whose AI review keeps failing now files its `ai:workflow-heal` issue after the second failed run regardless of how old its branch is; the reporter no longer depends on the PR branch carrying the script.

### For contributors

The backfill mirrors the `REVIEW_PREFLIGHT_*_SUPPORT_SCRIPTS` loop in the same step. `tests/test_workflow_failure_heal.py` pins the env list and executes the loop against a stale checkout plus a main snapshot.
