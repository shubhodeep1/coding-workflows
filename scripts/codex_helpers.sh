#!/usr/bin/env bash

if [ "${_CODEX_HELPERS_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_CODEX_HELPERS_LOADED="true"

_codex_helpers_resolve_scripts_dir()
{
	local configured_dir="${1:-${CODEX_HELPERS_SCRIPTS_DIR:-}}"
	local workspace_root=""

	if [ -z "${configured_dir}" ]; then
		(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
		return 0
	fi

	case "${configured_dir}" in
		/*)
			printf '%s\n' "${configured_dir}"
			;;
		*)
			workspace_root="${GITHUB_WORKSPACE:-$(pwd)}"
			printf '%s\n' "${workspace_root}/${configured_dir}"
			;;
	esac
}

_codex_helpers_resolve_writer_path()
{
	local scripts_dir="${1:-}"
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${scripts_dir}")"
	printf '%s\n' "${scripts_dir}/write_codex_config.sh"
}

_codex_helpers_resolve_catalog_path()
{
	local scripts_dir="${1:-}"
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${scripts_dir}")"
	printf '%s\n' "${scripts_dir}/codex_model_catalog.json"
}

codex_config_assemble()
{
	local model="${1:-}"
	local reasoning="${2:-}"
	local verbosity="${3:-low}"
	local scripts_dir_override=""
	local writer_path=""
	local catalog_path=""
	local web_search="live"
	local project_path=""
	local config_path=""
	local allow_elevation=""
	local writer_args=()

	if [ $# -ge 3 ]; then
		shift 3
	else
		shift "$#"
	fi

	while [ $# -gt 0 ]; do
		case "$1" in
			--scripts-dir|--web-search|--catalog-path|--project-path|--config-path|--allow-elevation)
				if [ $# -lt 2 ]; then
					echo "::error::codex_config_assemble: option $1 requires an argument" >&2
					return 2
				fi
				;;
		esac
		case "$1" in
			--scripts-dir)
				scripts_dir_override="${2:-}"
				shift 2
				;;
			--web-search)
				web_search="${2:-}"
				shift 2
				;;
			--catalog-path)
				catalog_path="${2:-}"
				shift 2
				;;
			--project-path)
				project_path="${2:-}"
				shift 2
				;;
			--config-path)
				config_path="${2:-}"
				shift 2
				;;
			--allow-elevation)
				allow_elevation="${2:-}"
				shift 2
				;;
			*)
				echo "::error::codex_config_assemble: unknown argument: $1" >&2
				return 2
				;;
		esac
	done

	writer_path="$(_codex_helpers_resolve_writer_path "${scripts_dir_override}")"
	if [ -z "${catalog_path}" ]; then
		catalog_path="$(_codex_helpers_resolve_catalog_path "${scripts_dir_override}")"
	fi
	if [ ! -r "${writer_path}" ]; then
		echo "::error::codex_config_assemble: missing writer helper ${writer_path}" >&2
		return 1
	fi

	CODEX_HOME="${HOME:-/root}/.codex"
	export CODEX_HOME
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf 'CODEX_HOME=%s\n' "${CODEX_HOME}" >> "${GITHUB_ENV}"
	fi

	# Keep the third positional parameter for the approved call-site
	# interface; the shared writer already emits model_verbosity.
	: "${verbosity}"

	writer_args=(
		--model "${model}"
		--reasoning "${reasoning}"
		--web-search "${web_search}"
		--catalog-path "${catalog_path}"
	)
	if [ -n "${project_path}" ]; then
		writer_args+=(--project-path "${project_path}")
	fi
	if [ -n "${config_path}" ]; then
		writer_args+=(--config-path "${config_path}")
	fi
	if [ -n "${allow_elevation}" ]; then
		writer_args+=(--allow-elevation "${allow_elevation}")
	fi

	bash "${writer_path}" "${writer_args[@]}"
}
