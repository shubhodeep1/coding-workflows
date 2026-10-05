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
#       --deps, install third-party dependencies from filtered manifests in
#       a credential-free, network-isolated container through the allowlisted
#       registry proxy. Source installation runs separately without network
#       access. Dependency output stays in the sandbox and is never copied back.
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
#   codex_isolated_exec.sh run --engine claude --mode read-only|workspace
#                              --claude-token-file FILE --claude-models LIST
#                              --claude-cli-version X.Y.Z
#                              --claude-settings FILE --claude-guard-hook FILE
#                              --claude-instructions FILE [--claude-home DIR]
#                              [--hide-claude-md] [--workdir DIR]
#                              [--include PATH]... -- CLAUDE_ARGS...
#       The Claude engine (scripts/ai_engine.sh claude_run): runs
#       `claude CLAUDE_ARGS...` in the same container, with the pinned Claude
#       Code CLI added to the image. scripts/claude_anthropic_relay.py runs
#       as the host broker: it alone reads the account's OAuth token from
#       --claude-token-file (never mounted, never in the environment) and
#       forwards only the --claude-models to api.anthropic.com. The settings,
#       guard hook and instructions are copied to /support/settings.json,
#       /support/guard.py and /support/instructions.md, the paths
#       CLAUDE_ARGS name. --claude-home is kept across runs as the CLI's
#       ~/.claude (session resume). --hide-claude-md keeps the top-level
#       CLAUDE.md out of the copy and out of the write-back.
#       Exit 75: isolation unavailable (no Docker or python3, image build
#       failed); the caller falls back to codex (plan D1). Exit 73: the relay
#       did not start for this account. Otherwise the CLI's exit status.
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
engine="codex"
root=""
workdir=""
codex_home=""
reasoning=""
web_search=""
want_deps="false"
claude_token_file=""
claude_models=""
claude_cli_version=""
claude_settings=""
claude_guard_hook=""
claude_instructions=""
claude_home=""
hide_claude_md="false"
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
		--engine) engine="${2:-}"; shift 2 ;;
		--claude-token-file) claude_token_file="${2:-}"; shift 2 ;;
		--claude-models) claude_models="${2:-}"; shift 2 ;;
		--claude-cli-version) claude_cli_version="${2:-}"; shift 2 ;;
		--claude-settings) claude_settings="${2:-}"; shift 2 ;;
		--claude-guard-hook) claude_guard_hook="${2:-}"; shift 2 ;;
		--claude-instructions) claude_instructions="${2:-}"; shift 2 ;;
		--claude-home) claude_home="${2:-}"; shift 2 ;;
		--hide-claude-md) hide_claude_md="true"; shift ;;
		--) shift; codex_args=("$@"); break ;;
		*) echo "::error::CODEX_ISOLATION unknown argument: $1" >&2; exit 2 ;;
	esac
done

case "${engine}" in codex|claude) ;; *) echo "::error::CODEX_ISOLATION --engine must be codex or claude" >&2; exit 2 ;; esac
[ "${engine}" = codex ] || [ "${action}" = run ] || { echo "::error::CODEX_ISOLATION --engine claude is only valid for run" >&2; exit 2; }
# Hidden top-level paths are filtered by every codex_isolated_workspace.py call.
if [ "${hide_claude_md}" = true ]; then
	export CODEX_ISOLATED_HIDE="CLAUDE.md"
else
	unset CODEX_ISOLATED_HIDE
fi

# Isolation that cannot be set up: the Claude engine reports it as
# unavailable (exit 75; ai_engine.sh then runs codex, plan D1, CLAUDE.md
# answer Q20 A) and never runs on the host either; codex fails hard.
unavailable()
{
	if [ "${engine}" = claude ]; then
		echo "CODEX_ISOLATION unavailable engine=claude reason=$1" >&2
		exit 75
	fi
	fail "$2"
}

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

command -v docker >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || unavailable docker_missing "Docker and python3 are required; Codex is never run on the host"
if [ "${engine}" = claude ]; then
	required_support=(codex_isolated_workspace.py claude_anthropic_relay.py)
else
	required_support=(codex_isolated_workspace.py clarify_openrouter_broker.py write_codex_config.sh codex_model_catalog.json)
fi
for required in "${required_support[@]}"; do
	[ -f "${support_dir}/${required}" ] && [ ! -L "${support_dir}/${required}" ] || unavailable support_missing "support file missing next to the helper: ${required}"
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
	/|/home/agent|/home/agent/*|/socket|/socket/*|/support|/support/*|/codex-deps|/codex-deps/*|/opt/codex-venv|/opt/codex-venv/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/etc|/etc/*|/usr|/usr/*)
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
	local -a build_args=(--build-arg "CODEX_VERSION=${version}")
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
	if [ "${engine}" = claude ]; then
		# The Claude engine adds the pinned CLI; the codex image is unchanged.
		cat >> "${context}/Dockerfile" <<'DOCKERFILE'
ARG CLAUDE_CLI_VERSION
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" --no-audit --no-fund
DOCKERFILE
		build_args+=(--build-arg "CLAUDE_CLI_VERSION=${claude_cli_version}")
	fi
	image="$(docker_env build -q "${build_args[@]}" "${context}" 2>"${context}.err")" || image=""
	if ! [[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
		tail -n 30 "${context}.err" >&2 || true
		rm -rf -- "${context}" "${context}.err"
		unavailable image_build_failed "sandbox image build failed"
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
	deps_broker_pid=""
	stop_deps_broker()
	{
		if [ -n "${deps_broker_pid}" ]; then
			kill "${deps_broker_pid}" 2>/dev/null || true
			wait "${deps_broker_pid}" 2>/dev/null || true
			deps_broker_pid=""
		fi
		rm -f -- "${root}/socket/registry.sock"
	}
	trap 'stop_deps_broker; remove_root_containers "${root}"; rm -rf -- "${root}"' EXIT
	workspace_py snapshot-workspace "${workdir}" "${root}/work" "${root}/manifest.json" || fail "workspace snapshot failed"
	proxy_state="skipped"
	if [ "${want_deps}" = true ]; then
		stage="${root}/deps-stage"
		mkdir -m 0755 "${stage}"
		if workspace_py stage-deps "${root}/work" "${stage}"; then
			if [ ! -f "${support_dir}/dependency_registry_proxy.py" ] || [ -L "${support_dir}/dependency_registry_proxy.py" ]; then
				echo "::warning::CODEX_ISOLATION deps_skipped reason=proxy_support_missing" >&2
			else
				install -m 0644 "${support_dir}/dependency_registry_proxy.py" "${root}/support/dependency_registry_proxy.py"
				env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 "${python_bin}" "${root}/support/dependency_registry_proxy.py" broker "${root}/socket/registry.sock" "${DEPENDENCY_PROXY_ALLOWED_HOSTS:-}" &
				deps_broker_pid=$!
				for _ in $(seq 1 50); do
					[ -S "${root}/socket/registry.sock" ] && break
					kill -0 "${deps_broker_pid}" 2>/dev/null || break
					command -p sleep 0.1
				done
				if [ ! -S "${root}/socket/registry.sock" ]; then
					echo "::warning::CODEX_ISOLATION deps_skipped reason=proxy_unavailable" >&2
					stop_deps_broker
				else
					proxy_state="on"
				fi
			fi
		else
			echo "::warning::CODEX_ISOLATION dependency staging failed; the agent runs without preinstalled dependencies." >&2
		fi
		if [ "${proxy_state}" = on ]; then
			image="$(build_image)"
			# Only filtered manifests, the restricted proxy and the venv enter
			# this network-isolated container, never the source tree.
			timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --init \
				--name "codex-isolated-deps-$$" --label "coding-workflows.codex-isolated.root=${root}" \
				--user "$(id -u):$(id -g)" --network none --cap-drop ALL --security-opt no-new-privileges \
				--pids-limit 512 --memory 6g --cpus "$(cpu_limit 4)" \
				--mount "type=bind,src=${stage},dst=/codex-deps" \
				--mount "type=bind,src=${root}/venv,dst=/opt/codex-venv" \
				--mount "type=bind,src=${root}/socket,dst=/socket" \
				--mount "type=bind,src=${root}/support/dependency_registry_proxy.py,dst=/support/dependency_registry_proxy.py,readonly" \
				--env HTTPS_PROXY=http://127.0.0.1:3128 --env https_proxy=http://127.0.0.1:3128 \
				--env HTTP_PROXY=http://127.0.0.1:3128 --env http_proxy=http://127.0.0.1:3128 \
				--env npm_config_https_proxy=http://127.0.0.1:3128 --env npm_config_proxy=http://127.0.0.1:3128 \
				--env YARN_HTTPS_PROXY=http://127.0.0.1:3128 --env YARN_HTTP_PROXY=http://127.0.0.1:3128 \
				--env PIP_PROXY=http://127.0.0.1:3128 --env NO_PROXY= --env no_proxy= \
				--env HOME=/tmp/agent-home --env PYTHONDONTWRITEBYTECODE=1 --workdir /codex-deps "${image}" /bin/bash -c '
					set -u
					python3 /support/dependency_registry_proxy.py bridge /socket/registry.sock 3128 &
					bridge_pid=$!
					trap "kill ${bridge_pid} 2>/dev/null || true" EXIT
					python3 -c "import socket,time; [(time.sleep(.1) if s.connect_ex((\"127.0.0.1\",3128)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)" \
						|| { echo "::warning::CODEX_ISOLATION dependency proxy bridge unavailable" >&2; exit 0; }
					install_failed=false
					python3 -m venv --system-site-packages /opt/codex-venv || exit 1
					export PATH=/opt/codex-venv/bin:$PATH
					case "$(cat .codex-deps/node)" in
						present) echo "node_modules already present — skipping the npm install" ;;
						npm-ci) npm ci --ignore-scripts 2>&1 || install_failed=true ;;
						yarn) npx --yes yarn install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true ;;
						pnpm) npx --yes pnpm install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true ;;
						npm-install) npm install --ignore-scripts 2>&1 || install_failed=true ;;
					esac
					case "$(cat .codex-deps/python)" in
						requirements) pip install -r .codex-deps/requirements.txt 2>&1 || install_failed=true ;;
						both) pip install -r .codex-deps/requirements.txt -r .codex-deps/build.txt -r .codex-deps/base.txt -r .codex-deps/dev.txt 2>&1 \
							|| { install_failed=true; pip install -r .codex-deps/requirements.txt -r .codex-deps/build.txt -r .codex-deps/base.txt 2>&1 || :; } ;;
						pyproject) pip install -r .codex-deps/build.txt -r .codex-deps/base.txt -r .codex-deps/dev.txt 2>&1 \
							|| { install_failed=true; pip install -r .codex-deps/build.txt -r .codex-deps/base.txt 2>&1 || :; } ;;
					esac
					if [ -f .codex-deps/want-pytest ] && ! python3 -c "import pytest" >/dev/null 2>&1; then
						pip install pytest 2>&1 || true
					fi
					[ "${install_failed}" = false ] || echo "::warning::Some project dependencies could not be installed in the isolated dependency phase; the agent marks validations it cannot run as UNVERIFIED."
				' >&2 || echo "::warning::CODEX_ISOLATION dependency phase failed; the agent runs without preinstalled dependencies." >&2
			stop_deps_broker
			if [ -d "${stage}/node_modules" ] && [ ! -L "${stage}/node_modules" ] && [ ! -e "${root}/work/node_modules" ] && [ ! -L "${root}/work/node_modules" ]; then
				mv -- "${stage}/node_modules" "${root}/work/node_modules" || echo "::warning::CODEX_ISOLATION node_modules hand-off failed" >&2
			elif [ -e "${stage}/node_modules" ] || [ -L "${stage}/node_modules" ]; then
				echo "::warning::CODEX_ISOLATION node_modules hand-off skipped" >&2
			fi
			if [ -f "${stage}/.codex-deps/source-install" ]; then
				# Editable builds may write into the disposable copy. prep-finalize
				# restores snapshotted files and quarantines new build output before
				# the agent runs; nothing from this step is transferred to the host.
				timeout --signal=TERM --kill-after=10s 600s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --init \
					--name "codex-isolated-deps-offline-$$" --label "coding-workflows.codex-isolated.root=${root}" \
					--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
					--pids-limit 512 --memory 6g --cpus "$(cpu_limit 4)" --tmpfs /tmp:rw,nosuid,nodev,exec,size=2g \
					--mount "type=bind,src=${root}/work,dst=${workdir}" \
					--mount "type=bind,src=${root}/venv,dst=/opt/codex-venv" \
					--env HOME=/tmp/agent-home --env PYTHONDONTWRITEBYTECODE=1 --workdir "${workdir}" "${image}" /bin/bash -c '
						export PATH=/opt/codex-venv/bin:$PATH
						pip install --no-deps --no-build-isolation --no-index -e .
					' >&2 || echo "::warning::CODEX_ISOLATION offline source install failed; the agent marks validations it cannot run as UNVERIFIED." >&2
			fi
		fi
		rm -rf -- "${stage}"
		workspace_py prep-finalize "${workdir}" "${root}/work" "${root}/manifest.json" || fail "dependency phase output rejected"
	fi
	trap - EXIT
	echo "CODEX_ISOLATION prepare root=${root} workdir=${workdir} deps=${want_deps} proxy=${proxy_state}" >&2
	printf '%s\n' "${root}"
	exit 0
fi

# ---- run ----
case "${mode}" in read-only|workspace) ;; *) fail "--mode must be read-only or workspace" ;; esac
[ "${#codex_args[@]}" -gt 0 ] || fail "${engine} arguments missing"
[ "${engine}" = claude ] || [ -n "${OPENROUTER_API_KEY:-}" ] || fail "OPENROUTER_API_KEY is required by the host-side broker"
model=""
for ((i = 0; i < ${#codex_args[@]}; i++)); do
	if [ "${codex_args[$i]}" = "--model" ] && [ $((i + 1)) -lt "${#codex_args[@]}" ]; then
		model="${codex_args[$((i + 1))]}"
	elif [[ "${codex_args[$i]}" == --model=* ]]; then
		model="${codex_args[$i]#--model=}"
	fi
done
[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || fail "${engine} arguments must name a valid --model"

# A regular, non-symlink file given by an absolute path; prints its real path.
trusted_file()
{
	local path="$1" label="$2"
	[[ "${path}" == /* ]] && [ -f "${path}" ] && [ ! -L "${path}" ] || fail "${label} must be an existing regular file: ${path}"
	realpath -e -- "${path}"
}
if [ "${engine}" = claude ]; then
	[[ "${claude_cli_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid --claude-cli-version"
	[[ "${claude_models}" =~ ^claude-[a-z0-9.-]+(,claude-[a-z0-9.-]+)*$ ]] || fail "invalid --claude-models"
	[[ ",${claude_models}," == *",${model},"* ]] || fail "--model must be one of --claude-models"
	# The token file is read only by the host relay: it is never mounted, and
	# its contents never reach an argument or the container environment.
	claude_token_file="$(trusted_file "${claude_token_file}" "--claude-token-file")"
	claude_settings="$(trusted_file "${claude_settings}" "--claude-settings")"
	claude_guard_hook="$(trusted_file "${claude_guard_hook}" "--claude-guard-hook")"
	claude_instructions="$(trusted_file "${claude_instructions}" "--claude-instructions")"
fi

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
if [ "${engine}" = codex ]; then
	[[ "${reasoning}" =~ ^(xhigh|high|medium|low|none)$ ]] || fail "invalid reasoning level"
	[[ "${web_search}" =~ ^(live|disabled)$ ]] || fail "invalid web-search value"
fi

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

if [ "${engine}" = claude ]; then
	install -m 0644 "${support_dir}/claude_anthropic_relay.py" "${root}/support/claude_anthropic_relay.py"
	install -m 0644 "${claude_settings}" "${root}/support/settings.json"
	install -m 0644 "${claude_guard_hook}" "${root}/support/guard.py"
	install -m 0644 "${claude_instructions}" "${root}/support/instructions.md"
else
	install -m 0644 "${support_dir}/clarify_openrouter_broker.py" "${root}/support/clarify_openrouter_broker.py"
	install -m 0644 "${support_dir}/write_codex_config.sh" "${root}/support/write_codex_config.sh"
	install -m 0644 "${support_dir}/codex_model_catalog.json" "${root}/support/codex_model_catalog.json"
fi

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
if [ -n "${claude_home}" ]; then
	[ "${engine}" = claude ] || fail "--claude-home needs --engine claude"
	mkdir -p "${claude_home}"
	chmod 0700 "${claude_home}"
	claude_home="$(realpath -e -- "${claude_home}")"
	valid_path "${claude_home}" || fail "claude home path rejected"
	case "${claude_home}" in "${workdir}"|"${workdir}"/*) fail "claude home must be outside the workdir" ;; esac
	mounts+=(--mount "type=bind,src=${claude_home},dst=/home/agent/.claude")
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
if [ "${engine}" = claude ]; then
	env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 \
		"${python_bin}" "${support_dir}/claude_anthropic_relay.py" broker "${root}/socket/provider.sock" "${claude_token_file}" "${claude_models}" &
else
	env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 \
		"${python_bin}" "${support_dir}/clarify_openrouter_broker.py" broker "${root}/socket/provider.sock" &
fi
broker_pid=$!
relay_unavailable()
{
	if [ "${engine}" = claude ]; then
		echo "CODEX_ISOLATION relay_unavailable engine=claude" >&2
		exit 73
	fi
	fail "$1"
}
for _ in $(seq 1 50); do
	[ -S "${root}/socket/provider.sock" ] && break
	kill -0 "${broker_pid}" 2>/dev/null || relay_unavailable "model broker failed to start"
	command -p sleep 0.1
done
[ -S "${root}/socket/provider.sock" ] || relay_unavailable "model broker unavailable"

if [ "${mode}" = workspace ]; then
	limits=(--pids-limit 1024 --memory 6g --cpus "$(cpu_limit 4)" --tmpfs "/tmp:rw,nosuid,nodev,exec,size=2g")
else
	limits=(--pids-limit 256 --memory 2g --cpus "$(cpu_limit 2)" --tmpfs "/tmp:rw,nosuid,nodev,size=256m")
fi

if [ "${engine}" = claude ]; then
	echo "CODEX_ISOLATION run engine=claude mode=${mode} model=${model} workdir=${workdir} persistent_root=$([ "${ephemeral}" = true ] && echo false || echo true) session_store=$([ -n "${claude_home}" ] && echo true || echo false) hide_claude_md=${hide_claude_md}" >&2
	# The CLI talks to the in-container bridge with a placeholder token; the
	# host relay swaps in the account token and checks the model.
	engine_env=(
		--env HOME=/home/agent --env PYTHONDONTWRITEBYTECODE=1
		--env ANTHROPIC_BASE_URL=http://127.0.0.1:8765
		--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder
		--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1
		--env "CLAUDE_ISOLATED_WORKDIR=${workdir}"
	)
	# The CLI refuses bypassPermissions as root unless it is told it runs in a
	# sandbox. The container runs as the runner's UID, which is root only on
	# runners that run as root; the container is the sandbox either way.
	[ "$(id -u)" -ne 0 ] || engine_env+=(--env IS_SANDBOX=1)
	inner_name="claude-isolated"
	# shellcheck disable=SC2016  # expanded inside the container
	inner_script='
		set -euo pipefail
		# Spike S10: the CLI ignores the allow list of an untrusted workspace.
		printf "{\"projects\":{\"%s\":{\"hasTrustDialogAccepted\":true}}}\n" "${CLAUDE_ISOLATED_WORKDIR}" > "${HOME}/.claude.json"
		if [ -d /opt/codex-venv/bin ]; then
			export PATH="/opt/codex-venv/bin:${PATH}"
			printf "export PATH=/opt/codex-venv/bin:\${PATH}\n" > "${HOME}/.bash_profile"
		fi
		python3 /support/claude_anthropic_relay.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap "kill ${bridge_pid} 2>/dev/null || true" EXIT
		python3 -c "import socket,time; [(time.sleep(.1) if s.connect_ex((\"127.0.0.1\",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)" \
			|| { echo "::error::CODEX_ISOLATION model bridge unavailable" >&2; exit 98; }
		claude "$@"
	'
else
	echo "CODEX_ISOLATION run mode=${mode} model=${model} reasoning=${reasoning} web_search=${web_search} workdir=${workdir} persistent_root=$([ "${ephemeral}" = true ] && echo false || echo true)" >&2
	engine_env=(
		--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex --env PYTHONDONTWRITEBYTECODE=1
		--env CODEX_ISOLATED_PROXY_KEY=isolated-placeholder
		--env "CODEX_ISOLATED_MODEL=${model}" --env "CODEX_ISOLATED_REASONING=${reasoning}"
		--env "CODEX_ISOLATED_WEB_SEARCH=${web_search}" --env "CODEX_ISOLATED_WORKDIR=${workdir}"
	)
	inner_name="codex-isolated"
	# shellcheck disable=SC2016  # expanded inside the container
	inner_script='
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
	'
fi
# No host checkout, .git, HOME, Docker socket, token or Git remote is
# mounted, and no runner environment variable is passed in.
rc=0
env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm -i --init \
	--name "${container}" --label "coding-workflows.codex-isolated.root=${root}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL \
	--security-opt no-new-privileges "${limits[@]}" \
	--tmpfs /home/agent:rw,nosuid,nodev,size=512m,mode=1777 \
	"${mounts[@]}" \
	"${engine_env[@]}" \
	--workdir "${workdir}" "${image}" /bin/bash -c "${inner_script}" "${inner_name}" "${codex_args[@]}" < "${root}/prompt" &
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
