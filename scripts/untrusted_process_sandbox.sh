#!/usr/bin/env bash
# Run a model process without runner credentials and with provider-only network.
set -euo pipefail

role=""
workspace=""
config_format=""
config_path=""
runtime_dir="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}"
host_home="${HOME:-/nonexistent}"
runner_command_files="${RUNNER_TEMP:-/tmp}/_runner_file_commands"
while [ "$#" -gt 0 ]; do
	case "$1" in
		--role) role="${2:-}"; shift 2 ;;
		--workspace) workspace="${2:-}"; shift 2 ;;
		--config-format) config_format="${2:-}"; shift 2 ;;
		--config) config_path="${2:-}"; shift 2 ;;
		--runtime-dir) runtime_dir="${2:-}"; shift 2 ;;
		--) shift; break ;;
		*) echo "untrusted_process_sandbox: unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ "$#" -gt 0 ] || { echo "untrusted_process_sandbox: command is required" >&2; exit 2; }
case "${role}" in
	plan|implement|implement-repair|reviewer|editor|resolver|judge|judge-fix|summary|audit) ;;
	*) echo "untrusted_process_sandbox: invalid role" >&2; exit 2 ;;
esac
case "${config_format}" in
	opencode|codex) ;;
	*) echo "untrusted_process_sandbox: invalid config format" >&2; exit 2 ;;
esac
workspace="$(cd "${workspace}" && pwd -P)"
[ -r "${config_path}" ] || { echo "untrusted_process_sandbox: config is unreadable" >&2; exit 2; }

credential_file="${MODEL_PROVIDER_CREDENTIAL_FILE:-}"
credential_file_is_temporary=false
if [ -z "${credential_file}" ] && [ -n "${OPENROUTER_API_KEY:-}" ]; then
	credential_file="$(mktemp "${runtime_dir%/}/provider-credential.XXXXXX")"
	credential_file_is_temporary=true
	chmod 600 "${credential_file}"
	printf '%s' "${OPENROUTER_API_KEY}" > "${credential_file}"
fi
[ -r "${credential_file}" ] || { echo "untrusted_process_sandbox: provider credential is unavailable" >&2; exit 1; }

sandbox_dir="$(mktemp -d "${runtime_dir%/}/agent-sandbox.XXXXXX")"
ready_file="${sandbox_dir}/proxy-ready.json"
proxy_log="${sandbox_dir}/proxy.log"
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

proxy_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/model_provider_proxy.py"
[ -r "${proxy_script}" ] || { echo "untrusted_process_sandbox: provider proxy is unavailable" >&2; exit 1; }
PYTHONDONTWRITEBYTECODE=1 python3 "${proxy_script}" \
	--credential-file "${credential_file}" --ready-file "${ready_file}" >"${proxy_log}" 2>&1 &
proxy_pid=$!
for _proxy_wait in $(seq 1 100); do
	[ -s "${ready_file}" ] && break
	kill -0 "${proxy_pid}" 2>/dev/null || { cat "${proxy_log}" >&2; exit 1; }
	sleep 0.05
done
[ -s "${ready_file}" ] || { echo "untrusted_process_sandbox: provider proxy did not become ready" >&2; exit 1; }
proxy_host="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["host"])' "${ready_file}")"
proxy_port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "${ready_file}")"
proxy_url="http://${proxy_host}:${proxy_port}/api/v1"

sandbox_config="${sandbox_dir}/agent-config"
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
	"PATH=${PATH}"
	"LANG=${LANG:-C.UTF-8}"
	"LC_ALL=${LC_ALL:-C.UTF-8}"
	"NO_COLOR=1"
	"SANDBOX_PROVIDER_TOKEN=sandbox-proxy"
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
runtime_write_paths=()
while IFS='=' read -r environment_name environment_value; do
	case "${environment_name}" in
		CODEX_THREAD_REUSE_RUNTIME_DIR|RUNTIME_DIR)
			;;
			CODEX_THREAD_REUSE_OUTPUT_FILE|CODEX_THREAD_REUSE_LOG_FILE|CODEX_THREAD_REUSE_CUMULATIVE_LOG_FILE|CODEX_THREAD_REUSE_STATUS_FILE)
			if [ -n "${environment_value}" ]; then
				mkdir -p "$(dirname "${environment_value}")"
				touch "${environment_value}"
				runtime_write_paths+=("${environment_value}")
				common_env+=("${environment_name}=${environment_value}")
			fi
			;;
		CODEX_THREAD_REUSE_*|MODEL_EDITOR|MODEL_VERBOSITY)
			common_env+=("${environment_name}=${environment_value}")
			;;
	esac
done < <(env)
if [ "${config_format}" = opencode ]; then
	common_env+=("UNTRUSTED_OPENCODE_CONFIG=${sandbox_config}" "OPENCODE_CONFIG=${sandbox_config}")
else
	common_env+=("CODEX_HOME=${sandbox_config}")
fi

if [ "${UNTRUSTED_PROCESS_SANDBOX_TEST_MODE:-}" = 1 ]; then
	for test_environment_name in MOCK_CODEX_OUTPUT MOCK_CODEX_STDERR MOCK_CODEX_EXIT_CODE MOCK_GH_STATE_FILE MOCK_GIT_FAILURE_MODE MOCK_GIT_BASE_SHA MOCK_REAL_GIT; do
		if [ -n "${!test_environment_name:-}" ]; then
			common_env+=("${test_environment_name}=${!test_environment_name}")
		fi
	done
	set +e
	env -i "${common_env[@]}" "$@"
	test_command_rc=$?
	set -e
	exit "${test_command_rc}"
fi
command -v systemd-run >/dev/null 2>&1 \
	|| { echo "untrusted_process_sandbox: systemd-run is required" >&2; exit 1; }
systemd_run=(systemd-run)
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
	systemd_run=(sudo -n systemd-run --uid="$(id -u)" --gid="$(id -g)")
fi
systemd_properties=(
	--property=NoNewPrivileges=yes
	--property=PrivateTmp=yes
	--property=ProtectProc=invisible
	--property=ProcSubset=pid
	--property=RestrictSUIDSGID=yes
	--property=LockPersonality=yes
	--property=IPAddressDeny=any
	--property="IPAddressAllow=${proxy_host}/32"
	--property="InaccessiblePaths=${credential_file} -/var/run/docker.sock -/run/docker.sock -/run/containerd/containerd.sock -/run/podman/podman.sock -${runner_command_files} -${host_home}/.config/gh -${host_home}/.git-credentials -${host_home}/.ssh"
	--property=ReadOnlyPaths=/
	--property="ReadWritePaths=${sandbox_dir}${runtime_write_paths[*]:+ ${runtime_write_paths[*]}}"
)
case "${role}" in
	implement|implement-repair|editor|resolver|judge-fix)
		systemd_properties+=(--property="ReadWritePaths=${workspace}")
		;;
esac
for protected_git_path in "${git_metadata_paths[@]:-}"; do
	[ -n "${protected_git_path}" ] || continue
	systemd_properties+=(--property="InaccessiblePaths=${protected_git_path}")
done
"${systemd_run[@]}" --quiet --wait --pipe --collect --service-type=exec \
	"${systemd_properties[@]}" \
	--working-directory="${workspace}" \
	env -i "${common_env[@]}" "$@"
