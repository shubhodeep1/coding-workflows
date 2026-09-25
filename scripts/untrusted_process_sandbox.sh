#!/usr/bin/env bash
# Run a model process without runner credentials and with provider-only network.
set -euo pipefail

role=""
workspace=""
config_format=""
config_path=""
runtime_dir="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}"
writable_output_dir=""
guard_action=""
host_home="${HOME:-/nonexistent}"
runner_command_files="${RUNNER_TEMP:-/tmp}/_runner_file_commands"
while [ "$#" -gt 0 ]; do
	case "$1" in
		--role) role="${2:-}"; shift 2 ;;
		--workspace) workspace="${2:-}"; shift 2 ;;
		--config-format) config_format="${2:-}"; shift 2 ;;
		--config) config_path="${2:-}"; shift 2 ;;
		--runtime-dir) runtime_dir="${2:-}"; shift 2 ;;
		--writable-output-dir) writable_output_dir="${2:-}"; shift 2 ;;
		--guard-action) guard_action="${2:-}"; shift 2 ;;
		--) shift; break ;;
		*) echo "untrusted_process_sandbox: unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ "$#" -gt 0 ] || { echo "untrusted_process_sandbox: command is required" >&2; exit 2; }
case "${role}" in
	plan|implement|implement-repair|diagnose|reviewer|editor|resolver|judge|judge-fix|summary|audit) ;;
	validator|workspace-guard) ;;
	*) echo "untrusted_process_sandbox: invalid role" >&2; exit 2 ;;
esac
workspace="$(cd "${workspace}" && pwd -P)"
provider_required=true
case "${role}" in
	validator|workspace-guard)
		provider_required=false
		[ "${1:-}" = /usr/bin/python3 ] && [ "${2:-}" = -I ] && [ "${3:-}" = -S ] || {
			echo "untrusted_process_sandbox: isolated roles require /usr/bin/python3 -I -S" >&2; exit 2;
		}
		[ -z "${config_format}" ] && [ -z "${config_path}" ] || exit 2
		;;
	*)
		case "${config_format}" in opencode|codex) ;; *) echo "untrusted_process_sandbox: invalid config format" >&2; exit 2 ;; esac
		[ -r "${config_path}" ] || { echo "untrusted_process_sandbox: config is unreadable" >&2; exit 2; }
		[ -z "${guard_action}" ] && [ -z "${writable_output_dir}" ] || exit 2
		;;
esac
if [ "${provider_required}" = false ]; then
	[ -n "${RUNNER_TEMP:-}" ] && [ -d "${RUNNER_TEMP}" ] || { echo "untrusted_process_sandbox: RUNNER_TEMP is required" >&2; exit 1; }
	runner_artifact_root="$(cd "${RUNNER_TEMP}" && pwd -P)"
	[ -d "${runtime_dir}" ] && [ ! -L "${runtime_dir}" ] || { echo "untrusted_process_sandbox: artifact directory is unavailable" >&2; exit 1; }
	runtime_dir="$(cd "${runtime_dir}" && pwd -P)"
	if [ "${UNTRUSTED_PROCESS_SANDBOX_TEST_MODE:-}" != 1 ]; then
		case "${runner_artifact_root}/" in /tmp/*|/var/tmp/*) echo "untrusted_process_sandbox: unit-visible RUNNER_TEMP is required" >&2; exit 1 ;; esac
	fi
	case "${runtime_dir}/" in "${runner_artifact_root}/"*) ;; *) echo "untrusted_process_sandbox: artifact directory must be under RUNNER_TEMP" >&2; exit 1 ;; esac
	case "${runtime_dir}/" in "${workspace}/"*|"${workspace}/") echo "untrusted_process_sandbox: artifact directory overlaps workspace" >&2; exit 1 ;; esac
	[ "$(stat -c %a "${runtime_dir}")" = 700 ] || { echo "untrusted_process_sandbox: artifact directory must be mode 0700" >&2; exit 1; }
	if [ "${role}" = workspace-guard ]; then
		case "${guard_action}" in snapshot|reconcile) ;; *) echo "untrusted_process_sandbox: guard action is required" >&2; exit 2 ;; esac
		[[ "${4:-}" == */post_agent_workspace_guard.py ]] && [ "${5:-}" = "${guard_action}" ] || exit 2
	else
		[ -z "${guard_action}" ] || exit 2
	fi
	if [ -n "${writable_output_dir}" ]; then
		[ "${role}" = validator ] && [ -d "${writable_output_dir}" ] && [ ! -L "${writable_output_dir}" ] || exit 2
		writable_output_dir="$(cd "${writable_output_dir}" && pwd -P)"
		case "${writable_output_dir}/" in "${runtime_dir}/"*) ;; *) echo "untrusted_process_sandbox: validator output must be inside artifact directory" >&2; exit 2 ;; esac
	fi
fi

# The unit below runs with PrivateTmp=yes, which gives it fresh, empty /tmp and
# /var/tmp.  A ReadWritePaths=/InaccessiblePaths= entry under the host's /tmp
# or /var/tmp therefore does not exist inside the unit, and systemd aborts
# namespace setup with status 226 before the command starts (every reviewer,
# the summariser, and the editor on PR #4323).  Every pipeline's RUNTIME_DIR
# lives under /tmp, so the sandbox's own state is kept under RUNNER_TEMP.
sandbox_path_is_private_tmp()
{
	case "${1%/}/" in
		/tmp/*|/var/tmp/*) return 0 ;;
	esac
	return 1
}
sandbox_state_root="$(cd "${runtime_dir}" && pwd -P)"
if sandbox_path_is_private_tmp "${sandbox_state_root}" && [ -n "${RUNNER_TEMP:-}" ] && [ -d "${RUNNER_TEMP}" ]; then
	sandbox_state_root="$(cd "${RUNNER_TEMP}" && pwd -P)"
fi

credential_file="${MODEL_PROVIDER_CREDENTIAL_FILE:-}"
credential_file_is_temporary=false
if [ "${provider_required}" = true ] && [ -z "${credential_file}" ] && [ -n "${OPENROUTER_API_KEY:-}" ]; then
	credential_file="$(mktemp "${sandbox_state_root%/}/provider-credential.XXXXXX")"
	credential_file_is_temporary=true
	chmod 600 "${credential_file}"
	printf '%s' "${OPENROUTER_API_KEY}" > "${credential_file}"
fi
if [ "${provider_required}" = true ]; then
	[ -r "${credential_file}" ] || { echo "untrusted_process_sandbox: provider credential is unavailable" >&2; exit 1; }
else
	credential_file=""
fi

sandbox_dir="$(mktemp -d "${sandbox_state_root%/}/agent-sandbox.XXXXXX")"
ready_file="${sandbox_dir}/proxy-ready.json"
proxy_log="${sandbox_dir}/proxy.log"
proxy_policy_file="${sandbox_dir}/proxy-policy.json"
proxy_pid=""
cleanup()
{
	if [ -n "${proxy_pid}" ]; then
		kill "${proxy_pid}" 2>/dev/null || true
		wait "${proxy_pid}" 2>/dev/null || true
	fi
	if [ "${credential_file_is_temporary}" = true ]; then
		rm -f -- "${credential_file}"
	fi
	rm -rf -- "${sandbox_dir}"
}
trap cleanup EXIT HUP INT TERM
if [ "${provider_required}" = false ]; then
	if [ "${role}" = workspace-guard ]; then
		guard_output_option=""
		for command_value in "$@"; do
			if [ -n "${guard_output_option}" ]; then
				case "${command_value}" in "${runtime_dir}/"*) ;; *) echo "untrusted_process_sandbox: guard artifact is outside run root" >&2; exit 1 ;; esac
				[ "$(realpath -m -- "${command_value}")" = "${command_value}" ] || { echo "untrusted_process_sandbox: guard artifact path is not canonical" >&2; exit 1; }
				[ ! -L "${command_value}" ] && [ ! -L "$(dirname "${command_value}")" ] || exit 1
				guard_output_option=""
				continue
			fi
			case "${command_value}" in --manifest|--report|--changed-paths-out|--quarantine-dir) guard_output_option="${command_value}" ;; esac
		done
		[ -z "${guard_output_option}" ] || exit 2
	fi
	command_args=("$@")
	mkdir -m 700 "${sandbox_dir}/inputs"
	for command_index in "${!command_args[@]}"; do
		command_value="${command_args[${command_index}]}"
		command_prefix=""
		case "${command_value}" in
			--*=/*) command_prefix="${command_value%%=*}="; command_value="${command_value#*=}" ;;
		esac
		case "${command_value}" in "${runtime_dir}/"*) continue ;; esac
		if sandbox_path_is_private_tmp "${command_value}"; then
			[ -f "${command_value}" ] && [ ! -L "${command_value}" ] || {
				echo "untrusted_process_sandbox: private-tmp validator input is not a regular file" >&2; exit 1;
			}
			command_input="${sandbox_dir}/inputs/${command_index}"
			install -m 0400 -- "${command_value}" "${command_input}"
			command_args[${command_index}]="${command_prefix}${command_input}"
		fi
	done
	set -- "${command_args[@]}"
fi

proxy_host="127.0.0.1"
proxy_port="0"
sandbox_config="${sandbox_dir}/agent-config"
if [ "${provider_required}" = true ]; then
proxy_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/model_provider_proxy.py"
[ -r "${proxy_script}" ] || { echo "untrusted_process_sandbox: provider proxy is unavailable" >&2; exit 1; }
python3 - "${config_format}" "${config_path}" "${proxy_policy_file}" "$@" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

config_format, config_path, output_path, *command = sys.argv[1:]
models: set[str] = set()
if config_format == "opencode":
	payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
	provider_models = payload.get("provider", {}).get("openrouter", {}).get("models", {})
	if isinstance(provider_models, dict):
		models.update(key for key in provider_models if isinstance(key, str))
	for key in ("model", "small_model"):
		value = payload.get(key)
		if isinstance(value, str):
			models.add(value.removeprefix("openrouter/"))
else:
	config_file = Path(config_path) / "config.toml"
	payload = tomllib.loads(config_file.read_text(encoding="utf-8"))
	value = payload.get("model")
	if isinstance(value, str):
		models.add(value.removeprefix("openrouter/"))
for index, value in enumerate(command):
	if value in {"--model", "-m"} and index + 1 < len(command):
		models.add(command[index + 1].removeprefix("openrouter/"))
	elif value.startswith("--model=") or value.startswith("-m="):
		models.add(value.split("=", 1)[1].removeprefix("openrouter/"))
pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*(?:/[A-Za-z0-9][A-Za-z0-9._:+-]*)+$")
models = {model for model in models if pattern.fullmatch(model)}
if not models:
	raise SystemExit("trusted model configuration produced an empty allowlist")
Path(output_path).write_text(json.dumps({"models": sorted(models)}) + "\n", encoding="utf-8")
PY
mapfile -t proxy_allowed_models < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["models"]))' "${proxy_policy_file}")
proxy_args=()
for proxy_allowed_model in "${proxy_allowed_models[@]}"; do
	proxy_args+=(--allowed-model "${proxy_allowed_model}")
done
if [ "${UNTRUSTED_PROCESS_SANDBOX_TEST_MODE:-}" = 1 ]; then
	proxy_host="127.0.0.2"
	proxy_port="1"
else
	PYTHONDONTWRITEBYTECODE=1 python3 "${proxy_script}" \
		--credential-file "${credential_file}" \
		--ready-file "${ready_file}" \
		--max-requests "${MODEL_PROVIDER_PROXY_MAX_REQUESTS:-64}" \
		--max-concurrency "${MODEL_PROVIDER_PROXY_MAX_CONCURRENCY:-1}" \
		--max-output-tokens "${MODEL_PROVIDER_PROXY_MAX_OUTPUT_TOKENS:-65536}" \
		--max-spend-usd "${MODEL_PROVIDER_PROXY_MAX_SPEND_USD:-25}" \
		--workers "${MODEL_PROVIDER_PROXY_WORKERS:-4}" \
		--queued-connections "${MODEL_PROVIDER_PROXY_QUEUED_CONNECTIONS:-8}" \
		--read-timeout-seconds "${MODEL_PROVIDER_PROXY_READ_TIMEOUT_SECONDS:-15}" \
		"${proxy_args[@]}" >"${proxy_log}" 2>&1 &
	proxy_pid=$!
	for _proxy_wait in $(seq 1 100); do
		[ -s "${ready_file}" ] && break
		kill -0 "${proxy_pid}" 2>/dev/null || { cat "${proxy_log}" >&2; exit 1; }
		sleep 0.05
	done
	[ -s "${ready_file}" ] || { echo "untrusted_process_sandbox: provider proxy did not become ready" >&2; exit 1; }
	proxy_host="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["host"])' "${ready_file}")"
	proxy_port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "${ready_file}")"
fi
proxy_url="http://${proxy_host}:${proxy_port}/api/v1"

sandbox_home="${sandbox_dir}/home"
sandbox_runtime="${sandbox_dir}/runtime"
mkdir -p "${sandbox_home}" "${sandbox_runtime}"
if [ "${config_format}" = opencode ]; then
	python3 - "${config_path}" "${sandbox_config}" "${proxy_url}" <<'PY'
import json, sys
source, destination, proxy_url = sys.argv[1:]
payload = json.load(open(source, encoding="utf-8"))
options = payload.setdefault("provider", {}).setdefault("openrouter", {}).setdefault("options", {})
options["baseURL"] = proxy_url
options["apiKey"] = "sandbox-proxy"
with open(destination, "w", encoding="utf-8") as handle:
	json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
	handle.write("\n")
PY
else
	mkdir -p "${sandbox_config}"
	[ -r "${config_path}/config.toml" ] \
		|| { echo "untrusted_process_sandbox: Codex config.toml is unavailable" >&2; exit 1; }
	install -m 0600 "${config_path}/config.toml" "${sandbox_config}/config.toml"
	python3 - "${sandbox_config}/config.toml" "${proxy_url}" "${sandbox_config}/model-catalog.json" <<'PY'
import re, shutil, sys
from pathlib import Path
path, proxy_url, sandbox_catalog = sys.argv[1:]
text = open(path, encoding="utf-8").read()
text = re.sub(r'(?m)^base_url\s*=\s*"[^"]*"', f'base_url = "{proxy_url}"', text)
text = re.sub(r'(?m)^env_key\s*=\s*"[^"]*"', 'env_key = "SANDBOX_PROVIDER_TOKEN"', text)
catalog_match = re.search(r'(?m)^model_catalog_json\s*=\s*"([^"]+)"', text)
if catalog_match is not None:
	catalog_source = Path(catalog_match.group(1))
	if not catalog_source.is_file():
		raise SystemExit("configured model catalog is unavailable")
	shutil.copyfile(catalog_source, sandbox_catalog)
	text = re.sub(
		r'(?m)^model_catalog_json\s*=\s*"[^"]+"',
		f'model_catalog_json = "{sandbox_catalog}"',
		text,
	)
open(path, "w", encoding="utf-8").write(text)
PY
fi
else
	sandbox_home="${sandbox_dir}/home"
	sandbox_runtime="${sandbox_dir}/runtime"
	mkdir -p "${sandbox_home}" "${sandbox_runtime}"
fi

safe_git_config="${sandbox_dir}/gitconfig"
: > "${safe_git_config}"
git_config_path=""
if git_config_path="$(git -C "${workspace}" rev-parse --git-path config 2>/dev/null)"; then
	case "${git_config_path}" in /*) ;; *) git_config_path="${workspace}/${git_config_path}" ;; esac
	if [ -r "${git_config_path}" ]; then
		cp "${git_config_path}" "${safe_git_config}"
		git config --file "${safe_git_config}" --remove-section credential 2>/dev/null || true
		while IFS= read -r extra_header_key; do
			[ -n "${extra_header_key}" ] && git config --file "${safe_git_config}" --unset-all "${extra_header_key}" || true
		done < <(git config --file "${safe_git_config}" --name-only --get-regexp '^http\..*\.extraheader$' 2>/dev/null || true)
		while IFS= read -r remote_name; do
			[ -n "${remote_name}" ] || continue
			git config --file "${safe_git_config}" "remote.${remote_name}.url" \
				"https://github.com/invalid/unauthenticated.git"
		done < <(git config --file "${safe_git_config}" --name-only --get-regexp '^remote\..*\.url$' 2>/dev/null \
			| sed 's/^remote\.\([^.]\+\)\.url$/\1/' || true)
	fi
fi

# Repository metadata is privileged state.  Writer roles need the ordinary
# worktree writable, but no model process may alter hooks, remotes, refs, the
# index, or credentials.  Cover the primary repository, linked-worktree
# indirection, and nested support checkouts rather than protecting only the
# current repository's config file.
git_metadata_paths=()
append_git_metadata_path()
{
	local candidate_path="${1:-}"
	[ -n "${candidate_path}" ] || return 0
	case "${candidate_path}" in
		/*) ;;
		*) candidate_path="${workspace}/${candidate_path}" ;;
	esac
	if [ -e "${candidate_path}" ]; then
		candidate_path="$(cd "$(dirname "${candidate_path}")" && pwd -P)/$(basename "${candidate_path}")"
	fi
	case "${candidate_path}" in
		*$'\n'*|*$'\r'*|*$'\t'*|*' '*)
			echo "untrusted_process_sandbox: Git metadata path cannot be represented safely" >&2
			exit 1
			;;
	esac
	for recorded_path in "${git_metadata_paths[@]:-}"; do
		[ "${recorded_path}" = "${candidate_path}" ] && return 0
	done
	git_metadata_paths+=("${candidate_path}")
}

for git_path_query in --absolute-git-dir --git-common-dir; do
	if discovered_git_path="$(git -C "${workspace}" rev-parse "${git_path_query}" 2>/dev/null)"; then
		append_git_metadata_path "${discovered_git_path}"
	fi
done
while IFS= read -r -d '' nested_git_entry; do
	append_git_metadata_path "${nested_git_entry}"
	if [ -f "${nested_git_entry}" ]; then
		nested_git_target="$(sed -n 's/^gitdir: //p' "${nested_git_entry}" | head -n1)"
		if [ -n "${nested_git_target}" ]; then
			case "${nested_git_target}" in
				/*) ;;
				*) nested_git_target="$(dirname "${nested_git_entry}")/${nested_git_target}" ;;
			esac
			append_git_metadata_path "${nested_git_target}"
		fi
	fi
done < <(find "${workspace}" -xdev -name .git -print0 2>/dev/null)

common_env=(
	"HOME=${sandbox_home}"
	"PATH=$([ "${provider_required}" = true ] && printf '%s' "${PATH}" || printf '/usr/bin:/bin')"
	"LANG=${LANG:-C.UTF-8}"
	"LC_ALL=${LC_ALL:-C.UTF-8}"
	"NO_COLOR=1"
	"UNTRUSTED_PROCESS_ISOLATED=1"
	"UNTRUSTED_SANDBOX_WORKSPACE=${workspace}"
	"GIT_CONFIG_GLOBAL=${safe_git_config}"
	"GIT_CONFIG_NOSYSTEM=1"
	"GIT_TERMINAL_PROMPT=0"
	"GIT_ASKPASS=/bin/false"
	"GIT_OPTIONAL_LOCKS=0"
	"PYTHONDONTWRITEBYTECODE=1"
	"RUNTIME_DIR=${sandbox_runtime}"
	"CODEX_THREAD_REUSE_RUNTIME_DIR=${sandbox_runtime}/thread-reuse"
)
if [ "${provider_required}" = true ]; then
	common_env+=("SANDBOX_PROVIDER_TOKEN=sandbox-proxy")
else
	common_env+=("POST_AGENT_ARTIFACT_DIR=${runtime_dir}")
fi
runtime_write_paths=()
while IFS='=' read -r environment_name environment_value; do
	case "${environment_name}" in
		CODEX_THREAD_REUSE_RUNTIME_DIR|RUNTIME_DIR)
			;;
		CODEX_THREAD_REUSE_OUTPUT_FILE|CODEX_THREAD_REUSE_LOG_FILE|CODEX_THREAD_REUSE_CUMULATIVE_LOG_FILE|CODEX_THREAD_REUSE_STATUS_FILE)
			[ "${provider_required}" = true ] || continue
			if [ -n "${environment_value}" ]; then
				mkdir -p "$(dirname "${environment_value}")"
				touch "${environment_value}"
				runtime_write_paths+=("${environment_value}")
				common_env+=("${environment_name}=${environment_value}")
			fi
			;;
		CODEX_THREAD_REUSE_*|MODEL_EDITOR|MODEL_VERBOSITY)
			[ "${provider_required}" = true ] || continue
			common_env+=("${environment_name}=${environment_value}")
			;;
	esac
done < <(env)
if [ "${config_format}" = opencode ]; then
	common_env+=("UNTRUSTED_OPENCODE_CONFIG=${sandbox_config}" "OPENCODE_CONFIG=${sandbox_config}")
elif [ "${config_format}" = codex ]; then
	common_env+=("CODEX_HOME=${sandbox_config}")
fi

if [ "${UNTRUSTED_PROCESS_SANDBOX_TEST_MODE:-}" = 1 ]; then
	for test_environment_name in MOCK_CODEX_OUTPUT MOCK_CODEX_STDERR MOCK_CODEX_EXIT_CODE MOCK_OPENCODE_OUTPUT_FILE MOCK_GH_STATE_FILE MOCK_GIT_FAILURE_MODE MOCK_GIT_BASE_SHA MOCK_REAL_GIT; do
		if [ -n "${!test_environment_name:-}" ]; then
			common_env+=("${test_environment_name}=${!test_environment_name}")
		fi
	done
	set +e
	(cd "${sandbox_runtime}" && env -i "${common_env[@]}" "$@")
	test_command_rc=$?
	set -e
	exit "${test_command_rc}"
fi
command -v systemd-run >/dev/null 2>&1 \
	|| { echo "untrusted_process_sandbox: systemd-run is required" >&2; exit 1; }
# systemd-run rejects unknown property assignments. Also require every kernel
# controller needed to enforce the accepted cgroup-backed properties.
sandbox_cgroup_controllers_file="/sys/fs/cgroup/cgroup.controllers"
[ -r "${sandbox_cgroup_controllers_file}" ] \
	|| { echo "untrusted_process_sandbox: cgroup v2 controllers are required" >&2; exit 1; }
sandbox_available_cgroup_controllers=" $(tr '\n' ' ' < "${sandbox_cgroup_controllers_file}") "
for sandbox_required_cgroup_controller in cpu io memory pids; do
	case "${sandbox_available_cgroup_controllers}" in
		*" ${sandbox_required_cgroup_controller} "*) ;;
		*) echo "untrusted_process_sandbox: required cgroup controller is unavailable: ${sandbox_required_cgroup_controller}" >&2; exit 1 ;;
	esac
done

validate_positive_integer()
{
	local value="$1"
	local label="$2"
	[[ "${value}" =~ ^[1-9][0-9]*$ ]] \
		|| { echo "untrusted_process_sandbox: ${label} must be a positive integer" >&2; exit 1; }
}

validate_resource_size()
{
	local value="$1"
	local label="$2"
	local allow_zero="${3:-false}"
	if [ "${allow_zero}" = true ] && [ "${value}" = 0 ]; then
		return 0
	fi
	[[ "${value}" =~ ^[1-9][0-9]*([KMGT])?$ ]] \
		|| { echo "untrusted_process_sandbox: ${label} must be a positive systemd size" >&2; exit 1; }
}

sandbox_tasks_max="${UNTRUSTED_SANDBOX_TASKS_MAX:-128}"
sandbox_memory_max="${UNTRUSTED_SANDBOX_MEMORY_MAX:-8G}"
sandbox_memory_swap_max="${UNTRUSTED_SANDBOX_MEMORY_SWAP_MAX:-0}"
sandbox_cpu_quota="${UNTRUSTED_SANDBOX_CPU_QUOTA:-200%}"
sandbox_io_read_max="${UNTRUSTED_SANDBOX_IO_READ_BANDWIDTH_MAX:-100M}"
sandbox_io_write_max="${UNTRUSTED_SANDBOX_IO_WRITE_BANDWIDTH_MAX:-50M}"
sandbox_limit_nofile="${UNTRUSTED_SANDBOX_LIMIT_NOFILE:-4096}"
sandbox_runtime_max_sec="${UNTRUSTED_SANDBOX_RUNTIME_MAX_SEC:-7200}"
sandbox_stop_timeout_sec="${UNTRUSTED_SANDBOX_STOP_TIMEOUT_SEC:-30}"
validate_positive_integer "${sandbox_tasks_max}" "TasksMax"
validate_resource_size "${sandbox_memory_max}" "MemoryMax"
validate_resource_size "${sandbox_memory_swap_max}" "MemorySwapMax" true
[[ "${sandbox_cpu_quota}" =~ ^[1-9][0-9]{0,3}%$ ]] \
	|| { echo "untrusted_process_sandbox: CPUQuota must be a percentage from 1% to 9999%" >&2; exit 1; }
validate_resource_size "${sandbox_io_read_max}" "IOReadBandwidthMax"
validate_resource_size "${sandbox_io_write_max}" "IOWriteBandwidthMax"
validate_positive_integer "${sandbox_limit_nofile}" "LimitNOFILE"
validate_positive_integer "${sandbox_runtime_max_sec}" "RuntimeMaxSec"
validate_positive_integer "${sandbox_stop_timeout_sec}" "TimeoutStopSec"
# Fail with a named cause instead of an opaque status 226 when a path the unit
# must see sits under the /tmp or /var/tmp that PrivateTmp=yes replaces.
for sandbox_private_tmp_checked_path in "${sandbox_dir}" "${workspace}" "${runtime_write_paths[@]:-}" "${git_metadata_paths[@]:-}"; do
	[ -n "${sandbox_private_tmp_checked_path}" ] || continue
	if sandbox_path_is_private_tmp "${sandbox_private_tmp_checked_path}"; then
		echo "untrusted_process_sandbox: sandbox_private_tmp_path role=${role} path=${sandbox_private_tmp_checked_path} reason=PrivateTmp=yes hides host /tmp and /var/tmp from the unit; set RUNNER_TEMP or pass --runtime-dir outside them" >&2
		exit 1
	fi
done
if [ "${provider_required}" = false ]; then
	for sandbox_private_tmp_checked_path in "${runtime_dir}" "${writable_output_dir}"; do
		[ -z "${sandbox_private_tmp_checked_path}" ] || ! sandbox_path_is_private_tmp "${sandbox_private_tmp_checked_path}" || exit 1
	done
fi
# PrivateTmp=yes already hides a caller-supplied credential under /tmp, and
# naming it in InaccessiblePaths= would make namespace setup fail instead.
credential_inaccessible_entry="${credential_file} "
if sandbox_path_is_private_tmp "${credential_file}"; then
	credential_inaccessible_entry=""
fi
systemd_run=(systemd-run)
sandbox_journal_cmd=(journalctl)
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
	systemd_run=(sudo -n systemd-run --uid="$(id -u)" --gid="$(id -g)")
	sandbox_journal_cmd=(sudo -n journalctl)
fi
sandbox_unit_name="untrusted-sandbox-${role}-$$-${RANDOM}.service"
systemd_properties=(
	--property=NoNewPrivileges=yes
	--property=PrivateTmp=yes
	--property=ProtectProc=invisible
	--property=ProcSubset=pid
	--property=RestrictSUIDSGID=yes
	--property=LockPersonality=yes
	--property="TasksMax=${sandbox_tasks_max}"
	--property="MemoryMax=${sandbox_memory_max}"
	--property="MemorySwapMax=${sandbox_memory_swap_max}"
	--property="CPUQuota=${sandbox_cpu_quota}"
	--property="IOReadBandwidthMax=/ ${sandbox_io_read_max}"
	--property="IOWriteBandwidthMax=/ ${sandbox_io_write_max}"
	--property="LimitNOFILE=${sandbox_limit_nofile}"
	--property="RuntimeMaxSec=${sandbox_runtime_max_sec}"
	--property=KillMode=control-group
	--property=OOMPolicy=kill
	--property="TimeoutStopSec=${sandbox_stop_timeout_sec}"
	--property=IPAddressDeny=any
	--property="InaccessiblePaths=${credential_inaccessible_entry}-/var/run/docker.sock -/run/docker.sock -/run/containerd/containerd.sock -/run/podman/podman.sock -${runner_command_files} -${host_home}/.config/gh -${host_home}/.git-credentials -${host_home}/.ssh"
	--property=ReadOnlyPaths=/
	--property="ReadWritePaths=${sandbox_dir}${runtime_write_paths[*]:+ ${runtime_write_paths[*]}}"
)
if [ "${provider_required}" = true ]; then
	systemd_properties+=(--property="IPAddressAllow=${proxy_host}/32")
fi
case "${role}" in
	implement|implement-repair|editor|resolver|judge-fix)
		systemd_properties+=(--property="ReadWritePaths=${workspace}")
		;;
	workspace-guard)
		if [ "${guard_action}" = reconcile ]; then
			systemd_properties+=(--property="ReadWritePaths=${workspace} ${runtime_dir}")
		else
			systemd_properties+=(--property="ReadWritePaths=${runtime_dir}")
		fi
		;;
	validator)
		if [ -n "${writable_output_dir}" ]; then
			systemd_properties+=(--property="ReadWritePaths=${writable_output_dir}")
		fi
		;;
esac
for protected_git_path in "${git_metadata_paths[@]:-}"; do
	[ -n "${protected_git_path}" ] || continue
	if [ "${provider_required}" = true ]; then
		systemd_properties+=(--property="InaccessiblePaths=${protected_git_path}")
	else
		systemd_properties+=(--property="ReadOnlyPaths=${protected_git_path}")
	fi
done
# No --quiet: systemd-run's unit name and "Main processes terminated with"
# lines are the only in-band trace of a unit that never reached the command.
sandbox_unit_rc=0
"${systemd_run[@]}" --wait --pipe --collect --service-type=exec \
	--unit="${sandbox_unit_name}" \
	"${systemd_properties[@]}" \
	--working-directory="$([ "${provider_required}" = true ] && printf '%s' "${workspace}" || printf '%s' "${sandbox_runtime}")" \
	env -i "${common_env[@]}" "$@" || sandbox_unit_rc=$?
if [ "${sandbox_unit_rc}" -eq 226 ]; then
	# 226 is systemd's EXIT_NAMESPACE: the unit failed while building its mount
	# namespace, before the command ran.  The reason is only in the journal.
	echo "untrusted_process_sandbox: sandbox_namespace_setup_failed role=${role} rc=226 unit=${sandbox_unit_name}" >&2
	timeout --kill-after=2 10 "${sandbox_journal_cmd[@]}" --no-pager -o cat -n 20 -u "${sandbox_unit_name}" 2>/dev/null \
		| sed 's/^/untrusted_process_sandbox: sandbox_namespace_setup_failed journal: /' >&2 || true
fi
exit "${sandbox_unit_rc}"
