#!/usr/bin/env bash
# codex_isolated_exec.sh — run one Codex agent in a credential-free,
# network-isolated container.
#
# Every Codex agent that reads untrusted text (issue bodies, comments, PR
# diffs, CI and workflow logs) runs through this helper. The agent process
# never holds GH_TOKEN / GITHUB_TOKEN / GH_PAT, the OpenRouter key, the
# Telegram secrets, the host checkout's .git (whose config carries the
# GH_PAT remote URL and checkout extraheader), HOME, or the runner's
# file-command files. Model traffic leaves the container only through
# scripts/clarify_openrouter_broker.py: a host-side broker on a Unix socket
# holds the key and forwards one fixed path, for one model, to OpenRouter.
# This generalises scripts/clarify_isolated_run.sh (clarify) and
# scripts/review_untrusted_sandbox.sh (review editor).
#
# Usage:
#   codex_isolated_exec.sh prepare --workdir DIR [--deps]
#       Workspace mode only. Create a persistent sandbox root for a job that
#       runs several agent attempts on DIR, and print its path. With
#       --deps, install the project's dependencies once in a credential-free
#       container that has network access (npm ci / pip install, scripts
#       disabled where the tool allows), the way review_untrusted_sandbox.sh
#       prepare does. Dependency output stays in the sandbox and is never
#       copied back.
#   codex_isolated_exec.sh run --mode read-only|workspace [--root DIR]
#                              [--workdir DIR] [--include PATH]...
#                              [--codex-home DIR] [--reasoning LEVEL]
#                              [--web-search live|disabled] -- CODEX_ARGS...
#       Run `codex CODEX_ARGS...` in the container. stdin is the prompt,
#       stdout/stderr are Codex's own. The exit status is Codex's.
#       read-only: the agent sees a read-only copy of DIR's tracked files.
#       workspace: the agent edits a disposable copy of DIR; every changed
#                  regular file is copied back afterwards (also after a
#                  failed or interrupted attempt, as an edit made on the
#                  host would have survived), new symlinks and special files
#                  are refused (scripts/codex_isolated_workspace.py).
#       --workdir   defaults to $PWD; mounted at the same absolute path.
#       --include   a trusted runtime file/directory the prompt names,
#                   copied and mounted read-only at the same absolute path.
#       --codex-home a host directory kept across attempts as the agent's
#                   CODEX_HOME (codex_thread_reuse.sh session resume).
#       --reasoning / --web-search default to the values the step's own
#                   config.toml (written by write_codex_config.sh) holds.
#   codex_isolated_exec.sh cleanup --root DIR
#
# Support files are read from this script's own directory, never from the
# working directory: callers run the helper from a trusted copy that no
# agent can write (CLAUDE.md answer Q9 A).
#
# Fails closed: a missing Docker, image build, broker or snapshot failure
# exits 1 with `::error::CODEX_ISOLATION ...`; Codex never falls back to
# running on the host. There is no switch that turns isolation off.
#
# Stable log prefix: CODEX_ISOLATION.

set -euo pipefail

support_dir="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
action="${1:-}"
[ "$#" -gt 0 ] && shift

fail()
{
	echo "::error::CODEX_ISOLATION $*" >&2
	exit 1
}

case "${action}" in
	prepare|run|cleanup) ;;
	*) echo "usage: codex_isolated_exec.sh prepare|run|cleanup ..." >&2; exit 2 ;;
esac

mode=""
root=""
workdir=""
codex_home=""
reasoning=""
web_search=""
want_deps="false"
includes=()
codex_args=()
while [ "$#" -gt 0 ]; do
	case "$1" in
		--mode) mode="${2:-}"; shift 2 ;;
		--root) root="${2:-}"; shift 2 ;;
		--workdir) workdir="${2:-}"; shift 2 ;;
		--include) includes+=("${2:-}"); shift 2 ;;
		--codex-home) codex_home="${2:-}"; shift 2 ;;
		--reasoning) reasoning="${2:-}"; shift 2 ;;
		--web-search) web_search="${2:-}"; shift 2 ;;
		--deps) want_deps="true"; shift ;;
		--) shift; codex_args=("$@"); break ;;
		*) echo "::error::CODEX_ISOLATION unknown argument: $1" >&2; exit 2 ;;
	esac
done

sandbox_parent="$(realpath -e -- "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"

# Paths are passed to `docker run --mount`, which splits on commas, and are
# mounted at the same absolute path inside the container.
valid_path()
{
	[[ "$1" =~ ^/[A-Za-z0-9._@+/-]+$ ]] && [[ "$1" != *,* ]] && [[ "$1" != *"/../"* ]] && [[ "$1" != *"/.." ]]
}
[ -d "${sandbox_parent}" ] && valid_path "${sandbox_parent}" || fail "sandbox parent path rejected: ${sandbox_parent}"

# Only a root this helper created may be reused or removed: a direct child
# of the runner temp directory, named codex-isolated-*, holding the marker.
check_root()
{
	local candidate="$1"
	[ -n "${candidate}" ] || fail "sandbox root missing"
	[[ "$(basename -- "${candidate}")" == codex-isolated-* ]] \
		&& [ "$(dirname -- "$(realpath -e -- "${candidate}" 2>/dev/null || echo /invalid/x)")" = "${sandbox_parent}" ] \
		&& [ -f "${candidate}/.codex-isolated-root" ] && [ ! -L "${candidate}" ] \
		|| fail "sandbox root rejected: ${candidate}"
}

docker_env()
{
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker "$@"
}

remove_root_containers()
{
	local ids
	ids="$(docker_env ps -aq --filter "label=coding-workflows.codex-isolated.root=$1" 2>/dev/null || true)"
	# shellcheck disable=SC2086  # one container id per word
	[ -z "${ids}" ] || docker_env rm -f ${ids} >/dev/null 2>&1 || true
}

if [ "${action}" = cleanup ]; then
	check_root "${root}"
	remove_root_containers "${root}"
	rm -rf -- "${root}"
	echo "CODEX_ISOLATION cleanup root=${root}" >&2
	exit 0
fi

command -v docker >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || fail "Docker and python3 are required; Codex is never run on the host"
for required in codex_isolated_workspace.py clarify_openrouter_broker.py write_codex_config.sh codex_model_catalog.json; do
	[ -f "${support_dir}/${required}" ] && [ ! -L "${support_dir}/${required}" ] || fail "support file missing next to the helper: ${required}"
done
# Resolve the interpreter once, as an absolute path: the broker starts with
# an empty environment, where a PATH-relative wrapper could not find it.
python_bin="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null)" || python_bin=""
[[ "${python_bin}" == /* ]] && [ -x "${python_bin}" ] || fail "could not resolve the python3 interpreter"
workspace_py()
{
	PYTHONDONTWRITEBYTECODE=1 "${python_bin}" "${support_dir}/codex_isolated_workspace.py" "$@"
}

workdir="$(realpath -e -- "${workdir:-${PWD}}" 2>/dev/null)" || fail "workdir does not exist"
[ -d "${workdir}" ] && valid_path "${workdir}" || fail "workdir path rejected: ${workdir}"
case "${workdir}" in
	/|/home/agent|/home/agent/*|/socket|/socket/*|/support|/support/*|/opt/codex-venv|/opt/codex-venv/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/etc|/etc/*|/usr|/usr/*)
		fail "workdir collides with a container path: ${workdir}" ;;
esac

new_root()
{
	local created
	created="$(mktemp -d "${sandbox_parent}/codex-isolated-XXXXXXXX")"
	chmod 0700 "${created}"
	: > "${created}/.codex-isolated-root"
	mkdir -m 0700 "${created}/socket" "${created}/home"
	mkdir -m 0755 "${created}/work" "${created}/venv" "${created}/support" "${created}/include"
	printf '%s\n' "${created}"
}

# The image is built from a fixed, generated build context: no repository
# file, secret or workflow input reaches `docker build`.
build_image()
{
	local version="${CODEX_VERSION:-v0.114.0}" context image
	[[ "${version}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid CODEX_VERSION"
	context="$(mktemp -d)"
	cat > "${context}/Dockerfile" <<'DOCKERFILE'
# Fixed build context generated by scripts/codex_isolated_exec.sh.
FROM node:22.16.0-bookworm-slim
ARG CODEX_VERSION=v0.114.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git ca-certificates build-essential ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
    "@openai/codex-linux-x64@npm:@openai/codex@${CODEX_VERSION#v}-linux-x64" \
    --no-audit --no-fund
DOCKERFILE
	image="$(docker_env build -q --build-arg "CODEX_VERSION=${version}" "${context}" 2>"${context}.err")" || image=""
	if ! [[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
		tail -n 30 "${context}.err" >&2 || true
		rm -rf -- "${context}" "${context}.err"
		fail "sandbox image build failed"
	fi
	rm -rf -- "${context}" "${context}.err"
	printf '%s\n' "${image}"
}

cpu_limit()
{
	local want="$1" have
	have="$(nproc 2>/dev/null || echo 1)"
	[ "${have}" -lt "${want}" ] && want="${have}"
	printf '%s\n' "${want}"
}

if [ "${action}" = prepare ]; then
	root="$(new_root)"
	trap 'remove_root_containers "${root}"; rm -rf -- "${root}"' EXIT
	workspace_py snapshot-workspace "${workdir}" "${root}/work" "${root}/manifest.json" || fail "workspace snapshot failed"
	if [ "${want_deps}" = true ]; then
		image="$(build_image)"
		# Network access is confined to this credential-free dependency
		# phase: no broker, no socket, no environment from the runner.
		timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --init \
			--name "codex-isolated-deps-$$" --label "coding-workflows.codex-isolated.root=${root}" \
			--user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
			--pids-limit 512 --memory 6g --cpus "$(cpu_limit 4)" \
			--mount "type=bind,src=${root}/work,dst=${workdir}" \
			--mount "type=bind,src=${root}/venv,dst=/opt/codex-venv" \
			--env HOME=/tmp/agent-home --env PYTHONDONTWRITEBYTECODE=1 --workdir "${workdir}" "${image}" /bin/bash -c '
				set -u
				install_failed=false
				python3 -m venv --system-site-packages /opt/codex-venv || exit 1
				export PATH=/opt/codex-venv/bin:$PATH
				if [ -d node_modules ]; then
					echo "node_modules already present — skipping the npm install"
				elif [ -f package-lock.json ]; then
					npm ci --ignore-scripts 2>&1 || install_failed=true
				elif [ -f yarn.lock ]; then
					npx --yes yarn install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
				elif [ -f pnpm-lock.yaml ]; then
					npx --yes pnpm install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
				elif [ -f package.json ]; then
					npm install --ignore-scripts 2>&1 || install_failed=true
				fi
				if [ -f requirements.txt ]; then
					pip install -r requirements.txt 2>&1 || install_failed=true
				elif [ -f pyproject.toml ] && [ ! -f package.json ]; then
					pip install -e ".[dev]" 2>&1 || pip install -e . 2>&1 || install_failed=true
				fi
				if { [ -f pytest.ini ] || [ -f conftest.py ] || [ -f tests/conftest.py ] || grep -qs "^\[tool\.pytest\.ini_options\]" pyproject.toml; } \
					&& ! python3 -c "import pytest" >/dev/null 2>&1; then
					pip install pytest 2>&1 || true
				fi
				[ "${install_failed}" = false ] || echo "::warning::Some project dependencies could not be installed in the isolated dependency phase; the agent marks validations it cannot run as UNVERIFIED."
			' >&2 || echo "::warning::CODEX_ISOLATION dependency phase failed; the agent runs without preinstalled dependencies." >&2
		workspace_py prep-finalize "${workdir}" "${root}/work" "${root}/manifest.json" || fail "dependency phase output rejected"
	fi
	trap - EXIT
	echo "CODEX_ISOLATION prepare root=${root} workdir=${workdir} deps=${want_deps}" >&2
	printf '%s\n' "${root}"
	exit 0
fi

# ---- run ----
case "${mode}" in read-only|workspace) ;; *) fail "--mode must be read-only or workspace" ;; esac
[ "${#codex_args[@]}" -gt 0 ] || fail "codex arguments missing"
[ -n "${OPENROUTER_API_KEY:-}" ] || fail "OPENROUTER_API_KEY is required by the host-side broker"
model=""
for ((i = 0; i < ${#codex_args[@]}; i++)); do
	if [ "${codex_args[$i]}" = "--model" ] && [ $((i + 1)) -lt "${#codex_args[@]}" ]; then
		model="${codex_args[$((i + 1))]}"
	elif [[ "${codex_args[$i]}" == --model=* ]]; then
		model="${codex_args[$i]#--model=}"
	fi
done
[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || fail "codex arguments must name a valid --model"

# Reasoning effort and web search follow the step's own config.toml, so the
# isolated run behaves like the host run it replaces.
host_config="${CODEX_HOME:-${HOME:-/root}/.codex}/config.toml"
config_value()
{
	[ -f "${host_config}" ] || return 0
	sed -n "s/^$1 = \"\\([a-z-]*\\)\"\$/\\1/p" "${host_config}" | head -n 1
}
[ -n "${reasoning}" ] || reasoning="$(config_value model_reasoning_effort)"
[ -n "${reasoning}" ] || reasoning="${MODEL_REASONING_EFFORT:-medium}"
[ -n "${web_search}" ] || web_search="$(config_value web_search)"
[ -n "${web_search}" ] || web_search="live"
[[ "${reasoning}" =~ ^(xhigh|high|medium|low|none)$ ]] || fail "invalid reasoning level"
[[ "${web_search}" =~ ^(live|disabled)$ ]] || fail "invalid web-search value"

ephemeral="false"
if [ -n "${root}" ]; then
	[ "${mode}" = workspace ] || fail "--root is only valid in workspace mode"
	check_root "${root}"
	remove_root_containers "${root}"
else
	root="$(new_root)"
	ephemeral="true"
fi
container="codex-isolated-$$-${RANDOM}"
broker_pid=""
docker_pid=""
interrupted=""
finish()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	docker_env rm -f "${container}" >/dev/null 2>&1 || true
	rm -f "${root}/socket/provider.sock" "${root}/prompt"
	if [ "${ephemeral}" = true ]; then
		rm -rf -- "${root}"
	fi
}
trap finish EXIT
on_signal()
{
	interrupted="$1"
	if [ -z "${docker_pid}" ]; then
		exit "${interrupted}"
	fi
	docker_env kill "${container}" >/dev/null 2>&1 || true
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

cat > "${root}/prompt"
chmod 0600 "${root}/prompt"
[ -s "${root}/prompt" ] || fail "empty prompt"

if [ "${mode}" = workspace ]; then
	workspace_py snapshot-workspace "${workdir}" "${root}/work" "${root}/manifest.json" || fail "workspace snapshot failed"
else
	workspace_py snapshot-readonly "${workdir}" "${root}/work" || fail "read-only snapshot failed"
fi
workspace_py seed-git "${workdir}" "${root}/work" || true

install -m 0644 "${support_dir}/clarify_openrouter_broker.py" "${root}/support/clarify_openrouter_broker.py"
install -m 0644 "${support_dir}/write_codex_config.sh" "${root}/support/write_codex_config.sh"
install -m 0644 "${support_dir}/codex_model_catalog.json" "${root}/support/codex_model_catalog.json"

mounts=()
work_mount="type=bind,src=${root}/work,dst=${workdir}"
[ "${mode}" = read-only ] && work_mount="${work_mount},readonly"
mounts+=(--mount "${work_mount}")
mounts+=(--mount "type=bind,src=${root}/socket,dst=/socket,readonly")
mounts+=(--mount "type=bind,src=${root}/support,dst=/support,readonly")
if [ -d "${root}/venv/bin" ]; then
	mounts+=(--mount "type=bind,src=${root}/venv,dst=/opt/codex-venv,readonly")
fi
if [ -n "${codex_home}" ]; then
	mkdir -p "${codex_home}"
	chmod 0700 "${codex_home}"
	codex_home="$(realpath -e -- "${codex_home}")"
	valid_path "${codex_home}" || fail "codex home path rejected"
	case "${codex_home}" in "${workdir}"|"${workdir}"/*) fail "codex home must be outside the workdir" ;; esac
	mounts+=(--mount "type=bind,src=${codex_home},dst=/home/agent/.codex")
fi
for include in "${includes[@]}"; do
	[ -n "${include}" ] || continue
	include="$(realpath -e -- "${include}" 2>/dev/null)" || fail "include does not exist"
	valid_path "${include}" || fail "include path rejected: ${include}"
	case "${include}" in
		"${workdir}")
			continue ;;
		"${workdir}"/*)
			# Inside the workdir: a workspace copy already holds it; a
			# read-only snapshot (tracked files only) gets it added.
			[ "${mode}" = read-only ] || continue
			workspace_py copy-include "${include}" "${root}/work/${include#"${workdir}"/}" || fail "include rejected: ${include}"
			continue ;;
	esac
	workspace_py copy-include "${include}" "${root}/include${include}" || fail "include rejected: ${include}"
	mounts+=(--mount "type=bind,src=${root}/include${include},dst=${include},readonly")
done

image="$(build_image)"
rm -f -- "${root}/socket/provider.sock"
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 \
	"${python_bin}" "${support_dir}/clarify_openrouter_broker.py" broker "${root}/socket/provider.sock" &
broker_pid=$!
for _ in $(seq 1 50); do
	[ -S "${root}/socket/provider.sock" ] && break
	kill -0 "${broker_pid}" 2>/dev/null || fail "model broker failed to start"
	command -p sleep 0.1
done
[ -S "${root}/socket/provider.sock" ] || fail "model broker unavailable"

if [ "${mode}" = workspace ]; then
	limits=(--pids-limit 1024 --memory 6g --cpus "$(cpu_limit 4)" --tmpfs "/tmp:rw,nosuid,nodev,exec,size=2g")
else
	limits=(--pids-limit 256 --memory 2g --cpus "$(cpu_limit 2)" --tmpfs "/tmp:rw,nosuid,nodev,size=256m")
fi

echo "CODEX_ISOLATION run mode=${mode} model=${model} reasoning=${reasoning} web_search=${web_search} workdir=${workdir} persistent_root=$([ "${ephemeral}" = true ] && echo false || echo true)" >&2
# No host checkout, .git, HOME, Docker socket, token or Git remote is
# mounted, and no runner environment variable is passed in.
rc=0
env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm -i --init \
	--name "${container}" --label "coding-workflows.codex-isolated.root=${root}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL \
	--security-opt no-new-privileges "${limits[@]}" \
	--tmpfs /home/agent:rw,nosuid,nodev,size=512m,mode=1777 \
	"${mounts[@]}" \
	--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex --env PYTHONDONTWRITEBYTECODE=1 \
	--env CODEX_ISOLATED_PROXY_KEY=isolated-placeholder \
	--env "CODEX_ISOLATED_MODEL=${model}" --env "CODEX_ISOLATED_REASONING=${reasoning}" \
	--env "CODEX_ISOLATED_WEB_SEARCH=${web_search}" --env "CODEX_ISOLATED_WORKDIR=${workdir}" \
	--workdir "${workdir}" "${image}" /bin/bash -c '
		set -euo pipefail
		mkdir -p "${CODEX_HOME}"
		bash /support/write_codex_config.sh --model "${CODEX_ISOLATED_MODEL}" --reasoning "${CODEX_ISOLATED_REASONING}" \
			--web-search "${CODEX_ISOLATED_WEB_SEARCH}" --catalog-path /support/codex_model_catalog.json \
			--project-path "${CODEX_ISOLATED_WORKDIR}" --config-path "${CODEX_HOME}/config.toml" --allow-elevation force
		sed -i -e "s#base_url = \"https://openrouter.ai/api/v1\"#base_url = \"http://127.0.0.1:8765/api/v1\"#" \
			-e "s/env_key = \"OPENROUTER_API_KEY\"/env_key = \"CODEX_ISOLATED_PROXY_KEY\"/" "${CODEX_HOME}/config.toml"
		grep -q "^base_url = \"http://127.0.0.1:8765/api/v1\"$" "${CODEX_HOME}/config.toml" || { echo "::error::CODEX_ISOLATION container config rewrite failed" >&2; exit 97; }
		if [ -d /opt/codex-venv/bin ]; then
			export PATH="/opt/codex-venv/bin:${PATH}"
			# Codex runs tool commands in a login shell, which resets PATH
			# from /etc/profile; HOME is a private tmpfs.
			printf "export PATH=/opt/codex-venv/bin:\${PATH}\n" > "${HOME}/.bash_profile"
		fi
		python3 /support/clarify_openrouter_broker.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap "kill ${bridge_pid} 2>/dev/null || true" EXIT
		# Bridge start-up is bounded; model traffic stays on loopback.
		python3 -c "import socket,time; [(time.sleep(.1) if s.connect_ex((\"127.0.0.1\",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)" \
			|| { echo "::error::CODEX_ISOLATION model bridge unavailable" >&2; exit 98; }
		codex "$@"
	' codex-isolated "${codex_args[@]}" < "${root}/prompt" &
docker_pid=$!
while :; do
	wait "${docker_pid}" && rc=0 || rc=$?
	# A trapped signal interrupts `wait`; keep waiting for the killed client.
	kill -0 "${docker_pid}" 2>/dev/null || break
done
docker_pid=""

if [ "${mode}" = workspace ]; then
	# Copied back after a failed or interrupted attempt too: an edit made on
	# the host before isolation survived those cases, and implement's retry
	# logic reads the worktree delta.
	if ! workspace_py transfer "${workdir}" "${root}/work" "${root}/manifest.json"; then
		echo "::error::CODEX_ISOLATION workspace transfer rejected; no host file was changed." >&2
		[ "${rc}" -ne 0 ] || rc=1
	fi
fi
# Nothing is logged after Codex's own stderr on the normal path: callers
# classify the last lines of that stream (provider errors, BLOCKED verdicts).
if [ -n "${interrupted}" ]; then
	echo "CODEX_ISOLATION interrupted mode=${mode} signal_exit=${interrupted}" >&2
	exit "${interrupted}"
fi
exit "${rc}"
