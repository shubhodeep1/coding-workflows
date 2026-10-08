#!/usr/bin/env bash
# Isolate a workflow-heal editor and validate its output before host transfer.
set -euo pipefail

prompt="${1:?prompt required}"
output="${2:?output required}"
# The prompt is bind-mounted into the container: refuse a symlink so it cannot
# expose another runner-readable file.
[ -f "${prompt}" ] && [ ! -L "${prompt}" ] || { echo 'HEAL_ISOLATED_EDITOR phase=prepare engine=codex outcome=failed reason=prompt_not_regular' >&2; exit 1; }
scope="${HEAL_SCOPE_FILE:-${RUNNER_TEMP:-/tmp}/heal-scope-${GITHUB_RUN_ID:-local}.txt}"
support="${HEAL_TRUSTED_SUPPORT_DIR:-${GITHUB_WORKSPACE:-.}/.codex-workflow-src/scripts}"
[ -s "${scope}" ] && [ -f "${support}/review_untrusted_workspace.py" ] || { echo 'HEAL_ISOLATED_EDITOR phase=prepare engine=codex outcome=failed reason=scope_or_support_missing' >&2; exit 1; }
if [ "${AI_ENGINE_RESOLVED_IMPLEMENT:-codex}" = claude ]; then
	# There is no write-capable Claude image on this path; never run Claude on
	# the credentialed host as a fallback. Use the isolated Codex engine.
	echo 'AI_ENGINE_FALLBACK role=IMPLEMENT reason=isolated_claude_unavailable' >&2
fi
root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/heal-isolated.XXXXXXXX")"
container="heal-editor-${GITHUB_RUN_ID:-local}-$$"
broker_pid=""
cleanup()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	env -i PATH="${PATH}" docker rm -f "${container}" >/dev/null 2>&1 || true
	rm -rf -- "${root}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 0700 "${root}/socket" "${root}/home" "${root}/results"
mkdir -m 0755 "${root}/source"
host_git_dir="${GIT_DIR:-}"
snapshot_args=()
[ -z "${host_git_dir}" ] || snapshot_args+=("${host_git_dir}")
PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${WORKSPACE_PATH:-${PWD}}" "${root}/source" "${root}/manifest.json" "${snapshot_args[@]}"
image="$(env -i PATH="${PATH}" docker build -q -f "${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/clarify_sandbox/Dockerfile" "${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/clarify_sandbox")"
[ -n "${image}" ] || { echo 'HEAL_ISOLATED_EDITOR phase=prepare engine=codex outcome=failed reason=image_build_failed' >&2; exit 1; }
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY:?}" CLARIFY_MODEL="${MODEL_EDITOR:-openai/gpt-6-sol}" PYTHONDONTWRITEBYTECODE=1 \
	python3 "${support}/clarify_openrouter_broker.py" broker "${root}/socket/provider.sock" &
broker_pid=$!
# Up to 30s: a loaded runner can take more than a few seconds to start Python.
for _ in $(seq 1 300); do
	[ ! -S "${root}/socket/provider.sock" ] || break
	kill -0 "${broker_pid}" 2>/dev/null || { echo 'HEAL_ISOLATED_EDITOR phase=prepare engine=codex outcome=failed reason=broker_exited' >&2; exit 1; }
	sleep 0.1
done
[ -S "${root}/socket/provider.sock" ] || { echo 'HEAL_ISOLATED_EDITOR phase=prepare engine=codex outcome=failed reason=broker_timeout' >&2; exit 1; }
wall="${HEAL_ISOLATED_EDITOR_WALL_SECS:-${EDITOR_MAX_WALL:-7800}}"
[[ "${wall}" =~ ^[1-9][0-9]{0,5}$ ]] || exit 1
run_editor()
{
	local editor_prompt="$1" rc=0
	env -i PATH="${PATH}" timeout --kill-after=30s "${wall}" docker run --rm --init --name "${container}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
	--pids-limit 256 --memory 4g --cpus 2 \
	--tmpfs /tmp:rw,nosuid,nodev,size=128m \
	--mount "type=bind,src=${root}/home,dst=/home/agent" \
	--mount "type=bind,src=${root}/source,dst=/source" \
	--mount "type=bind,src=$(realpath "${editor_prompt}"),dst=/prompt,readonly" \
	--mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
	--mount "type=bind,src=${support}/clarify_openrouter_broker.py,dst=/bridge.py,readonly" \
	--mount "type=bind,src=${root}/results,dst=/results" \
	--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex \
	--env CLARIFY_PROXY_KEY=isolated-placeholder --env "CLARIFY_MODEL=${MODEL_EDITOR:-openai/gpt-6-sol}" \
	--env "CLARIFY_REASONING=${MODEL_REASONING_EFFORT:-high}" \
	--workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		mkdir -p "${CODEX_HOME}"
		bash /source/scripts/write_codex_config.sh --model "${CLARIFY_MODEL}" --reasoning "${CLARIFY_REASONING}" --catalog-path /source/scripts/codex_model_catalog.json --project-path /source --config-path "${CODEX_HOME}/config.toml" --allow-elevation forbid
		sed -i -e '\''s#base_url = "https://openrouter.ai/api/v1"#base_url = "http://127.0.0.1:8765/api/v1"#'\'' -e '\''s/env_key = "OPENROUTER_API_KEY"/env_key = "CLARIFY_PROXY_KEY"/'\'' "${CODEX_HOME}/config.toml"
		python3 /bridge.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${CLARIFY_MODEL}" --sandbox danger-full-access < /prompt > /results/output 2> /results/stderr
	' || rc=$?
	env -i PATH="${PATH}" docker rm -f "${container}" >/dev/null 2>&1 || true
	if [ -n "$(env -i PATH="${PATH}" docker ps -aq --filter "name=^/${container}$")" ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=run engine=codex outcome=container_survived' >&2
		return 1
	fi
	if [ "${rc}" -ne 0 ]; then
		echo "HEAL_ISOLATED_EDITOR phase=run engine=codex outcome=failed reason=editor_exit_${rc}" >&2
		return 1
	fi
}
run_editor "${prompt}"
# Syntax checks also run in a credential-free container against the snapshot;
# never run a checkout script on the host after editor transfer.
validate_snapshot()
{
	local validation_rc=0
	env -i PATH="${PATH}" timeout --kill-after=30s 600 docker run --rm --init --name "${container}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
	--pids-limit 128 --memory 2g --tmpfs /tmp:rw,nosuid,nodev,size=128m \
	--mount "type=bind,src=${root}/source,dst=/source" \
	--mount "type=bind,src=${support}/validate_changed_files_syntax.sh,dst=/validator.sh,readonly" \
	--workdir /source "${image}" /bin/bash /validator.sh > "${root}/results/validation.log" 2>&1 || validation_rc=$?
	env -i PATH="${PATH}" docker rm -f "${container}" >/dev/null 2>&1 || true
	if [ -n "$(env -i PATH="${PATH}" docker ps -aq --filter "name=^/${container}$")" ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=validate engine=codex outcome=container_survived' >&2
		exit 1
	fi
	# timeout/docker failures are not syntax errors: never hand them to repair.
	case "${validation_rc}" in
		124|125|126|127|137)
			echo "HEAL_ISOLATED_EDITOR phase=validate engine=codex outcome=failed reason=validator_unavailable rc=${validation_rc}" >&2
			exit 1
			;;
	esac
	return "${validation_rc}"
}
repair_limit="${MAX_POST_CODEX_REPAIR_ATTEMPTS:-3}"
[[ "${repair_limit}" =~ ^[0-3]$ ]] || repair_limit=3
repair_count=0
while ! validate_snapshot; do
	if [ "${repair_count}" -ge "${repair_limit}" ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=validate engine=codex outcome=failed reason=syntax' >&2
		exit 1
	fi
	repair_count=$((repair_count + 1))
	repair_template="${GITHUB_WORKSPACE}/.codex-workflow-src/prompts/mode-implement-repair-syntax.txt"
	[ -f "${repair_template}" ] || { echo 'HEAL_ISOLATED_EDITOR phase=validate engine=codex outcome=failed reason=repair_template_missing' >&2; exit 1; }
	{
		cat "${repair_template}"
		printf '\n=== UNTRUSTED SYNTAX DIAGNOSTICS ===\n'
		tail -c 4000 "${root}/results/validation.log"
		printf '\n=== END UNTRUSTED SYNTAX DIAGNOSTICS ===\nFix only the reported syntax errors within the approved file scope.\n'
	} > "${root}/repair-prompt"
	run_editor "${root}/repair-prompt"
done
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${WORKSPACE_PATH:-${PWD}}" "${root}/source" "${root}/manifest.json" --scope-file "${scope}"; then
	echo 'HEAL_ISOLATED_EDITOR phase=transfer engine=codex outcome=failed reason=out_of_heal_scope' >&2
	exit 42
fi
install -m 0600 "${root}/results/output" "${output}"
echo 'HEAL_ISOLATED_EDITOR phase=transfer engine=codex outcome=success reason=validated'
