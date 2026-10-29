#!/usr/bin/env bash
# sandbox_images_publish.sh — prebuild and push every sandbox image the
# build sites resolve through scripts/sandbox_image.sh.
#
# Run from the repository root by .github/workflows/publish-sandbox-images.yml
# after `docker login ghcr.io`. Each line below mirrors one build site's
# inputs; a site whose inputs differ (a consumer overriding CODEX_VERSION,
# OPENCODE_VERSION or the Claude CLI pin) finds no tag and builds locally.
# tests/test_sandbox_image.py checks these defaults against the build sites.
#
# Usage: sandbox_images_publish.sh [--force]
#   --force rebuilds and re-pushes existing tags (the weekly refresh that
#   picks up Debian security updates under an unchanged input hash).
#
# Environment: CODEX_VERSION (default v0.114.0), OPENCODE_VERSION (default
# 1.18.23), SANDBOX_IMAGE_REGISTRY (see scripts/sandbox_image.sh).
#
# Exit status: 1 when any image failed to publish; the others still run.

set -euo pipefail

publish_scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
publish_force=()
case "${1:-}" in
	--force) publish_force=(--force) ;;
	"") ;;
	*) echo "usage: sandbox_images_publish.sh [--force]" >&2; exit 2 ;;
esac

publish_codex_version="${CODEX_VERSION:-v0.114.0}"
publish_opencode_version="${OPENCODE_VERSION:-1.18.23}"
publish_claude_cli_version="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${publish_scripts_dir}/claude_engine.py" config --key cli_version)"
publish_failed=0

publish_one()
{
	if ! bash "${publish_scripts_dir}/sandbox_image.sh" publish "${publish_force[@]}" "$@"; then
		echo "::error::SANDBOX_IMAGE publish failed: $*" >&2
		publish_failed=1
	fi
}

# scripts/clarify_isolated_run.sh: codex path, then the Claude branch.
publish_one --family clarify --build-arg "CODEX_VERSION=${publish_codex_version}" "${publish_scripts_dir}/clarify_sandbox"
publish_one --family clarify --build-arg "CODEX_VERSION=${publish_codex_version}" --build-arg "CLAUDE_CLI_VERSION=${publish_claude_cli_version}" "${publish_scripts_dir}/clarify_sandbox"
# scripts/heal_isolated_implement.sh: the Dockerfile's own defaults.
publish_one --family clarify "${publish_scripts_dir}/clarify_sandbox"
# scripts/review_untrusted_sandbox.sh: OpenCode, then `prepare claude`.
publish_one --family review --build-arg "OPENCODE_VERSION=${publish_opencode_version}" "${publish_scripts_dir}/review_sandbox"
publish_one --family review --build-arg "OPENCODE_VERSION=${publish_opencode_version}" --build-arg "CLAUDE_CLI_VERSION=${publish_claude_cli_version}" "${publish_scripts_dir}/review_sandbox"
# scripts/codex_isolated_exec.sh: the generated context, per engine.
for publish_engine in codex claude; do
	publish_context="$(mktemp -d)"
	bash "${publish_scripts_dir}/codex_isolated_exec.sh" image-context --engine "${publish_engine}" --out "${publish_context}"
	publish_args=(--build-arg "CODEX_VERSION=${publish_codex_version}")
	[ "${publish_engine}" = codex ] || publish_args+=(--build-arg "CLAUDE_CLI_VERSION=${publish_claude_cli_version}")
	publish_one --family "codex-isolated-${publish_engine}" "${publish_args[@]}" "${publish_context}"
	rm -rf -- "${publish_context}"
done

exit "${publish_failed}"
