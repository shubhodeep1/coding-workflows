#!/usr/bin/env bash

SEMBLE_VERSION="${SEMBLE_VERSION:-0.1.3}"
SEMBLE_SPEC="semble==${SEMBLE_VERSION}"

_semble_bool_true()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

_semble_append_env()
{
	local name="$1"
	local value="$2"
	export "${name}=${value}"
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf '%s=%s\n' "${name}" "${value}" >> "${GITHUB_ENV}"
	fi
}

_semble_append_path()
{
	local dir="$1"
	[ -n "${dir}" ] || return 0
	[ -d "${dir}" ] || return 0
	case ":${PATH}:" in
		*":${dir}:"*)
			;;
		*)
			export PATH="${dir}:${PATH}"
			;;
	esac
	if [ -n "${GITHUB_PATH:-}" ]; then
		printf '%s\n' "${dir}" >> "${GITHUB_PATH}"
	fi
}

_semble_log()
{
	printf '%s\n' "$*" >&2
}

_find_uv_bin()
{
	if [ -n "${UV_BIN:-}" ] && [ -x "${UV_BIN}" ]; then
		printf '%s\n' "${UV_BIN}"
		return 0
	fi
	if command -v uv >/dev/null 2>&1; then
		command -v uv
		return 0
	fi
	if [ -x "${HOME}/.local/bin/uv" ]; then
		printf '%s\n' "${HOME}/.local/bin/uv"
		return 0
	fi
	return 1
}

_uv_tool_bin_dir()
{
	local uv_bin="$1"
	"${uv_bin}" tool dir --bin 2>/dev/null || return 1
}

_has_matching_semble_tool()
{
	local uv_bin="$1"
	local tool_list bin_dir
	tool_list="$("${uv_bin}" tool list 2>/dev/null || true)"
	printf '%s\n' "${tool_list}" | grep -Eq "^semble v${SEMBLE_VERSION}$" || return 1
	bin_dir="$(_uv_tool_bin_dir "${uv_bin}")" || return 1
	_semble_append_path "${bin_dir}"
	[ -x "${bin_dir}/semble" ] || command -v semble >/dev/null 2>&1 || return 1
	return 0
}

main()
{
	local uv_bin install_log bin_dir

	_semble_append_env SEMBLE_INDEX_AVAILABLE false

	if ! _semble_bool_true "${SEMBLE_ENABLED:-false}"; then
		_semble_append_env SEMBLE_AVAILABLE false
		_semble_log "SEMBLE_INSTALL target=install status=disabled enabled=false"
		exit 0
	fi

	if ! uv_bin="$(_find_uv_bin)"; then
		_semble_append_env SEMBLE_AVAILABLE false
		_semble_log "SEMBLE_FALLBACK target=install reason=uv_unavailable"
		exit 0
	fi

	if _has_matching_semble_tool "${uv_bin}"; then
		_semble_append_env SEMBLE_AVAILABLE true
		_semble_log "SEMBLE_INSTALL target=install status=already_installed version=${SEMBLE_VERSION}"
		exit 0
	fi

	install_log="$("${uv_bin}" tool install --force "${SEMBLE_SPEC}" 2>&1)"
	if [ $? -ne 0 ]; then
		_semble_append_env SEMBLE_AVAILABLE false
		install_log="$(printf '%s' "${install_log}" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
		_semble_log "SEMBLE_FALLBACK target=install reason=install_failed detail=${install_log}"
		exit 0
	fi

	bin_dir="$(_uv_tool_bin_dir "${uv_bin}")" || bin_dir=""
	_semble_append_path "${bin_dir}"
	if [ -n "${bin_dir}" ] && [ -x "${bin_dir}/semble" ]; then
		_semble_append_env SEMBLE_AVAILABLE true
		_semble_log "SEMBLE_INSTALL target=install status=installed version=${SEMBLE_VERSION}"
		exit 0
	fi

	if command -v semble >/dev/null 2>&1; then
		_semble_append_env SEMBLE_AVAILABLE true
		_semble_log "SEMBLE_INSTALL target=install status=installed version=${SEMBLE_VERSION}"
		exit 0
	fi

	_semble_append_env SEMBLE_AVAILABLE false
	_semble_log "SEMBLE_FALLBACK target=install reason=missing_binary_after_install"
	exit 0
}

main "$@"
