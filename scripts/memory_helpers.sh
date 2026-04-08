#!/usr/bin/env bash

MEMORY_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_memory_enabled()
{
	local enabled
	enabled="$(printf '%s' "${AI_MEMORY_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"

	case "${enabled}" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

_memory_warn()
{
	echo "::warning::memory $*"
}

_memory_retrieve_fallback()
{
	local output_file="${1:-}"
	local status="${2:-unavailable}"

	if [[ -z "${output_file}" ]]; then
		_memory_warn "retrieve fallback missing output file path"
		return 0
	fi

	printf 'AI MEMORY CONTEXT\nstatus: %s\n' "${status}" >"${output_file}"

	return 0
}

memory_record_run_event()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-run-event "$@" 2>&1 || {
		_memory_warn "record-run-event failed (fail-open)"
		return 0
	}
}

memory_record_candidate()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-candidate "$@" 2>&1 || {
		_memory_warn "record-candidate failed (fail-open)"
		return 0
	}
}

memory_retrieve()
{
	local output_file="${1:-}"

	if ! _memory_enabled; then
		_memory_retrieve_fallback "${output_file}" "disabled"
		return 0
	fi

	if [[ $# -gt 0 ]]; then
		shift
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" retrieve --output-file "${output_file}" "$@" 2>&1 || {
		_memory_warn "retrieve failed (fail-open)"
		_memory_retrieve_fallback "${output_file}" "unavailable"
		return 0
	}
}

memory_processed_command_check()
{
	if ! _memory_enabled; then
		echo '{"exists": false}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-check "$@" 2>&1 || {
		_memory_warn "processed-command-check failed (fail-open)"
		echo '{"exists": false}'
		return 0
	}
}

memory_finalize_task()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" finalize-task "$@"
}

memory_promote()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" promote "$@"
}

memory_processed_command_claim()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-claim "$@"
}

memory_processed_command_complete()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-complete "$@"
}
