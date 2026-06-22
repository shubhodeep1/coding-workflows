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
	# Emit a structured telemetry line. Callers that also return structured
	# stdout payloads should redirect this to stderr.
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

_memory_bootstrap_define_fallback()
{
	local helper_name="${1:-}"

	case "${helper_name}" in
		memory_retrieve)
			memory_retrieve()
			{
				local output_file="${1:-}"
				_memory_retrieve_fallback "${output_file}" "unavailable"
				return 0
			}
			;;
		memory_record_run_event)
			memory_record_run_event()
			{
				return 0
			}
			;;
		memory_record_candidate)
			memory_record_candidate()
			{
				return 0
			}
			;;
		*)
			_memory_warn "bootstrap unsupported helper: ${helper_name}"
			return 1
			;;
	esac

	return 0
}

memory_bootstrap()
{
	local ensure_branch="false"
	local required_helpers=()
	local required_helper_name=""

	while [ $# -gt 0 ]; do
		case "$1" in
			--ensure-branch)
				ensure_branch="true"
				shift
				;;
			--require)
				if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
					_memory_warn "bootstrap --require expects a non-empty helper name"
					return 2
				fi
				required_helpers+=("${2}")
				shift 2
				;;
			*)
				_memory_warn "bootstrap unknown arg: $1"
				return 2
				;;
		esac
	done

	if [ "${ensure_branch}" = "true" ] && declare -F memory_ensure_branch >/dev/null 2>&1; then
		memory_ensure_branch 2>/dev/null || true
	fi

	for required_helper_name in "${required_helpers[@]}"; do
		[ -n "${required_helper_name}" ] || continue
		if declare -F "${required_helper_name}" >/dev/null 2>&1; then
			continue
		fi
		if ! _memory_bootstrap_define_fallback "${required_helper_name}"; then
			return 2
		fi
		if ! declare -F "${required_helper_name}" >/dev/null 2>&1; then
			_memory_warn "bootstrap failed to provide required helper: ${required_helper_name}"
			return 2
		fi
	done

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
		_memory_telemetry '{"op":"record-run-event","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-run-event "$@" || {
		_memory_warn "record-run-event failed (fail-open)"
		_memory_telemetry '{"op":"record-run-event","ok":false,"fail_open":true,"source":"shell"}' >&2
		return 0
	}
}

memory_record_candidate()
{
	if ! _memory_enabled; then
		_memory_telemetry '{"op":"record-candidate","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	python3 "${MEMORY_SCRIPTS_DIR}/ai_memory.py" record-candidate "$@" || {
		_memory_warn "record-candidate failed (fail-open)"
		_memory_telemetry '{"op":"record-candidate","ok":false,"fail_open":true,"source":"shell"}' >&2
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

_memory_force_tick_remote_url()
{
	local repo_root=""
	local repository=""
	local origin_url=""
	local token=""
	local server_url="${GITHUB_SERVER_URL:-https://github.com}"
	local server_host=""

	while [ $# -gt 0 ]; do
		case "$1" in
			--repo-root)
				repo_root="${2:-}"
				shift 2
				;;
			--repo)
				repository="${2:-}"
				shift 2
				;;
			*)
				break
				;;
		esac
	done

	if [ -n "${repo_root}" ] && [ -d "${repo_root}/.git" ]; then
		origin_url="$(git -C "${repo_root}" remote get-url origin 2>/dev/null || echo "")"
	fi

	token="${GH_PAT:-${GH_TOKEN:-}}"
	if [ -n "${origin_url}" ]; then
		case "${origin_url}" in
			/*|./*|../*|file://*)
				printf '%s\n' "${origin_url}"
				return 0
				;;
			*)
				;;
		esac
	fi

	if [ -n "${token}" ] && [ -n "${repository}" ]; then
		server_host="${server_url#https://}"
		server_host="${server_host#http://}"
		server_host="${server_host%/}"
		printf 'https://x-access-token:%s@%s/%s\n' "${token}" "${server_host}" "${repository}"
		return 0
	fi

	if [ -n "${origin_url}" ]; then
		printf '%s\n' "${origin_url}"
		return 0
	fi

	return 1
}

_memory_force_tick_remote_branch_exists()
{
	local remote_url="${1:?remote url required}"
	local branch="${2:?branch required}"
	git ls-remote --heads "${remote_url}" "${branch}" 2>/dev/null | awk '{print $2}' | grep -Fxq "refs/heads/${branch}"
}

_memory_force_tick_ensure_branch()
{
	local remote_url="${1:?remote url required}"
	local branch="${2:?branch required}"
	local memory_root="${3:?memory root required}"
	local tmp_dir=""

	if _memory_force_tick_remote_branch_exists "${remote_url}" "${branch}"; then
		return 0
	fi

	tmp_dir="$(mktemp -d)"
	(
		cd "${tmp_dir}"
		git init --quiet
		git config user.name "codex-bot"
		git config user.email "codex@users.noreply.github.com"
		git checkout --orphan "${branch}" >/dev/null 2>&1
		mkdir -p "${memory_root}"
		echo "AI memory branch — created automatically." > "${memory_root}/README.md"
		git add "${memory_root}/README.md"
		git commit --quiet -m "Initialize ${branch}"
		git remote add origin "${remote_url}"
		git push origin "${branch}" >/dev/null 2>&1
	) || {
		rm -rf "${tmp_dir}"
		return 1
	}
	rm -rf "${tmp_dir}"
}

_memory_force_tick_collision_wrapper()
{
	local current_file="${1:?current file required}"
	local incoming_file="${2:?incoming file required}"
	local cooldown_seconds="${3:-30}"

	python3 - <<'PY' "${current_file}" "${incoming_file}" "${cooldown_seconds}"
import datetime as dt
import json
import pathlib
import sys


def _load(path_str: str):
	path = pathlib.Path(path_str)
	if not path.is_file():
		return None
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		print("force_tick_collision: unparseable record", file=sys.stderr)
		return None


def _latest(record):
	if not isinstance(record, dict):
		return ""
	if record.get("dispatch_status") == "disabled":
		return record.get("last_dispatch_timestamp") or ""
	return record.get("last_attempted_timestamp") or record.get("last_dispatch_timestamp") or ""


def _parse(ts: str):
	if not ts:
		return None
	try:
		return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
	except ValueError:
		print("force_tick_collision: unparseable timestamp", file=sys.stderr)
		return None


current = _load(sys.argv[1])
incoming = _load(sys.argv[2])
try:
	cooldown = int(sys.argv[3]) if sys.argv[3] else 30
except ValueError:
	cooldown = 30
if not current or not incoming:
	raise SystemExit(0)

current_ts = _latest(current)
incoming_ts = _latest(incoming)
if not current_ts or not incoming_ts or current_ts == incoming_ts:
	raise SystemExit(0)

current_dt = _parse(current_ts)
incoming_dt = _parse(incoming_ts)
if current_dt is None or incoming_dt is None:
	raise SystemExit(0)

age_seconds = max(0, int((incoming_dt - current_dt).total_seconds()))
if age_seconds < cooldown:
	print(json.dumps({"ok": True, "enabled": True, "stored": False, "record": current}))
PY
}

memory_force_tick_get()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "hit": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-get","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local repo_root=""
	local repository=""
	local tracking_issue=""
	local memory_branch="${AI_MEMORY_BRANCH:-ai-memory}"
	local memory_root="${AI_MEMORY_ROOT:-ai-memory}"
	local remote_url=""
	local tmp_dir=""
	local record_path=""

	while [ $# -gt 0 ]; do
		case "$1" in
			--repo-root)
				repo_root="${2:-}"
				shift 2
				;;
			--repo)
				repository="${2:-}"
				shift 2
				;;
			--tracking-issue)
				tracking_issue="${2:-}"
				shift 2
				;;
			--memory-branch)
				memory_branch="${2:-}"
				shift 2
				;;
			--memory-root)
				memory_root="${2:-}"
				shift 2
				;;
			*)
				_memory_warn "force-tick-get unknown arg: $1"
				_memory_telemetry '{"op":"force-tick-get","ok":false,"fail_open":true,"source":"shell"}' >&2
				echo '{"ok": false, "enabled": true, "hit": false, "record": null}'
				return 0
				;;
		esac
	done

	if ! [[ "${tracking_issue:-}" =~ ^[0-9]+$ ]] || [ "${tracking_issue:-0}" -le 0 ]; then
		echo '{"ok": true, "enabled": true, "hit": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-get","ok":true,"enabled":true,"source":"shell","hit":false}' >&2
		return 0
	fi

	if ! remote_url="$(_memory_force_tick_remote_url --repo-root "${repo_root}" --repo "${repository}")"; then
		_memory_warn "force-tick-get could not resolve remote URL (fail-open)"
		_memory_telemetry '{"op":"force-tick-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "hit": false, "record": null}'
		return 0
	fi

	if ! _memory_force_tick_remote_branch_exists "${remote_url}" "${memory_branch}"; then
		echo '{"ok": true, "enabled": true, "hit": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-get","ok":true,"enabled":true,"source":"shell","hit":false}' >&2
		return 0
	fi

	tmp_dir="$(mktemp -d)"
	if ! git clone --quiet --depth 1 --branch "${memory_branch}" "${remote_url}" "${tmp_dir}" >/dev/null 2>&1; then
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-get failed to clone ${memory_branch} (fail-open)"
		_memory_telemetry '{"op":"force-tick-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "hit": false, "record": null}'
		return 0
	fi

	record_path="${tmp_dir}/${memory_root}/runs/force_tick/${tracking_issue}.json"
	if [ ! -f "${record_path}" ]; then
		rm -rf "${tmp_dir}"
		echo '{"ok": true, "enabled": true, "hit": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-get","ok":true,"enabled":true,"source":"shell","hit":false}' >&2
		return 0
	fi

	local record_wrapper=""
	if ! record_wrapper="$(python3 - <<'PY' "${record_path}"
import json
import pathlib
import sys

record = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps({"ok": True, "enabled": True, "hit": True, "record": record}))
PY
		)"; then
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-get could not decode ${record_path} (fail-open)"
		_memory_telemetry '{"op":"force-tick-get","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "hit": false, "record": null}'
		return 0
	fi

	printf '%s\n' "${record_wrapper}"
	rm -rf "${tmp_dir}"
}

memory_force_tick_put()
{
	if ! _memory_enabled; then
		echo '{"ok": true, "enabled": false, "stored": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-put","ok":true,"enabled":false,"source":"shell"}' >&2
		return 0
	fi

	local repo_root=""
	local repository=""
	local tracking_issue=""
	local record_file=""
	local memory_branch="${AI_MEMORY_BRANCH:-ai-memory}"
	local memory_root="${AI_MEMORY_ROOT:-ai-memory}"
	local remote_url=""
	local tmp_dir=""
	local target_path=""
	local cooldown_seconds="${FORCE_TICK_COOLDOWN_SECONDS:-30}"

	while [ $# -gt 0 ]; do
		case "$1" in
			--repo-root)
				repo_root="${2:-}"
				shift 2
				;;
			--repo)
				repository="${2:-}"
				shift 2
				;;
			--tracking-issue)
				tracking_issue="${2:-}"
				shift 2
				;;
			--record-file)
				record_file="${2:-}"
				shift 2
				;;
			--memory-branch)
				memory_branch="${2:-}"
				shift 2
				;;
			--memory-root)
				memory_root="${2:-}"
				shift 2
				;;
			*)
				_memory_warn "force-tick-put unknown arg: $1"
				_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
				echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
				return 0
				;;
		esac
	done

	if ! [[ "${tracking_issue:-}" =~ ^[0-9]+$ ]] || [ "${tracking_issue:-0}" -le 0 ] || [ -z "${record_file}" ] || [ ! -f "${record_file}" ]; then
		echo '{"ok": true, "enabled": true, "stored": false, "record": null}'
		_memory_telemetry '{"op":"force-tick-put","ok":true,"enabled":true,"source":"shell","stored":false}' >&2
		return 0
	fi

	if ! remote_url="$(_memory_force_tick_remote_url --repo-root "${repo_root}" --repo "${repository}")"; then
		_memory_warn "force-tick-put could not resolve remote URL (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	fi

	if ! _memory_force_tick_ensure_branch "${remote_url}" "${memory_branch}" "${memory_root}"; then
		_memory_warn "force-tick-put could not ensure ${memory_branch} (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	fi

	if ! [[ "${cooldown_seconds}" =~ ^[0-9]+$ ]]; then
		cooldown_seconds=30
	fi

	tmp_dir="$(mktemp -d)"
	if ! git clone --quiet --depth 1 --branch "${memory_branch}" "${remote_url}" "${tmp_dir}" >/dev/null 2>&1; then
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-put failed to clone ${memory_branch} (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	fi

	target_path="${tmp_dir}/${memory_root}/runs/force_tick/${tracking_issue}.json"
	if [ -f "${target_path}" ]; then
		local collision_wrapper=""
		collision_wrapper="$(_memory_force_tick_collision_wrapper "${target_path}" "${record_file}" "${cooldown_seconds}" || true)"
		if [ -n "${collision_wrapper}" ]; then
			rm -rf "${tmp_dir}"
			printf '%s\n' "${collision_wrapper}"
			return 0
		fi
	fi

	if ! mkdir -p "$(dirname "${target_path}")" || ! cp "${record_file}" "${target_path}"; then
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-put failed to stage ${target_path} (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	fi

	(
		cd "${tmp_dir}"
		git config user.name "codex-bot"
		git config user.email "codex@users.noreply.github.com"
		git add "${memory_root}/runs/force_tick/${tracking_issue}.json"
		if git diff --cached --quiet; then
			:
		else
			git commit --quiet -m "ai-memory: update force tick #${tracking_issue}"
			git push origin "${memory_branch}" >/dev/null 2>&1
		fi
	) || {
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-put failed to commit/push (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	}

	local stored_wrapper=""
	if ! stored_wrapper="$(python3 - <<'PY' "${target_path}"
import json
import pathlib
import sys

record = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps({"ok": True, "enabled": True, "stored": True, "record": record}))
PY
		)"; then
		rm -rf "${tmp_dir}"
		_memory_warn "force-tick-put could not decode ${target_path} after push (fail-open)"
		_memory_telemetry '{"op":"force-tick-put","ok":false,"fail_open":true,"source":"shell"}' >&2
		echo '{"ok": false, "enabled": true, "stored": false, "record": null}'
		return 0
	fi

	printf '%s\n' "${stored_wrapper}"
	rm -rf "${tmp_dir}"
}
