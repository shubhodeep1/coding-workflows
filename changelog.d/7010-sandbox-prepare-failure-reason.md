<!-- changelog: fixed -->
- **Review sandbox image builds retry Docker Hub failures, and a failed sandbox prepare is reported as `sandbox_prepare_failed`.** One Docker Hub 500 or 504 no longer costs a review run its editor, and when the prepare does fail the run says so instead of reporting an empty editor.

`scripts/review_untrusted_sandbox.sh` now retries the sandbox `docker build` when the error names a registry or network failure: Docker Hub auth or registry endpoints, 429 or 5xx, `failed to resolve source metadata`, TLS or I/O timeouts, connection resets. Any other build failure still fails at once. When `Install project dependencies (best-effort)` fails anyway, `Post editor summary comment` in `review_autofix.yml` names the failure `sandbox_prepare_failed`. That name reaches the failure marker, the fingerprint, the heal report and the run summary's `finalize_reason`. The no-output comment names the sandbox prepare and its error. On PR #6645, runs 37989220029 and 37994304266 lost their editor to a Docker Hub error on `node:22.16.0-bookworm-slim` and were reported as `editor_empty_noop`.

| The numbers that matter | Value |
| --- | --- |
| `REVIEW_SANDBOX_BUILD_ATTEMPTS` | `3` (default) |
| `REVIEW_SANDBOX_BUILD_RETRY_SLEEP_1` / `_2` | `10` s / `30` s (defaults) |
| Log line | `REVIEW_SANDBOX_BUILD attempt=<n> outcome=ok\|retry\|fail` |
| Reason precedence | after `reviewers_failed`, ahead of `editor_empty_noop` |

What this means for operators: transient Docker Hub errors heal inside the run. A prepare that keeps failing shows up as `sandbox_prepare_failed` with the build error as evidence, so heal issues point at the sandbox instead of the editor. Retry handling is unchanged: the run still posts the no-output comment and applies no `ai:review-blocked`.

### For contributors

The install step has `id: deps_prepare` and keeps each prepare's stderr in `${RUNTIME_DIR}/sandbox_prepare_stderr.txt`. The editor summary step writes `sandbox_prepare_failure_evidence.txt`, which every fingerprint and failure-headline call site reads, and exports `AUTOFIX_SANDBOX_PREPARE_FAILED=true`. Tests are in `tests/test_review_sandbox_build_retry.py`.
