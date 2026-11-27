#!/usr/bin/env bash
# sandbox_image.sh — resolve a sandbox image: pull the prebuilt copy from
# GHCR, build it locally when the pull fails.
#
# The sandbox images (scripts/clarify_sandbox, scripts/review_sandbox and the
# context scripts/codex_isolated_exec.sh generates) are built from fixed,
# trusted build contexts. Building them on every job costs minutes and
# depends on Docker Hub, npm and Debian mirrors being up (the 2026-10-09
# Docker Hub outage pushed Claude roles to the codex fallback).
# .github/workflows/publish-sandbox-images.yml prebuilds them and pushes each
# one under a tag derived from its inputs, so a job pulls exactly the image a
# local build of the same inputs would produce.
#
# Usage:
#   sandbox_image.sh build --family NAME [--build-arg K=V]... [-f DOCKERFILE] CONTEXT
#       Print the image ID (sha256:...) on stdout. Pulls
#       <registry>:<family>-<input hash> first; when the pull fails, times out
#       or returns an image whose input label does not match, runs
#       `docker build -q` with the same inputs. Exit status is the local
#       build's when it runs, else 0.
#   sandbox_image.sh publish --family NAME [--force] [--build-arg K=V]... [-f DOCKERFILE] CONTEXT
#       Build and push the tagged image (publish workflow only; needs a
#       `docker login` to the registry). Without --force an existing tag is
#       left alone.
#   sandbox_image.sh ref --family NAME [--build-arg K=V]... [-f DOCKERFILE] CONTEXT
#       Print the image reference without touching Docker.
#
# The input hash covers every regular file in CONTEXT (relative path and
# content), the Dockerfile's path inside CONTEXT, and the sorted build args.
# The Dockerfile must live inside CONTEXT. Both build paths add the label
# coding-workflows.sandbox-input=<hash>, so a pulled and a locally built
# image differ only in when their layers were built.
#
# Environment (all optional):
#   SANDBOX_IMAGE_REGISTRY            default ghcr.io/shubhodeep1/coding-workflows-sandbox;
#                                     `off` skips the pull and always builds.
#   SANDBOX_IMAGE_PULL_TIMEOUT_SECS   default 60.
#
# Callers run this helper from the same trusted support copy as themselves,
# under the same `env -i` / `env -u` wrapper they used for `docker build`;
# the helper adds no credential. The pull is anonymous: the package is
# public. A missing helper means the caller builds locally, as before.
#
# Stable log prefix (stderr): SANDBOX_IMAGE.

set -euo pipefail

sandbox_image_default_registry="ghcr.io/shubhodeep1/coding-workflows-sandbox"
sandbox_image_label="coding-workflows.sandbox-input"

sandbox_image_action="${1:-}"
[ "$#" -gt 0 ] && shift
case "${sandbox_image_action}" in
	build|publish|ref) ;;
	*) echo "usage: sandbox_image.sh build|publish|ref --family NAME [--force] [--build-arg K=V]... [-f DOCKERFILE] CONTEXT" >&2; exit 2 ;;
esac

sandbox_image_family=""
sandbox_image_force="false"
sandbox_image_dockerfile=""
sandbox_image_context=""
sandbox_image_build_args=()
while [ "$#" -gt 0 ]; do
	case "$1" in
		--family) sandbox_image_family="${2:-}"; shift 2 ;;
		--force) sandbox_image_force="true"; shift ;;
		--build-arg) sandbox_image_build_args+=("${2:-}"); shift 2 ;;
		-f) sandbox_image_dockerfile="${2:-}"; shift 2 ;;
		-*) echo "::error::SANDBOX_IMAGE unknown argument: $1" >&2; exit 2 ;;
		*)
			[ -z "${sandbox_image_context}" ] || { echo "::error::SANDBOX_IMAGE more than one build context" >&2; exit 2; }
			sandbox_image_context="$1"; shift ;;
	esac
done

[[ "${sandbox_image_family}" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "::error::SANDBOX_IMAGE invalid --family" >&2; exit 2; }
[ -n "${sandbox_image_context}" ] && [ -d "${sandbox_image_context}" ] || { echo "::error::SANDBOX_IMAGE build context missing" >&2; exit 2; }
sandbox_image_context="$(cd "${sandbox_image_context}" && pwd -P)"
[ -n "${sandbox_image_dockerfile}" ] || sandbox_image_dockerfile="${sandbox_image_context}/Dockerfile"
[ -f "${sandbox_image_dockerfile}" ] || { echo "::error::SANDBOX_IMAGE Dockerfile missing" >&2; exit 2; }
sandbox_image_dockerfile="$(cd "$(dirname "${sandbox_image_dockerfile}")" && pwd -P)/$(basename "${sandbox_image_dockerfile}")"
case "${sandbox_image_dockerfile}" in
	"${sandbox_image_context}"/*) ;;
	*) echo "::error::SANDBOX_IMAGE Dockerfile must be inside the build context" >&2; exit 2 ;;
esac
for sandbox_image_arg in "${sandbox_image_build_args[@]}"; do
	[[ "${sandbox_image_arg}" =~ ^[A-Z][A-Z0-9_]*=[A-Za-z0-9._@+-]*$ ]] || { echo "::error::SANDBOX_IMAGE invalid --build-arg" >&2; exit 2; }
done

# Input hash: schema version, Dockerfile path, every context file, sorted args.
sandbox_image_hash="$(
	{
		printf 'sandbox-image-v1\n'
		printf 'dockerfile=%s\n' "${sandbox_image_dockerfile#"${sandbox_image_context}"/}"
		(cd "${sandbox_image_context}" && find . -type f -print | LC_ALL=C sort | while IFS= read -r sandbox_image_file; do
			printf 'file=%s sha256=%s\n' "${sandbox_image_file#./}" "$(sha256sum < "${sandbox_image_file}" | cut -d' ' -f1)"
		done)
		if [ "${#sandbox_image_build_args[@]}" -gt 0 ]; then
			printf 'arg=%s\n' "${sandbox_image_build_args[@]}" | LC_ALL=C sort
		fi
	} | sha256sum | cut -d' ' -f1
)"

sandbox_image_registry="${SANDBOX_IMAGE_REGISTRY:-${sandbox_image_default_registry}}"
if [ "${sandbox_image_registry}" != off ] && ! [[ "${sandbox_image_registry}" =~ ^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+$ ]]; then
	echo "::warning::SANDBOX_IMAGE invalid SANDBOX_IMAGE_REGISTRY; building locally" >&2
	sandbox_image_registry="off"
fi
sandbox_image_ref="${sandbox_image_registry}:${sandbox_image_family}-${sandbox_image_hash:0:32}"

sandbox_image_local_build()
{
	local cmd_args=(build -q --label "${sandbox_image_label}=${sandbox_image_hash}")
	local arg
	for arg in "${sandbox_image_build_args[@]}"; do
		cmd_args+=(--build-arg "${arg}")
	done
	cmd_args+=(-f "${sandbox_image_dockerfile}" "${sandbox_image_context}")
	docker "${cmd_args[@]}"
}

case "${sandbox_image_action}" in
	ref)
		[ "${sandbox_image_registry}" != off ] || { echo "::error::SANDBOX_IMAGE registry is off" >&2; exit 1; }
		printf '%s\n' "${sandbox_image_ref}"
		exit 0
		;;
	publish)
		[ "${sandbox_image_registry}" != off ] || { echo "::error::SANDBOX_IMAGE registry is off" >&2; exit 1; }
		if [ "${sandbox_image_force}" != true ] && docker manifest inspect "${sandbox_image_ref}" >/dev/null 2>&1; then
			echo "SANDBOX_IMAGE family=${sandbox_image_family} outcome=publish_skipped reason=tag_exists ref=${sandbox_image_ref}" >&2
			exit 0
		fi
		sandbox_image_id="$(sandbox_image_local_build)"
		[[ "${sandbox_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "::error::SANDBOX_IMAGE family=${sandbox_image_family} outcome=publish_failed reason=build_failed" >&2; exit 1; }
		docker tag "${sandbox_image_id}" "${sandbox_image_ref}"
		docker push -q "${sandbox_image_ref}" >&2
		echo "SANDBOX_IMAGE family=${sandbox_image_family} outcome=published ref=${sandbox_image_ref} image=${sandbox_image_id}" >&2
		exit 0
		;;
esac

# build: pull first, fall back to the local build.
sandbox_image_reason="disabled"
if [ "${sandbox_image_registry}" != off ]; then
	sandbox_image_timeout="${SANDBOX_IMAGE_PULL_TIMEOUT_SECS:-60}"
	[[ "${sandbox_image_timeout}" =~ ^[1-9][0-9]{0,3}$ ]] || sandbox_image_timeout=60
	sandbox_image_reason="pull_failed"
	if timeout --signal=TERM --kill-after=10s "${sandbox_image_timeout}s" docker pull -q "${sandbox_image_ref}" >/dev/null 2>&1; then
		sandbox_image_reason="label_mismatch"
		sandbox_image_id="$(docker image inspect --format '{{.Id}}' "${sandbox_image_ref}" 2>/dev/null || true)"
		sandbox_image_got="$(docker image inspect --format "{{index .Config.Labels \"${sandbox_image_label}\"}}" "${sandbox_image_ref}" 2>/dev/null || true)"
		if [[ "${sandbox_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] && [ "${sandbox_image_got}" = "${sandbox_image_hash}" ]; then
			echo "SANDBOX_IMAGE family=${sandbox_image_family} outcome=pulled ref=${sandbox_image_ref}" >&2
			printf '%s\n' "${sandbox_image_id}"
			exit 0
		fi
	fi
fi
echo "SANDBOX_IMAGE family=${sandbox_image_family} outcome=built reason=${sandbox_image_reason} ref=${sandbox_image_ref}" >&2
sandbox_image_local_build
