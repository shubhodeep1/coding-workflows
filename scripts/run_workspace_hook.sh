#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat <<'USAGE' >&2
Usage: run_workspace_hook.sh <phase> <hook>

Hooks:
	after_create
	before_run
	after_run
	before_remove
USAGE
}

fail()
{
	printf 'workspace_hook: %s\n' "$*" >&2
	exit 1
}

is_falsey()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		0|false|no|off)
			return 0
			;;
	esac
	return 1
}

is_fatal_hook()
{
	case "${1:-}" in
		after_create|before_run)
			return 0
			;;
	esac
	return 1
}

emit_log_tail()
{
	local log_file="$1"
	local max_bytes="${2:-10240}"
	local total_bytes="0"

	if [ ! -f "${log_file}" ]; then
		printf '%s\n' '(no workspace hook log captured)' >&2
		return 0
	fi

	if ! total_bytes="$(wc -c < "${log_file}" 2>/dev/null | tr -d '[:space:]')" || ! [[ "${total_bytes}" =~ ^[0-9]+$ ]]; then
		total_bytes="0"
	fi
	if [ "${total_bytes}" -gt "${max_bytes}" ]; then
		printf '%s\n' "--- last ${max_bytes} bytes of ${log_file} (${total_bytes} total bytes) ---" >&2
	else
		printf '%s\n' "--- ${log_file} (${total_bytes} bytes) ---" >&2
	fi
	tail -c "${max_bytes}" "${log_file}" >&2 || true
	printf '\n%s\n' '--- end workspace hook log ---' >&2
}

report_hook_failure()
{
	local phase="$1"
	local hook="$2"
	local message="$3"
	local log_file="${4:-}"
	local exit_code="${5:-1}"

	if is_fatal_hook "${hook}"; then
		printf '::error::Workspace hook %s/%s %s\n' "${phase}" "${hook}" "${message}" >&2
		if [ -n "${log_file}" ]; then
			emit_log_tail "${log_file}" "10240"
		fi
		exit "${exit_code}"
	fi

	printf '::warning::Workspace hook %s/%s %s\n' "${phase}" "${hook}" "${message}" >&2
	if [ -n "${log_file}" ]; then
		emit_log_tail "${log_file}" "10240"
	fi
	return 0
}

main()
{
	if [ "$#" -ne 2 ]; then
		usage
		exit 64
	fi

	local phase="$1"
	local hook="$2"
	local timeout_seconds="${WORKSPACE_HOOK_TIMEOUT_SECONDS:-600}"
	local script_dir repo_root hook_path runner_temp workspace_path log_dir log_file
	local run_status="0"
	local status_text

	case "${phase}" in
		''|*[!A-Za-z0-9._-]*)
			fail "unsupported phase: ${phase}"
			;;
	esac
	case "${hook}" in
		after_create|before_run|after_run|before_remove)
			;;
		*)
			fail "unsupported hook: ${hook}"
			;;
	esac

	if [ "${hook}" = "after_create" ] && is_falsey "${CREATED_NOW:-}"; then
		exit 0
	fi

	script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
	repo_root="$(cd "${script_dir}/.." && pwd -P)"
	hook_path="${repo_root}/.github/ai/workspace_hooks/${phase}/${hook}.sh"
	if [ ! -s "${hook_path}" ]; then
		exit 0
	fi

	if ! [[ "${timeout_seconds}" =~ ^[0-9]+$ ]] || [ "${timeout_seconds}" -le 0 ]; then
		printf '::warning::Invalid WORKSPACE_HOOK_TIMEOUT_SECONDS=%s; defaulting to 600.\n' "${timeout_seconds}" >&2
		timeout_seconds="600"
	fi

	runner_temp="${RUNNER_TEMP:-}"
	if [ -z "${runner_temp}" ]; then
		report_hook_failure "${phase}" "${hook}" 'could not start because RUNNER_TEMP is unset.' "" 1
	fi

	workspace_path="${WORKSPACE_PATH:-}"
	if [ -z "${workspace_path}" ] || [ ! -d "${workspace_path}" ]; then
		report_hook_failure "${phase}" "${hook}" "could not start because WORKSPACE_PATH is missing or does not exist: ${workspace_path:-<unset>}." "" 1
	fi

	log_dir="${runner_temp}/workspace-hooks"
	log_file="${log_dir}/${phase}-${hook}.log"
	mkdir -p "${log_dir}"
	: > "${log_file}"

	set +e
	(
		cd "${workspace_path}" && \
		timeout --kill-after=10s "${timeout_seconds}" bash -lc 'bash "$1"' bash "${hook_path}"
	) >"${log_file}" 2>&1
	run_status="$?"
	set -e

	if [ "${run_status}" -eq 0 ]; then
		exit 0
	fi

	status_text="failed with exit code ${run_status}. Log: ${log_file}."
	if [ "${run_status}" -eq 124 ]; then
		status_text="timed out after ${timeout_seconds} seconds. Log: ${log_file}."
	fi
	report_hook_failure "${phase}" "${hook}" "${status_text}" "${log_file}" "${run_status}"
}

main "$@"
