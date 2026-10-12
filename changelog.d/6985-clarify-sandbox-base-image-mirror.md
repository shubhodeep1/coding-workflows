<!-- changelog: fixed -->
- **Clarify no longer fails when Docker Hub rate-limits its sandbox image build.** Clarify runs 37990077414, 37990177613, 37990757342, 37990879005 and 37992097499 (heal issue #6985) failed on all three attempts: the build of `scripts/clarify_sandbox/Dockerfile` pulled `node:22.16.0-bookworm-slim` anonymously from Docker Hub, and Docker Hub answered `429 Too Many Requests` every time, so retrying never helped. `.github/workflows/clarify.yml` now has a "Pre-seed clarify sandbox base image" step right before "Run Codex". It reads the Dockerfile's `FROM` image, loads the same tag from a Docker Official Images mirror, and tags it locally under its Docker Hub name. The unchanged `docker build` (no `--pull`) then uses the local image and sends no request to Docker Hub.

  | Setting | Default | Meaning |
  | --- | --- | --- |
  | `CLARIFY_BASE_IMAGE_MIRRORS` (repo var) | `public.ecr.aws/docker/library mirror.gcr.io/library` | Mirrors tried in order. Entries that are not a plain `host/path` are skipped. |
  | `CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` (repo var) | `300` | Time limit for each mirror pull. Values outside 30-1800 fall back to `300`. |

  The step uses no credentials and always exits 0. Each run logs one `CLARIFY_BASE_IMAGE_PRESEED image=<ref> outcome=seeded|present|skip|mirror_failed|unavailable` line. If every mirror fails, it emits a warning and the build falls back to Docker Hub as before. A `FROM` that is not a `name:tag` official image is skipped with `reason=unsupported_ref`. Plan, implement and review sandboxes (`scripts/codex_isolated_exec.sh`, `scripts/review_sandbox/Dockerfile`) still pull from Docker Hub and are not covered by this change. Regression test: `tests/test_clarify_sandbox_base_image_preseed.py`.
