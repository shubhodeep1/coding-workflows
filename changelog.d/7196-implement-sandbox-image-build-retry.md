<!-- changelog: fixed -->
- **Implement retries a failed sandbox image build before giving up.** A single failed Docker build or registry pull no longer fails the whole implementation run.

Activation verification of PR #7195 found that both `prepare` calls in `implement.yml` (the "Run Codex implementation" step and the post-Codex syntax-repair fallback) failed after one image build attempt. `build_image` in `scripts/codex_isolated_exec.sh` now reads `CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS`, and those two calls set it to 3 inline. Only the image build is retried. Workspace snapshots, dependency staging, the registry proxy and transfer checks still fail on their first error, and an exhausted build still fails the step with no host fallback.

| The numbers that matter | Value |
| --- | --- |
| Helper default | 1 attempt (other callers unchanged) |
| Implement `prepare` calls | 3 attempts |
| Cap | 5 attempts |
| Wait before attempt n+1 | `CODEX_ISOLATED_IMAGE_BUILD_RETRY_SLEEP_SECS` (15) × n seconds |
| Log line | `CODEX_ISOLATION image_build attempt=<n>/<max> outcome=ok\|retry\|fail` |

What this means for operators: nothing to configure. A transient Docker Hub or npm outage during implement's sandbox preparation now costs up to about 45 seconds of waiting instead of a failed run.
