#!/usr/bin/env bash
# Credential-free read-profile Claude runner; called only by ai_engine.sh.
set -euo pipefail

role="${1:?}" prompt_file="${2:?}" out_file="${3:?}" workdir="${4:?}"
run_dir="${5:?}" model="${6:?}" effort="${7:?}" instructions="${8:?}"
hide_claude_md="${9:-false}" session_id="${10:-}"
engine_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_root=""
socket_root=""
broker_pid=""
container_name=""
model_pid=""

cleanup_attempt()
{
	if [ -n "${container_name}" ]; then
		docker rm -f "${container_name}" >/dev/null 2>&1 || true
		container_name=""
	fi
	if [ -n "${model_pid}" ]; then
		kill "${model_pid}" 2>/dev/null || true
		wait "${model_pid}" 2>/dev/null || true
		model_pid=""
	fi
	if [ -n "${broker_pid}" ]; then
		kill "${broker_pid}" 2>/dev/null || true
		wait "${broker_pid}" 2>/dev/null || true
		broker_pid=""
	fi
}
cleanup()
{
	cleanup_attempt
	if [ -n "${run_root}" ]; then
		rm -rf -- "${run_root}"
	fi
	if [ -n "${socket_root}" ]; then
		rm -rf -- "${socket_root}"
	fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

unavailable()
{
	printf 'isolation_unavailable\n' > "${run_dir}/fallback_reason"
	echo "CLAUDE_READ_ISOLATION role=${role} outcome=unavailable reason=${1}" >&2
	exit 75
}

command -v docker >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || unavailable docker_missing
for support_file in claude_anthropic_relay.py claude_read_snapshot.py review_untrusted_workspace.py claude_engine.py; do
	[ -f "${engine_dir}/${support_file}" ] || unavailable support_missing
done
guard_hook="$(PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" support-file --name guard-hook 2>/dev/null)" || unavailable support_missing
[ -f "${guard_hook}" ] || unavailable support_missing
pool_dir="${CLAUDE_ENGINE_POOL_DIR:-${RUNNER_TEMP:-/tmp}/claude-pool}"
pool_dir="$(realpath -e -- "${pool_dir}")" || unavailable support_missing
workdir="$(realpath -e -- "${workdir}")" || unavailable snapshot_rejected
case "${workdir}/" in "${pool_dir}/"*) unavailable pool_inside_workdir ;; esac
case "${pool_dir}/" in "${workdir}/"*) unavailable pool_inside_workdir ;; esac
for trusted_mount in "${instructions}" "${guard_hook}"; do
	[ -f "${trusted_mount}" ] || unavailable policy_unavailable
	trusted_mount="$(realpath -e -- "${trusted_mount}")" || unavailable policy_unavailable
	case "${trusted_mount}" in "${pool_dir}/"*) unavailable policy_unavailable ;; esac
done
run_root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/claude-read-XXXXXXXX")" || unavailable snapshot_rejected
# AF_UNIX paths are capped at 108 bytes even when RUNNER_TEMP is deeply nested.
socket_root="$(mktemp -d /tmp/crsock-XXXXXXXX)" || unavailable snapshot_rejected
# Only an ordinary prompt can be mounted. Copy it instead of following a
# symlink into the token pool (or another host-only file) at docker run time.
[ -f "${prompt_file}" ] && [ ! -L "${prompt_file}" ] || unavailable snapshot_rejected
case "$(realpath -e -- "${prompt_file}")" in "${pool_dir}/"*) unavailable snapshot_rejected ;; esac
if ! env -i PATH="${PATH}" HOME="${run_root}" PYTHONDONTWRITEBYTECODE=1 \
	python3 - "${prompt_file}" "${run_root}/prompt" <<'PY'
import os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(fd, 'rb') as source:
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
        raise ValueError('unsafe prompt')
    data = source.read(32 * 1024 * 1024 + 1)
    if len(data) != info.st_size:
        raise ValueError('prompt changed')
with open(sys.argv[2], 'xb') as target:
    target.write(data)
PY
then unavailable snapshot_rejected; fi
snapshot_args=()
[ "${hide_claude_md}" = true ] && snapshot_args+=(--exclude-root-claude-md)
# No host runner environment, git credential helper, or GitHub token reaches
# the snapshot's git subprocesses. The objects path is an internal host value.
snapshot_env=(env -i PATH="${PATH}" HOME="${run_root}" PYTHONDONTWRITEBYTECODE=1)
if [ -n "${GIT_DIR:-}" ] && [ -n "${GIT_WORK_TREE:-}" ] && [ "$(realpath -m -- "${GIT_WORK_TREE}")" = "${workdir}" ]; then
	snapshot_env+=(GIT_DIR="${GIT_DIR}" GIT_WORK_TREE="${workdir}")
fi
objects_path="$("${snapshot_env[@]}" python3 "${engine_dir}/claude_read_snapshot.py" \
	"${workdir}" "${run_root}/source" "${run_root}/gitdir" "${snapshot_args[@]}")" || unavailable snapshot_rejected
case "${objects_path}" in "${pool_dir}/"*) unavailable snapshot_rejected ;; esac
PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" settings \
	--checkout /source --out "${run_root}/claude-settings.json" --profile read \
	--guard-hook /guard.py --read-guard-hook /claude_engine.py >/dev/null || unavailable policy_unavailable
chmod 0644 "${run_root}/claude-settings.json"
version="$(PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" config --key cli_version)" || unavailable policy_unavailable
probe_model="$(PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" config --key probe_model)" || unavailable policy_unavailable
build_dir="${run_root}/empty"
mkdir "${build_dir}"
image="$(env -i PATH="${PATH}" HOME="${run_root}" docker build -q --build-arg "CLAUDE_CLI_VERSION=${version}" -f - "${build_dir}" <<'DOCKERFILE'
FROM node:22.16.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG CLAUDE_CLI_VERSION
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}"
DOCKERFILE
)" || unavailable image_build_failed
[ -n "${image}" ] || unavailable image_build_failed
echo "CLAUDE_READ_ISOLATION role=${role} outcome=started reason=isolated" >&2
if [ -n "${session_id}" ]; then
	echo '::notice::Claude read isolation starts a fresh session.' >&2
fi
mapfile -t accounts
for account in "${accounts[@]}"; do
	[[ "${account}" =~ ^[A-Z0-9_]{1,64}$ ]] || unavailable support_missing
	socket_path="${socket_root}/provider.sock"
	rm -f -- "${socket_path}"
	env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_anthropic_relay.py" broker \
		"${socket_path}" "${pool_dir}/tokens/${account}" "${model},${probe_model}" &
	broker_pid=$!
	for ((i=0; i<50; i++)); do
		[ -S "${socket_path}" ] && break
		kill -0 "${broker_pid}" 2>/dev/null || break
		sleep 0.1
	done
	if [ ! -S "${socket_path}" ]; then
		echo "CLAUDE_POOL run role=${role} account=${account} outcome=crashed reason=relay_unavailable" >&2
		cleanup_attempt
		continue
	fi
	container_name="claude-read-${$}-${RANDOM}"
	transcript="${run_dir}/transcript-${account}.jsonl"
	stderr_file="${run_dir}/stderr-${account}.txt"
	# shellcheck disable=SC2054 # Docker mount/tmpfs options contain comma-separated values.
	cmd=(env -i PATH="${PATH}" HOME="${run_root}" docker run --rm --name "${container_name}" --user "$(id -u):$(id -g)"
		--network none --read-only --cap-drop ALL --security-opt no-new-privileges
		--pids-limit 128 --memory 2g --cpus 2
		--tmpfs /tmp:rw,nosuid,nodev,size=64m --tmpfs /home/agent:rw,nosuid,nodev,size=64m,mode=1777
		--mount "type=bind,src=${run_root}/source,dst=/source,readonly"
		--mount "type=bind,src=${run_root}/prompt,dst=/prompt,readonly"
		--mount "type=bind,src=${socket_root},dst=/socket,readonly"
		--mount "type=bind,src=${engine_dir}/claude_anthropic_relay.py,dst=/relay.py,readonly"
		--mount "type=bind,src=${engine_dir}/claude_engine.py,dst=/claude_engine.py,readonly"
		--mount "type=bind,src=${instructions},dst=/instructions.md,readonly"
		--mount "type=bind,src=${run_root}/claude-settings.json,dst=/settings.json,readonly"
		--mount "type=bind,src=${guard_hook},dst=/guard.py,readonly"
		--env HOME=/home/agent --env ANTHROPIC_BASE_URL=http://127.0.0.1:8765
		--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder
		--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1
		--env "CLAUDE_MODEL=${model}" --env "CLAUDE_EFFORT=${effort}" --workdir /source)
	if [ -n "${objects_path}" ]; then
		cmd+=(--mount "type=bind,src=${run_root}/gitdir,dst=/source/.git,readonly"
			--mount "type=bind,src=${objects_path},dst=/gitobjects,readonly")
	fi
	cmd+=("${image}" /bin/bash -c '
		set -euo pipefail
		printf "{\"projects\":{\"/source\":{\"hasTrustDialogAccepted\":true}}}\n" > "${HOME}/.claude.json"
		python3 /relay.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" \
			--system-prompt-file /instructions.md --setting-sources "" --settings /settings.json \
			--strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections \
			--tools Read,Grep,Glob,Bash --permission-mode dontAsk --output-format stream-json --verbose < /prompt
	')
	attempt_rc=0
	if [ -f "${engine_dir}/codex_stall_guard.sh" ]; then
		bash "${engine_dir}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
			--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- "${cmd[@]}" &
	else
		"${cmd[@]}" > "${transcript}" 2> "${stderr_file}" &
	fi
	model_pid=$!
	# An interruptible wait lets TERM run the trap while the model is active.
	wait "${model_pid}" || attempt_rc=$?
	model_pid=""
	cleanup_attempt
	verdict="$(PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" classify --transcript "${transcript}" --exit-code "${attempt_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
	outcome="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["outcome"])' "${verdict}")"
	reason="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["reason"])' "${verdict}")"
	echo "CLAUDE_POOL run role=${role} account=${account} outcome=${outcome} reason=${reason} exit_code=${attempt_rc}" >&2
	case "${outcome}" in
		success)
			PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" extract --transcript "${transcript}" --out "${out_file}" >&2
			ln -s -- "transcript-${account}.jsonl" "${run_dir}/successful-transcript.jsonl"
			exit 0 ;;
		usage_limit|auth_failed) continue ;;
		timeout) exit 124 ;;
		*) PYTHONDONTWRITEBYTECODE=1 python3 "${engine_dir}/claude_engine.py" extract --transcript "${transcript}" --out "${out_file}" >&2 || true
			exit 1 ;;
	esac
done
printf 'all_accounts_failed\n' > "${run_dir}/fallback_reason"
exit 75
