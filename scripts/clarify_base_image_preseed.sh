#!/usr/bin/env bash
# Pre-seed clarify sandbox base image (shared helper).
#
# Loads the FROM image of the clarify sandbox Dockerfile from a Docker Official
# Images mirror and tags it under its Docker Hub name, so the `docker build`
# in scripts/clarify_isolated_run.sh (no --pull) finds it locally instead of
# making an anonymous Docker Hub pull that can fail with 429 Too Many Requests
# (workflow failure heal #6985, PR #7200; moved out of clarify.yml for #7215).
#
# Usage: clarify_base_image_preseed.sh [DOCKERFILE]
#   DOCKERFILE defaults to scripts/clarify_sandbox/Dockerfile.
# Environment (defaults set here, §8):
#   CLARIFY_BASE_IMAGE_MIRRORS            space-separated mirror prefixes
#                                         (unset: public.ecr.aws/docker/library mirror.gcr.io/library;
#                                         set but empty: no mirrors)
#   CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS  per-mirror pull timeout, 30-1800 (default 300)
# Always exits 0. Log prefix: CLARIFY_BASE_IMAGE_PRESEED.
set -uo pipefail
preseed_dockerfile="${1:-scripts/clarify_sandbox/Dockerfile}"
# Only an implicit Docker Hub official image (`name:tag`) maps onto
# `<mirror>/<name>:<tag>`; anything else is left to docker build.
preseed_ref_re='^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._-]{1,128}$'
# The first character must be alphanumeric so an entry can never be
# parsed by `docker pull` as a CLI flag (for example `-x/lib`).
preseed_mirror_re='^[a-z0-9][a-z0-9.-]*(/[a-z0-9._-]+)*$'
preseed_sanitize() { printf '%s' "${1:-}" | tr -c 'A-Za-z0-9._:/@-' '_' | cut -c1-200; }
preseed_docker() { env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker "$@"; }

preseed_timeout="${CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS:-300}"
if ! [[ "${preseed_timeout}" =~ ^[0-9]{1,4}$ ]] || [ "$((10#${preseed_timeout}))" -lt 30 ] || [ "$((10#${preseed_timeout}))" -gt 1800 ]; then
	preseed_timeout=300
fi
preseed_timeout=$((10#${preseed_timeout}))

if [ ! -f "${preseed_dockerfile}" ]; then
	echo "::warning::CLARIFY_BASE_IMAGE_PRESEED image=none outcome=skip reason=dockerfile_missing"
	exit 0
fi
preseed_ref="$(awk 'toupper($1) == "FROM" { print $2; exit }' "${preseed_dockerfile}")"
if ! [[ "${preseed_ref}" =~ ${preseed_ref_re} ]]; then
	preseed_safe_ref="$(preseed_sanitize "${preseed_ref}")"
	echo "::warning::CLARIFY_BASE_IMAGE_PRESEED image=${preseed_safe_ref:-none} outcome=skip reason=unsupported_ref"
	exit 0
fi

if preseed_docker image inspect "${preseed_ref}" >/dev/null 2>&1; then
	echo "CLARIFY_BASE_IMAGE_PRESEED image=${preseed_ref} outcome=present"
	exit 0
fi

read -r -a preseed_mirrors <<< "${CLARIFY_BASE_IMAGE_MIRRORS-public.ecr.aws/docker/library mirror.gcr.io/library}"
# At most 8 mirrors, so a misconfigured list cannot queue dozens of sequential pulls.
preseed_mirrors=("${preseed_mirrors[@]:0:8}")
preseed_attempted=0
for preseed_mirror in "${preseed_mirrors[@]}"; do
	if ! [[ "${preseed_mirror}" =~ ${preseed_mirror_re} ]]; then
		echo "CLARIFY_BASE_IMAGE_PRESEED image=${preseed_ref} outcome=mirror_skipped source=$(preseed_sanitize "${preseed_mirror}") reason=invalid_mirror"
		continue
	fi
	preseed_attempted=$((preseed_attempted + 1))
	preseed_source="${preseed_mirror}/${preseed_ref}"
	# Actions runs this body under `bash -e`; a failed assignment from a
	# command substitution would exit before the next mirror, so the
	# status is captured with `||` instead of a separate `$?` read.
	preseed_pull_rc=0
	preseed_pull_out="$(env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN timeout "${preseed_timeout}" docker pull --quiet "${preseed_source}" 2>&1)" || preseed_pull_rc=$?
	if [ "${preseed_pull_rc}" -eq 0 ] \
		&& preseed_docker tag "${preseed_source}" "${preseed_ref}" >/dev/null 2>&1 \
		&& preseed_docker image inspect "${preseed_ref}" >/dev/null 2>&1; then
		echo "CLARIFY_BASE_IMAGE_PRESEED image=${preseed_ref} outcome=seeded source=${preseed_mirror}"
		exit 0
	fi
	preseed_detail="$(printf '%s' "${preseed_pull_out}" | tail -n 1 | tr -c 'A-Za-z0-9._:/@ -' '_' | cut -c1-200)"
	echo "CLARIFY_BASE_IMAGE_PRESEED image=${preseed_ref} outcome=mirror_failed source=${preseed_mirror} rc=${preseed_pull_rc} detail=${preseed_detail:-none}"
done
echo "::warning::CLARIFY_BASE_IMAGE_PRESEED image=${preseed_ref} outcome=unavailable mirrors=${preseed_attempted}"
exit 0
