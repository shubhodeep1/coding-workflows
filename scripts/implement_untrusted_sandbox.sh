#!/usr/bin/env bash
# Run implement model CLIs in a disposable, credential-free PID namespace.
set -euo pipefail

action="${1:-}"
support="${IMPLEMENT_SANDBOX_SUPPORT_DIR:-}/scripts"
root="${IMPLEMENT_SANDBOX_ROOT:-}"
runtime="${RUNTIME_DIR:-/tmp}"
docker_clean()
{
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker "$@"
}
fail()
{
	printf '::error::Implement isolation %s\n' "$1" >&2
	printf '%s\n' "$1" > "${runtime}/implement_sandbox_failed"
	echo "IMPLEMENT_ISOLATION action=${action} outcome=failed reason=${1}" >&2
	exit 1
}
transfer_result()
{
	: > "${runtime}/implement_sandbox_transfer_failed"
	local -a protected=()
	[ -z "${IMPLEMENT_SANDBOX_PROTECTED_PATHS:-}" ] || protected=("${IMPLEMENT_SANDBOX_PROTECTED_PATHS}")
	if UNTRUSTED_WORKSPACE_PROFILE=implement PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${workspace}" "${root}/source" "${root}/baseline.json" "${protected[@]}" 2> "${runtime}/implement_sandbox_transfer_reason.txt"; then
		rm -f "${runtime}/implement_sandbox_transfer_failed"
	else
		fail transfer_rejected
	fi
}
case "${action}" in prepare|codex|claude|cleanup) ;; *) exit 2 ;; esac
command -v docker >/dev/null && command -v python3 >/dev/null || fail requires_docker_and_python
for required in implement_untrusted_sandbox.sh review_untrusted_workspace.py clarify_openrouter_broker.py claude_anthropic_relay.py review_sandbox/Dockerfile; do
	[ -f "${support}/${required}" ] || fail support_missing
done

if [ "${action}" = prepare ]; then
	version="${IMPLEMENT_SANDBOX_CODEX_VERSION:-v0.114.0}"
	[[ "${version}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail invalid_version
	root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/implement-isolated-XXXXXXXX")" || fail root_unavailable
	dep_container="implement-deps-$$"
	trap 'docker_clean rm -f "${dep_container}" >/dev/null 2>&1 || true; rm -rf -- "${root}"' EXIT
	mkdir -m 0700 "${root}/socket" "${root}/home" "${root}/bin"
	mkdir -m 0755 "${root}/source"
	workspace="${GITHUB_WORKSPACE:-$PWD}"
	git_dir=()
	if [ -n "${WORKSPACE_PATH:-}" ]; then
		workspace_root="$(realpath -e -- "${RUNNER_TEMP:-/tmp}/workspaces" 2>/dev/null || echo /invalid)"
		workspace="$(realpath -e -- "${WORKSPACE_PATH}" 2>/dev/null || echo /invalid)"
		checkout_git_dir="$(realpath -e -- "${GITHUB_WORKSPACE:-/invalid}/.git" 2>/dev/null || echo /invalid)"
		[ -d "${workspace}" ] && [[ "${workspace}" != *$'\n'* ]] && [ "$(dirname -- "${workspace}")" = "${workspace_root}" ] && [ -d "${checkout_git_dir}" ] || fail workspace_rejected
		git_dir=("${checkout_git_dir}")
	fi
	printf '%s\n' "${workspace}" > "${root}/workspace"
	UNTRUSTED_WORKSPACE_PROFILE=implement PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${workspace}" "${root}/source" "${root}/baseline.json" "${git_dir[@]}" || fail snapshot_rejected
	build_args=(--build-arg "OPENCODE_VERSION=1.18.23" --build-arg "CODEX_VERSION=${version}")
	if [ "${2:-}" = claude ]; then
		[ -f "${support}/ai_engine.sh" ] && [ -f "${support}/claude_engine.py" ] || fail support_missing
		# shellcheck source=/dev/null
		source "${support}/ai_engine.sh"
		claude_version="$(ai_engine_cli_version)" || fail support_missing
		[[ "${claude_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail invalid_version
		build_args+=(--build-arg "CLAUDE_CLI_VERSION=${claude_version}")
		printf 'claude\n' > "${root}/engine"
	fi
	image="$(docker_clean build -q "${build_args[@]}" -f "${support}/review_sandbox/Dockerfile" "${support}/review_sandbox")" || fail image_build_failed
	[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail image_invalid
	printf '%s\n' "${image}" > "${root}/image"
	# Credential-free dependency build; the model never sees this network phase.
	timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${dep_container}" \
		--user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
		--mount "type=bind,src=${root}/source,dst=/source" --workdir /source "${image}" /bin/bash -c '
			set -u
			python3 -m venv --system-site-packages /source/.review-venv || exit 1
			export PATH=/source/.review-venv/bin:$PATH
			if [ -f package-lock.json ]; then npm ci --ignore-scripts || true
			elif [ -f yarn.lock ]; then yarn install --frozen-lockfile --ignore-scripts || true
			elif [ -f pnpm-lock.yaml ]; then npx pnpm install --frozen-lockfile --ignore-scripts || true
			elif [ -f package.json ]; then npm install --ignore-scripts || true; fi
			if [ -f requirements.txt ]; then pip install -r requirements.txt || true
			elif [ -f pyproject.toml ] && [ ! -f package.json ]; then pip install -e ".[dev]" || pip install -e . || true; fi
			if [ -f pytest.ini ] || [ -f tests/conftest.py ]; then python3 -c "import pytest" || pip install pytest || true; fi
		' || fail dependency_build_failed
	UNTRUSTED_WORKSPACE_PROFILE=implement PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" refresh "${workspace}" "${root}/source" "${root}/baseline.json" || fail refresh_rejected
	printf '#!/usr/bin/env bash\nexec bash %q codex "$@"\n' "${support}/implement_untrusted_sandbox.sh" > "${root}/bin/codex"
	chmod 0755 "${root}/bin/codex"
	printf 'IMPLEMENT_SANDBOX_ROOT=%s\n' "${root}" >> "${GITHUB_ENV:?}"
	trap - EXIT
	echo 'IMPLEMENT_ISOLATION action=prepare outcome=ok reason=ready' >&2
	exit 0
fi

[[ "${root}" == "${RUNNER_TEMP:-/tmp}"/implement-isolated-* ]] && \
	[[ "$(basename -- "${root}")" =~ ^implement-isolated-[A-Za-z0-9]{8}$ ]] && \
	[ "${root}" = "$(realpath -e -- "${root}" 2>/dev/null || echo /invalid)" ] && \
	[ "$(dirname -- "${root}")" = "$(realpath -e -- "${RUNNER_TEMP:-/tmp}")" ] && \
	[ -f "${root}/baseline.json" ] && [ -f "${root}/image" ] || fail not_prepared
image="$(< "${root}/image")"
[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail image_invalid
if [ "${action}" = cleanup ]; then
	if [ -f "${root}/active-container" ]; then
		active="$(< "${root}/active-container")"
		[[ "${active}" =~ ^implement-editor-[0-9]+$ ]] && docker_clean rm -f "${active}" >/dev/null 2>&1 || true
	fi
	rm -rf -- "${root}"
	echo 'IMPLEMENT_ISOLATION action=cleanup outcome=ok reason=removed' >&2
	exit 0
fi
[ -f "${root}/workspace" ] || fail not_prepared
workspace="$(< "${root}/workspace")"
[ -d "${workspace}" ] || fail workspace_rejected
container="implement-editor-$$"
broker_pid=""
finish()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	docker_clean rm -f "${container}" >/dev/null 2>&1 || true
	rm -f -- "${root}/active-container" "${root}/socket/provider.sock"
}
trap finish EXIT
on_signal()
{
	local signal_status="$1"
	trap - INT TERM
	finish
	# A killed attempt may still have useful edits. If transfer cannot finish,
	# the marker set before Docker started fails the workflow closed.
	if [ "${resynced:-false}" = true ]; then
		transfer_result
	fi
	exit "${signal_status}"
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
cpu_count="$(nproc)"
[ "${cpu_count}" -le 4 ] || cpu_count=4
common=(--rm --name "${container}" --user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory 6g --cpus "${cpu_count}" --tmpfs "/tmp:rw,nosuid,nodev,size=256m")

if [ "${action}" = codex ]; then
	shift
	# Probes do not access the checkout, HOME or broker.
	if [ "${1:-}" = --version ] || { [ "${1:-}" = exec ] && [ "${2:-}" = resume ] && [ "${3:-}" = --help ]; }; then
		docker_clean run "${common[@]}" --env HOME=/tmp "${image}" codex "$@"
		echo 'IMPLEMENT_ISOLATION action=codex outcome=ok reason=probe' >&2
		exit 0
	fi
	# codex_thread_reuse_run_once prefixes `--ask-for-approval` and `-c`.
	[[ " $* " == *" exec "* ]] || fail invalid_codex_args
	model=""
	args=("$@")
	for ((idx=0; idx < ${#args[@]}; idx++)); do
		if [ "${args[idx]}" = --model ] && [ "$((idx+1))" -lt "${#args[@]}" ]; then model="${args[idx+1]}"; fi
	done
	[[ "${model}" =~ ^[a-zA-Z0-9._:+-]+/[a-zA-Z0-9._:+/-]+$ ]] || fail invalid_model
	[ -n "${OPENROUTER_API_KEY:-}" ] || fail broker_unavailable
	# Rewrite only the known provider and catalog paths, rejecting all others.
	config_src="${CODEX_HOME:-${HOME}/.codex}/config.toml"
	mkdir -p "${root}/home/.codex"
	if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${config_src}" "${root}/home/.codex" "${support}/codex_model_catalog.json" 2>/dev/null <<'PY'
import pathlib
import re
import shutil
import sys

source, dest, trusted_catalog = (pathlib.Path(arg) for arg in sys.argv[1:4])
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
output = []
inside_mcp = False
base_count = 0
key_count = 0
project_count = 0
for line in lines:
	section = re.match(r"^\s*\[([^]]+)\]\s*$", line)
	if section:
		inside_mcp = section[1].startswith("mcp_servers")
	if inside_mcp:
		continue
	if re.match(r'^base_url\s*=\s*"https://openrouter.ai/api/v1"\s*$', line):
		base_count += 1
		line = 'base_url = "http://127.0.0.1:8765/api/v1"\n'
	elif re.match(r'^env_key\s*=\s*"OPENROUTER_API_KEY"\s*$', line):
		key_count += 1
		line = 'env_key = "CLARIFY_PROXY_KEY"\n'
	elif re.fullmatch(r'\[projects\."/[^"\n]+"\]\s*', line):
		project_count += 1
		line = '[projects."/source"]\n'
	elif line.lstrip().startswith("model_catalog_json = "):
		match = re.fullmatch(r'model_catalog_json = "([^"]+)"\s*', line)
		if not match or not pathlib.Path(match[1]).is_absolute() or not trusted_catalog.is_file():
			raise SystemExit(1)
		shutil.copyfile(trusted_catalog, dest / "model_catalog.json")
		line = 'model_catalog_json = "/home/agent/.codex/model_catalog.json"\n'
	elif line.strip() == 'network_access = true':
		line = 'network_access = false\n'
	elif re.search(r'"/(?!/)', line):
		raise SystemExit(1)
	output.append(line)
if base_count != 1 or key_count != 1 or project_count != 1:
	raise SystemExit(1)
(dest / "config.toml").write_text("".join(output), encoding="utf-8")
PY
	then fail config_rejected; fi
	UNTRUSTED_WORKSPACE_PROFILE=implement PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" resync "${workspace}" "${root}/source" "${root}/baseline.json" || fail resync_rejected
	: > "${runtime}/implement_sandbox_transfer_failed"
	resynced=true
	env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 python3 "${support}/clarify_openrouter_broker.py" broker "${root}/socket/provider.sock" &
	broker_pid=$!
	for _ in $(seq 1 50); do
		[ -S "${root}/socket/provider.sock" ] && break
		kill -0 "${broker_pid}" 2>/dev/null || break
		sleep 0.1
	done
	[ -S "${root}/socket/provider.sock" ] || fail broker_unavailable
	printf '%s\n' "${container}" > "${root}/active-container"
	rc=0
	docker_clean run -i "${common[@]}" --mount "type=bind,src=${root}/source,dst=/source" --mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
		--mount "type=bind,src=${root}/home,dst=/home/agent" --mount "type=bind,src=${support}/clarify_openrouter_broker.py,dst=/bridge.py,readonly" \
		--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex --env CLARIFY_PROXY_KEY=isolated-placeholder --workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		python3 /bridge.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		export PATH=/source/.review-venv/bin:$PATH
		exec codex "$@"
	' _ "$@" || rc=$?
	transfer_result
	echo "IMPLEMENT_ISOLATION action=codex outcome=ok reason=model_exit_${rc}" >&2
	exit "${rc}"
fi

# Claude is optional; unavailability falls back only to codex in this image.
[ "$#" -eq 6 ] || fail invalid_claude_args
role="$2"; prompt="$3"; output="$4"; workdir="$5"; session="$6"
[[ "${role}" =~ ^[A-Z][A-Z_]{0,39}$ ]] && [ "${workdir}" = "${workspace}" ] && [ -s "${prompt}" ] || fail invalid_claude_args
claude_fallback()
{
	local reason="$1"
	if type ai_engine_fallback >/dev/null 2>&1; then
		ai_engine_fallback "${role}" "${reason}"
	else
		echo "AI_ENGINE_FALLBACK role=${role} reason=${reason}" >&2
	fi
	echo "IMPLEMENT_ISOLATION action=claude outcome=fallback reason=${reason}" >&2
	exit 75
}
if [ "$(cat "${root}/engine" 2>/dev/null)" != claude ]; then
	claude_fallback sandbox_not_prepared
fi
for required in ai_engine.sh claude_engine.py claude_settings.json.tmpl codex_stall_guard.sh; do
	[ -f "${support}/${required}" ] || claude_fallback support_missing
done
# shellcheck source=/dev/null
source "${support}/ai_engine.sh"
export SUPPORT_ROOT_DIR="${IMPLEMENT_SANDBOX_SUPPORT_DIR}"
export SUPPORT_INSTRUCTIONS_FILE="${IMPLEMENT_SANDBOX_SUPPORT_DIR}/unattended_system_instructions.md"
resolved="$(_ai_engine_py resolve --role "${role}" --model-hint "${AI_ENGINE_MODEL_HINT:-}" --effort-hint "${AI_ENGINE_EFFORT_HINT:-}")" || claude_fallback policy_unavailable
model="$(_ai_engine_json_field "${resolved}" model)"
effort="$(_ai_engine_json_field "${resolved}" effort)"
probe="$(_ai_engine_py config --key probe_model)"
guard="$(_ai_engine_py support-file --name guard-hook)" || claude_fallback policy_unavailable
instructions="$(_ai_engine_instructions_file)" || claude_fallback instructions_missing
settings=(settings --checkout /source --out "${root}/settings.json" --profile write --guard-hook /guard.py)
[ "${ALLOW_WORKFLOW_EDITS:-false}" != true ] || settings+=(--allow-workflow-edits)
_ai_engine_py "${settings[@]}" || claude_fallback policy_unavailable
mapfile -t accounts < <(ai_engine_accounts)
[ "${#accounts[@]}" -gt 0 ] || claude_fallback no_credential
UNTRUSTED_WORKSPACE_PROFILE=implement PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" resync "${workspace}" "${root}/source" "${root}/baseline.json" || fail resync_rejected
: > "${runtime}/implement_sandbox_transfer_failed"
resynced=true
install -m 0600 "${prompt}" "${root}/prompt"
session_args=()
if [[ "${session}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
	if compgen -G "${root}/home/.claude/projects/*/${session}.jsonl" >/dev/null; then session_args=(--resume "${session}")
	else session_args=(--session-id "${session}"); fi
fi
hide_mount=()
[ "$(_ai_engine_py config --key hide_claude_md)" != true ] || [ ! -f "${root}/source/CLAUDE.md" ] || hide_mount=(--mount "type=bind,src=/dev/null,dst=/source/CLAUDE.md,readonly")
printf '%s\n' "${container}" > "${root}/active-container"
rc=75
for account in "${accounts[@]}"; do
	rm -f -- "${root}/socket/provider.sock" "${root}/transcript.jsonl"
	env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 python3 "${support}/claude_anthropic_relay.py" broker "${root}/socket/provider.sock" "$(ai_engine_pool_dir)/tokens/${account}" "${model},${probe}" &
	broker_pid=$!
	for _ in $(seq 1 50); do
		[ -S "${root}/socket/provider.sock" ] && break
		kill -0 "${broker_pid}" 2>/dev/null || break
		sleep 0.1
	done
	if [ ! -S "${root}/socket/provider.sock" ]; then
		kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
		continue
	fi
	# A previous account can create the session before exhausting its quota.
	if [ "${#session_args[@]}" -gt 0 ] && compgen -G "${root}/home/.claude/projects/*/${session}.jsonl" >/dev/null; then
		session_args=(--resume "${session}")
	fi
	run_rc=0
	# The trusted host stall guard owns the process group; Docker is still credential-free.
	bash "${support}/codex_stall_guard.sh" --phase "${role,,}" --engine claude --stdout-file "${root}/transcript.jsonl" --stderr-file "${root}/stderr" -- \
		env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run -i "${common[@]}" --mount "type=bind,src=${root}/source,dst=/source" --mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
		--mount "type=bind,src=${root}/home,dst=/home/agent" --mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
		--mount "type=bind,src=${support}/claude_anthropic_relay.py,dst=/relay.py,readonly" \
		--mount "type=bind,src=${instructions},dst=/instructions.md,readonly" --mount "type=bind,src=${guard},dst=/guard.py,readonly" \
		--mount "type=bind,src=${root}/settings.json,dst=/settings.json,readonly" "${hide_mount[@]}" \
		--env HOME=/home/agent --env ANTHROPIC_BASE_URL=http://127.0.0.1:8765 --env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder \
		--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1 --workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		printf "{\"projects\":{\"/source\":{\"hasTrustDialogAccepted\":true}}}\n" > "${HOME}/.claude.json"
		python3 /relay.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		export PATH=/source/.review-venv/bin:$PATH
		claude -p --model "$1" --effort "$2" --system-prompt-file /instructions.md --setting-sources "" --settings /settings.json \
			--strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections \
			--tools Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch --permission-mode bypassPermissions \
			--output-format stream-json --verbose "${@:3}" < /prompt
	' _ "${model}" "${effort}" "${session_args[@]}" || run_rc=$?
	# Stop the relay before trying another account; never leak a token to Docker.
	kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
	verdict="$(_ai_engine_py classify --transcript "${root}/transcript.jsonl" --exit-code "${run_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
	outcome="$(_ai_engine_json_field "${verdict}" outcome)"
	echo "CLAUDE_POOL run role=${role} account=${account} outcome=${outcome} reason=$(_ai_engine_json_field "${verdict}" reason) exit_code=${run_rc}" >&2
	case "${outcome}" in
		success) _ai_engine_py extract --transcript "${root}/transcript.jsonl" --out "${output}" >&2 && rc=0 || rc=1; break ;;
		usage_limit|auth_failed) continue ;;
		timeout) rc=124; break ;;
		*) _ai_engine_py extract --transcript "${root}/transcript.jsonl" --out "${output}" >&2 || true; rc=1; break ;;
	esac
done
transfer_result
if [ "${rc}" -eq 75 ]; then ai_engine_fallback "${role}" all_accounts_failed; fi
if [ "${rc}" -eq 75 ]; then
	echo 'IMPLEMENT_ISOLATION action=claude outcome=fallback reason=all_accounts_failed' >&2
else
	echo "IMPLEMENT_ISOLATION action=claude outcome=ok reason=model_exit_${rc}" >&2
fi
exit "${rc}"
