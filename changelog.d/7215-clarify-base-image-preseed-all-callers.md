<!-- changelog: fixed -->
- **Plan, clarify-respond, check-failure triage, the unblock judge and the Claude engine smoke also get the clarify sandbox base image from a mirror first.** A Docker Hub rate limit no longer fails them while a mirror is reachable, as it already no longer failed clarify.

The mirror pre-seed added for clarify in heal #6985 (PR #7200) now lives in `scripts/clarify_base_image_preseed.sh`. `scripts/clarify_isolated_run.sh` runs it right before it builds the sandbox image, so every caller of that script seeds the image. Clarify's "Pre-seed clarify sandbox base image" step keeps its name and now calls the same helper. The log line and its fields are unchanged; a support tree without the helper logs `CLARIFY_BASE_IMAGE_PRESEED image=none outcome=skip reason=helper_missing`.

| The numbers that matter | Value |
| --- | --- |
| `CLARIFY_BASE_IMAGE_MIRRORS` | unset: `public.ecr.aws/docker/library mirror.gcr.io/library`; set but empty: no mirrors |
| `CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` | default `300` per mirror pull; values outside 30-1800 fall back to `300` |
| Extra wait before the model starts when every mirror fails | at most 2 × 300 s with the defaults |

What this means for operators: nothing needs configuring. The repository variables `CLARIFY_BASE_IMAGE_MIRRORS` and `CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` still apply to clarify's step; the other callers use the helper's defaults. The helper uses no credentials and never fails a run.

### For contributors

CI's "Clarify sandbox base image pre-seed tests (#6985)" step now runs `tests/test_clarify_sandbox_base_image_preseed.py` with `TMPDIR` set to the runner temp directory, because its fake `docker` must be executable. The helper is staged with the other clarify isolation support files in `clarify.yml`, `plan.yml`, `orchestrate_clarify_respond.yml` and `check_failure_triage.yml` (including the triage trusted-support copy). The GHCR image build in `scripts/sandbox_images_publish.sh` and the implement and review sandboxes (`scripts/codex_isolated_exec.sh`, `scripts/review_sandbox/Dockerfile`) still pull from Docker Hub.
