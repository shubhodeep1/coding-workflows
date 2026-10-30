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
cleanup()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	if [ "${gate_tree_added}" = true ]; then
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree remove --force "${root}/gate-tree" >/dev/null 2>&1 || true
		git -C "${WORKSPACE_PATH:-${PWD}}" worktree prune >/dev/null 2>&1 || true
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
	# An exported GIT_DIR / GIT_WORK_TREE would point the gate's git at the heal
	# checkout instead of the gate tree.
	gate_base="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git -C "${root}/gate-tree" rev-parse HEAD 2>/dev/null || true)"
	# PREPUSH_TEST_GATE_TIMEOUT_MINUTES=10 matches validate_snapshot's 600 s bound.
	env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE PREPUSH_TEST_GATE_TIMEOUT_MINUTES=10 bash "${gate_step}" gate --phase heal --worktree "${root}/gate-tree" --base "${gate_base}" --image "${image}" \
		--out "${root}/results/prepush" > "${root}/results/prepush.status" || true
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
