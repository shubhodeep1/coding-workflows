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
	echo "::warning::memory $*" >&2
}

_memory_telemetry()
{
	# Emit a structured telemetry line to stdout for log-analysis visibility.
	# Usage: _memory_telemetry '{"op":"retrieve","ok":true,...}'
	echo "AI_MEMORY_TELEMETRY: $1"
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

memory_ensure_branch()
{
	# Ensure the ai-memory branch exists on the remote.  If it doesn't,
	# create an empty orphan branch and push it so that subsequent memory
	# read operations (which clone with --branch ai-memory) don't fail.
	if ! _memory_enabled; then
		return 0
	fi

	local branch="${AI_MEMORY_BRANCH:-ai-memory}"
	local token="${GH_TOKEN:-}"

	# Resolve authenticated origin URL
	local origin_url
	origin_url="$(git remote get-url origin 2>/dev/null || echo "")"
	if [[ -z "${origin_url}" ]]; then
		_memory_warn "ensure-branch: no origin remote configured"
		return 0
	fi

	# Check if branch exists on remote
	if git ls-remote --heads origin "${branch}" 2>/dev/null | grep -q "${branch}"; then
		return 0
	fi

	echo "AI memory branch '${branch}' does not exist — creating it."

	local temp_dir
	temp_dir="$(mktemp -d)"

	(
		# The workspace shell context exports GIT_DIR/GIT_WORK_TREE pointing at
		# the host repo (see the "Activate workspace shell context" step in the
		# implement/validate/review_autofix workflows).  Leaving them set would
		# make `git init` and the orphan-branch commit below operate on the host
		# repo instead of this throwaway temp dir.  Unset them so git resolves
		# the repository from the temp working directory.
		unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
			GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE
		cd "${temp_dir}"
		git init --quiet
		git config user.name "codex-bot"
		git config user.email "codex@users.noreply.github.com"
		git remote add origin "${origin_url}"
		git checkout --orphan "${branch}"

		mkdir -p ai-memory
		echo "AI memory branch — created automatically." > ai-memory/README.md
		git add ai-memory/README.md
		git commit --quiet -m "Initialize ai-memory branch"
		git push origin "${branch}" 2>&1
	) || {
		_memory_warn "ensure-branch: failed to create '${branch}' (fail-open)"
		rm -rf "${temp_dir}"
		return 0
	}

	rm -rf "${temp_dir}"
	echo "AI memory branch '${branch}' created successfully."
}

memory_record_run_event()
{
	if ! _memory_enabled; then
		_memory_telemetry '{"op":"record-run-event","ok":true,"enabled":false,"source":"shell"}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-run-event "$@" 2>&1 || {
		_memory_warn "record-run-event failed (fail-open)"
		_memory_telemetry '{"op":"record-run-event","ok":false,"fail_open":true,"source":"shell"}'
		return 0
	}
}

memory_record_candidate()
{
	if ! _memory_enabled; then
		_memory_telemetry '{"op":"record-candidate","ok":true,"enabled":false,"source":"shell"}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-candidate "$@" 2>&1 || {
		_memory_warn "record-candidate failed (fail-open)"
		_memory_telemetry '{"op":"record-candidate","ok":false,"fail_open":true,"source":"shell"}'
		return 0
	}
}

memory_retrieve()
{
	local output_file="${1:-}"

	if ! _memory_enabled; then
		_memory_retrieve_fallback "${output_file}" "disabled"
		_memory_telemetry '{"op":"retrieve","ok":true,"enabled":false,"source":"shell"}'
		return 0
	fi

	if [[ $# -gt 0 ]]; then
		shift
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" retrieve --output-file "${output_file}" "$@" 2>&1 || {
		_memory_warn "retrieve failed (fail-open)"
		_memory_retrieve_fallback "${output_file}" "unavailable"
		_memory_telemetry '{"op":"retrieve","ok":false,"fail_open":true,"source":"shell"}'
		return 0
	}
}

memory_validation_history_get()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "hit": false, "validation_history": null}'
		_memory_telemetry '{"op":"validation-history-get","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local get_result
	if get_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" validation-history get "$@")"; then
		printf '%s\n' "${get_result}"
		return 0
	fi

	{
		_memory_warn "validation-history-get failed (fail-open)"
		_memory_telemetry '{"op":"validation-history-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "hit": false, "validation_history": null, "warning_code": "history_read_failed", "warning": "validation-history-get failed (shell wrapper fail-open)"}'
		return 0
	}
}

memory_validation_history_append()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "stored": false, "validation_history": null}'
		_memory_telemetry '{"op":"validation-history-append","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local append_result
	if append_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" validation-history append "$@")"; then
		printf '%s\n' "${append_result}"
		return 0
	fi

	{
		_memory_warn "validation-history-append failed (fail-open)"
		_memory_telemetry '{"op":"validation-history-append","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "stored": false, "validation_history": null, "warning_code": "history_write_failed", "warning": "validation-history-append failed (shell wrapper fail-open)"}'
		return 0
	}
}

memory_validation_discovery_get()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "hit": false, "validation_discovery": null}'
		_memory_telemetry '{"op":"validation-discovery-get","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local get_result
	if get_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" validation-discovery get "$@")"; then
		printf '%s\n' "${get_result}"
		return 0
	fi

	{
		_memory_warn "validation-discovery-get failed (fail-open)"
		_memory_telemetry '{"op":"validation-discovery-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "hit": false, "validation_discovery": null, "warning_code": "discovery_read_failed", "warning": "validation-discovery-get failed (shell wrapper fail-open)"}'
		return 0
	}
}

memory_validation_discovery_append()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "stored": false, "validation_discovery": null}'
		_memory_telemetry '{"op":"validation-discovery-append","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local append_result
	if append_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" validation-discovery append "$@")"; then
		printf '%s\n' "${append_result}"
		return 0
	fi

	{
		_memory_warn "validation-discovery-append failed (fail-open)"
		_memory_telemetry '{"op":"validation-discovery-append","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "stored": false, "validation_discovery": null, "warning_code": "discovery_write_failed", "warning": "validation-discovery-append failed (shell wrapper fail-open)"}'
		return 0
	}
}

memory_operator_bypass_audit_get()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "hit": false, "audit": null}'
		_memory_telemetry '{"op":"operator-bypass-audit-get","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local get_result
	if get_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" operator-bypass-audit get "$@")"; then
		printf '%s\n' "${get_result}"
		return 0
	fi

	{
		_memory_warn "operator-bypass-audit-get failed (fail-open)"
		_memory_telemetry '{"op":"operator-bypass-audit-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "hit": false, "audit": null}'
		return 0
	}
}

memory_operator_bypass_audit_append()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "stored": false, "audit": null}'
		_memory_telemetry '{"op":"operator-bypass-audit-append","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local append_result
	if append_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" operator-bypass-audit append "$@")"; then
		printf '%s\n' "${append_result}"
		return 0
	fi

	{
		_memory_warn "operator-bypass-audit-append failed (fail-open)"
		_memory_telemetry '{"op":"operator-bypass-audit-append","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "stored": false, "audit": null}'
		return 0
	}
}

memory_revalidate_events_get()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "hit": false, "events": null}'
		_memory_telemetry '{"op":"revalidate-events-get","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local get_result
	if get_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" revalidate-events get "$@")"; then
		printf '%s\n' "${get_result}"
		return 0
	fi

	{
		_memory_warn "revalidate-events-get failed (fail-open)"
		_memory_telemetry '{"op":"revalidate-events-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "hit": false, "events": null}'
		return 0
	}
}

memory_revalidate_events_append()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "stored": false, "events": null}'
		_memory_telemetry '{"op":"revalidate-events-append","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local append_result
	if append_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" revalidate-events append "$@")"; then
		printf '%s\n' "${append_result}"
		return 0
	fi

	{
		_memory_warn "revalidate-events-append failed (fail-open)"
		_memory_telemetry '{"op":"revalidate-events-append","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": true, "enabled": true, "stored": false, "events": null}'
		return 0
	}
}

memory_processed_command_check()
{
	if ! _memory_enabled; then
		echo '{"exists": false}'
		_memory_telemetry '{"op":"processed-command-check","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local check_result
	if check_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-check "$@")"; then
		printf '%s\n' "${check_result}"
		return 0
	fi

	{
		_memory_warn "processed-command-check failed (fail-open)"
		_memory_telemetry '{"op":"processed-command-check","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"exists": false}'
		return 0
	}
}

memory_processed_command_list()
{
	if ! _memory_enabled; then
		echo '{"entries": [], "count": 0}'
		_memory_telemetry '{"op":"processed-command-list","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local list_result
	if list_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-list "$@")"; then
		printf '%s\n' "${list_result}"
		return 0
	fi

	{
		_memory_warn "processed-command-list failed (fail-open)"
		_memory_telemetry '{"op":"processed-command-list","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"entries": [], "count": 0}'
		return 0
	}
}

memory_clarify_loop_guard()
{
	if ! _memory_enabled; then
		echo '{"result": {"blocked": false, "reason": "none", "cycle": 1, "max_cycles": null}}'
		_memory_telemetry '{"op":"clarify-loop-guard","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local guard_result
	if guard_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" clarify-loop-guard "$@")"; then
		printf '%s\n' "${guard_result}"
		return 0
	fi

	{
		_memory_warn "clarify-loop-guard failed (fail-open)"
		_memory_telemetry '{"op":"clarify-loop-guard","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"result": {"blocked": false, "reason": "none", "cycle": 1, "max_cycles": null}}'
		return 0
	}
}

memory_finalize_task()
{
	if ! _memory_enabled; then
		_memory_telemetry '{"op":"finalize-task","ok":true,"enabled":false,"source":"shell"}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" finalize-task "$@"
}

memory_promote()
{
	if ! _memory_enabled; then
		_memory_telemetry '{"op":"promote","ok":true,"enabled":false,"source":"shell"}'
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" promote "$@"
}

memory_processed_command_claim()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "operation_result": {"claimed": true, "entry": null}}'
		_memory_telemetry '{"op":"processed-command-claim","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-claim "$@"
}

memory_processed_command_complete()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "entry": null}'
		_memory_telemetry '{"op":"processed-command-complete","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-complete "$@" || {
		_memory_warn "processed-command-complete failed (fail-open)"
		_memory_telemetry '{"op":"processed-command-complete","ok":false,"fail_open":true,"source":"shell"}'
		return 0
	}
}
