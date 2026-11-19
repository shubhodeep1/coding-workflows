<!-- changelog: fixed -->
- **The release gate no longer fails a job when one GitHub API read hits a transient error.** Every `$(gh api … 2>/dev/null || echo <fallback>)` capture in `.github/workflows/test-and-mark-stable.yml` now assigns its fallback outside the substitution.

On an HTTP error, `gh api` prints the error body to stdout and exits non-zero. The old shape appended the fallback to that body, so the captured value held multi-line JSON followed by the fallback. In release gate run 44490333814 a momentary "API rate limit exceeded" on the clarify-run lookup put that value into `$GITHUB_OUTPUT`, and the runner killed the `clarify-rejects-unsolvable-test` job with "Invalid format". The captures now read `V=$(gh api …) || V=<fallback>`, and the run ids written to `$GITHUB_OUTPUT` by the clarify-negative and alt-model `create` and capture steps are also checked to be numeric before they are written.

| The numbers that matter | Value |
| --- | --- |
| Captures rewritten in `test-and-mark-stable.yml` | 29 |
| Run-id outputs with a numeric shape check | 4 |
| Failing run | 44490333814 (`clarify-rejects-unsolvable-test`, 2026-11-19) |

What this means for operators: a single rate-limited or failed read inside the release gate now falls back to the default its step was designed for, such as run id `0`, instead of failing the job and blocking the `@stable` release.

### For contributors

`tests/test_test_and_mark_stable_gh_api_error_body_capture.py` runs the real `create` steps against a stub `gh` that prints a JSON error body to stdout and exits 1, and fails if any `$(gh api …)` capture in the workflow puts `|| echo` inside the substitution again. The same pattern in `scripts/review_sweep_head_gate.sh`, `scripts/unblock_judge.sh` and `scripts/workflow_retro_fanout.sh` was left as it is: in each of them the leaked body already resolves to the intended fallback.
