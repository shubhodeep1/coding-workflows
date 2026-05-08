#!/usr/bin/env bash

if [ "${_SEMBLE_HELPERS_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || true
fi
_SEMBLE_HELPERS_LOADED=1

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

_semble_sanitize_one_line()
{
	printf '%s' "${1:-}" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//'
}

_semble_stderr_log()
{
	printf '%s\n' "$*" >&2
}

_semble_now_ms()
{
	if command -v python3 >/dev/null 2>&1; then
		python3 - <<'PY' 2>/dev/null || printf '0\n'
import time
print(int(time.time() * 1000))
PY
		return 0
	fi
	date +%s%3N 2>/dev/null || printf '0\n'
}

semble_query_block()
{
	if [ "$#" -lt 3 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=query reason=invalid_arguments"
		return 1
	fi

	local query_text="$1"
	local max_chunks="$2"
	local header_label="$3"
	shift 3

	local target index_dir repo_root_file repo_root semble_bin stdout_file stderr_file output_body stderr_body bytes start_ts end_ts elapsed_ms
	target="$(_semble_sanitize_one_line "${header_label}")"
	index_dir="${SEMBLE_INDEX_DIR:-${RUNTIME_DIR:-}/.semble-index}"
	repo_root_file="${index_dir}/repo_root"

	if ! _semble_bool_true "${SEMBLE_INDEX_AVAILABLE:-false}"; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=index_unavailable"
		return 1
	fi

	if [ -z "${RUNTIME_DIR:-}" ] || [ ! -d "${index_dir}" ] || [ ! -f "${repo_root_file}" ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=index_metadata_missing"
		return 1
	fi

	repo_root="$(cat "${repo_root_file}" 2>/dev/null || true)"
	if [ -z "${repo_root}" ] || [ ! -d "${repo_root}" ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=repo_root_missing"
		return 1
	fi

	semble_bin="${SEMBLE_BIN:-$(command -v semble 2>/dev/null || true)}"
	if [ -z "${semble_bin}" ] || [ ! -x "${semble_bin}" ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=missing_binary"
		return 1
	fi

	stdout_file="$(mktemp)"
	stderr_file="$(mktemp)"
	start_ts="$(_semble_now_ms)"
	if ! "${semble_bin}" search "$@" --top-k "${max_chunks}" "${query_text}" "${repo_root}" >"${stdout_file}" 2>"${stderr_file}"; then
		stderr_body="$(_semble_sanitize_one_line "$(cat "${stderr_file}" 2>/dev/null || true)")"
		rm -f "${stdout_file}" "${stderr_file}"
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=query_failed detail=${stderr_body}"
		return 1
	fi
	end_ts="$(_semble_now_ms)"
	case "${start_ts}:${end_ts}" in
		*[!0-9:]*|:*|*:)
			elapsed_ms=0
			;;
		*)
			elapsed_ms=$(( end_ts - start_ts ))
			;;
	esac
	output_body="$(cat "${stdout_file}" 2>/dev/null || true)"
	rm -f "${stdout_file}" "${stderr_file}"

	if [ -z "${output_body}" ] || [ "${output_body}" = "No results found." ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=no_results"
		return 1
	fi

	bytes="$(printf '%s' "${output_body}" | wc -c | tr -d '[:space:]')"
	_semble_stderr_log "SEMBLE_QUERY target=${target} chunks=${max_chunks} bytes=${bytes} ms=${elapsed_ms}"
	printf '=== SEMBLE: %s ===\n' "${header_label}"
	printf '%s\n' "${output_body}"
	printf '=== END SEMBLE ===\n'
	return 0
}
