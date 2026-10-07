<!-- changelog: fixed -->
- **GitHub API calls on the review, validate, implement, clarify-respond and security-audit hot paths now wait out a rate limit instead of failing on the first error, and a failed call no longer reads as "nothing found".** Refs #5873.

On 2026-10-01 the `GH_PAT` budget ran out and about 18 review gates and two validation runs failed on their first call; two projects stopped for a human. The new `gh_api_retry` helper in `scripts/gh_helpers.sh` (Python twin `scripts/gh_api_retry.py`) reads the response status and headers. A primary limit waits for the reset of the limited bucket (core, GraphQL or search). A secondary limit waits for `retry-after`. Server errors back off. Other 4xx responses are not retried. When a reset is more than 10 minutes away it gives up at once with exit code 75 instead of sleeping.

| Call site | Before | Now |
| --- | --- | --- |
| Review gate PR read | a failed read skipped the review as `pr_state_unknown` | `AUTOFIX_GATE_PR_READ_FAILED`, the run fails and the sweep retries it; a 404 still skips |
| `validate.yml` authorize step | a transient error read as "not authorized" | `VALIDATE_AUTHORIZE_TARGET outcome=api_unavailable` |
| Integration-ref resolve (validate, implement) | a rate limit fell back to the default branch | `INTEGRATION_REF_RESOLVE outcome=rate_limited`, the step fails |
| `implement.yml` existing-PR check | a failed listing read as "no PR" | `IMPLEMENT_PR_SAFETY_CHECK outcome=api_unavailable` |
| Review sweep active-run snapshot | a failed listing read as "no active runs" | `AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE`, no dispatches that tick |

The existing helpers changed too. `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` make one attempt for a POST create (comments, issues, PRs, dispatches) unless the caller marks it `--idempotent` or `GH_RETRY_IDEMPOTENT=true`; label and assignee adds carry that marker. `gh_retry_to_file` no longer leaves a failed response in its output file. No helper sleeps after its last attempt, and a GraphQL or search limit waits for its own reset. No GitHub API calls were added; the early steps get the helper with a git clone of `main` (here) or `stable` (consumers).

### For contributors

New environment variables, all with defaults: `GH_RETRY_IDEMPOTENT` (`false`), `GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS` (`600`), `GH_RETRY_BACKOFF_CAP_SECS` (`120`), `GH_API_RETRY_LOW_BUDGET_REMAINING` (`100`). New log prefix `GH_API_RETRY`. Removing the inline retry copies, the poller's default-data fallbacks, the Python readers and re-running after a reset are follow-ups.
