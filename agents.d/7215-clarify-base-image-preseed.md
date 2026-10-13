<!-- agents: section="Workflow architecture" -->
Clarify sandbox base image pre-seed (#6985, #7215): `scripts/clarify_base_image_preseed.sh [DOCKERFILE]`
pulls the `FROM` image of `scripts/clarify_sandbox/Dockerfile` from a Docker Official Images
mirror and tags it under its Docker Hub name, so the build without `--pull` does not hit Docker
Hub's anonymous-pull 429. `clarify_image_build` in `scripts/clarify_isolated_run.sh` first tries
the prebuilt GHCR image (`sandbox_image.sh ref`, then one pull bounded by
`SANDBOX_IMAGE_PULL_TIMEOUT_SECS`, default 60) and runs the helper (output to stderr, credentials
unset) only before a local build, so slow mirrors never delay a pullable prebuilt image. A failed
prebuilt pull is retried once by `sandbox_image.sh build`; when that hand-off fails (its unseeded
local build after a label mismatch or failed re-pull), the helper runs and the image is built
again. A failed `sandbox_image.sh ref` logs `SANDBOX_IMAGE family=clarify outcome=ref_failed`.
This covers clarify, plan,
clarify-respond, check-failure triage, the unblock judge and the Claude engine smoke; clarify's
"Pre-seed clarify sandbox base image" step also calls it with the repository-variable overrides.
It always exits 0. Defaults live in the helper: `CLARIFY_BASE_IMAGE_MIRRORS` unset means
`public.ecr.aws/docker/library mirror.gcr.io/library` (set but empty means no mirrors) and
`CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` is `300` (30-1800). A support tree without the helper logs
`reason=helper_missing` and builds as before.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CLARIFY_BASE_IMAGE_PRESEED` (`scripts/clarify_base_image_preseed.sh`: `image= outcome=present|seeded|mirror_failed|mirror_skipped|unavailable|skip reason=dockerfile_missing|unsupported_ref|invalid_mirror|helper_missing source= rc= detail= mirrors=`)
LOG_PREFIX.name=CLARIFY_BASE_IMAGE_PRESEED
