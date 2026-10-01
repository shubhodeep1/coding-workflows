<!-- changelog: fixed -->
- **`gh_retry` no longer hands callers the error bodies of attempts that failed.** When a GitHub call was retried and then succeeded, the caller got the failed attempts' response bodies followed by the real response. It now gets only the successful response.

`gh api` prints the error response body to stdout when a call fails, and `gh_retry` in `scripts/gh_helpers.sh` redirected only stderr for each attempt. On 2026-09-30, clarify run 36670937896 was rate-limited twice while fetching #5016 and then succeeded. Its metadata file held two rate-limit error objects before the issue, `jq -r '.number'` printed `null`, `null`, `5016`, and the step failed with `Invalid format 'null'`. The `/reclarify` it was handling was lost. `gh_retry` now buffers each attempt's stdout and prints only the buffer of the attempt that succeeds. A call that never succeeds prints nothing to stdout and still returns 1. Return codes, retry counts, rate-limit waits, the Telegram alert, the circuit breaker, and the existing stderr lines are unchanged, and no caller had to change.

| The numbers that matter | Value |
| --- | --- |
| Failing run | clarify run 36670937896, step **Fetch issue metadata** (`clarify.yml:392`) |
| `gh_retry gh` occurrences covered, with no call-site edits | 557 across `.github/workflows/` and `scripts/` |
| New stderr line per failed attempt with output | `gh_retry: dropped <N> bytes of stdout from failed attempt <a>/<m>` |

What this means for operators: a workflow step that hits a rate limit and recovers now continues with correct data instead of failing later on malformed output. For a failed attempt, the new warning line gives the size of the output that was dropped, and gh's own stderr message still names the error.

### For contributors

`gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api` were checked and left unchanged: each already keeps a failed attempt out of a successful result. `gh_retry_to_file` still leaves the last error body in its output file on failure, because callers print it as a diagnostic. If the successful attempt's output cannot be delivered (the reader closed the pipe), `gh_retry` returns non-zero and does not run the command again; before this change, `gh` took the SIGPIPE itself and the command was retried. The two inline `gh_retry()` retry loops in `.github/workflows/review_autofix.yml` (the fallback in **Dispatch standalone validate for orchestrator short-circuit issues** and the wrapper in the deterministic-skip-merge step, whose `$(gh_retry gh api …)` head-SHA capture gates the merge-authorization labels) now buffer stdout the same way, with the same dropped-bytes warning. `tests/test_gh_retry_stdout_isolation.py` drives the helper and both inline wrappers through a fake `gh` and runs in the `ci.yml` gh_helpers test step.
