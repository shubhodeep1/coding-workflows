<!-- changelog: fixed -->
- **Auto-merge now waits for CI itself, not just for any green check.** Before the review enables `gh pr merge --auto`, the configured CI check (`AUTO_MERGE_REQUIRED_CI_CHECK`, default `lint` in this repository) must exist on the reviewed head and have succeeded. An empty check-run list no longer counts as green.

The required-checks wait added for #6906 read a head with no check-runs as green after two poll intervals. It also accepted any green required check, so an earlier review run's `review / gate` success on the same head could authorize a merge that CI never ran on. Activation verification of PR #6938 found both gaps. The wait now polls until a check-run with exactly the configured name exists on the head. A failed, skipped, neutral or cancelled run refuses the merge (`outcome=failed`). If the check never appears or never finishes, the wait times out after `AUTO_MERGE_CHECKS_WAIT_MINUTES` (`outcome=timeout`). Every run with that name counts, so a failing or still-running duplicate outranks a success, and the review's own run never counts. The callers still pass the reviewed head and merge with `--match-head-commit`. Actions `pull_request` CI reports on that head even though it checks out the test-merge commit.

| The numbers that matter | Value |
| --- | --- |
| Variable | `AUTO_MERGE_REQUIRED_CI_CHECK`: unset or empty means `lint` in `shubhodeep1/coding-workflows` and off elsewhere; `none` turns it off |
| Paths covered | codex-agent `Enable auto-merge on PR`, the `deterministic-skip-merge` job, the review-blocked judge's `merge` and terminal `fix` |
| Extra API calls | none (reads the check-run listing the wait already fetches) |
| Log line | `AUTOFIX_AUTO_MERGE_CHECKS ... ci_check=<name> ci_state=absent\|pending\|success\|failure\|unknown\|disabled` |

What this means for operators: in this repository auto-merge waits for `lint` (about 9 minutes). A consumer repository keeps its current behaviour until it sets `AUTO_MERGE_REQUIRED_CI_CHECK` to the name of its aggregate CI check-run. A wrong name makes every auto-merge time out with `ci_state=absent`; set the variable to `none` to turn the requirement off.

### For contributors

`_pr_checks_completed` in `scripts/pr_checks_lib.sh` records `PR_CHECKS_LAST_CI_NAME` and `PR_CHECKS_LAST_CI_STATE` but returns exactly what it did before, so the orchestrator's direct merges are unchanged. `_pr_wait_for_required_checks` enforces the requirement. Adding the variable pushed `.github/workflows/review_autofix.yml` over the 480,000-byte guard, so eight `codex-agent` step bodies moved verbatim to `scripts/review_autofix_step_*.sh` (481,544 to 429,786 bytes). Tests that read those bodies now use `expanded_review_autofix_text()`. The match is by check name and head SHA only, not by workflow path, so a same-repository PR could add its own job named `lint`.
