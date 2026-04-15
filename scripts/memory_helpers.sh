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

	local check_result
	if check_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-check "$@")"; then
		printf '%s\n' "${check_result}"
		return 0
	fi

	{
		_memory_warn "processed-command-check failed (fail-open)"
		echo '{"exists": false}'
		return 0
	}
}

memory_processed_command_list()
{
	if ! _memory_enabled; then
		echo '{"entries": [], "count": 0}'
		return 0
	fi

	local list_result
	if list_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" processed-command-list "$@")"; then
		printf '%s\n' "${list_result}"
		return 0
	fi

	{
		_memory_warn "processed-command-list failed (fail-open)"
		echo '{"entries": [], "count": 0}'
		return 0
	}
}

memory_clarify_loop_guard()
{
	if ! _memory_enabled; then
		echo '{"result": {"blocked": false, "reason": "none", "cycle": 1, "max_cycles": 1}}'
		return 0
	fi

	local guard_result
	if guard_result="$(python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" clarify-loop-guard "$@")"; then
		printf '%s\n' "${guard_result}"
		return 0
	fi

	{
		_memory_warn "clarify-loop-guard failed (fail-open)"
		echo '{"result": {"blocked": false, "reason": "none", "cycle": 1, "max_cycles": 1}}'
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
