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

_semble_query_block_internal()
{
	if [ "$#" -lt 4 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=query reason=invalid_arguments"
		return 1
	fi

	local log_target="$1"
	local query_text="$2"
	local max_chunks="$3"
	local header_label="$4"
	shift 4

	local target index_dir repo_root_file repo_root semble_bin stdout_file stderr_file output_body stderr_body bytes start_ts end_ts elapsed_ms query_rc
	target="$(_semble_sanitize_one_line "${log_target}")"
	if [ -z "${target}" ]; then
		target="$(_semble_sanitize_one_line "${header_label}")"
	fi
	index_dir="${SEMBLE_INDEX_DIR:-${RUNTIME_DIR:-}/.semble-index}"
	case "${index_dir}" in
		""|"/"|"/.semble-index")
			_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=index_dir_unset"
			return 1
			;;
	esac
	repo_root_file="${index_dir}/repo_root"

	if ! _semble_bool_true "${SEMBLE_INDEX_AVAILABLE:-false}"; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=index_unavailable"
		return 1
	fi

	if [ ! -d "${index_dir}" ] || [ ! -f "${repo_root_file}" ]; then
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
	if timeout 5s "${semble_bin}" query "${query_text}" --index "${index_dir}" --top-k "${max_chunks}" --format text "$@" >"${stdout_file}" 2>"${stderr_file}"; then
		:
	else
		query_rc=$?
		stderr_body="$(_semble_sanitize_one_line "$(cat "${stderr_file}" 2>/dev/null || true)")"
		rm -f "${stdout_file}" "${stderr_file}"
		if [ "${query_rc}" -eq 124 ]; then
			_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=query_timeout"
		else
			_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=query_failed detail=${stderr_body}"
		fi
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

	_semble_query_block_internal "${header_label}" "${query_text}" "${max_chunks}" "${header_label}" "$@"
}

semble_query_block_with_target()
{
	if [ "$#" -lt 4 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=query reason=invalid_arguments"
		return 1
	fi

	local log_target="$1"
	local query_text="$2"
	local max_chunks="$3"
	local header_label="$4"
	shift 4

	_semble_query_block_internal "${log_target}" "${query_text}" "${max_chunks}" "${header_label}" "$@"
}

semble_collect_query_text()
{
	local max_chars="${1:-2000}"
	shift || true

	if ! printf '%s' "${max_chars}" | grep -Eq '^[0-9]+$' || [ "${max_chars}" -le 0 ]; then
		max_chars=2000
	fi

	local collected fragment sanitized
	collected=""
	for fragment in "$@"; do
		sanitized="$(_semble_sanitize_one_line "${fragment:-}")"
		[ -n "${sanitized}" ] || continue
		if [ -n "${collected}" ]; then
			collected="${collected} | ${sanitized}"
		else
			collected="${sanitized}"
		fi
		sanitized="$(printf '%s' "${collected}" | head -c "${max_chars}")"
		collected="${sanitized}"
		[ "${#collected}" -lt "${max_chars}" ] || break
	done

	printf '%s' "${collected}"
}

semble_prompt_block_from_text()
{
	if [ "$#" -lt 4 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=prompt_block reason=invalid_arguments"
		return 1
	fi

	local header_label="$1"
	local max_chunks="$2"
	local max_query_chars="$3"
	shift 3

	local query_text target
	if ! printf '%s' "${max_chunks}" | grep -Eq '^[0-9]+$' || [ "${max_chunks}" -le 0 ]; then
		max_chunks=4
	fi
	target="$(_semble_sanitize_one_line "${header_label}")"
	query_text="$(semble_collect_query_text "${max_query_chars}" "$@")"
	if [ -z "${query_text}" ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=query_text_empty"
		return 1
	fi

	semble_query_block "${query_text}" "${max_chunks}" "${header_label}"
}

semble_prompt_block_from_files()
{
	if [ "$#" -lt 4 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=prompt_block reason=invalid_arguments"
		return 1
	fi

	local header_label="$1"
	local max_chunks="$2"
	local max_query_chars="$3"
	shift 3

	local per_file_chars target
	local -a fragments=()
	local file snippet
	target="$(_semble_sanitize_one_line "${header_label}")"
	per_file_chars="${SEMBLE_QUERY_FILE_SNIPPET_CHARS:-1200}"
	if ! printf '%s' "${per_file_chars}" | grep -Eq '^[0-9]+$' || [ "${per_file_chars}" -le 0 ]; then
		per_file_chars=1200
	fi

	for file in "$@"; do
		[ -n "${file:-}" ] || continue
		[ -s "${file}" ] || continue
		snippet="$(head -c "${per_file_chars}" "${file}" 2>/dev/null || true)"
		[ -n "${snippet}" ] || continue
		fragments+=("${file#./}: ${snippet}")
	done

	if [ "${#fragments[@]}" -eq 0 ]; then
		_semble_stderr_log "SEMBLE_FALLBACK target=${target} reason=query_text_empty"
		return 1
	fi

	semble_prompt_block_from_text "${header_label}" "${max_chunks}" "${max_query_chars}" "${fragments[@]}"
}
