#!/usr/bin/env bash
# release_manifest_publish.sh — build and publish the release manifest for a
# stable release (safeguarded consumer updater, phase 1, issue #6943).
#
# Called from the `release` job of .github/workflows/mark-stable.yml and
# .github/workflows/test-and-mark-stable.yml, after the version tag is pushed
# and before consumers are notified:
#
#   build  --tag <vX.Y.Z> --release-sha <40-hex> --output-dir <dir> [--repo-root <dir>]
#     Checks that the tag peels to exactly <release-sha>, checks that commit out
#     into a detached worktree outside the workspace, runs that tree's own
#     scripts/release_manifest.py build, and verifies schema, release_sha and
#     tag in the result. Writes <dir>/${RELEASE_MANIFEST_ASSET_NAME} and, when
#     GITHUB_OUTPUT is set, the step outputs manifest_path and manifest_sha256.
#     The workflow then attests that file with actions/attest-build-provenance.
#
#   upload --tag <vX.Y.Z> --manifest <path> [--repo <owner/repo>]
#     Uploads the manifest to the GitHub Release (--clobber, so a re-run of a
#     partially failed release is safe: the manifest is deterministic for a
#     fixed tag) and checks that an asset with that name and size landed.
#
# Every failure prints one
#   ::error::RELEASE_MANIFEST outcome=failed reason=<token> [tag=<tag>]
# line (fixed tokens only, never paths or credentials) and exits 1, so the
# release job stops before "Notify consumer repos via repository_dispatch".
#
# Assumption: the authoritative plan
# (docs/plans/safeguarded-consumer-updater-plan.md on branch
# claude/elegant-mendel-5pli2i-safeguarded-updater-plan) could not be read
# from the planning or implementation sandbox, so this helper uses the
# provisional names and behaviour from issue #6943: log prefix
# RELEASE_MANIFEST, asset name release-manifest.v1.json, and a fail-closed
# release before consumer dispatch. If the plan's Decisions (D1-D6) name
# different identifiers or a different failure policy, the plan wins and a
# follow-up should align this helper.
#
# Env (all optional, defaults set here per §8):
#   RELEASE_MANIFEST_ASSET_NAME  asset/file name (default release-manifest.v1.json)
#   RUNNER_TEMP / TMPDIR         parent of the temporary worktree
#   GITHUB_REPOSITORY            default for upload --repo
#   GITHUB_OUTPUT                step-output file (build only)

set -euo pipefail

RELEASE_MANIFEST_ASSET_NAME="${RELEASE_MANIFEST_ASSET_NAME:-release-manifest.v1.json}"
RELEASE_MANIFEST_SCHEMA_VERSION="release_manifest.v1"

_rmp_tag_for_log=""
_rmp_repo_root=""
_rmp_tmp=""

fail()
{
	local reason="$1"
	if [ -n "${_rmp_tag_for_log}" ]; then
		echo "::error::RELEASE_MANIFEST outcome=failed reason=${reason} tag=${_rmp_tag_for_log}" >&2
	else
		echo "::error::RELEASE_MANIFEST outcome=failed reason=${reason}" >&2
	fi
	exit 1
}

_rmp_cleanup()
{
	if [ -n "${_rmp_tmp}" ] && [ -d "${_rmp_tmp}" ]; then
		if [ -n "${_rmp_repo_root}" ] && [ -d "${_rmp_tmp}/tree" ]; then
			git -C "${_rmp_repo_root}" worktree remove --force "${_rmp_tmp}/tree" >/dev/null 2>&1 || true
		fi
		rm -rf "${_rmp_tmp}"
		if [ -n "${_rmp_repo_root}" ]; then
			git -C "${_rmp_repo_root}" worktree prune >/dev/null 2>&1 || true
		fi
	fi
}

_rmp_validate_asset_name()
{
	case "${RELEASE_MANIFEST_ASSET_NAME}" in
		''|*/*|.*) fail invalid_asset_name ;;
		*.json) ;;
		*) fail invalid_asset_name ;;
	esac
}

_rmp_validate_tag()
{
	local tag="$1"
	if ! [[ "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		fail invalid_tag
	fi
	_rmp_tag_for_log="${tag}"
}

_rmp_sha256()
{
	sha256sum "$1" | awk '{print $1}'
}

cmd_build()
{
	local tag="" sha="" output_dir="" repo_root="${PWD}"
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--tag) [ "$#" -ge 2 ] || fail usage; tag="$2"; shift 2 ;;
			--release-sha) [ "$#" -ge 2 ] || fail usage; sha="$2"; shift 2 ;;
			--output-dir) [ "$#" -ge 2 ] || fail usage; output_dir="$2"; shift 2 ;;
			--repo-root) [ "$#" -ge 2 ] || fail usage; repo_root="$2"; shift 2 ;;
			*) fail usage ;;
		esac
	done

	_rmp_validate_tag "${tag}"
	sha="${sha,,}"
	if ! [[ "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
		fail invalid_release_sha
	fi
	if [ -z "${output_dir}" ]; then
		fail invalid_output_dir
	fi
	_rmp_validate_asset_name
	if [ -z "${repo_root}" ] || [ ! -d "${repo_root}" ]; then
		fail invalid_repo_root
	fi

	local peeled=""
	if ! peeled="$(git -C "${repo_root}" rev-parse --verify --quiet "refs/tags/${tag}^{commit}" 2>/dev/null)"; then
		fail tag_unresolved
	fi
	peeled="${peeled,,}"
	if [ -z "${peeled}" ]; then
		fail tag_unresolved
	fi
	if [ "${peeled}" != "${sha}" ]; then
		fail tag_sha_mismatch
	fi

	_rmp_repo_root="${repo_root}"
	local tmp_parent="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
	if ! _rmp_tmp="$(mktemp -d "${tmp_parent%/}/release-manifest-tree.XXXXXX")"; then
		_rmp_tmp=""
		fail tempdir_failed
	fi
	trap _rmp_cleanup EXIT

	if ! git -C "${repo_root}" worktree add --detach "${_rmp_tmp}/tree" "${sha}" >/dev/null 2>&1; then
		fail checkout_failed
	fi
	local checked_out=""
	checked_out="$(git -C "${_rmp_tmp}/tree" rev-parse HEAD 2>/dev/null || true)"
	if [ "${checked_out,,}" != "${sha}" ]; then
		fail checkout_mismatch
	fi

	local builder="${_rmp_tmp}/tree/scripts/release_manifest.py"
	if [ ! -f "${builder}" ] || [ -L "${builder}" ]; then
		fail builder_missing
	fi

	if ! mkdir -p "${output_dir}"; then
		fail invalid_output_dir
	fi
	local manifest="${output_dir%/}/${RELEASE_MANIFEST_ASSET_NAME}"
	rm -f "${manifest}"

	# The builder prints its own `RELEASE_MANIFEST error reason=<token>` line on
	# refusal; it passes through on stderr.
	if ! PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${builder}" build \
		--repo-root "${_rmp_tmp}/tree" \
		--release-sha "${sha}" \
		--tag "${tag}" \
		--output "${manifest}"; then
		rm -f "${manifest}"
		fail builder_failed
	fi

	local verdict=""
	verdict="$(RMP_MANIFEST="${manifest}" RMP_SHA="${sha}" RMP_TAG="${tag}" RMP_SCHEMA="${RELEASE_MANIFEST_SCHEMA_VERSION}" \
		PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c '
import json, os
try:
	with open(os.environ["RMP_MANIFEST"], encoding="utf-8") as handle:
		data = json.load(handle)
except (OSError, ValueError):
	print("unreadable_manifest")
	raise SystemExit(0)
if not isinstance(data, dict) or data.get("schema_version") != os.environ["RMP_SCHEMA"]:
	print("schema_mismatch")
elif data.get("release_sha") != os.environ["RMP_SHA"]:
	print("release_sha_mismatch")
elif data.get("tag") != os.environ["RMP_TAG"]:
	print("tag_mismatch")
elif not isinstance(data.get("files"), list) or not data["files"]:
	print("empty_manifest")
else:
	print("ok %d" % len(data["files"]))
' 2>/dev/null || echo "verify_failed")"
	case "${verdict}" in
		ok\ *) ;;
		*)
			rm -f "${manifest}"
			case "${verdict}" in
				unreadable_manifest|schema_mismatch|release_sha_mismatch|tag_mismatch|empty_manifest) fail "${verdict}" ;;
				*) fail verify_failed ;;
			esac
			;;
	esac
	local files_count="${verdict#ok }"
	local digest
	digest="$(_rmp_sha256 "${manifest}")"

	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		{
			printf 'manifest_path=%s\n' "${manifest}"
			printf 'manifest_sha256=%s\n' "${digest}"
		} >> "${GITHUB_OUTPUT}"
	fi
	echo "RELEASE_MANIFEST outcome=built tag=${tag} sha=${sha} files=${files_count} sha256=${digest}"
}

cmd_upload()
{
	local tag="" manifest="" repo="${GITHUB_REPOSITORY:-}"
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--tag) [ "$#" -ge 2 ] || fail usage; tag="$2"; shift 2 ;;
			--manifest) [ "$#" -ge 2 ] || fail usage; manifest="$2"; shift 2 ;;
			--repo) [ "$#" -ge 2 ] || fail usage; repo="$2"; shift 2 ;;
			*) fail usage ;;
		esac
	done

	_rmp_validate_tag "${tag}"
	_rmp_validate_asset_name
	if [ -z "${repo}" ]; then
		fail missing_repository
	fi
	if ! [[ "${repo}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
		fail invalid_repository
	fi
	if [ -z "${manifest}" ] || [ -L "${manifest}" ] || [ ! -f "${manifest}" ]; then
		fail manifest_missing
	fi
	if [ "$(basename "${manifest}")" != "${RELEASE_MANIFEST_ASSET_NAME}" ]; then
		fail manifest_name_mismatch
	fi
	local local_size
	local_size="$(wc -c < "${manifest}" | tr -d '[:space:]')"

	# shellcheck disable=SC1091
	source "$(dirname "$0")/gh_helpers.sh" 2>/dev/null || true
	type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

	if ! gh_retry gh release upload "${tag}" "${manifest}" --clobber --repo "${repo}" >/dev/null; then
		fail upload_failed
	fi

	# GitHub API audit (§14): no existing call in the release job returns
	# per-asset state; the "Create GitHub Release" lookup reads only the
	# release's tag_name. One view call confirms the uploaded asset.
	local view_json=""
	if ! view_json="$(gh_retry gh release view "${tag}" --repo "${repo}" --json assets)"; then
		fail asset_lookup_failed
	fi
	local remote_size=""
	remote_size="$(RMP_ASSET_NAME="${RELEASE_MANIFEST_ASSET_NAME}" PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c '
import json, os, sys
try:
	data = json.loads(sys.stdin.read())
except ValueError:
	print("invalid")
	raise SystemExit(0)
assets = data.get("assets") if isinstance(data, dict) else None
if not isinstance(assets, list):
	print("invalid")
	raise SystemExit(0)
sizes = [a.get("size") for a in assets if isinstance(a, dict) and a.get("name") == os.environ["RMP_ASSET_NAME"]]
if not sizes:
	print("missing")
elif len(sizes) != 1 or not isinstance(sizes[0], int) or isinstance(sizes[0], bool):
	print("invalid")
else:
	print(sizes[0])
' <<<"${view_json}" 2>/dev/null || echo "invalid")"
	case "${remote_size}" in
		missing) fail asset_missing ;;
		''|*[!0-9]*) fail asset_lookup_failed ;;
	esac
	if [ "${remote_size}" != "${local_size}" ]; then
		fail asset_size_mismatch
	fi
	echo "RELEASE_MANIFEST outcome=uploaded tag=${tag} asset=${RELEASE_MANIFEST_ASSET_NAME} size=${remote_size}"
}

main()
{
	if [ "$#" -lt 1 ]; then
		fail usage
	fi
	local command="$1"
	shift
	case "${command}" in
		build) cmd_build "$@" ;;
		upload) cmd_upload "$@" ;;
		*) fail usage ;;
	esac
}

main "$@"
