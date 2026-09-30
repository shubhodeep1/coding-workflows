<!-- changelog: security -->
- **Pre-approved `internal-review.yml` dispatches now run only from the default branch.** Before this change, an unattended session could be led into running a pushable branch's copy of the workflow, with the workflow's secrets and write permissions, and no permission prompt (issue #5375).

Issue #4985 pre-approved `internal-review.yml` dispatches so `/fix-claude-pr` can re-run a stalled review. The allow rule `Bash(gh workflow run internal-review.yml *)` and the `gh api` guard accepted any `--ref`, and `.claude/scripts/dispatch_workflow.py` passed any `--ref` and any input through. Now the allow rule is gone, and the guard in `.claude/hooks/gh_api_write_guard.py` asks before a raw `gh api …/internal-review.yml/dispatches` call. `dispatch_workflow.py` is the only pre-approved path left. It reads the default branch from the REST API and dispatches on it, and it refuses any other `--ref`. It also refuses a missing or non-numeric `pr_number` and any other input, exiting 1 before the POST. This matches the rule `review_autofix_sweep.yml` has followed since issue #4618.

| The numbers that matter | Value |
| --- | --- |
| Finding | `untrusted-ref-review-dispatch`, severity high, confidence 9/10 (issue #5375) |
| Pre-approved paths to dispatch `internal-review.yml` | 3 before, 1 after (`dispatch_workflow.py`) |
| Inputs accepted for `internal-review.yml` | `pr_number` only, 1 to 10 digits, no leading zero |
| Extra API calls | at most 1 REST read of the repository, only when `--ref` is given |

What this means for operators: `/fix-claude-pr` re-dispatches a stalled review exactly as before, because it already passes no `--ref` and only `pr_number`. Typing `gh workflow run internal-review.yml …` by hand, or dispatching it through `gh api`, now asks for permission. Consumer repos get the helper and guard change with the next `.claude/` sync. They have no `internal-review.yml`, so nothing changes for them in practice.

### For contributors

The helper keeps `DISPATCHABLE_WORKFLOWS` and adds `DEFAULT_BRANCH_ONLY_WORKFLOWS` and `DEFAULT_BRANCH_ONLY_INPUT_KEYS`. `tests/test_dispatch_workflow.py` asserts that the `gh workflow run` allow rules equal `DISPATCHABLE_WORKFLOWS - DEFAULT_BRANCH_ONLY_WORKFLOWS`. The guard's `DISPATCHABLE_WORKFLOWS` stays equal to the allow rules, so it no longer lists `internal-review.yml`. The six older `gh workflow run <file> *` rules still accept an unpinned `--ref`; this change does not touch them.
