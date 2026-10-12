<!-- changelog: fixed -->
- **Clarify gets its sandbox base image from a mirror first, so a Docker Hub rate limit no longer fails it while a mirror is reachable.** The clarify sandbox's base image now comes from a Docker Official Images mirror instead of an anonymous Docker Hub pull; Docker Hub is used only when every mirror fails.

Clarify runs 37990077414, 37990177613, 37990757342, 37990879005 and 37992097499 (heal issue #6985) failed on all three attempts. Each time, the build of `scripts/clarify_sandbox/Dockerfile` pulled `node:22.16.0-bookworm-slim` from Docker Hub and got `429 Too Many Requests`, so retrying never helped. `.github/workflows/clarify.yml` now runs a "Pre-seed clarify sandbox base image" step right before "Run Codex". The step reads the Dockerfile's `FROM` image, pulls the same tag from a mirror, and tags it locally under its Docker Hub name. The unchanged `docker build` (no `--pull`) then uses that local image and sends no request to Docker Hub.

| The numbers that matter | Value |
| --- | --- |
| `CLARIFY_BASE_IMAGE_MIRRORS` (repo var) | default `public.ecr.aws/docker/library mirror.gcr.io/library`, tried in order; entries that are not a plain `host/path` are skipped |
| `CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` (repo var) | default `300` per mirror pull; values outside 30-1800 fall back to `300` |
| Log line | `CLARIFY_BASE_IMAGE_PRESEED image=<ref> outcome=seeded\|present\|skip\|mirror_skipped\|mirror_failed\|unavailable` |

What this means for operators: clarify keeps running while Docker Hub throttles anonymous pulls, and nothing needs configuring. The step uses no credentials and never fails the run. If every mirror fails, it logs a warning and the build falls back to Docker Hub as before, so a Docker Hub 429 can still fail that run. A `FROM` that is not a `name:tag` official image is skipped with `reason=unsupported_ref`.

### For contributors

The step has `continue-on-error: true` and records a failed pull with `|| preseed_pull_rc=$?`, so the next mirror is still tried under the runner's `bash -e`. Only `clarify.yml` is covered. These other callers of the same `scripts/clarify_isolated_run.sh` build still pull the base image from Docker Hub:
- plan (`scripts/run_plan_codex.sh`)
- clarify-respond (`orchestrate_clarify_respond.yml`)
- the unblock judge (`scripts/unblock_judge.sh`)
- check-failure triage (`scripts/check_failure_triage.sh`)
- `claude-engine-smoke.yml`

The implement and review sandboxes (`scripts/codex_isolated_exec.sh`, `scripts/review_sandbox/Dockerfile`) also still pull from Docker Hub. The regression test `tests/test_clarify_sandbox_base_image_preseed.py` runs in `ci.yml`'s isolated-agent test step.
