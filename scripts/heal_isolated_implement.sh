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
# Claude engine: the heal editor runs through ai_engine.sh claude_run, which
# starts the Claude Code CLI in codex_isolated_exec.sh's credential-free
# write container on this script's snapshot (never on the host). The snapshot
# then goes through the same syntax validation and scoped transfer as a codex
# edit. Claude unavailable (exit 75) runs the isolated codex editor below.
# A Codex-written snapshot that reaches the host leaves the codex_thread_reuse.sh
# fallback marker, so implement_commit_changes.sh commits the edits as
# codex-bot, not claude-bot. Returns non-zero when the marker cannot be written.
heal_mark_claude_unavailable()
{
	local runtime="${CODEX_THREAD_REUSE_RUNTIME_DIR:-${RUNTIME_DIR:-}}"
	[ -n "${runtime}" ] || return 0
	{ mkdir -p "${runtime}/codex-thread-reuse/states" && : > "${runtime}/codex-thread-reuse/states/claude-implement.unavailable"; } 2>/dev/null
}
heal_engine=codex
if [ "${AI_ENGINE_RESOLVED_IMPLEMENT:-codex}" = claude ]; then
	if [ -f "${support}/ai_engine.sh" ] && [ ! -L "${support}/ai_engine.sh" ]; then
		heal_engine=claude
	else
		echo 'AI_ENGINE_FALLBACK role=IMPLEMENT reason=support_missing' >&2
	fi
fi
root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/heal-isolated.XXXXXXXX")"
container="heal-editor-${GITHUB_RUN_ID:-local}-$$"
broker_pid=""
gate_tree_added=false
# Set only for the pre-push gate's repair turn: run_editor's codex fallback
# after a Claude exit 75 gets what is left of it, not a second full wall.
heal_repair_deadline=""
# Heal reproduction check (Refs #8434, P2 steps 5 and 8), behind
# WORKFLOW_HEAL_REPRODUCE_ENABLED (default false; only `true` turns it on).
heal_impl_repro_enabled=false
case "$(printf '%s' "${WORKFLOW_HEAL_REPRODUCE_ENABLED:-false}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
	true) heal_impl_repro_enabled=true ;;
esac
heal_impl_repro_result_file="${HEAL_REPRO_RESULT_FILE:-${RUNNER_TEMP:-/tmp}/heal-repro-result-${GITHUB_RUN_ID:-local}.json}"
heal_impl_repro_tree_added=false
heal_gate_prep_root=""
heal_gate_venv_args=()
# Allowlisted environment for prepare --deps and heal_reproduce.sh (the same
# list heal_reproduce.sh uses): no GitHub token and no model API key.
heal_impl_repro_clean_env=(env -i "PATH=${PATH}" "HOME=${HOME:-/tmp}" PYTHONDONTWRITEBYTECODE=1)
for heal_impl_repro_name in RUNNER_TEMP TMPDIR DOCKER_HOST DOCKER_CONFIG SANDBOX_IMAGE_REGISTRY SANDBOX_IMAGE_PULL_TIMEOUT_SECS \
	CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS CODEX_ISOLATED_IMAGE_BUILD_RETRY_SLEEP_SECS CODEX_VERSION DEPENDENCY_PROXY_ALLOWED_HOSTS; do
	if [ -n "${!heal_impl_repro_name:-}" ]; then
		heal_impl_repro_clean_env+=("${heal_impl_repro_name}=${!heal_impl_repro_name}")
	fi
done
cleanup()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	if [ "${gate_tree_added}" = true ]; then
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree remove --force "${root}/gate-tree" >/dev/null 2>&1 || true
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree prune >/dev/null 2>&1 || true
	fi
	if [ "${heal_impl_repro_tree_added}" = true ]; then
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree remove --force "${root}/repro-tree" >/dev/null 2>&1 || true
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree prune >/dev/null 2>&1 || true
	fi
	if [ -n "${heal_gate_prep_root}" ]; then
		"${heal_impl_repro_clean_env[@]}" bash "${support}/codex_isolated_exec.sh" cleanup --root "${heal_gate_prep_root}" </dev/null >/dev/null 2>&1 || true
	fi
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
# Pull the prebuilt sandbox image, or build it locally (scripts/sandbox_image.sh).
heal_sandbox_context="${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/clarify_sandbox"
if [ -f "${support}/sandbox_image.sh" ] && [ ! -L "${support}/sandbox_image.sh" ]; then
	image="$(env -i PATH="${PATH}" SANDBOX_IMAGE_REGISTRY="${SANDBOX_IMAGE_REGISTRY:-}" SANDBOX_IMAGE_PULL_TIMEOUT_SECS="${SANDBOX_IMAGE_PULL_TIMEOUT_SECS:-}" bash "${support}/sandbox_image.sh" build --family clarify -f "${heal_sandbox_context}/Dockerfile" "${heal_sandbox_context}")" || image=""
else
	image="$(env -i PATH="${PATH}" docker build -q -f "${heal_sandbox_context}/Dockerfile" "${heal_sandbox_context}")"
fi
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
run_editor_claude()
{
	local editor_prompt="$1" claude_rc=0
	# The snapshot is this run's own workdir: never reuse the implement job's
	# persistent workspace sandbox, which holds a different tree.
	# shellcheck disable=SC2016  # expanded by the inner bash
	env -u CODEX_ISOLATED_ROOT -u CODEX_ISOLATED_MODE timeout --kill-after=30s "${wall}" \
		bash -c 'source "$1/ai_engine.sh" && claude_run IMPLEMENT "$2" "$3" "$4"' heal-claude-editor \
		"${support}" "${editor_prompt}" "${root}/results/output" "${root}/source" || claude_rc=$?
	case "${claude_rc}" in
		0)
			echo 'HEAL_ISOLATED_EDITOR phase=run engine=claude outcome=success' >&2
			return 0
			;;
		75)
			# claude_run logged AI_ENGINE_FALLBACK role=IMPLEMENT reason=...
			echo 'HEAL_ISOLATED_EDITOR phase=run engine=claude outcome=unavailable action=codex' >&2
			heal_engine=codex
			return 75
			;;
		80)
			# Claude capacity wait (issue #7370): never run the codex editor.
			echo 'HEAL_ISOLATED_EDITOR phase=run engine=claude outcome=capacity_wait action=stop' >&2
			return 80
			;;
		*)
			# Any other exit (1 = editor failure, 124 = timeout, or anything
			# unexpected) fails closed without a transfer. claude_run already
			# maps its internal isolation-unavailable exit 76 to 75, so only
			# 75 (no Docker/image, no credential, every account exhausted)
			# runs the isolated codex editor.
			echo "HEAL_ISOLATED_EDITOR phase=run engine=claude outcome=failed reason=editor_exit_${claude_rc}" >&2
			return 1
			;;
	esac
}
run_editor()
{
	local editor_prompt="$1" rc=0
	if [ "${heal_engine}" = claude ]; then
		run_editor_claude "${editor_prompt}" && return 0
		rc=$?
		[ "${rc}" -eq 80 ] && return 80
		[ "${rc}" -eq 75 ] || return 1
		rc=0
		if [ -n "${heal_repair_deadline}" ]; then
			local wall="${wall}" repair_left=$(( heal_repair_deadline - SECONDS ))
			if [ "${repair_left}" -lt 60 ]; then
				echo 'HEAL_ISOLATED_EDITOR phase=run engine=codex outcome=skipped reason=step_budget' >&2
				return 1
			fi
			[ "${repair_left}" -ge "${wall}" ] || wall="${repair_left}"
		fi
	fi
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
		echo "HEAL_ISOLATED_EDITOR phase=validate engine=${heal_engine} outcome=container_survived" >&2
		exit 1
	fi
	# The validator's findings, so a failed heal names the file and checker
	# (#6982's two discarded attempts logged only reason=syntax). The log comes
	# from the editor-modified snapshot: only its ::error lines are shown, each
	# prefixed so the runner never reads one as a workflow command.
	if [ "${validation_rc}" -ne 0 ]; then
		grep -E '^::error' "${root}/results/validation.log" 2>/dev/null | head -n 20 | tr -d '\r' | cut -c1-300 \
			| sed 's/^/HEAL_ISOLATED_EDITOR validation: /' >&2 || true
	fi
	# Timeout/docker failures and a missing checker (validator exit 3) are not
	# syntax errors: never hand them to repair.
	case "${validation_rc}" in
		3|124|125|126|127|137)
			echo "HEAL_ISOLATED_EDITOR phase=validate engine=${heal_engine} outcome=failed reason=validator_unavailable rc=${validation_rc}" >&2
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
		echo "HEAL_ISOLATED_EDITOR phase=validate engine=${heal_engine} outcome=failed reason=syntax" >&2
		exit 1
	fi
	repair_count=$((repair_count + 1))
	repair_template="${GITHUB_WORKSPACE}/.codex-workflow-src/prompts/mode-implement-repair-syntax.txt"
	[ -f "${repair_template}" ] || { echo "HEAL_ISOLATED_EDITOR phase=validate engine=${heal_engine} outcome=failed reason=repair_template_missing" >&2; exit 1; }
	{
		cat "${repair_template}"
		printf '\n=== UNTRUSTED SYNTAX DIAGNOSTICS ===\n'
		tail -c 4000 "${root}/results/validation.log"
		printf '\n=== END UNTRUSTED SYNTAX DIAGNOSTICS ===\nFix only the reported syntax errors within the approved file scope.\n'
	} > "${root}/repair-prompt"
	run_editor "${root}/repair-prompt"
done
# Pre-push affected-test gate (plan P4). Tests run only in the gate's
# credential-free container. The editor can rewrite the snapshot's synthetic
# .git, so the gate never runs host git on ${root}/source: it runs on a
# detached worktree of the heal checkout (whose .git the editor never saw) that
# receives the snapshot's content through the same scoped transfer. On
# failures the change caused, one more editor turn runs with the test-repair
# prompt and validate_snapshot runs once more; a syntax failure restores the
# pre-repair snapshot. Fail-open: the transfer below always follows.
# Seconds the gate budget has used: from the job's start (JOB_START_EPOCH,
# which implement.yml exports) when it is set, so a heal retry later in the
# job does not start a fresh budget; else from this script's start.
heal_gate_elapsed()
{
	local now job_elapsed
	now="$(date +%s)"
	if [[ "${JOB_START_EPOCH:-}" =~ ^[1-9][0-9]{8,10}$ ]] && [ "${JOB_START_EPOCH}" -le "${now}" ]; then
		job_elapsed=$(( now - JOB_START_EPOCH ))
		[ "${job_elapsed}" -ge "${SECONDS}" ] && { echo "${job_elapsed}"; return 0; }
	fi
	echo "${SECONDS}"
}
heal_prepush_test_gate()
{
	local gate_step="" gate_mode gate_ws="${WORKSPACE_PATH:-${PWD}}" gate_base gate_status="" gate_pr=0 gate_inh=0 rc
	local candidate
	for candidate in "${support}/review_autofix_step_prepush_test_gate.sh" "${GITHUB_WORKSPACE:-.}/.codex-workflow-src/scripts/review_autofix_step_prepush_test_gate.sh"; do
		if [ -f "${candidate}" ] && [ ! -L "${candidate}" ]; then
			gate_step="${candidate}"
			break
		fi
	done
	if [ -z "${gate_step}" ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=skipped reason=module_absent' >&2
		return 0
	fi
	gate_mode="$(bash "${gate_step}" mode 2>/dev/null || echo repair)"
	if [ "${gate_mode}" = off ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=skipped reason=mode_off' >&2
		return 0
	fi
	# Step budget: heal_gate_elapsed counts from the job's start. The heal step runs
	# under implement.yml's 180-minute job cap, after earlier steps and before
	# the commit and push, so the gate and its repair turn must leave room for
	# the scoped transfer below. One gate run is at most 10 minutes plus the
	# wrapper's 120 s overhead and 30 s kill grace.
	local gate_budget="${HEAL_PREPUSH_GATE_BUDGET_SECS:-9000}"
	[[ "${gate_budget}" =~ ^[1-9][0-9]{0,5}$ ]] || gate_budget=9000
	if [ $(( gate_budget - $(heal_gate_elapsed) )) -lt 750 ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=skipped reason=step_budget' >&2
		return 0
	fi
	if git -C "${gate_ws}" -c core.hooksPath=/dev/null worktree add --detach "${root}/gate-tree" HEAD >/dev/null 2>&1; then
		gate_tree_added=true
	fi
	# A private manifest copy (with its sidecars, such as the admitted-command
	# inventory) keeps the real transfer's baseline untouched.
	local manifest_part
	for manifest_part in "${root}"/manifest.json*; do
		[ -f "${manifest_part}" ] || continue
		cp -f "${manifest_part}" "${root}/manifest.gate.json${manifest_part#"${root}/manifest.json"}" 2>/dev/null || true
	done
	if [ "${gate_tree_added}" != true ] || ! PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer \
		"${root}/gate-tree" "${root}/source" "${root}/manifest.gate.json" --scope-file "${scope}" >/dev/null 2>&1; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=unverified reason=gate_tree_unavailable' >&2
		return 0
	fi
	# Switch on: the gate gets the dependency venv from prepare --deps (as
	# heal_reproduce.sh does), so pytest units are not `pytest_unavailable`.
	# Fail-open: without a venv the gate runs exactly as with the switch off.
	if [ "${heal_impl_repro_enabled}" = true ]; then
		local gate_prep=""
		if [ -f "${support}/codex_isolated_exec.sh" ] && [ ! -L "${support}/codex_isolated_exec.sh" ]; then
			gate_prep="$("${heal_impl_repro_clean_env[@]}" bash "${support}/codex_isolated_exec.sh" prepare --workdir "${root}/gate-tree" --deps </dev/null | tail -n 1)" || gate_prep=""
		fi
		if [[ "${gate_prep}" == /* ]] && [ -d "${gate_prep}" ]; then
			heal_gate_prep_root="${gate_prep}"
		fi
		if [ -n "${heal_gate_prep_root}" ] && [ -d "${gate_prep}/venv/bin" ] && [ ! -L "${gate_prep}/venv" ]; then
			heal_gate_venv_args=(--venv "${gate_prep}/venv")
			echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate_deps outcome=ok' >&2
		else
			echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate_deps outcome=unavailable' >&2
		fi
	fi
	# An exported GIT_DIR / GIT_WORK_TREE would point the gate's git at the heal
	# checkout instead of the gate tree.
	gate_base="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git -C "${root}/gate-tree" rev-parse HEAD 2>/dev/null || true)"
	# PREPUSH_TEST_GATE_TIMEOUT_MINUTES=10 matches validate_snapshot's 600 s bound.
	env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE PREPUSH_TEST_GATE_TIMEOUT_MINUTES=10 bash "${gate_step}" gate --phase heal --worktree "${root}/gate-tree" --base "${gate_base}" --image "${image}" \
		--out "${root}/results/prepush" ${heal_gate_venv_args[@]+"${heal_gate_venv_args[@]}"} > "${root}/results/prepush.status" || true
	read -r gate_status gate_pr gate_inh < "${root}/results/prepush.status" || true
	if [ "${gate_status}" != fail ] || ! [ "${gate_pr:-0}" -gt 0 ] 2>/dev/null || [ "${gate_mode}" != repair ]; then
		echo "HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=${gate_status:-unverified} failures=${gate_pr:-0} inherited=${gate_inh:-0} repair=none" >&2
		return 0
	fi
	# The allow-list and the prompt's diff come from the gate tree (the
	# snapshot's content on a checkout the editor never saw); the transfer
	# still enforces the heal scope.
	if ! ( cd "${root}/gate-tree" && unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE \
		&& { git diff --name-only HEAD -- 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } | sed '/^$/d' | sort -u > "${root}/results/prepush-changed.txt" \
		&& bash "${gate_step}" allow-list "${root}/gate-tree" "${root}/results/prepush-changed.txt" "${root}/results/prepush/result.json" "${root}/results/prepush-allow.txt" \
		&& [ -s "${root}/results/prepush-allow.txt" ] \
		&& bash "${gate_step}" repair-prompt "${root}/results/prepush/result.json" "${root}/repair-prompt-tests" "${root}/results/prepush-allow.txt" ) 2>/dev/null; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=fail repair=skipped reason=repair_prompt_unavailable' >&2
		return 0
	fi
	# The repair turn gets what is left of the budget after validate_snapshot
	# (600 s plus 30 s kill grace); run_editor reads this local wall.
	local wall="${wall}" repair_wall=$(( gate_budget - $(heal_gate_elapsed) - 630 ))
	if [ "${repair_wall}" -lt 300 ]; then
		echo "HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=fail failures=${gate_pr} repair=skipped reason=step_budget" >&2
		return 0
	fi
	[ "${repair_wall}" -ge "${wall}" ] || wall="${repair_wall}"
	rm -rf -- "${root}/source.pre-gate"
	cp -a "${root}/source" "${root}/source.pre-gate" || { echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=fail repair=skipped reason=backup_failed' >&2; return 0; }
	rc=0
	heal_repair_deadline=$(( SECONDS + wall ))
	run_editor "${root}/repair-prompt-tests" || rc=$?
	heal_repair_deadline=""
	# validate_snapshot exits the script on validator infrastructure failures;
	# a subshell keeps that from skipping the restore and the transfer below.
	# The rc file is written only when validate_snapshot returns.
	local vrc=0
	if [ "${rc}" -eq 0 ]; then
		rm -f -- "${root}/results/prepush-validate.rc"
		( r=0; validate_snapshot || r=$?; echo "${r}" > "${root}/results/prepush-validate.rc" ) || true
		vrc="$(cat "${root}/results/prepush-validate.rc" 2>/dev/null || echo unavailable)"
	fi
	if [ "${rc}" -eq 0 ] && [ "${vrc}" = 0 ]; then
		echo "HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=repaired failures=${gate_pr} repair=ran" >&2
		rm -rf -- "${root}/source.pre-gate"
		return 0
	fi
	# Editor failure or broken syntax: the transfer gets the pre-repair snapshot.
	rm -rf -- "${root}/source"
	mv -- "${root}/source.pre-gate" "${root}/source"
	if [ "${rc}" -ne 0 ]; then
		echo "HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=repair_reverted reason=editor_exit_${rc}" >&2
	elif [ "${vrc}" = unavailable ]; then
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=repair_reverted reason=validator_unavailable' >&2
	else
		echo 'HEAL_ISOLATED_EDITOR phase=prepush_gate outcome=repair_reverted reason=syntax' >&2
	fi
	return 0
}
heal_prepush_test_gate || true
# Heal reproduction check (Refs #8434, P2 step 8). Runs only when the approving
# comment (APPROVAL_COMMENT_ID in ISSUE_COMMENTS_FILE) carries
# [repro-required]; without it nothing below runs. Every listed test must fail
# at the failing SHA and pass on the edited tree, else the script exits 43
# before the host transfer and implement.yml maps 43 to
# `implement_heal_preflight.sh refuse repro_not_fixed`. heal_reproduce.sh runs
# a test at its baseline only when it failed on the tested tree, so one run
# (edited tree vs failing SHA) cannot show the "fails at the failing SHA" half:
# run 1 tests the edited tree, run 2 (`--sha F --baseline F`) the failing SHA,
# where a failing test comes back `not_reproduced/pre_existing`. The result
# (heal_implement_repro.v1) goes to HEAL_REPRO_RESULT_FILE for the preflight.
IFS= read -r -d '' heal_impl_repro_py <<'PY' || true
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
STATUSES = ("reproduced", "not_reproduced", "unverified")
PATH_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
NAME_RE = re.compile(r"^[^\s`<>]+$")


def tok(value, fallback="invalid_reason"):
	return value if isinstance(value, str) and TOKEN_RE.match(value) else fallback


def listed(path):
	try:
		with open(path, encoding="utf-8") as handle:
			return [line.strip() for line in handle if line.strip()]
	except (OSError, UnicodeDecodeError):
		return []


def load(path):
	if not path:
		return None
	try:
		with open(path, encoding="utf-8") as handle:
			data = json.load(handle)
	except (OSError, ValueError):
		return None
	if not isinstance(data, dict) or data.get("schema") != "heal_repro.v1" or data.get("status") not in STATUSES:
		return None
	if not isinstance(data.get("tests"), list):
		return None
	return data


def per_test(data):
	out = {}
	for item in data["tests"]:
		if isinstance(item, dict) and isinstance(item.get("test"), str):
			out.setdefault(item["test"], (item.get("status"), item.get("reason")))
	return out


def on_edited(pair):
	# Run 1 tested the edited tree against the failing SHA: its
	# `passes_at_failing_sha` token means "passes on the edited tree".
	if pair == ("not_reproduced", "passes_at_failing_sha"):
		return "passes", ""
	if pair == ("not_reproduced", "pre_existing"):
		return "fails", "still_failing"
	if pair == ("reproduced", "regression"):
		return "fails", "regression"
	if pair[0] == "unverified":
		return "unverified", tok(pair[1])
	return "unverified", "unexpected_result"


def at_failing(pair):
	# Run 2 used the failing SHA as both tree and baseline.
	if pair == ("not_reproduced", "pre_existing"):
		return "fails", ""
	if pair == ("not_reproduced", "passes_at_failing_sha"):
		return "passes", "passes_at_failing_sha"
	if pair == ("reproduced", "regression"):
		# Failed, then passed on the same commit: flaky, not a proof.
		return "unverified", "flaky_at_failing_sha"
	if pair[0] == "unverified":
		return "unverified", tok(pair[1])
	return "unverified", "unexpected_result"


def cmd_tests(support, plan, out):
	sys.path.insert(0, support)
	from partial_delivery import HEAL_CF_LINE_RE, _unfenced_lines
	try:
		with open(plan, encoding="utf-8") as handle:
			lines = handle.read().splitlines()
	except (OSError, UnicodeDecodeError):
		return 1
	found = [m.group(1) for m in (HEAL_CF_LINE_RE.match(line) for line in _unfenced_lines(lines)) if m]
	if len(found) != 1:
		return 1
	match = re.match(r"^REPRODUCIBLE[ \t]*:[ \t]*(.*)$", found[0])
	if not match:
		return 1
	# Assumed list format (the sibling parser is not on this ref): ids split on
	# commas and/or whitespace; one pair of surrounding backticks is dropped.
	ids = []
	for raw in re.split(r"[,\s]+", match.group(1)):
		if raw.startswith("`") and raw.endswith("`") and len(raw) > 2:
			raw = raw[1:-1]
		if raw:
			ids.append(raw)
	if not 1 <= len(ids) <= 50:
		return 1
	for item in ids:
		path, sep, name = item.partition("::")
		if not PATH_RE.match(path) or path.startswith("/") or ".." in path.split("/"):
			return 1
		if sep and not NAME_RE.match(name):
			return 1
	with open(out, "w", encoding="utf-8") as handle:
		handle.write("\n".join(ids) + "\n")
	return 0


def cmd_edited_ok(tests_file, edited):
	tests = listed(tests_file)
	data = load(edited)
	ok = bool(tests) and data is not None and data["status"] != "unverified"
	if ok:
		mapped = per_test(data)
		ok = all(on_edited(mapped.get(t, ("missing", "missing")))[0] == "passes" for t in tests)
	print("yes" if ok else "no")
	return 0


def cmd_final(tests_file, edited, failing, sha, out, forced):
	tests = listed(tests_file)
	rows = []
	unverified = ""
	mismatch = ""
	if forced or not tests:
		unverified = tok(forced or "no_tests")
		rows = [{"test": t, "at_failing_sha": "not_run", "on_edited_tree": "not_run"} for t in tests]
	else:
		run1 = load(edited)
		run2 = load(failing) if failing else None
		if run1 is None:
			unverified = "result_invalid"
		elif run1["status"] == "unverified":
			unverified = tok(run1.get("reason"))
		if failing and run2 is None and not unverified:
			unverified = "result_invalid"
		elif run2 is not None and run2["status"] == "unverified" and not unverified:
			unverified = tok(run2.get("reason"))
		map1 = per_test(run1) if run1 is not None else {}
		map2 = per_test(run2) if run2 is not None else {}
		for test in tests:
			on, reason1 = on_edited(map1.get(test, ("unverified", "test_missing"))) if run1 is not None else ("unverified", "result_invalid")
			at = "not_run"
			if on == "unverified":
				unverified = unverified or reason1
			elif on == "fails":
				mismatch = mismatch or reason1
			elif run2 is not None:
				at, reason2 = at_failing(map2.get(test, ("unverified", "test_missing")))
				if at == "unverified":
					unverified = unverified or reason2
				elif at == "passes":
					mismatch = mismatch or reason2
			else:
				unverified = unverified or "not_run_at_failing_sha"
			rows.append({"test": test, "at_failing_sha": at, "on_edited_tree": on})
	if unverified:
		outcome, reason = "unverified", unverified
	elif mismatch:
		outcome, reason = "not_fixed", mismatch
	elif rows and all(r["at_failing_sha"] == "fails" and r["on_edited_tree"] == "passes" for r in rows):
		outcome, reason = "fixed", "fixed"
	else:
		outcome, reason = "unverified", "incomplete"
	result = {"schema": "heal_implement_repro.v1", "outcome": outcome, "reason": reason,
		"failing_sha": sha if re.fullmatch(r"[0-9a-f]{40}", sha or "") else "", "tests": rows}
	tmp = out + ".tmp"
	try:
		fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(result, handle)
			handle.write("\n")
		os.replace(tmp, out)
	except OSError:
		print("HEAL_ISOLATED_EDITOR phase=repro_check outcome=warning reason=result_unwritable", file=sys.stderr)
	print(outcome, reason, len(rows))
	return 0


def main(argv):
	if argv[0] == "tests":
		return cmd_tests(*argv[1:4])
	if argv[0] == "edited_ok":
		return cmd_edited_ok(*argv[1:3])
	if argv[0] == "final":
		return cmd_final(*argv[1:7])
	return 2


sys.exit(main(sys.argv[1:]))
PY
heal_impl_repro_py_run()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c "${heal_impl_repro_py}" "$@"
}
# Prints none, required or unreadable. An unreadable approval holds.
heal_impl_repro_required()
{
	local comment_id="${APPROVAL_COMMENT_ID:-}" comments="${ISSUE_COMMENTS_FILE:-}" first_line
	if [ -z "${comment_id}" ]; then
		echo none
		return 0
	fi
	if ! [[ "${comment_id}" =~ ^[1-9][0-9]{0,19}$ ]] || [ ! -f "${comments}" ] || [ -L "${comments}" ]; then
		echo unreadable
		return 0
	fi
	first_line="$(jq -r --arg id "${comment_id}" '[.[] | select((.id | tostring) == $id)] | if length == 1 then (.[0].body // "" | split("\n")[0] | rtrimstr("\r")) else error("missing") end' "${comments}" 2>/dev/null)" || { echo unreadable; return 0; }
	if [[ "${first_line}" == /approved* ]] && [[ "${first_line}" == *"[repro-required]"* ]]; then
		echo required
	else
		echo none
	fi
}
# The pipeline-written `- **Head SHA:** `<sha>`` line of the heal issue body
# (scripts/workflow_failure_heal.py); exactly one match, else nothing.
heal_impl_repro_failing_sha()
{
	local body="${ISSUE_BODY_FILE:-}" matches
	[ -f "${body}" ] && [ ! -L "${body}" ] || return 1
	matches="$(tr -d '\r' < "${body}" | grep -E '^- \*\*Head SHA:\*\* `[0-9a-f]{40}`$')" || return 1
	[ "$(printf '%s\n' "${matches}" | wc -l)" -eq 1 ] || return 1
	printf '%s' "${matches}" | sed -E 's/^.*`([0-9a-f]{40})`$/\1/'
}
# The trusted support copy has no gate module next to it; the verified
# .codex-workflow-src checkout does (the same fallback the gate step uses).
heal_impl_repro_script()
{
	local candidate dir
	for candidate in "${support}/heal_reproduce.sh" "${GITHUB_WORKSPACE:-.}/.codex-workflow-src/scripts/heal_reproduce.sh"; do
		[ -f "${candidate}" ] && [ ! -L "${candidate}" ] || continue
		dir="$(dirname "${candidate}")"
		if [ -f "${dir}/prepush_affected_tests.py" ] || [ -f "${dir}/../workflow-templates/.claude/hooks/prepush_affected_tests.py" ]; then
			printf '%s' "${candidate}"
			return 0
		fi
	done
	return 1
}
heal_impl_repro_finish()
{
	local forced="$1" failing_sha="${2:-}" summary outcome reason count
	summary="$(heal_impl_repro_py_run final "${root}/repro-tests.txt" "${root}/results/repro-edited.json" \
		"$( [ -f "${root}/results/repro-failing.json" ] && printf '%s' "${root}/results/repro-failing.json" )" \
		"${failing_sha}" "${heal_impl_repro_result_file}" "${forced}")" || summary=""
	read -r outcome reason count <<<"${summary}" || true
	case "${outcome:-}" in
		fixed|not_fixed|unverified) ;;
		*) outcome=unverified reason=interpret_failed count=0 ;;
	esac
	echo "HEAL_ISOLATED_EDITOR phase=repro_check outcome=${outcome} reason=${reason:-unknown} tests=${count:-0}" >&2
	[ "${outcome}" = fixed ] && [ -z "${forced}" ] && return 0
	exit 43
}
heal_impl_repro_check()
{
	local required failing_sha="" reproduce="" budget remaining per_run manifest_part
	required="$(heal_impl_repro_required)"
	[ "${required}" = none ] && return 0
	: > "${root}/repro-tests.txt"
	rm -f -- "${root}/results/repro-edited.json" "${root}/results/repro-failing.json"
	[ "${required}" = required ] || heal_impl_repro_finish approval_unreadable
	heal_impl_repro_py_run tests "${support}" "${PLAN_FILE:-}" "${root}/repro-tests.txt.new" 2>/dev/null \
		&& mv -f -- "${root}/repro-tests.txt.new" "${root}/repro-tests.txt" || true
	# Fail closed: [repro-required] with the switch off holds, never skips.
	[ "${heal_impl_repro_enabled}" = true ] || heal_impl_repro_finish reproduction_disabled
	[ -s "${root}/repro-tests.txt" ] || heal_impl_repro_finish plan_tests_unreadable
	failing_sha="$(heal_impl_repro_failing_sha)" || failing_sha=""
	[[ "${failing_sha}" =~ ^[0-9a-f]{40}$ ]] || heal_impl_repro_finish failing_sha_unavailable
	reproduce="$(heal_impl_repro_script)" || heal_impl_repro_finish reproduce_unavailable "${failing_sha}"
	budget="${HEAL_PREPUSH_GATE_BUDGET_SECS:-9000}"
	[[ "${budget}" =~ ^[1-9][0-9]{0,5}$ ]] || budget=9000
	# Same 630 s transfer reserve as the gate's repair turn; two runs at most.
	remaining=$(( budget - $(heal_gate_elapsed) - 630 ))
	per_run=$(( remaining / 2 ))
	[ "${per_run}" -le 3600 ] || per_run=3600
	[ "${per_run}" -ge 120 ] || heal_impl_repro_finish step_budget "${failing_sha}"
	if ! git -C "${WORKSPACE_PATH:-${PWD}}" -c core.hooksPath=/dev/null worktree add --detach "${root}/repro-tree" HEAD >/dev/null 2>&1; then
		heal_impl_repro_finish repro_tree_unavailable "${failing_sha}"
	fi
	heal_impl_repro_tree_added=true
	for manifest_part in "${root}"/manifest.json*; do
		[ -f "${manifest_part}" ] || continue
		cp -f "${manifest_part}" "${root}/manifest.repro.json${manifest_part#"${root}/manifest.json"}" 2>/dev/null || true
	done
	# The same scoped transfer as the host's below: a failure here is the one
	# the host transfer would hit, so it keeps that exit code (42).
	if ! PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer \
		"${root}/repro-tree" "${root}/source" "${root}/manifest.repro.json" --scope-file "${scope}" >/dev/null 2>&1; then
		echo "HEAL_ISOLATED_EDITOR phase=transfer engine=${heal_engine} outcome=failed reason=out_of_heal_scope" >&2
		exit 42
	fi
	# Run 1: the edited tree, baseline the failing SHA.
	"${heal_impl_repro_clean_env[@]}" WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS="${per_run}" bash "${reproduce}" \
		--tests-file "${root}/repro-tests.txt" --worktree "${root}/repro-tree" --repo "${root}/repro-tree" \
		--baseline "${failing_sha}" --out "${root}/results/repro-edited.json" </dev/null >&2 || true
	if [ "$(heal_impl_repro_py_run edited_ok "${root}/repro-tests.txt" "${root}/results/repro-edited.json" 2>/dev/null)" = yes ]; then
		# Run 2: the failing SHA itself (only when every test passed in run 1).
		"${heal_impl_repro_clean_env[@]}" WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS="${per_run}" bash "${reproduce}" \
			--tests-file "${root}/repro-tests.txt" --sha "${failing_sha}" --repo "${root}/repro-tree" \
			--baseline "${failing_sha}" --out "${root}/results/repro-failing.json" </dev/null >&2 || true
		[ -f "${root}/results/repro-failing.json" ] || : > "${root}/results/repro-failing.json"
	fi
	heal_impl_repro_finish "" "${failing_sha}"
}
heal_impl_repro_check
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${WORKSPACE_PATH:-${PWD}}" "${root}/source" "${root}/manifest.json" --scope-file "${scope}"; then
	echo "HEAL_ISOLATED_EDITOR phase=transfer engine=${heal_engine} outcome=failed reason=out_of_heal_scope" >&2
	exit 42
fi
# Mark only once Codex edits reached the host: a Codex attempt that never
# transferred must not relabel a later Claude-written heal commit, and a
# later Claude success never clears edits Codex already transferred.
if [ "${heal_engine}" = codex ] && ! heal_mark_claude_unavailable; then
	echo '::warning::HEAL_ISOLATED_EDITOR phase=transfer engine=codex outcome=marker_write_failed (commit identity may read claude-bot)' >&2
fi
install -m 0600 "${root}/results/output" "${output}"
echo "HEAL_ISOLATED_EDITOR phase=transfer engine=${heal_engine} outcome=success reason=validated"
