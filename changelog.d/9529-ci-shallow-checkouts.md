<!-- changelog: changed -->
- **Three slow CI jobs no longer clone full history.** `tests-hooks-and-orchestrator`, `tests-release-and-log-analysis` and the 24-shard `tests-system-python` matrix in `.github/workflows/ci.yml` now check out at depth 1.

A new step in each job, "Fetch base commits for the shallow checkout (...)", fetches only what the history readers need (`tests/claude_template_drift.py` and the template-parity Prepare step): a 200-commit window of HEAD and `main`, plus the pull-request base or push `before` commit. It unshallows those two refs only when the merge-base is deeper than the window. Pushes to `main` and `stable` fetch nothing, because nothing reads a base there. A pull request whose base or merge-base cannot be fetched fails the job with an `::error::` line instead of running the parity and drift tests without history; a branch-creation or force push keeps its existing fallback to the merge-base with `main`, with a warning.

| The numbers that matter | Value |
| --- | --- |
| Jobs changed | 3 (26 job instances per CI run, counting the 24 shards) |
| Median `Checkout repository` time before, full history | 2.7 to 3.6 min per job |
| History window fetched | 200 commits of HEAD and `main`, base commit at depth 1 |
| Fetch attempts | 3, backoff `SHALLOW_FETCH_RETRY_DELAY_SECONDS` (default 5) times the attempt number |

What this means for operators: CI runs in this repository spend minutes less on checkout in every one of those jobs, with the same tests. Nothing changes for consumer repositories.

### For contributors

`tests/test_ci_shallow_checkout_contract.py` pins the layout (depth 1, fetch step second, identical bodies in all three jobs) and runs the step against a local bare origin for the pull-request, branch-creation, force-push, project-push, `main`-push and deep-merge-base cases. Refs #9528.
