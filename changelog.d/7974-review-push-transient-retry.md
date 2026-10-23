<!-- changelog: fixed -->
- **Review/autofix no longer loses a finished edit cycle to a transient push failure.** The "Push all pending commits" step of `review_autofix.yml` now retries a GitHub server error, a `remote rejected ... (failed)` or a network drop up to 2 times, and stops retrying once the remote branch already shows the pushed commit.

Before, the step retried only non-fast-forward rejections. Every other failure ended the run, so runs 35868679924 (GitHub `Internal Server Error`) and 35843269131 (`remote rejected ... (failed)`) committed a full edit cycle and then lost it at the push.

- A failure is retried only when the push output matches a GitHub 5xx (`Internal Server Error`, `The requested URL returned error: 5xx`, `HTTP 5xx`), `remote rejected ... (failed)`, or a network error (`RPC failed`, `unexpected disconnect`, `early EOF`, `Connection reset`, `Could not resolve host`, `Operation timed out`).
- Protected-branch and repository-rule rejections (`GH006`, `GH013`, `protected branch`, `push declined`, `repository rule`) are checked first and still fail at once.
- Each retry waits 5 s, then 10 s, and reads the remote tip with `git ls-remote`. When the tip already equals the local commit, the push counts as done; the tip is checked once more before giving up after the second retry. Otherwise the same plain push runs again; a remote that moved meanwhile goes through the existing fetch-and-merge path. Nothing is force-pushed or rebased.
- Each decision logs `AUTOFIX_PUSH_TRANSIENT_RETRY pr=<n> attempt=<i>/2 class=server_error|remote_rejected_failed|network outcome=retry|landed|gave_up`.
- Switch: `AUTOFIX_PUSH_TRANSIENT_RETRY_ENABLED` (default `true`; `false` in any letter case restores the old behaviour).

Tests: additions to `tests/test_review_autofix_review_pipeline_contract.py`, which run the step body against a fake `git`.
