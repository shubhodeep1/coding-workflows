#!/usr/bin/env bash

MEMORY_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_memory_enabled()
{
	local enabled="${AI_MEMORY_ENABLED:-}"

	case "${enabled}" in
		1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn])
			return 0
			;;
		*)
			return 1
			;;
	esac
}

_memory_warn()
{
	echo "[memory_helpers] WARN: $*" >&2
}

_memory_retrieve_fallback()
{
	local output_file="${1:-}"

	if [[ -z "${output_file}" ]]; then
		_memory_warn "memory_retrieve requires output file path as first argument"
		return 0
	fi

	cat >"${output_file}" <<'EOF'
Memory retrieval unavailable.
Proceeding without prior context.
EOF

	return 0
}

memory_record_run_event()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-run-event "$@" || {
		_memory_warn "record-run-event failed; continuing"
		return 0
	}
}

memory_record_candidate()
{
	if ! _memory_enabled; then
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-candidate "$@" || {
		_memory_warn "record-candidate failed; continuing"
		return 0
	}
}

memory_retrieve()
{
	local output_file="${1:-}"

	if ! _memory_enabled; then
		_memory_retrieve_fallback "${output_file}"
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" retrieve "$@" || {
		_memory_warn "retrieve failed; writing fallback context"
		_memory_retrieve_fallback "${output_file}"
		return 0
	}
}

memory_processed_command_check()
{
	if ! _memory_enabled; then
		echo '{"exists": false}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-check "$@" || {
		_memory_warn "processed-command-check failed; returning default"
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
