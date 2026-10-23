<!-- changelog: fixed -->
- **Implement runs retry opening the PR after a transient GitHub error.** A one-off GitHub failure on `gh pr create` no longer fails a finished implementation run and strands its pushed branch.

The "Create Pull Request" step of `.github/workflows/implement.yml` made a single `gh pr create` attempt. In release run 39926633796, implement run 39937572634 finished its change and pushed `ai/issue-7956`. GitHub then answered the create with `GraphQL: Something went wrong while executing your query`, no PR existed, and the run failed. The smoke test's implement phase timed out and blocked v1.39.0. The step now retries the create up to two more times, 10 s and then 20 s later, but only when the existing recovery lookup finds no PR on the head branch and the error is transient: that GraphQL error, HTTP 500 to 504, or a network timeout, reset or EOF. Any other error, such as "No commits between", still fails on the first attempt. Each decision is logged as `IMPLEMENT_PR_CREATE ... outcome=retry|give_up`.

| The numbers that matter | Value |
| --- | --- |
| `gh pr create` attempts per run | at most 3 (was 1) |
| Added wait before giving up | at most 30 s |
| Extra GitHub API calls | only after a failed create: one open-PR listing and, when that misses, one issue-timeline read per retry |

What this means for operators: a short GitHub API problem at PR-creation time no longer costs a failed implement run and a manual re-approval. GitHub allows one open PR per head and base branch, so a retry cannot open a duplicate PR. No variables or settings change.
