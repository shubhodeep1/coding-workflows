#!/usr/bin/env bash
# semble_helpers.sh — shared, sourceable Semble query helpers.

_SEMBLE_HELPERS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "scripts")"
if [ -f "${_SEMBLE_HELPERS_SCRIPT_DIR}/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source "${_SEMBLE_HELPERS_SCRIPT_DIR}/emit_event.sh"
elif [ -f "scripts/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source scripts/emit_event.sh
fi
# Resolve once so calls inside command substitutions share the same state.
export SEMBLE_BOOTSTRAP_STATE_FILE="${SEMBLE_BOOTSTRAP_STATE_FILE:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/semble_bootstrap-${GITHUB_RUN_ID:-$BASHPID}-${GITHUB_RUN_ATTEMPT:-1}.state}"
if ! type emit_event >/dev/null 2>&1; then
	emit_event()
	{
		return 0
	}
fi

_semble_log_event()
{
	local prefix="${1:?_semble_log_event: prefix required}"
	local has_context="false"
	local context_field=""
	local -a emit_fields=()
	shift || true
	printf '%s' "${prefix}" >&2
	while [ "$#" -gt 0 ]; do
		case "$1" in
			context=*) has_context="true" ;;
		esac
		emit_fields+=("$1")
		printf ' %s' "$1" >&2
		shift
	done
	if [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] && [[ "${GITHUB_RUN_ATTEMPT:-1}" =~ ^[0-9]+$ ]]; then
		emit_fields+=("run=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}")
		printf ' run=%s-%s' "${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT:-1}" >&2
	fi
	if [ "${prefix}" = "SEMBLE_FALLBACK" ]; then
		# semble_query_block reads this so a failed lazy bootstrap is logged once.
		_SEMBLE_BOOTSTRAP_FALLBACK_LOGGED=true
		emit_fields+=("sources=0")
		printf ' sources=0' >&2
		if [ -f "${SEMBLE_STATIC_CONTEXT_FILE:-/dev/null/nonexistent}" ] && [ -r "${SEMBLE_STATIC_CONTEXT_FILE}" ]; then
			emit_fields+=("static_dup_bytes=0")
			printf ' static_dup_bytes=0' >&2
		fi
	fi
	if [ "${has_context}" != "true" ] && [ -n "${SEMBLE_LOG_CONTEXT:-}" ]; then
		context_field="$(printf '%s' "${SEMBLE_LOG_CONTEXT}" | tr '\r\n\t ' '-' | tr -s '-')"
		context_field="${context_field#-}"
		context_field="${context_field%-}"
		if [ -n "${context_field}" ]; then
			emit_fields+=("context=${context_field}")
			printf ' context=%s' "${context_field}" >&2
		fi
	fi
	printf '\n' >&2
	emit_event "${prefix}" "${emit_fields[@]}"
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

_semble_normalize_max_chunks()
{
	local raw="${1:-0}"
	local normalized="${raw}"
	case "${raw}" in
		''|*[!0-9]*) printf '1\n' ;;
		*)
			normalized="${normalized#"${normalized%%[!0]*}"}"
			if [ -z "${normalized}" ]; then
				printf '1\n'
			elif [ "${#normalized}" -gt 2 ] || [ "${normalized}" -gt 20 ]; then
				printf '20\n'
			else
				printf '%s\n' "${normalized}"
			fi
			;;
	esac
}

_semble_bootstrap_mode()
{
	local mode
	mode="$(printf '%s' "${SEMBLE_BOOTSTRAP_MODE:-eager}" | tr '[:upper:]' '[:lower:]')"
	case "${mode}" in
		lazy|eager) _SEMBLE_RESOLVED_BOOTSTRAP_MODE="${mode}" ;;
		*)
			if [ "${_SEMBLE_INVALID_MODE_WARNED:-false}" != "true" ]; then
				printf '%s\n' '::warning::SEMBLE_BOOTSTRAP_MODE is invalid; using eager' >&2
				_SEMBLE_INVALID_MODE_WARNED=true
			fi
			_SEMBLE_RESOLVED_BOOTSTRAP_MODE=eager
			;;
	esac
}

_semble_state_read()
{
	[ -f "${SEMBLE_BOOTSTRAP_STATE_FILE}" ] || return 1
	local state_value index_value bin_value expected_bin
	state_value="$(grep -m1 '^state=' "${SEMBLE_BOOTSTRAP_STATE_FILE}" 2>/dev/null || true)"
	case "${state_value}" in
		state=failed) return 2 ;;
		state=ready) ;;
		*) return 1 ;;
	esac
	index_value="$(grep -m1 '^index_path=' "${SEMBLE_BOOTSTRAP_STATE_FILE}" 2>/dev/null || true)"
	bin_value="$(grep -m1 '^semble_bin=' "${SEMBLE_BOOTSTRAP_STATE_FILE}" 2>/dev/null || true)"
	index_value="${index_value#index_path=}"
	bin_value="${bin_value#semble_bin=}"
	expected_bin="${SEMBLE_WRAPPER_DIR:-$(dirname "$(_semble_index_path)")/semble/bin}/semble"
	# A state file cannot redirect a query to an arbitrary executable or index.
	[ "${index_value}" = "$(_semble_index_path)" ] && [ "${bin_value}" = "${expected_bin}" ] && [ -s "${index_value}" ] && [ -x "${bin_value}" ] || return 1
	export SEMBLE_AVAILABLE=true SEMBLE_INDEX_AVAILABLE=true SEMBLE_INDEX_PATH="${index_value}" SEMBLE_BIN="${bin_value}"
	case ":${PATH}:" in *":$(dirname "${bin_value}"):"*) ;; *) PATH="$(dirname "${bin_value}"):${PATH}" ;; esac
	export PATH
	return 0
}

_semble_state_write()
{
	local state_value="$1" bin_value="${2:-}" index_value="${3:-}" temp_state
	temp_state="$(mktemp "${SEMBLE_BOOTSTRAP_STATE_FILE}.XXXXXX")" || return 1
	if ! printf 'state=%s\nsemble_bin=%s\nindex_path=%s\n' "${state_value}" "${bin_value}" "${index_value}" > "${temp_state}" || ! mv -f -- "${temp_state}" "${SEMBLE_BOOTSTRAP_STATE_FILE}"; then
		rm -f -- "${temp_state}"
		return 1
	fi
}

semble_should_query()
{
	[ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ] && return 0
	_semble_bootstrap_mode
	[ "${_SEMBLE_RESOLVED_BOOTSTRAP_MODE}" = "lazy" ] && [ "${SEMBLE_ENABLED:-false}" = "true" ] || return 1
	_semble_state_read && return 0
	[ "$(grep -m1 '^state=' "${SEMBLE_BOOTSTRAP_STATE_FILE}" 2>/dev/null || true)" != 'state=failed' ]
}

semble_ensure_ready()
{
	local target="${1:-semble}" bootstrap_root timeout_secs deadline remaining index_path wrapper_path install_result start_ms install_ms=0 workspace_root build_log="" build_rc=1
	local state_status=0 build_start_ms="" failed_index_ms=0
	if [ "${SEMBLE_AVAILABLE:-false}" = "true" ] && [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ]; then return 0; fi
	_semble_bootstrap_mode
	[ "${_SEMBLE_RESOLVED_BOOTSTRAP_MODE}" = "lazy" ] && [ "${SEMBLE_ENABLED:-false}" = "true" ] || return 1
	_semble_state_read && return 0
	state_status=$?
	[ "${state_status}" -ne 2 ] || return 1
	bootstrap_root="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}"
	workspace_root="${GITHUB_WORKSPACE:-$PWD}"
	timeout_secs="${SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS:-180}"
	[[ "${timeout_secs}" =~ ^[0-9]+$ ]] && [ "${timeout_secs}" -gt 0 ] && [ "${timeout_secs}" -le 3600 ] || timeout_secs=180
	deadline="$(($(date +%s) + timeout_secs))"
	if ! mkdir -p -- "${bootstrap_root}" "$(dirname "${SEMBLE_BOOTSTRAP_STATE_FILE}")"; then return 1; fi
	if ! command -v flock >/dev/null 2>&1; then
		# Record the failure so later queries do not retry, and name the cause.
		_semble_state_write failed '' "$(_semble_index_path)" || true
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=flock-unavailable"
		return 1
	fi
	local lock_fd
	exec {lock_fd}>"${SEMBLE_BOOTSTRAP_STATE_FILE}.lock" || return 1
	if ! flock -w "${timeout_secs}" "${lock_fd}"; then
		exec {lock_fd}>&-
		_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=lazy-bootstrap-failed"
		return 1
	fi
	_semble_state_read && { exec {lock_fd}>&-; return 0; }
	state_status=$?
	if [ "${state_status}" -eq 2 ]; then exec {lock_fd}>&-; return 1; fi
	index_path="$(_semble_index_path)"
	wrapper_path="${SEMBLE_WRAPPER_DIR:-$(dirname "${index_path}")/semble/bin}/semble"
	install_result="$(mktemp "${bootstrap_root}/semble-install.XXXXXX")" || { exec {lock_fd}>&-; return 1; }
	remaining="$((deadline - $(date +%s)))"
	start_ms="$(date +%s%3N)"
	if [ "${remaining}" -gt 0 ] && [ -f "${_SEMBLE_HELPERS_SCRIPT_DIR}/install_semble.sh" ] &&
		(cd "${bootstrap_root}" && GITHUB_WORKSPACE="${workspace_root}" SEMBLE_INSTALL_RESULT_FILE="${install_result}" env -u BASH_ENV -u ENV timeout "${remaining}" bash "${_SEMBLE_HELPERS_SCRIPT_DIR}/install_semble.sh") &&
		[ "$(cat "${install_result}" 2>/dev/null)" = "available" ]; then
		install_ms="$(_semble_elapsed_ms "${start_ms}")"
		export SEMBLE_INSTALL_MS="${install_ms}"
		remaining="$((deadline - $(date +%s)))"
		build_log="$(mktemp "${bootstrap_root}/semble-build.XXXXXX")" || true
		if [ -n "${build_log}" ] && [ "${remaining}" -gt 0 ] && [ -f "${_SEMBLE_HELPERS_SCRIPT_DIR}/build_semble_wrapper.sh" ]; then
			build_start_ms="$(date +%s%3N)"
			(cd "${bootstrap_root}" && GITHUB_WORKSPACE="${workspace_root}" SEMBLE_INDEX_PATH="${index_path}" env -u BASH_ENV -u ENV timeout "${remaining}" bash "${_SEMBLE_HELPERS_SCRIPT_DIR}/build_semble_wrapper.sh") 2> "${build_log}" && build_rc=0
			cat "${build_log}" >&2
		fi
		if [ "${build_rc}" -eq 0 ] &&
			[ -s "${index_path}" ] && [ -x "${wrapper_path}" ] &&
			_semble_state_write ready "${wrapper_path}" "${index_path}" && _semble_state_read; then
			rm -f -- "${install_result}" "${build_log}"
			exec {lock_fd}>&-
			return 0
		fi
		if [ -z "${build_log}" ] || ! grep -q '^SEMBLE_BOOTSTRAP ' "${build_log}"; then
			# A killed or silent builder still spent index time; report it.
			[ -z "${build_start_ms}" ] || failed_index_ms="$(_semble_elapsed_ms "${build_start_ms}")"
			_semble_log_event "SEMBLE_BOOTSTRAP" "mode=lazy" "state=failed" "install_ms=${install_ms}" "index_ms=${failed_index_ms}" "reason=lazy-bootstrap-failed"
		fi
	else
		install_ms="$(_semble_elapsed_ms "${start_ms}")"
		_semble_log_event "SEMBLE_BOOTSTRAP" "mode=lazy" "state=failed" "install_ms=${install_ms}" "index_ms=0" "reason=lazy-bootstrap-failed"
	fi
	rm -f -- "${install_result}" "${build_log}"
	_semble_state_write failed '' "${index_path}" || true
	export SEMBLE_AVAILABLE=false SEMBLE_INDEX_AVAILABLE=false
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf 'SEMBLE_AVAILABLE=false\nSEMBLE_INDEX_AVAILABLE=false\n' >> "${GITHUB_ENV}" || true
	fi
	_semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=lazy-bootstrap-failed"
	exec {lock_fd}>&-
	return 1
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
	max_chunks="$(_semble_normalize_max_chunks "${max_chunks}")"

	target="$(_semble_target_slug "${header_label}")"
	_SEMBLE_BOOTSTRAP_FALLBACK_LOGGED=false
	if ! semble_ensure_ready "${target}" >/dev/null && [ "${_SEMBLE_BOOTSTRAP_FALLBACK_LOGGED}" = "true" ]; then
		# The bootstrap already logged this query's fallback; do not count it twice.
		return 1
	fi

	if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then
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

	tmp_stdout="$(mktemp "${TMPDIR:-/tmp}/semble.stdout.XXXXXX" 2>/dev/null || true)"
	tmp_stderr="$(mktemp "${TMPDIR:-/tmp}/semble.stderr.XXXXXX" 2>/dev/null || true)"
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
	local sources=0 static_dup_bytes="" static_field=""
	sources="$({ grep -E '^\[[0-9]+\] [^[:space:]]+:[0-9]+-[0-9]+' "${tmp_stdout}" || true; } | sed -E 's/^\[[0-9]+\] (.*):[0-9]+-[0-9]+.*/\1/' | sort -u | wc -l | tr -d '[:space:]')"
	if [ -f "${SEMBLE_STATIC_CONTEXT_FILE:-/dev/null/nonexistent}" ] && [ -r "${SEMBLE_STATIC_CONTEXT_FILE}" ]; then
		static_dup_bytes="$(python3 - "${tmp_stdout}" "${SEMBLE_STATIC_CONTEXT_FILE}" <<'PY' 2>/dev/null || true
from pathlib import Path
import sys

static = set(Path(sys.argv[2]).read_text(errors="replace").splitlines())
print(sum(len(line.encode()) for line in Path(sys.argv[1]).read_text(errors="replace").splitlines() if len(line.strip()) >= 40 and line in static))
PY
)"
		[[ "${static_dup_bytes}" =~ ^[0-9]+$ ]] && static_field="static_dup_bytes=${static_dup_bytes}"
	fi
	local -a contribution_fields=("sources=${sources}")
	[ -z "${static_field}" ] || contribution_fields+=("${static_field}")
	_semble_log_event "SEMBLE_QUERY" "target=${target}" "chunks=${max_chunks}" "bytes=${bytes}" "ms=${elapsed_ms}" "${contribution_fields[@]}"

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
