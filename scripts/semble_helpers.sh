#!/usr/bin/env bash
# semble_helpers.sh — shared, sourceable Semble query helpers.

_semble_log_event()
{
	local prefix="${1:?_semble_log_event: prefix required}"
	shift || true
	printf '%s' "${prefix}" >&2
	while [ "$#" -gt 0 ]; do
		printf ' %s' "$1" >&2
		shift
	done
	printf '\n' >&2
}

_semble_target_slug()
{
	local raw="${1:-}"
	local slug=""

	slug="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g')"
	if [ -z "${slug}" ]; then
		slug="semble"
	fi
	printf '%s\n' "${slug}"
}

_semble_index_path()
{
	if [ -n "${SEMBLE_INDEX_PATH:-}" ]; then
		printf '%s\n' "${SEMBLE_INDEX_PATH}"
		return 0
	fi
	if [ -n "${RUNTIME_DIR:-}" ]; then
		printf '%s/.semble-index\n' "${RUNTIME_DIR}"
		return 0
	fi
	printf '.semble-index\n'
}

_semble_elapsed_ms()
{
	local start_ms="${1:-0}"
	local end_ms=""
	local diff="0"

	end_ms="$(date +%s%3N 2>/dev/null || printf '0')"
	case "${start_ms}${end_ms}" in
		*[!0-9]*)
			printf '0\n'
			return 0
			;;
	esac
	diff="$((end_ms - start_ms))"
	if [ "${diff}" -lt 0 ]; then
		printf '0\n'
		return 0
	fi
	printf '%s\n' "${diff}"
}

_semble_cleanup_tempfiles()
{
	if [ "$#" -eq 0 ]; then
		return 0
	fi
	rm -f "$@"
}

semble_query_block()
{
	local query_text="${1:-}"
	local max_chunks="${2:-}"
	local header_label="${3:-}"
	local target=""
	local semble_bin=""
	local semble_index=""
	local timeout_secs="${SEMBLE_QUERY_TIMEOUT_SECS:-5}"
	local start_ms=""
	local elapsed_ms="0"
	local tmp_stdout=""
	local tmp_stderr=""
	local status=0
	local stderr_tail=""
	local bytes="0"
	local last_byte=""

	if [ "$#" -lt 3 ] || [ -z "${max_chunks}" ] || [ -z "${header_label}" ]; then
		_semble_log_event "SEMBLE_FALLBACK" "target=invalid" "reason=invalid-args"
		return 1
	fi
	shift 3 || return 1

	target="$(_semble_target_slug "${header_label}")"

	if [ "${SEMBLE_AVAILABLE:-true}" != "true" ]; then
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=binary-unavailable"
		return 1
	fi
	if [ "${SEMBLE_INDEX_AVAILABLE:-}" != "true" ]; then
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=index-unavailable"
		return 1
	fi

	semble_bin="${SEMBLE_BIN:-$(command -v semble 2>/dev/null || true)}"
	if [ -z "${semble_bin}" ]; then
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=binary-unavailable"
		return 1
	fi

	semble_index="$(_semble_index_path)"
	if [ ! -e "${semble_index}" ]; then
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=index-unavailable"
		return 1
	fi

	tmp_stdout="$(mktemp 2>/dev/null || true)"
	tmp_stderr="$(mktemp 2>/dev/null || true)"
	if [ -z "${tmp_stdout}" ] || [ -z "${tmp_stderr}" ]; then
		_semble_cleanup_tempfiles "${tmp_stdout}" "${tmp_stderr}"
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=tempfile-unavailable"
		return 1
	fi

	start_ms="$(date +%s%3N 2>/dev/null || printf '0')"
	if command -v timeout >/dev/null 2>&1; then
		if timeout --preserve-status "${timeout_secs}s" \
			"${semble_bin}" query "${query_text}" --index "${semble_index}" --top-k "${max_chunks}" --format text "$@" >"${tmp_stdout}" 2>"${tmp_stderr}"; then
			status=0
		else
			status=$?
		fi
	else
		if "${semble_bin}" query "${query_text}" --index "${semble_index}" --top-k "${max_chunks}" --format text "$@" >"${tmp_stdout}" 2>"${tmp_stderr}"; then
			status=0
		else
			status=$?
		fi
	fi
	elapsed_ms="$(_semble_elapsed_ms "${start_ms}")"

	if [ "${status}" -ne 0 ]; then
		if [ "${status}" -eq 124 ] || [ "${status}" -eq 137 ]; then
			_semble_cleanup_tempfiles "${tmp_stdout}" "${tmp_stderr}"
			_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=timeout" "ms=${elapsed_ms}"
			return 1
		fi
		stderr_tail="$(tail -n 1 "${tmp_stderr}" 2>/dev/null || true)"
		_semble_cleanup_tempfiles "${tmp_stdout}" "${tmp_stderr}"
		if [ -n "${stderr_tail}" ]; then
			_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=exit=${status} ${stderr_tail}" "ms=${elapsed_ms}"
		else
			_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=exit=${status}" "ms=${elapsed_ms}"
		fi
		return 1
	fi

	if ! grep -q '[^[:space:]]' "${tmp_stdout}" 2>/dev/null; then
		_semble_cleanup_tempfiles "${tmp_stdout}" "${tmp_stderr}"
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=empty-result" "ms=${elapsed_ms}"
		return 1
	fi

	bytes="$(wc -c < "${tmp_stdout}" | tr -d '[:space:]')"
	_semble_log_event "SEMBLE_QUERY" "target=${target}" "chunks=${max_chunks}" "bytes=${bytes}" "ms=${elapsed_ms}"

	printf '=== SEMBLE: %s ===\n' "${header_label}"
	cat "${tmp_stdout}"
	last_byte="$(tail -c 1 "${tmp_stdout}" 2>/dev/null | od -An -t x1 | tr -d '[:space:]')"
	if [ -n "${last_byte}" ] && [ "${last_byte}" != "0a" ]; then
		printf '\n'
	fi
	printf '=== END SEMBLE ===\n'

	_semble_cleanup_tempfiles "${tmp_stdout}" "${tmp_stderr}"
	return 0
}
