#!/usr/bin/env bash
# Isolate PR dependency builds and OpenCode writer/test subprocesses.
set -euo pipefail

action="${1:-}"
support="${SUPPORT_SCRIPTS_DIR:-scripts}"
root="${REVIEW_SANDBOX_ROOT:-}"
workspace="${GITHUB_WORKSPACE:-$PWD}"
case "${action}" in prepare|run|cleanup) ;; *) exit 2 ;; esac
command -v docker >/dev/null && command -v python3 >/dev/null || { echo '::error::Review isolation requires Docker and Python' >&2; exit 1; }
[ -f "${support}/review_untrusted_workspace.py" ] && [ -f "${support}/clarify_openrouter_broker.py" ] && [ -f "${support}/review_sandbox/Dockerfile" ] || { echo '::error::Review isolation support missing' >&2; exit 1; }

if [ "${action}" != cleanup ]; then
	# The review worktree is distinct from the runner checkout. An explicitly
	# selected but unavailable worktree must never fall back to the checkout.
	if [ "${WORKSPACE_PATH+x}" ]; then
		workspace="${WORKSPACE_PATH}"
	fi
	[ -n "${workspace}" ] && workspace="$(realpath -e -- "${workspace}")" && [ -d "${workspace}" ] || { echo '::error::Review worktree unavailable' >&2; exit 1; }
	workspace_identity="$(stat -Lc '%d:%i' -- "${workspace}")" || { echo '::error::Review worktree identity unavailable' >&2; exit 1; }
fi

# Only the trusted prepare step may select a root; never accept a path from
# the PR checkout or a model-controlled environment variable.
if [ "${action}" = prepare ]; then
	root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/review-isolated-XXXXXXXX")"
	dep_container="review-deps-$$"
	trap 'env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${dep_container}" >/dev/null 2>&1 || true; rm -rf -- "${root}"' EXIT
	mkdir -m 0700 "${root}/socket" "${root}/home"
	mkdir -m 0755 "${root}/source"
	printf '%s\0%s\0' "${workspace}" "${workspace_identity}" > "${root}/workspace-identity"
	PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${workspace}" "${root}/source" "${root}/baseline.json"
	version="${OPENCODE_VERSION:-1.18.23}"
	[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo '::error::Invalid review OpenCode version' >&2; exit 1; }
	image="$(env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker build -q --build-arg "OPENCODE_VERSION=${version}" -f "${support}/review_sandbox/Dockerfile" "${support}/review_sandbox")"
	[ -n "${image}" ] || exit 1
	printf '%s\n' "${image}" > "${root}/image"
	# Network access is confined to the credential-free dependency phase.
	# The user cannot specify the image, executable, mounts or Docker flags.
	timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${dep_container}" --user "$(id -u):$(id -g)" \
		--cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
		--mount "type=bind,src=${root}/source,dst=/source" \
		--workdir /source "${image}" /bin/bash -c '
			set -u
			install_failed=false
			python3 -m venv --system-site-packages /source/.review-venv || exit 1
			export PATH=/source/.review-venv/bin:$PATH
			if [ -f package-lock.json ]; then
				echo "Found package-lock.json — running npm ci"
				npm ci --ignore-scripts 2>&1 || install_failed=true
			elif [ -f yarn.lock ]; then
				echo "Found yarn.lock — running yarn install"
				yarn install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
			elif [ -f pnpm-lock.yaml ]; then
				echo "Found pnpm-lock.yaml — running pnpm install"
				npx pnpm install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
			elif [ -f package.json ]; then
				echo "Found package.json (no lockfile) — running npm install"
				npm install --ignore-scripts 2>&1 || install_failed=true
			fi
			if [ -f requirements.txt ]; then
				echo "Found requirements.txt — running pip install"
				pip install -r requirements.txt 2>&1 || install_failed=true
			elif [ -f pyproject.toml ] && [ ! -f package.json ]; then
				echo "Found pyproject.toml — running pip install"
				pip install -e ".[dev]" 2>&1 || pip install -e . 2>&1 || install_failed=true
			fi
			[ "$install_failed" = false ] || echo "::warning::Some project dependencies could not be installed. Editor validation may be limited."
			pytest_bootstrap_wanted=false
			if [ -f pytest.ini ] || [ -f conftest.py ] || [ -f tests/conftest.py ]; then
				pytest_bootstrap_wanted=true
			elif [ -f pyproject.toml ] && grep -q "^\[tool\.pytest\.ini_options\]" pyproject.toml; then
				pytest_bootstrap_wanted=true
			elif [ -f setup.cfg ] && grep -q "^\[tool:pytest\]" setup.cfg; then
				pytest_bootstrap_wanted=true
			elif [ -f tox.ini ] && grep -q "^\[pytest\]" tox.ini; then
				pytest_bootstrap_wanted=true
			fi
			if [ "$pytest_bootstrap_wanted" = true ]; then
				if python3 -c "import pytest" >/dev/null 2>&1; then
					echo "Repository declares pytest configuration and pytest is already importable."
				else
					echo "Repository declares pytest configuration but pytest is not importable — installing pytest"
					python3 -m pip install pytest 2>&1 || python3 -m pip install --user --break-system-packages pytest 2>&1 || true
					if python3 -c "import pytest" >/dev/null 2>&1; then
						echo "pytest bootstrap succeeded."
					else
						echo "::warning::pytest is declared by this repository but could not be installed; the editor cannot run its pytest suites and will fall back to ad-hoc validation."
					fi
				fi
			fi
		' || { echo '::error::Review dependency isolation failed' >&2; exit 1; }
	# PR build backends may write source files. Never publish their writes as
	# editor output: restore the exact host snapshot before launching the model.
	PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" refresh "${workspace}" "${root}/source" "${root}/baseline.json"
	printf 'REVIEW_SANDBOX_ROOT=%s\n' "${root}" >> "${GITHUB_ENV:?}"
	trap - EXIT
	exit 0
fi

[[ "${root}" == "${RUNNER_TEMP:-/tmp}"/review-isolated-* ]] && \
	[ "$(dirname "$(realpath -e -- "${root}" 2>/dev/null || echo /invalid)")" = "$(realpath -e -- "${RUNNER_TEMP:-/tmp}")" ] && \
	[ -f "${root}/image" ] && [ -f "${root}/baseline.json" ] || { echo '::error::Review sandbox not prepared' >&2; exit 1; }
if [ "${action}" = cleanup ]; then
	if [ -f "${root}/active-container" ]; then
		active_container="$(< "${root}/active-container")"
		[[ "${active_container}" =~ ^review-editor-[0-9]+$ ]] && env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${active_container}" >/dev/null 2>&1 || true
	fi
	rm -rf -- "${root}"
	exit 0
fi

# A prepared snapshot cannot be published into a different worktree, even
# when the environment points to another valid directory on a later step.
[ -f "${root}/workspace-identity" ] && cmp -s "${root}/workspace-identity" <(printf '%s\0%s\0' "${workspace}" "${workspace_identity}") || { echo '::error::Review worktree changed since snapshot' >&2; exit 1; }

[ "$#" -eq 6 ] || exit 2
prompt="$2"
output="$3"
model="$4"
variant="$5"
config="$6"
[[ "${model}" =~ ^[a-zA-Z0-9._:+-]+/[a-zA-Z0-9._:+/-]+$ ]] && [[ "${variant}" =~ ^(none|low|medium|high|xhigh)$ ]] || exit 2
[ -n "${OPENROUTER_API_KEY:-}" ] && [ -s "${prompt}" ] || { echo '::error::Review relay preflight failed' >&2; exit 1; }

# Never mount a host-generated config with other providers or host paths.
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${config}" "${root}/config.json" "${model}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
	config = json.load(handle)
assert config["provider"]["openrouter"]["options"]["baseURL"] == "https://openrouter.ai/api/v1"
assert config["model"] == "openrouter/" + sys.argv[3]
config["provider"]["openrouter"]["options"] = {"baseURL": "http://127.0.0.1:8765/api/v1", "apiKey": "{env:OPENROUTER_API_KEY}"}
config.pop("mcp", None)  # Serena runs only on the host, never inside the writer.
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	json.dump(config, handle)
PY
then
	echo '::error::Review isolation config rejected' >&2
	exit 1
fi
install -m 0600 "${prompt}" "${root}/prompt"
image="$(< "${root}/image")"
[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid review image ID' >&2; exit 1; }
container="review-editor-$$"
printf '%s\n' "${container}" > "${root}/active-container"
broker_pid=""
finish()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${container}" >/dev/null 2>&1 || true
	rm -f "${root}/active-container" "${root}/socket/provider.sock"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 \
	python3 "${support}/clarify_openrouter_broker.py" review-broker "${root}/socket/provider.sock" &
broker_pid=$!
for _ in $(seq 1 50); do
	[ -S "${root}/socket/provider.sock" ] && break
	kill -0 "${broker_pid}" 2>/dev/null || exit 1
	sleep 0.1
done
[ -S "${root}/socket/provider.sock" ] || exit 1

# No host checkout, HOME, Docker socket, tokens or Git remote is mounted.
rc=0
env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${container}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL \
	--security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
	--tmpfs /tmp:rw,nosuid,nodev,size=128m \
	--mount "type=bind,src=${root}/source,dst=/source" \
	--mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
	--mount "type=bind,src=${root}/home,dst=/home/agent" \
	--mount "type=bind,src=${root}/config.json,dst=/config.json,readonly" \
	--mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
	--mount "type=bind,src=$(realpath "${support}/clarify_openrouter_broker.py"),dst=/bridge.py,readonly" \
	--env HOME=/home/agent --env OPENROUTER_API_KEY=isolated-placeholder \
	--env "MODEL=${model}" --env "VARIANT=${variant}" --workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		python3 /bridge.py review-bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		export PATH=/source/.review-venv/bin:$PATH OPENCODE_CONFIG=/config.json NO_COLOR=1
		opencode run --dir /source -m "openrouter/${MODEL}" --agent writer --variant "${VARIANT}" --title coding-workflows-agent-run --print-logs --log-level INFO --auto < /prompt
	' > "${output}" || rc=$?
	# Never transfer on a failed model invocation or a swapped host baseline.
	if [ "${rc}" -eq 0 ]; then
		# The marker survives a killed/incomplete transfer. The editor wrapper
		# fails the step instead of treating a partial host edit as a retry.
		: > "${RUNTIME_DIR:?}/review_sandbox_transfer_failed"
		if PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${workspace}" "${root}/source" "${root}/baseline.json"; then
			rm -f "${RUNTIME_DIR}/review_sandbox_transfer_failed"
		else
			rc=1
		fi
	fi
	exit "${rc}"
