#!/usr/bin/env bash

if [ "${_CODEX_HELPERS_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_CODEX_HELPERS_LOADED="true"

_codex_helpers_resolve_scripts_dir()
{
	local configured_dir="${1:-${CODEX_HELPERS_SCRIPTS_DIR:-}}"
	local workspace_root=""

	if [ -z "${configured_dir}" ]; then
		(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
		return 0
	fi

	case "${configured_dir}" in
		/*)
			printf '%s\n' "${configured_dir}"
			;;
		*)
			workspace_root="${GITHUB_WORKSPACE:-$(pwd)}"
			printf '%s\n' "${workspace_root}/${configured_dir}"
			;;
	esac
}

_codex_helpers_resolve_writer_path()
{
	local scripts_dir="${1:-}"
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${scripts_dir}")"
	printf '%s\n' "${scripts_dir}/write_codex_config.sh"
}

_codex_helpers_resolve_catalog_path()
{
	local scripts_dir="${1:-}"
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${scripts_dir}")"
	printf '%s\n' "${scripts_dir}/codex_model_catalog.json"
}

codex_config_assemble()
{
	local model="${1:-}"
	local reasoning="${2:-}"
	local verbosity="${3:-low}"
	local scripts_dir_override=""
	local writer_path=""
	local catalog_path=""
	local web_search="live"
	local project_path=""
	local config_path=""
	local allow_elevation=""
	local provider_base_url=""
	local writer_args=()

	if [ $# -ge 3 ]; then
		shift 3
	else
		shift "$#"
	fi

	while [ $# -gt 0 ]; do
		case "$1" in
			--scripts-dir|--web-search|--catalog-path|--project-path|--config-path|--allow-elevation|--provider-base-url)
				if [ $# -lt 2 ]; then
					echo "::error::codex_config_assemble: option $1 requires an argument" >&2
					return 2
				fi
				;;
		esac
		case "$1" in
			--scripts-dir)
				scripts_dir_override="${2:-}"
				shift 2
				;;
			--web-search)
				web_search="${2:-}"
				shift 2
				;;
			--catalog-path)
				catalog_path="${2:-}"
				shift 2
				;;
			--project-path)
				project_path="${2:-}"
				shift 2
				;;
			--config-path)
				config_path="${2:-}"
				shift 2
				;;
			--allow-elevation)
				allow_elevation="${2:-}"
				shift 2
				;;
			--provider-base-url)
				provider_base_url="${2:-}"
				shift 2
				;;
			*)
				echo "::error::codex_config_assemble: unknown argument: $1" >&2
				return 2
				;;
		esac
	done

	writer_path="$(_codex_helpers_resolve_writer_path "${scripts_dir_override}")"
	if [ -z "${catalog_path}" ]; then
		catalog_path="$(_codex_helpers_resolve_catalog_path "${scripts_dir_override}")"
	fi
	if [ ! -r "${writer_path}" ]; then
		echo "::error::codex_config_assemble: missing writer helper ${writer_path}" >&2
		return 1
	fi

	CODEX_HOME="${HOME:-/root}/.codex"
	export CODEX_HOME
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf 'CODEX_HOME=%s\n' "${CODEX_HOME}" >> "${GITHUB_ENV}"
	fi

	# Keep the third positional parameter for the approved call-site
	# interface; the shared writer already emits model_verbosity.
	: "${verbosity}"

	writer_args=(
		--model "${model}"
		--reasoning "${reasoning}"
		--web-search "${web_search}"
		--catalog-path "${catalog_path}"
	)
	if [ -n "${project_path}" ]; then
		writer_args+=(--project-path "${project_path}")
	fi
	if [ -n "${config_path}" ]; then
		writer_args+=(--config-path "${config_path}")
	fi
	if [ -n "${allow_elevation}" ]; then
		writer_args+=(--allow-elevation "${allow_elevation}")
	fi
	if [ -n "${provider_base_url}" ]; then
		writer_args+=(--provider-base-url "${provider_base_url}")
	fi

	bash "${writer_path}" "${writer_args[@]}"
}

model_provider_broker_start()
{
	local scripts_dir="" broker_path="" ready_file="" pid_file="" broker_pid="" broker_runtime_dir=""
	local ready_deadline=0 ready_json="" broker_allowed_models_csv="" broker_model=""
	local -a broker_policy_args=() broker_allowed_models=()
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${CODEX_HELPERS_SCRIPTS_DIR:-}")"
	broker_path="${scripts_dir}/model_provider_broker.py"
	ready_file="${MODEL_PROVIDER_BROKER_READY_FILE:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/model-provider-broker-ready.json}"
	pid_file="${MODEL_PROVIDER_BROKER_PID_FILE:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/model-provider-broker.pid}"
	broker_runtime_dir="$(dirname -- "${ready_file}")"
	if [ ! -r "${broker_path}" ] || [ -z "${OPENROUTER_API_KEY:-}" ]; then
		echo "::error::model provider broker prerequisites are unavailable" >&2
		return 1
	fi
	broker_allowed_models_csv="${MODEL_PROVIDER_BROKER_ALLOWED_MODELS:-${MODEL_EDITOR:-}}"
	IFS=',' read -r -a broker_allowed_models <<< "${broker_allowed_models_csv}"
	for broker_model in "${broker_allowed_models[@]}"; do
		if ! [[ "${broker_model}" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._:-]+$ ]]; then
			echo "::error::model provider broker allowed-model policy is missing or invalid" >&2
			return 1
		fi
		broker_policy_args+=(--allowed-model "${broker_model}")
	done
	if [ "${#broker_policy_args[@]}" -eq 0 ]; then
		echo "::error::model provider broker allowed-model policy is missing or invalid" >&2
		return 1
	fi
	rm -f -- "${ready_file}" "${pid_file}"
	umask 077
	MODEL_PROVIDER_BROKER_PID_FILE="${pid_file}"
	MODEL_PROVIDER_BROKER_READY_FILE="${ready_file}"
	MODEL_PROVIDER_BROKER_AGENT_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME:-${broker_runtime_dir}/model-provider-agent-home}"
	mkdir -p "${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" "${MODEL_PROVIDER_BROKER_AGENT_HOME}/.cache"
	chmod 0700 "${MODEL_PROVIDER_BROKER_AGENT_HOME}"
	export MODEL_PROVIDER_BROKER_PID_FILE MODEL_PROVIDER_BROKER_READY_FILE MODEL_PROVIDER_BROKER_AGENT_HOME
	env -i PATH="${PATH}" HOME="${HOME:-/root}" PYTHONDONTWRITEBYTECODE=1 \
		OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" \
		python3 "${broker_path}" --ready-file "${ready_file}" \
		--max-requests "${MODEL_PROVIDER_BROKER_MAX_REQUESTS:-100}" \
		--max-output-tokens "${MODEL_PROVIDER_BROKER_MAX_OUTPUT_TOKENS:-16384}" \
		--max-total-output-tokens "${MODEL_PROVIDER_BROKER_MAX_TOTAL_OUTPUT_TOKENS:-1638400}" \
		--max-prompt-price "${MODEL_PROVIDER_BROKER_MAX_PROMPT_PRICE:-10}" \
		--max-completion-price "${MODEL_PROVIDER_BROKER_MAX_COMPLETION_PRICE:-30}" \
		--max-request-price "${MODEL_PROVIDER_BROKER_MAX_REQUEST_PRICE:-0.10}" \
		--max-image-price "${MODEL_PROVIDER_BROKER_MAX_IMAGE_PRICE:-1}" \
		"${broker_policy_args[@]}" &
	broker_pid=$!
	printf '%s\n' "${broker_pid}" > "${pid_file}"
	chmod 0600 "${pid_file}"
	ready_deadline=$((SECONDS + ${MODEL_PROVIDER_BROKER_READY_TIMEOUT_SECONDS:-10}))
	while [ "${SECONDS}" -lt "${ready_deadline}" ]; do
		if ! kill -0 "${broker_pid}" 2>/dev/null; then
			wait "${broker_pid}" 2>/dev/null || true
			echo "::error::model provider broker exited before readiness" >&2
			return 1
		fi
		if [ -s "${ready_file}" ]; then
			ready_json="$(cat "${ready_file}")"
			MODEL_PROVIDER_BROKER_BASE_URL="$(printf '%s' "${ready_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["base_url"])')" || return 1
			MODEL_PROVIDER_BROKER_TOKEN="$(printf '%s' "${ready_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')" || return 1
			export MODEL_PROVIDER_BROKER_BASE_URL MODEL_PROVIDER_BROKER_TOKEN
			return 0
		fi
		sleep 0.1
	done
	model_provider_broker_stop || true
	echo "::error::model provider broker readiness timed out" >&2
	return 1
}

model_provider_broker_stop()
{
	local broker_pid=""
	if [ -s "${MODEL_PROVIDER_BROKER_ACL_BACKUP:-/nonexistent}" ] && command -v setfacl >/dev/null 2>&1; then
		setfacl --restore="${MODEL_PROVIDER_BROKER_ACL_BACKUP}" >/dev/null 2>&1 || return 1
	fi
	if [ -n "${MODEL_PROVIDER_BROKER_PID_FILE:-}" ] && [ -r "${MODEL_PROVIDER_BROKER_PID_FILE}" ]; then
		broker_pid="$(cat "${MODEL_PROVIDER_BROKER_PID_FILE}")"
	fi
	if [[ "${broker_pid}" =~ ^[1-9][0-9]*$ ]] && kill -0 "${broker_pid}" 2>/dev/null; then
		kill -TERM "${broker_pid}" 2>/dev/null || return 1
		for _broker_wait in $(seq 1 50); do
			kill -0 "${broker_pid}" 2>/dev/null || break
			if [ -r "/proc/${broker_pid}/stat" ] && grep -q ') Z ' "/proc/${broker_pid}/stat"; then
				break
			fi
			/bin/sleep 0.1
		done
		if kill -0 "${broker_pid}" 2>/dev/null \
			&& { [ ! -r "/proc/${broker_pid}/stat" ] || ! grep -q ') Z ' "/proc/${broker_pid}/stat"; }; then
			echo "::error::model provider broker did not stop cleanly" >&2
			return 1
		fi
		wait "${broker_pid}" 2>/dev/null || true
	fi
	[ -z "${MODEL_PROVIDER_BROKER_PID_FILE:-}" ] || rm -f -- "${MODEL_PROVIDER_BROKER_PID_FILE}"
	[ -z "${MODEL_PROVIDER_BROKER_READY_FILE:-}" ] || rm -f -- "${MODEL_PROVIDER_BROKER_READY_FILE}"
	if [ -n "${MODEL_PROVIDER_BROKER_AGENT_HOME:-}" ] && [ -d "${MODEL_PROVIDER_BROKER_AGENT_HOME}" ] \
		&& [ "$(stat -c %u "${MODEL_PROVIDER_BROKER_AGENT_HOME}")" != "$(id -u)" ]; then
		sudo -n chown -R "$(id -u):$(id -g)" "${MODEL_PROVIDER_BROKER_AGENT_HOME}" || return 1
	fi
	unset MODEL_PROVIDER_BROKER_BASE_URL MODEL_PROVIDER_BROKER_TOKEN MODEL_PROVIDER_BROKER_PID_FILE MODEL_PROVIDER_BROKER_READY_FILE MODEL_PROVIDER_BROKER_AGENT_HOME MODEL_PROVIDER_BROKER_ACL_BACKUP MODEL_PROVIDER_BROKER_ISOLATION_USER
	unset MODEL_PROVIDER_BROKER_ACL_CAPTURED
}

_model_provider_broker_capture_acl()
{
	local capture_path="${1:?capture path required}" recursive="${2:-false}"
	if ! declare -p MODEL_PROVIDER_BROKER_ACL_CAPTURED >/dev/null 2>&1; then
		declare -g -A MODEL_PROVIDER_BROKER_ACL_CAPTURED=()
	fi
	if [ -n "${MODEL_PROVIDER_BROKER_ACL_CAPTURED[${capture_path}]:-}" ]; then
		return 0
	fi
	if [ "${recursive}" = "true" ]; then
		getfacl -R -p "${capture_path}" >> "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
	else
		getfacl -p "${capture_path}" >> "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
	fi
	MODEL_PROVIDER_BROKER_ACL_CAPTURED["${capture_path}"]=1
}

model_provider_broker_grant_read_path()
{
	local read_path="${1:?read path required}" isolation_user="${2:-${MODEL_PROVIDER_BROKER_ISOLATION_USER:-nobody}}"
	local resolved_path="" parent_path=""
	if ! command -v getfacl >/dev/null 2>&1 || ! command -v setfacl >/dev/null 2>&1; then
		echo "::error::getfacl/setfacl are required for unprivileged model execution" >&2
		return 1
	fi
	resolved_path="$(realpath -e -- "${read_path}")" || return 1
	MODEL_PROVIDER_BROKER_ACL_BACKUP="${MODEL_PROVIDER_BROKER_ACL_BACKUP:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/model-provider-broker-access.acl}"
	if [ ! -e "${MODEL_PROVIDER_BROKER_ACL_BACKUP}" ]; then
		: > "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
		chmod 0600 "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
	fi
	_model_provider_broker_capture_acl "${resolved_path}" true
	setfacl -R -m "u:${isolation_user}:rX" "${resolved_path}"
	parent_path="$(dirname -- "${resolved_path}")"
	while [ "${parent_path}" != "/" ] && [ "${parent_path}" != "." ]; do
		if sudo -n -u "${isolation_user}" -- test -x "${parent_path}"; then
			parent_path="$(dirname -- "${parent_path}")"
			continue
		fi
		_model_provider_broker_capture_acl "${parent_path}" false
		setfacl -m "u:${isolation_user}:x" "${parent_path}"
		parent_path="$(dirname -- "${parent_path}")"
	done
	export MODEL_PROVIDER_BROKER_ACL_BACKUP
}

model_provider_broker_prepare_codex_writer()
{
	local model="${1:?model required}" reasoning="${2:?reasoning required}"
	local project_path="${3:-$(pwd)}" scripts_dir="" writer_path="" catalog_path="" source_config=""
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${CODEX_HELPERS_SCRIPTS_DIR:-}")"
	writer_path="${scripts_dir}/write_codex_config.sh"
	catalog_path="${scripts_dir}/codex_model_catalog.json"
	source_config="${CODEX_HOME:-${HOME:-/root}/.codex}/config.toml"
	CODEX_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME:?MODEL_PROVIDER_BROKER_AGENT_HOME is required}/.codex"
	mkdir -p "${CODEX_HOME}"
	if [ -r "${source_config}" ]; then
		if [ "${source_config}" != "${CODEX_HOME}/config.toml" ]; then
			install -m 0600 "${source_config}" "${CODEX_HOME}/config.toml"
		fi
		PYTHONDONTWRITEBYTECODE=1 python3 - \
			"${CODEX_HOME}/config.toml" \
			"${MODEL_PROVIDER_BROKER_BASE_URL:?broker base URL required}" \
			"${reasoning}" <<'PY'
import os
import re
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
provider_base_url = sys.argv[2]
reasoning = sys.argv[3]
lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
provider_section = False
provider_replaced = False
reasoning_replaced = False
first_table = len(lines)
for index, line in enumerate(lines):
	stripped = line.strip()
	if stripped.startswith("["):
		first_table = min(first_table, index)
		provider_section = stripped == "[model_providers.openrouter]"
		continue
	if index < first_table and re.match(r"^\s*model_reasoning_effort\s*=", line):
		lines[index] = f'model_reasoning_effort = "{reasoning}"\n'
		reasoning_replaced = True
	elif provider_section and re.match(r"^\s*base_url\s*=", line):
		lines[index] = f'base_url = "{provider_base_url}"\n'
		provider_replaced = True
if not provider_replaced:
	raise SystemExit("model provider broker config rewrite found no openrouter base_url")
if not reasoning_replaced:
	lines.insert(first_table, f'model_reasoning_effort = "{reasoning}"\n')
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=config_path.parent)
try:
	os.fchmod(descriptor, 0o600)
	with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
		handle.writelines(lines)
		handle.flush()
		os.fsync(handle.fileno())
	os.replace(temporary_name, config_path)
except BaseException:
	try:
		os.unlink(temporary_name)
	except OSError:
		pass
	raise
PY
	else
		bash "${writer_path}" \
			--model "${model}" \
			--reasoning "${reasoning}" \
			--web-search disabled \
			--catalog-path "${catalog_path}" \
			--project-path "${project_path}" \
			--config-path "${CODEX_HOME}/config.toml" \
			--allow-elevation force \
			--provider-base-url "${MODEL_PROVIDER_BROKER_BASE_URL:?broker base URL required}"
	fi
	export CODEX_HOME
}

model_provider_broker_prepare_codex_readonly()
{
	local isolation_user="${1:?isolation user required}" model="${2:?model required}" reasoning="${3:?reasoning required}"
	local project_path="${4:-$(pwd)}" scripts_dir="" writer_path="" catalog_path="" codex_binary="" git_directory=""
	scripts_dir="$(_codex_helpers_resolve_scripts_dir "${CODEX_HELPERS_SCRIPTS_DIR:-}")"
	writer_path="${scripts_dir}/write_codex_config.sh"
	catalog_path="${scripts_dir}/codex_model_catalog.json"
	CODEX_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME:?MODEL_PROVIDER_BROKER_AGENT_HOME is required}/.codex"
	mkdir -p "${CODEX_HOME}"
	bash "${writer_path}" \
		--model "${model}" \
		--reasoning "${reasoning}" \
		--web-search disabled \
		--catalog-path "${catalog_path}" \
		--project-path "${project_path}" \
		--config-path "${CODEX_HOME}/config.toml" \
		--allow-elevation forbid \
		--provider-base-url "${MODEL_PROVIDER_BROKER_BASE_URL:?broker base URL required}"
	chmod 0700 "${MODEL_PROVIDER_BROKER_AGENT_HOME}"
	sudo -n chown -R "${isolation_user}" "${MODEL_PROVIDER_BROKER_AGENT_HOME}"
	MODEL_PROVIDER_BROKER_ACL_BACKUP="${MODEL_PROVIDER_BROKER_ACL_BACKUP:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/model-provider-broker-access.acl}"
	if [ ! -e "${MODEL_PROVIDER_BROKER_ACL_BACKUP}" ]; then
		: > "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
		chmod 0600 "${MODEL_PROVIDER_BROKER_ACL_BACKUP}"
	fi
	export MODEL_PROVIDER_BROKER_ACL_BACKUP
	git_directory="$(git -C "${project_path}" rev-parse --absolute-git-dir 2>/dev/null || true)"
	if [ -n "${git_directory}" ] && [ -d "${git_directory}" ]; then
		_model_provider_broker_capture_acl "${git_directory}" true
	fi
	model_provider_broker_grant_read_path "${project_path}" "${isolation_user}"
	if [ -n "${git_directory}" ] && [ -d "${git_directory}" ]; then
		setfacl -R -b "${git_directory}"
		chmod -R go-rwx "${git_directory}"
	fi
	codex_binary="$(command -v codex 2>/dev/null || true)"
	if [ -z "${codex_binary}" ]; then
		echo "::error::codex executable is unavailable" >&2
		return 1
	fi
	model_provider_broker_grant_read_path "${codex_binary}" "${isolation_user}"
	MODEL_PROVIDER_BROKER_ISOLATION_USER="${isolation_user}"
	export CODEX_HOME MODEL_PROVIDER_BROKER_ISOLATION_USER
}

model_provider_broker_exec_sanitized()
{
	if [ -z "${MODEL_PROVIDER_BROKER_TOKEN:-}" ] || [ -z "${MODEL_PROVIDER_BROKER_BASE_URL:-}" ]; then
		echo "::error::model provider broker is not ready; refusing direct-provider fallback" >&2
		return 1
	fi
	env -i \
		HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME:?MODEL_PROVIDER_BROKER_AGENT_HOME is required}" \
		PATH="${PATH}" \
		CODEX_HOME="${CODEX_HOME:-${HOME:-/root}/.codex}" \
		XDG_CACHE_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME}/.cache" \
		TMPDIR="${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" \
		LANG="${LANG:-C.UTF-8}" \
		LC_ALL="${LC_ALL:-C.UTF-8}" \
		NO_PROXY="127.0.0.1,localhost" \
		GITHUB_ACTIONS="${GITHUB_ACTIONS:-}" \
		GITHUB_RUN_ID="${GITHUB_RUN_ID:-}" \
		GITHUB_RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-}" \
		GITHUB_WORKSPACE="${GITHUB_WORKSPACE:-$(pwd)}" \
		GIT_DIR="${GIT_DIR:-.git}" \
		GIT_WORK_TREE="${GIT_WORK_TREE:-$(pwd)}" \
		RUNNER_TEMP="${RUNNER_TEMP:-/tmp}" \
		ISSUE_NUMBER="${ISSUE_NUMBER:-}" \
		PR_NUMBER="${PR_NUMBER:-}" \
		TRACKING_ISSUE="${TRACKING_ISSUE:-}" \
		TRACKING_ISSUE_NUM="${TRACKING_ISSUE_NUM:-}" \
		CODEX_HEARTBEAT_ENABLED="${CODEX_HEARTBEAT_ENABLED:-1}" \
		CODEX_HEARTBEAT_INTERVAL_SECS="${CODEX_HEARTBEAT_INTERVAL_SECS:-30}" \
		CODEX_STALL_GUARD_ENABLED="${CODEX_STALL_GUARD_ENABLED:-false}" \
		CODEX_STALL_TIMEOUT_SECONDS="${CODEX_STALL_TIMEOUT_SECONDS:-600}" \
		CODEX_STALL_KILL_GRACE_SECONDS="${CODEX_STALL_KILL_GRACE_SECONDS:-30}" \
		CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}" \
		CODEX_THREAD_REUSE_RUNTIME_DIR="${CODEX_THREAD_REUSE_RUNTIME_DIR:-}" \
		CODEX_THREAD_REUSE_REAL_CODEX="${CODEX_THREAD_REUSE_REAL_CODEX:-}" \
		CODEX_THREAD_REUSE_WRAPPER_DIR="${CODEX_THREAD_REUSE_WRAPPER_DIR:-}" \
		CODEX_THREAD_REUSE_STATE_KEY="${CODEX_THREAD_REUSE_STATE_KEY:-}" \
		CODEX_THREAD_REUSE_PROMPT_FILE="${CODEX_THREAD_REUSE_PROMPT_FILE:-}" \
		CODEX_THREAD_REUSE_OUTPUT_FILE="${CODEX_THREAD_REUSE_OUTPUT_FILE:-}" \
		CODEX_THREAD_REUSE_PHASE="${CODEX_THREAD_REUSE_PHASE:-}" \
		CODEX_THREAD_REUSE_MODEL="${CODEX_THREAD_REUSE_MODEL:-}" \
		CODEX_THREAD_REUSE_SANDBOX="${CODEX_THREAD_REUSE_SANDBOX:-}" \
		CODEX_THREAD_REUSE_LOG_FILE="${CODEX_THREAD_REUSE_LOG_FILE:-}" \
		CODEX_THREAD_REUSE_CUMULATIVE_LOG_FILE="${CODEX_THREAD_REUSE_CUMULATIVE_LOG_FILE:-}" \
		CODEX_THREAD_REUSE_STATUS_FILE="${CODEX_THREAD_REUSE_STATUS_FILE:-}" \
		CODEX_THREAD_REUSE_STALL_GUARD_HELPER="${CODEX_THREAD_REUSE_STALL_GUARD_HELPER:-}" \
		CODEX_THREAD_REUSE_HEARTBEAT_HELPER="${CODEX_THREAD_REUSE_HEARTBEAT_HELPER:-}" \
		CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK="${CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK:-true}" \
		CODEX_THREAD_REUSE_TIMEOUT_SECS="${CODEX_THREAD_REUSE_TIMEOUT_SECS:-}" \
		CODEX_THREAD_REUSE_CONTINUATION_FILE="${CODEX_THREAD_REUSE_CONTINUATION_FILE:-}" \
		CODEX_THREAD_REUSE_TRANSFORM_MODE="${CODEX_THREAD_REUSE_TRANSFORM_MODE:-none}" \
		CODEX_THREAD_REUSE_MARKER_START="${CODEX_THREAD_REUSE_MARKER_START:-}" \
		CODEX_THREAD_REUSE_MARKER_END="${CODEX_THREAD_REUSE_MARKER_END:-}" \
		OPENROUTER_API_KEY="${MODEL_PROVIDER_BROKER_TOKEN}" \
		MODEL_PROVIDER_BROKER_BASE_URL="${MODEL_PROVIDER_BROKER_BASE_URL}" \
		MODEL_PROVIDER_BROKER_TOKEN="${MODEL_PROVIDER_BROKER_TOKEN}" \
		MODEL_PROVIDER_BROKER_AGENT_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME}" \
		MODEL_PROVIDER_BROKER_ISOLATION_USER="${MODEL_PROVIDER_BROKER_ISOLATION_USER:-nobody}" \
		"$@"
}

model_provider_broker_exec_unprivileged()
{
	local isolation_user="${1:?isolation user required}"
	shift
	if [ -z "${MODEL_PROVIDER_BROKER_TOKEN:-}" ] || [ -z "${MODEL_PROVIDER_BROKER_BASE_URL:-}" ]; then
		echo "::error::model provider broker is not ready; refusing unprivileged direct-provider fallback" >&2
		return 1
	fi
	sudo -n -u "${isolation_user}" -- env -i \
		HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME:?MODEL_PROVIDER_BROKER_AGENT_HOME is required}" \
		PATH="${PATH}" \
		XDG_CACHE_HOME="${MODEL_PROVIDER_BROKER_AGENT_HOME}/.cache" \
		CODEX_HOME="${CODEX_HOME:-${MODEL_PROVIDER_BROKER_AGENT_HOME}/.codex}" \
		TMPDIR="${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" \
		LANG="${LANG:-C.UTF-8}" \
		LC_ALL="${LC_ALL:-C.UTF-8}" \
		NO_PROXY="127.0.0.1,localhost" \
		OPENROUTER_API_KEY="${MODEL_PROVIDER_BROKER_TOKEN}" \
		"$@"
}
