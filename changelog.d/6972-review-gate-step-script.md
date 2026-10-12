<!-- changelog: fixed -->
- **`review_autofix.yml` is back under the 480,000-byte workflow guard.** CI failed on every PR (run 37981664287) because the file had grown to 481,287 bytes; GitHub stops starting a workflow above 512,000 bytes.

The "Evaluate review gate" step body moved verbatim to `scripts/review_autofix_step_evaluate_gate.sh`. The step keeps its `id: evaluate`, `env:` and outputs. The gate job runs before support staging, so the script is loaded only from the SHA-verified `.codex-head-gate-src` sparse checkout pinned to the review support commit, never from a PR-head copy.

| The numbers that matter | Value |
| --- | --- |
| `review_autofix.yml` size | 350,009 bytes (was: 429,287 before this change, 481,287 in the failing run) |
| Headroom below the guard | 129,991 bytes |
| New GitHub API calls | 0 |

What this means for operators: when the head-gate helper checkout fails or cannot be verified, the review gate now fails on every event. It used to fail only on `synchronize` and otherwise evaluate without the helper. The next push, the review sweep or the poller re-runs the gate.
