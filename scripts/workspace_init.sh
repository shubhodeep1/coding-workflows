#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat <<'USAGE' >&2
Usage: workspace_init.sh <command>

Commands:
  metadata   Emit workspace metadata to GITHUB_OUTPUT/GITHUB_ENV.
  finalize   Materialize the current checkout into the workspace path.
USAGE
}

fail()
{
	printf 'workspace_init: %s\n' "$*" >&2
	exit 1
}

truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
	esac
	return 1
}

sanitize_key()
{
	local raw="${1:-}"
	local sanitized
	sanitized="$(printf '%s' "${raw}" | tr -c 'A-Za-z0-9._-' '_')"
	if [ -z "${sanitized}" ]; then
		sanitized="workspace"
	fi
	printf '%s\n' "${sanitized}"
}

ensure_output_file()
{
	local output_path="${1:-}"
	if [ -z "${output_path}" ]; then
		fail "required output file path is missing"
	fi
	mkdir -p "$(dirname "${output_path}")"
	touch "${output_path}"
}

append_kv()
{
	local file_path="$1"
	local key="$2"
	local value="$3"
	printf '%s=%s\n' "${key}" "${value}" >> "${file_path}"
}

WORKSPACE_IDENTIFIER_SOURCE="default"
WORKSPACE_IDENTIFIER_VALUE=""
resolve_issue_identifier()
{
	local raw_identifier="${WORKSPACE_ISSUE_IDENTIFIER:-}"
	local fallback_identifier="${WORKSPACE_FALLBACK_IDENTIFIER:-}"
	local default_identifier="${WORKSPACE_DEFAULT_IDENTIFIER:-run-${GITHUB_RUN_ID:-unknown}}"

	WORKSPACE_IDENTIFIER_SOURCE="default"
	WORKSPACE_IDENTIFIER_VALUE="${default_identifier}"
	if [ -n "${raw_identifier}" ]; then
		WORKSPACE_IDENTIFIER_SOURCE="explicit"
		WORKSPACE_IDENTIFIER_VALUE="${raw_identifier}"
		return 0
	fi
	if [ -n "${fallback_identifier}" ]; then
		WORKSPACE_IDENTIFIER_SOURCE="fallback"
		WORKSPACE_IDENTIFIER_VALUE="${fallback_identifier}"
		return 0
	fi
	return 0
}

resolve_workspace_root()
{
	local runner_temp="${RUNNER_TEMP:-}"
	if [ -z "${runner_temp}" ]; then
		fail "RUNNER_TEMP is required"
	fi
	printf '%s/workspaces\n' "${runner_temp}"
}

resolve_workspace_fingerprint()
{
	local source_path="$1"
	local fingerprint="${WORKSPACE_FINGERPRINT:-}"
	if [ -n "${fingerprint}" ]; then
		printf '%s\n' "${fingerprint}"
		return 0
	fi

	local fingerprint_file="${WORKSPACE_FINGERPRINT_FILE:-}"
	if [ -n "${fingerprint_file}" ] && [ -f "${fingerprint_file}" ]; then
		fingerprint="$(tr -d '\r\n' < "${fingerprint_file}")"
		if [ -n "${fingerprint}" ]; then
			printf '%s\n' "${fingerprint}"
			return 0
		fi
	fi

	if [ -n "${source_path}" ] && [ -d "${source_path}" ]; then
		fingerprint="$(git -C "${source_path}" rev-parse HEAD^{tree} 2>/dev/null || true)"
		if [ -n "${fingerprint}" ]; then
			printf '%s\n' "${fingerprint}"
			return 0
		fi
	fi

	printf 'none\n'
}

assert_workspace_path_under_root()
{
	local workspace_root="$1"
	local workspace_path="$2"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${workspace_root}" "${workspace_path}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=False)
path = Path(sys.argv[2]).resolve(strict=False)
try:
    path.relative_to(root)
except ValueError:
    raise SystemExit(1)
PY
}

resolve_restore_state()
{
	local reuse_enabled="$1"
	local matched_key="$2"
	local exact_prefix="$3"
	local issue_prefix="$4"

	if [ "${reuse_enabled}" != "true" ]; then
		printf 'disabled\n'
		return 0
	fi
	if [ -z "${matched_key}" ]; then
		printf 'miss\n'
		return 0
	fi
	case "${matched_key}" in
		"${exact_prefix}"*)
			printf 'exact\n'
			return 0
			;;
		"${issue_prefix}"*)
			printf 'partial\n'
			return 0
			;;
	esac
	printf 'miss\n'
}

command_metadata()
{
	local github_output="${GITHUB_OUTPUT:-}"
	local github_env="${GITHUB_ENV:-}"
	ensure_output_file "${github_output}"
	ensure_output_file "${github_env}"

	local workspace_reuse_enabled="false"
	if truthy "${WORKSPACE_REUSE_ENABLED:-false}"; then
		workspace_reuse_enabled="true"
	fi

	resolve_issue_identifier
	local issue_identifier="${WORKSPACE_IDENTIFIER_VALUE}"
	if [ "${workspace_reuse_enabled}" = "true" ] && \
	   truthy "${WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE:-false}" && \
	   [ "${WORKSPACE_IDENTIFIER_SOURCE}" != "explicit" ]; then
		workspace_reuse_enabled="false"
	fi

	local workspace_key
	workspace_key="$(sanitize_key "${issue_identifier}")"
	local workspace_root
	workspace_root="$(resolve_workspace_root)"
	local workspace_source_path="${WORKSPACE_SOURCE_PATH:-${GITHUB_WORKSPACE:-}}"
	local workspace_fingerprint
	workspace_fingerprint="$(resolve_workspace_fingerprint "${workspace_source_path}")"
	local workspace_path="${workspace_root}/${workspace_key}"
	local cache_key_prefix="workspace-v1-${workspace_key}-${workspace_fingerprint}-"
	local cache_key="${cache_key_prefix}${GITHUB_RUN_ID:-manual}"
	local cache_restore_prefix_exact="${cache_key_prefix}"
	local cache_restore_prefix_issue="workspace-v1-${workspace_key}-"
	local matched_key="${WORKSPACE_CACHE_MATCHED_KEY:-}"
	local cache_restore_state
	local created_now="true"

	if [ "${workspace_reuse_enabled}" != "true" ]; then
		workspace_path="${workspace_root}/${workspace_key}-${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-1}"
		cache_key=""
		cache_restore_prefix_exact=""
		cache_restore_prefix_issue=""
	fi

	if ! assert_workspace_path_under_root "${workspace_root}" "${workspace_path}"; then
		fail "resolved workspace path escapes ${workspace_root}: ${workspace_path}"
	fi

	cache_restore_state="$(resolve_restore_state \
		"${workspace_reuse_enabled}" \
		"${matched_key}" \
		"${cache_restore_prefix_exact}" \
		"${cache_restore_prefix_issue}")"
	if [ "${cache_restore_state}" = "exact" ]; then
		created_now="false"
	fi

	mkdir -p "${workspace_path}"

	append_kv "${github_output}" "workspace_issue_identifier" "${issue_identifier}"
	append_kv "${github_output}" "workspace_identifier_source" "${WORKSPACE_IDENTIFIER_SOURCE}"
	append_kv "${github_output}" "workspace_key" "${workspace_key}"
	append_kv "${github_output}" "workspace_root" "${workspace_root}"
	append_kv "${github_output}" "workspace_path" "${workspace_path}"
	append_kv "${github_output}" "workspace_source_path" "${workspace_source_path}"
	append_kv "${github_output}" "workspace_fingerprint" "${workspace_fingerprint}"
	append_kv "${github_output}" "workspace_cache_key" "${cache_key}"
	append_kv "${github_output}" "workspace_cache_restore_prefix_exact" "${cache_restore_prefix_exact}"
	append_kv "${github_output}" "workspace_cache_restore_prefix_issue" "${cache_restore_prefix_issue}"
	append_kv "${github_output}" "workspace_cache_matched_key" "${matched_key}"
	append_kv "${github_output}" "workspace_cache_restore_state" "${cache_restore_state}"
	append_kv "${github_output}" "workspace_reuse_enabled" "${workspace_reuse_enabled}"
	append_kv "${github_output}" "created_now" "${created_now}"

	append_kv "${github_env}" "WORKSPACE_ISSUE_IDENTIFIER" "${issue_identifier}"
	append_kv "${github_env}" "WORKSPACE_IDENTIFIER_SOURCE" "${WORKSPACE_IDENTIFIER_SOURCE}"
	append_kv "${github_env}" "WORKSPACE_KEY" "${workspace_key}"
	append_kv "${github_env}" "WORKSPACE_ROOT" "${workspace_root}"
	append_kv "${github_env}" "WORKSPACE_PATH" "${workspace_path}"
	append_kv "${github_env}" "WORKSPACE_SOURCE_PATH" "${workspace_source_path}"
	append_kv "${github_env}" "WORKSPACE_FINGERPRINT" "${workspace_fingerprint}"
	append_kv "${github_env}" "WORKSPACE_CACHE_KEY" "${cache_key}"
	append_kv "${github_env}" "WORKSPACE_CACHE_RESTORE_PREFIX_EXACT" "${cache_restore_prefix_exact}"
	append_kv "${github_env}" "WORKSPACE_CACHE_RESTORE_PREFIX_ISSUE" "${cache_restore_prefix_issue}"
	append_kv "${github_env}" "WORKSPACE_CACHE_MATCHED_KEY" "${matched_key}"
	append_kv "${github_env}" "WORKSPACE_CACHE_RESTORE_STATE" "${cache_restore_state}"
	append_kv "${github_env}" "WORKSPACE_REUSE_ENABLED" "${workspace_reuse_enabled}"
	append_kv "${github_env}" "CREATED_NOW" "${created_now}"
}

clean_stale_workspace_state()
{
	local workspace_path="$1"
	rm -rf \
		"${workspace_path}/.ai/validate-hints-cache" \
		"${workspace_path}/.ai/review_runtime" \
		"${workspace_path}/validation" \
		"${workspace_path}/.serena"
}

materialize_source_tree()
{
	local source_path="$1"
	local workspace_path="$2"
	local manifest_path="${workspace_path}/.ai/.workspace_source_manifest.txt"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${source_path}" "${workspace_path}" "${manifest_path}" <<'PY'
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve(strict=False)
workspace = Path(sys.argv[2]).resolve(strict=False)
manifest_path = Path(sys.argv[3])
excluded_roots = {'.git', '.codex-workflow-src', '.codex-workflow-src-main'}

if source == workspace:
    raise SystemExit("source and workspace paths must differ")


def prune_empty_parents(target: Path) -> None:
    current = target.parent
    while current != workspace and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def iter_source_relpaths() -> list[str]:
    relpaths: list[str] = []
    for root, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(source)
        filtered_dirnames: list[str] = []
        for dirname in dirnames:
            if dirname in excluded_roots:
                continue
            dir_path = root_path / dirname
            rel_path = (rel_root / dirname) if rel_root != Path('.') else Path(dirname)
            if dir_path.is_symlink():
                relpaths.append(rel_path.as_posix())
                continue
            filtered_dirnames.append(dirname)
        dirnames[:] = filtered_dirnames
        for filename in filenames:
            if filename in excluded_roots:
                continue
            rel_path = (rel_root / filename) if rel_root != Path('.') else Path(filename)
            relpaths.append(rel_path.as_posix())
    relpaths.sort()
    return relpaths


def remove_path(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
        prune_empty_parents(target)
        return
    if target.is_dir():
        shutil.rmtree(target)
        prune_empty_parents(target)


current_relpaths = iter_source_relpaths()
current_set = set(current_relpaths)
previous_set: set[str] = set()
if manifest_path.exists():
    previous_set = {
        line.strip()
        for line in manifest_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    }

for relpath in sorted(previous_set - current_set, key=lambda item: (item.count('/'), item), reverse=True):
    remove_path(workspace / relpath)

for relpath in current_relpaths:
    src = source / relpath
    dst = workspace / relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        remove_path(dst)
        os.symlink(os.readlink(src), dst)
        continue
    if dst.is_symlink() or dst.is_dir():
        remove_path(dst)
    shutil.copy2(src, dst)

manifest_path.parent.mkdir(parents=True, exist_ok=True)
manifest_text = "\n".join(current_relpaths)
if manifest_text:
    manifest_text += "\n"
manifest_path.write_text(manifest_text, encoding='utf-8')
PY
}

command_finalize()
{
	local source_path="${WORKSPACE_SOURCE_PATH:-${GITHUB_WORKSPACE:-}}"
	local workspace_path="${WORKSPACE_PATH:-}"
	local workspace_root
	workspace_root="$(resolve_workspace_root)"
	local cache_restore_state="${WORKSPACE_CACHE_RESTORE_STATE:-miss}"
	local workspace_reuse_enabled="${WORKSPACE_REUSE_ENABLED:-false}"

	[ -n "${source_path}" ] || fail "WORKSPACE_SOURCE_PATH or GITHUB_WORKSPACE is required"
	[ -n "${workspace_path}" ] || fail "WORKSPACE_PATH is required"
	[ -d "${source_path}" ] || fail "source path does not exist: ${source_path}"

	if ! assert_workspace_path_under_root "${workspace_root}" "${workspace_path}"; then
		fail "resolved workspace path escapes ${workspace_root}: ${workspace_path}"
	fi

	mkdir -p "${workspace_path}"

	if truthy "${workspace_reuse_enabled}" && [ "${cache_restore_state}" != "exact" ]; then
		clean_stale_workspace_state "${workspace_path}"
	fi

	materialize_source_tree "${source_path}" "${workspace_path}"
}

main()
{
	local command="${1:-}"
	case "${command}" in
		metadata)
			shift
			command_metadata "$@"
			;;
		finalize)
			shift
			command_finalize "$@"
			;;
		-h|--help|help)
			usage
			;;
		*)
			usage
			exit 1
			;;
	esac
}

main "$@"
