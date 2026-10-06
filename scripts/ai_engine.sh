#!/usr/bin/env bash
# ai_engine.sh — choose and run the model engine (codex or Claude) of one role.
#
# Source this file; it only defines functions. Every decision is made by
# scripts/claude_engine.py next to it, which reads the trusted
# .github/ai/claude_engine.json from the support checkout (never the caller's
# checkout) and falls back to "every role on codex" when it is missing.
#
#   ai_engine_for_role <role>
#       Prints `codex` or `claude` and logs
#       `AI_ENGINE_SELECTED role= engine= model= effort= source=` to stderr.
#       Order: CLAUDE_FIXER_ENABLED=false for the four review write roles
#       (codex, Phase 5c), work-item labels in AI_ENGINE_LABELS (`ai:codex` beats
#       `ai:engine-claude`), AI_ENGINE_<ROLE>, AI_ENGINE, the code default.
#   ai_engine_model <role> [model_hint]
#   ai_engine_effort <role> [effort_hint]
#       The Claude model and effort of a role (plan D3). The hint is the
#       role's existing model / reasoning variable; a model hint is used only
#       when it starts with `claude-`.
#   ai_engine_cli_version
#       The pinned @anthropic-ai/claude-code version.
#   ai_engine_fallback <role> <reason>
#       Logs `AI_ENGINE_FALLBACK role= reason=` and sends at most one
#       Telegram note per job (plan D1). The caller then runs codex.
#   ai_engine_pool_dir
#       The account pool directory (CLAUDE_ENGINE_POOL_DIR below).
#   ai_engine_claude_home
#       The isolated CLI's session store ($RUNNER_TEMP/claude-isolated-home),
#       where a resumable session's <id>.jsonl lives.
#   ai_engine_accounts
#       The usable account names, best first: each line of `order` that is a
#       valid name and has a regular, non-empty `tokens/<NAME>` file.
#   claude_run_selected <role> <prompt_file> <out_file> <workdir> [session_id]
#       claude_run when AI_ENGINE_RESOLVED_<ROLE>=claude, else 75 at once.
#   claude_run <role> <prompt_file> <out_file> <workdir> [session_id]
#       Runs `claude -p` for the role in a credential-free, network-isolated
#       container and writes the final result text to <out_file>. Read
#       profiles use a bounded source snapshot and trusted-support lock;
#       write profiles use codex_isolated_exec.sh (`run --engine claude`)
#       to copy validated changes back. Returns 0 on success; 75 when
#       Claude is unavailable (caller runs codex); 124 on timeout; 86 if
#       trusted read-profile support changed (terminal); otherwise nonzero
#       on a crash, following the role's existing retry rules.
#       AI_ENGINE_LAST_RUN_DIR names the run directory afterwards; it holds
#       transcript-<NAME>.jsonl and stderr-<NAME>.txt for each account tried.
#       Read-profile calls use a network-isolated container and a bounded,
#       credential-free source snapshot; the real token stays in a host relay.
#
# Inputs (environment):
#   AI_ENGINE_LABELS        work-item labels (comma/space list or JSON list)
#   AI_ENGINE, AI_ENGINE_<ROLE>   `codex` | `claude`
#   CLAUDE_FIXER_ENABLED    `false` keeps REVIEW_EDITOR, REVIEW_CONSOLIDATOR,
#                           CONFLICT_RESOLVER and RB_JUDGE on codex (Q35)
#   AI_ENGINE_MODEL_HINT, AI_ENGINE_EFFORT_HINT   claude_run's D3 hints
#   CLAUDE_ENGINE_POOL_DIR  account pool written by the token step
#                           (default ${RUNNER_TEMP}/claude-pool): `order`
#                           lists account names, best first; `tokens/<NAME>`
#                           holds each token (0600)
#   ALLOW_WORKFLOW_EDITS    `true` lifts the .github/workflows deny rules (P5)
#   AI_ENGINE_READ_ONLY     `true` runs a write role with the read profile
#                           (read-profile Claude children receive no GitHub,
#                           Telegram, OpenRouter or Actions OIDC credentials)
#   SUPPORT_INSTRUCTIONS_FILE   unattended_system_instructions.md
#   AI_ENGINE_READ_EXTRA_DIRS  optional colon-separated absolute snapshot roots
#   AI_ENGINE_ISOLATED_READ_PATHS  WORKFLOW_HEAL only: colon-separated heal_src
#                                   and heal_branch_tip under RUNTIME_DIR; filtered snapshots,
#                                   never raw host mounts. Other paths fall back.
#
# The OAuth token never reaches the CLI: scripts/claude_anthropic_relay.py
# reads it from the pool file on the host and swaps it into each request; the
# container holds a placeholder. It never appears in argv, a log line, the
# environment or a file outside the pool directory.

if [ "${_AI_ENGINE_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_AI_ENGINE_LOADED="true"

_AI_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_AI_ENGINE_EXIT_FALLBACK=75
_AI_ENGINE_EXIT_SUPPORT_TAMPERED=86

_ai_engine_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "${_AI_ENGINE_DIR}/claude_engine.py" "$@"
}

_ai_engine_valid_role()
{
	[[ "${1:-}" =~ ^[A-Z][A-Z_]{0,39}$ ]]
}

ai_engine_for_role()
{
	local role="${1:-}" resolved engine
	if ! _ai_engine_valid_role "${role}"; then
		echo "::warning::AI engine: invalid role '${role}'; using codex." >&2
		printf 'codex\n'
		return 0
	fi
	if ! resolved="$(_ai_engine_py resolve --role "${role}" --model-hint "${AI_ENGINE_MODEL_HINT:-}" --effort-hint "${AI_ENGINE_EFFORT_HINT:-}")"; then
		echo "::warning::AI engine: could not resolve role ${role}; using codex." >&2
		echo "AI_ENGINE_SELECTED role=${role} engine=codex model= effort= source=error" >&2
		printf 'codex\n'
		return 0
	fi
	engine="$(printf '%s' "${resolved}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["engine"])')" || engine="codex"
	printf '%s' "${resolved}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("AI_ENGINE_SELECTED role={role} engine={engine} model={model} effort={effort} source={source}".format(**r))
' >&2 || true
	printf '%s\n' "${engine}"
}

ai_engine_model()
{
	local role="${1:-}" hint="${2:-}"
	_ai_engine_valid_role "${role}" || return 2
	_ai_engine_py resolve --role "${role}" --model-hint "${hint}" --field model
}

ai_engine_effort()
{
	local role="${1:-}" hint="${2:-}"
	_ai_engine_valid_role "${role}" || return 2
	_ai_engine_py resolve --role "${role}" --effort-hint "${hint}" --field effort
}

ai_engine_cli_version()
{
	_ai_engine_py config --key cli_version
}

ai_engine_fallback()
{
	local role="${1:-unknown}" reason="${2:-unknown}"
	reason="$(printf '%s' "${reason}" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-60)"
	echo "AI_ENGINE_FALLBACK role=${role} reason=${reason}" >&2
	local marker="${RUNNER_TEMP:-/tmp}/ai-engine-fallback-notified"
	[ -e "${marker}" ] && return 0
	: > "${marker}" 2>/dev/null || return 0
	(
		if ! type tg_send_msg >/dev/null 2>&1 && [ -f "${_AI_ENGINE_DIR}/tg_helpers.sh" ]; then
			# shellcheck disable=SC1091
			source "${_AI_ENGINE_DIR}/tg_helpers.sh"
		fi
		if type tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "AI engine: Claude unavailable for ${role} (${reason}) in ${GITHUB_REPOSITORY:-unknown} run ${GITHUB_RUN_ID:-local}; running codex." "WARNING" >/dev/null
		fi
	) 2>/dev/null || true
	return 0
}

ai_engine_claude_home()
{
	# The isolated CLI's ~/.claude (session store), kept across claude_run
	# calls in a job; callers that look for a resumable session use it.
	printf '%s\n' "${RUNNER_TEMP:-/tmp}/claude-isolated-home"
}

ai_engine_pool_dir()
{
	printf '%s\n' "${CLAUDE_ENGINE_POOL_DIR:-${RUNNER_TEMP:-/tmp}/claude-pool}"
}

ai_engine_accounts()
{
	local pool_dir name token_file
	pool_dir="$(ai_engine_pool_dir)"
	[ -f "${pool_dir}/order" ] || return 0
	while IFS= read -r name || [ -n "${name}" ]; do
		[[ "${name}" =~ ^[A-Z0-9_]{1,64}$ ]] || continue
		token_file="${pool_dir}/tokens/${name}"
		if [ -f "${token_file}" ] && [ ! -L "${token_file}" ] && [ -s "${token_file}" ]; then
			printf '%s\n' "${name}"
		else
			echo "CLAUDE_POOL account_skipped account=${name} reason=token_missing" >&2
		fi
	done < "${pool_dir}/order"
}

_ai_engine_json_field()
{
	python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"
}

_ai_engine_instructions_file()
{
	local candidate
	for candidate in \
		"${SUPPORT_INSTRUCTIONS_FILE:-}" \
		"${SUPPORT_ROOT_DIR:+${SUPPORT_ROOT_DIR}/unattended_system_instructions.md}" \
		"${GITHUB_WORKSPACE:+${GITHUB_WORKSPACE}/.codex-workflow-src/unattended_system_instructions.md}" \
		"${_AI_ENGINE_DIR}/../unattended_system_instructions.md"; do
		if [ -n "${candidate}" ] && [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done
	return 1
}

_ai_engine_read_path_valid()
{
	[[ "${1:-}" == /* && "${1}" != *[$'\001'-$'\037',:\"\'\$\`\\]* && "${1}" != *","* && "${1}" != *"="* && "${1}" != *".."* ]] &&
		[ -d "$1" ] && [ "$(realpath -e -- "$1" 2>/dev/null)" = "$1" ]
}

_ai_engine_read_path_sensitive()
{
	local candidate="$1" private_root home_root
	for private_root in "$2" "$3"; do
		[ -n "${private_root}" ] || continue
		case "${private_root}/" in "${candidate%/}/"*) return 0 ;; esac
		case "${candidate}/" in "${private_root%/}/"*) return 0 ;; esac
	done
	# Do not snapshot HOME itself (or a parent); heal worktrees may be below HOME.
	home_root="$(realpath -e -- "${HOME:-}" 2>/dev/null)" || return 0
	case "${home_root}/" in "${candidate%/}/"*) return 0 ;; esac
	return 1
}

_ai_engine_snapshot_summary()
{
	python3 -c 'import json,sys; d=json.loads(sys.argv[1]); (type(d["files"]) is int and d["files"] >= 0 and d["git"] in ("copied", "omitted", "none") and d["reason"] in ("", "alternates", "filtered_history")) or sys.exit(1); print(d["files"], d["git"], d["reason"] or "none")' "$1"
}

_ai_engine_support_finish()
{
	local run_dir="$1" role="$2" status=0
	# Run the pre-model copy: the installed verifier itself may have been
	# overwritten by the model. Always restore modes, even on a mismatch.
	PYTHONDONTWRITEBYTECODE=1 python3 "${run_dir}/claude_engine.py" support-verify --manifest "${run_dir}/support-lock.json" || status=1
	PYTHONDONTWRITEBYTECODE=1 python3 "${run_dir}/claude_engine.py" support-unlock --manifest "${run_dir}/support-lock.json" || status=1
	if [ "${status}" -ne 0 ]; then
		echo "AI_ENGINE_SUPPORT_LOCK role=${role} outcome=tampered" >&2
		return "${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"
	fi
	echo "AI_ENGINE_SUPPORT_LOCK role=${role} outcome=verified" >&2
}

_ai_engine_isolation_preflight()
{
	local pool_dir="$1" workdir="$2" prompt_file="$3" instructions="$4" role="$5" candidate pool_real runtime_real
	AI_ENGINE_ISOLATION_FAILURE=""
	command -v docker >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_docker_missing; return 1; }
	command -v python3 >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_python_missing; return 1; }
	for entry in setsid ps git realpath sha256sum timeout; do
		command -v "${entry}" >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_support_missing; return 1; }
	done
	if [ ! -f "${_AI_ENGINE_DIR}/claude_anthropic_relay.py" ] || [ ! -f "${_AI_ENGINE_DIR}/clarify_sandbox/Dockerfile" ]; then
		AI_ENGINE_ISOLATION_FAILURE=isolation_support_missing; return 1
	fi
	AI_ENGINE_ISOLATION_GUARD="$(_ai_engine_py support-file --name guard-hook 2>/dev/null)" || { AI_ENGINE_ISOLATION_FAILURE=policy_unavailable; return 1; }
	[ -f "${AI_ENGINE_ISOLATION_GUARD}" ] || { AI_ENGINE_ISOLATION_FAILURE=policy_unavailable; return 1; }
	AI_ENGINE_ISOLATION_PATHS=()
	if [ -n "${AI_ENGINE_ISOLATED_READ_PATHS:-}" ]; then
		if [ "${role}" != WORKFLOW_HEAL ] || [ -z "${RUNTIME_DIR:-}" ] || [[ "${AI_ENGINE_ISOLATED_READ_PATHS}" == *$'\n'* ]]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
		runtime_real="$(realpath -e -- "${RUNTIME_DIR}")" || { AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1; }
		IFS=: read -r -a AI_ENGINE_ISOLATION_PATHS <<< "${AI_ENGINE_ISOLATED_READ_PATHS}"
		if [ "${#AI_ENGINE_ISOLATION_PATHS[@]}" -gt 2 ] || [[ "${AI_ENGINE_ISOLATED_READ_PATHS}" == *: ]]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
		if [ "${#AI_ENGINE_ISOLATION_PATHS[@]}" -eq 2 ] &&
		   [ "${AI_ENGINE_ISOLATION_PATHS[0]}" = "${AI_ENGINE_ISOLATION_PATHS[1]}" ]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
		for candidate in "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
			if [ ! -d "${candidate}" ] || [ -L "${candidate}" ] ||
			   { [ "${candidate}" != "${RUNTIME_DIR}/heal_src" ] && [ "${candidate}" != "${RUNTIME_DIR}/heal_branch_tip" ]; }; then
				AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
			fi
		done
	fi
	pool_real="$(realpath -e -- "${pool_dir}")" || { AI_ENGINE_ISOLATION_FAILURE=isolation_pool_overlap; return 1; }
	for candidate in "${workdir}" "${prompt_file}" "${instructions}" "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
		candidate="$(realpath -e -- "${candidate}")" || { AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1; }
		if [[ "${candidate}" == *,* || "${candidate}" == *$'\n'* ]]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
		if [[ "${candidate}/" == "${pool_real}/"* || "${pool_real}/" == "${candidate}/"* ]]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_pool_overlap; return 1
		fi
	done
	for candidate in "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
		if [ "$(realpath -e -- "${candidate}")" != "${runtime_real}/$(basename -- "${candidate}")" ]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
	done
}

_ai_engine_isolation_image()
{
	local dockerfile="${_AI_ENGINE_DIR}/clarify_sandbox/Dockerfile" version tag
	version="$(ai_engine_cli_version)" || return 1
	[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
	tag="coding-workflows-claude-read:${version}-$(sha256sum "${dockerfile}" | cut -c1-12)"
	if ! env -i PATH="${PATH}" docker image inspect "${tag}" >/dev/null 2>&1; then
		env -i PATH="${PATH}" docker build -q --build-arg "CLAUDE_CLI_VERSION=${version}" \
			-t "${tag}" -f "${dockerfile}" "${_AI_ENGINE_DIR}/clarify_sandbox" >/dev/null || return 1
	fi
	printf '%s\n' "${tag}"
}

_ai_engine_git_mask_configs()
{
	local run_dir="$1" directory gitdir common config config_real mount_dir key count=0 keys_file
	AI_ENGINE_ISOLATION_MASKS=()
	local -a seen=()
	for directory in "${AI_ENGINE_ISOLATION_WORKDIR}" "${AI_ENGINE_ISOLATION_PATHS[@]}" \
			"${AI_ENGINE_ISOLATION_WORKDIR}/.codex-workflow-src" \
			"${AI_ENGINE_ISOLATION_WORKDIR}/.codex-workflow-src-main"; do
		if ! gitdir="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -C "${directory}" rev-parse --absolute-git-dir 2>/dev/null)"; then
			# A malformed checkout must not bypass masking just because git cannot parse it.
			[ ! -e "${directory}/.git" ] && [ ! -L "${directory}/.git" ] || return 1
			continue
		fi
		common="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -C "${directory}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
		for config in "${gitdir}/config" "${gitdir}/config.worktree" "${common}/config"; do
			[ -f "${config}" ] || continue
			config_real="$(realpath -e -- "${config}")" || return 1
			for mount_dir in "${AI_ENGINE_ISOLATION_WORKDIR}" "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
				[[ "${config_real}" == "${mount_dir}/"* ]] || continue
				if printf '%s\n' "${seen[@]}" | grep -Fxq -- "${config_real}"; then break; fi
				seen+=("${config_real}")
				count=$((count + 1))
				[ "${count}" -le 20 ] || return 1
				mkdir -p "${run_dir}/git-mask/${count}" || return 1
				cp -- "${config_real}" "${run_dir}/git-mask/${count}/config" || return 1
				keys_file="${run_dir}/git-mask/${count}/keys"
				local git_rc=0
				# Remote URLs can themselves contain an embedded username/password.
				git config --file "${run_dir}/git-mask/${count}/config" --name-only --get-regexp '^(http(\..*)?\.extraheader|credential\.|url\..*\.insteadof|include\.|includeif\.|remote\..*\.(url|pushurl))' > "${keys_file}" || git_rc=$?
				case "${git_rc}" in 0|1) ;; *) return 1 ;; esac
				sort -u -o "${keys_file}" "${keys_file}" || return 1
				while IFS= read -r key; do
					[ -n "${key}" ] || continue
					git config --file "${run_dir}/git-mask/${count}/config" --unset-all "${key}" || return 1
				done < "${keys_file}"
				AI_ENGINE_ISOLATION_MASKS+=(--mount "type=bind,src=${run_dir}/git-mask/${count}/config,dst=${config_real},readonly")
				break
			done
		done
	done
}

_ai_engine_claude_run_isolated()
{
	local role="$1" prompt_file="$2" out_file="$3" workdir="$4" session_id="$5" model="$6" effort="$7" instructions="$8" pool_dir="$9" run_dir="${10}" hide_claude_md="${11}"
	local guard_hook image probe_model reason name token_file transcript stderr_file verdict outcome attempt_rc broker_pid reaper_pid container_name parent_pid attempt=0 relay_failures=0 i session_mount_root pool_mount_root snapshot_dir snapshot_result snapshot_summary snapshot_files snapshot_git snapshot_reason extra_files
	local extra_snapshot_dir extra_source extra_dest extra_summary extra_existing run_dir_real extra_overlaps extra_details read_max_secs
	local -a mounts=() session_args=() cmd=() accounts=() snapshot_args=() extra_sources=() extra_roots=()
	local git_objects snapshot_files_used snapshot_bytes_used snapshot_files_limit snapshot_bytes_limit extra_snapshot
	local AI_ENGINE_ISOLATION_WORKDIR AI_ENGINE_ISOLATION_FAILURE AI_ENGINE_ISOLATION_GUARD
	local -a AI_ENGINE_ISOLATION_PATHS=() AI_ENGINE_ISOLATION_MASKS=()
	AI_ENGINE_ISOLATION_WORKDIR="${workdir}"
	if ! _ai_engine_isolation_preflight "${pool_dir}" "${workdir}" "${prompt_file}" "${instructions}" "${role}"; then
		ai_engine_fallback "${role}" "${AI_ENGINE_ISOLATION_FAILURE}"; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	pool_mount_root="$(realpath -e -- "${pool_dir}")" && run_dir_real="$(realpath -e -- "${run_dir}")" || {
		ai_engine_fallback "${role}" isolation_pool_unavailable; return "${_AI_ENGINE_EXIT_FALLBACK}"
	}
	# Extra worktrees are data, never bind mounts of the caller's source tree.
	IFS=: read -r -a extra_sources <<< "${AI_ENGINE_READ_EXTRA_DIRS:-}"
	for extra_source in "${extra_sources[@]}"; do
		[ -n "${extra_source}" ] || continue
		if ! _ai_engine_read_path_valid "${extra_source}" ||
			_ai_engine_read_path_sensitive "${extra_source}" "${run_dir_real}" "${pool_mount_root}" ||
			[[ "${extra_source}/" == "${workdir}/"* || "${workdir}/" == "${extra_source}/"* ]]; then
			echo '::warning::Claude read isolation: invalid extra directory skipped' >&2
			continue
		fi
		extra_overlaps=0
		for extra_existing in "${extra_roots[@]}"; do
			if [[ "${extra_source}/" == "${extra_existing}/"* || "${extra_existing}/" == "${extra_source}/"* ]]; then
				extra_overlaps=1
				break
			fi
		done
		if [ "${extra_overlaps}" -eq 1 ]; then
			echo '::warning::Claude read isolation: overlapping extra directory skipped' >&2
			continue
		fi
		extra_roots+=("${extra_source}")
	done
	guard_hook="${AI_ENGINE_ISOLATION_GUARD}"
	if ! _ai_engine_py settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile read --guard-hook /guard.py --read-guard-hook /read-guard.py; then
		ai_engine_fallback "${role}" policy_unavailable; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	chmod 0644 "${run_dir}/claude-settings.json"
	if ! image="$(_ai_engine_isolation_image)"; then
		ai_engine_fallback "${role}" isolation_image_build_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	snapshot_dir="${run_dir}/source-snapshot"
	if [ "${hide_claude_md}" = true ]; then
		snapshot_args+=(--omit-root-claude-md --omit-claude-md)
	fi
	if ! snapshot_result="$(_ai_engine_py read-snapshot --workdir "${workdir}" --dest "${snapshot_dir}" "${snapshot_args[@]}")" ||
	   ! snapshot_summary="$(_ai_engine_snapshot_summary "${snapshot_result}")"; then
		echo "CLAUDE_READ_ISOLATION role=${role} outcome=rejected reason=isolation_snapshot_failed files=0 git=none extra_dirs=0" >&2
		ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	read -r snapshot_files snapshot_git snapshot_reason <<< "${snapshot_summary}"
	mounts=(--mount "type=bind,src=${snapshot_dir},dst=${workdir},readonly")
	snapshot_files_limit="${CLAUDE_READ_SNAPSHOT_MAX_FILES:-50000}"
	snapshot_bytes_limit="${CLAUDE_READ_SNAPSHOT_MAX_BYTES:-1073741824}"
	if [[ ! "${snapshot_files_limit}" =~ ^[0-9]+$ || ! "${snapshot_bytes_limit}" =~ ^[0-9]+$ ]]; then
		ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	snapshot_files_limit=$((10#${snapshot_files_limit}))
	snapshot_bytes_limit=$((10#${snapshot_bytes_limit}))
	snapshot_files_used="$(_ai_engine_json_field "${snapshot_result}" files)" || { ai_engine_fallback "${role}" isolation_snapshot_failed; return 75; }
	snapshot_bytes_used="$(_ai_engine_json_field "${snapshot_result}" bytes)" || { ai_engine_fallback "${role}" isolation_snapshot_failed; return 75; }
	extra_snapshot_dir="${run_dir}/extra-snapshots"
	for extra_source in "${extra_roots[@]}"; do
		extra_dest="${extra_snapshot_dir}/${#mounts[@]}"
		if [ "${snapshot_files_limit}" -le "${snapshot_files_used}" ] || [ "${snapshot_bytes_limit}" -le "${snapshot_bytes_used}" ] ||
		   ! mkdir -p -- "${extra_snapshot_dir}" ||
		   ! extra_summary="$(CLAUDE_READ_SNAPSHOT_MAX_FILES="$((snapshot_files_limit - snapshot_files_used))" \
			CLAUDE_READ_SNAPSHOT_MAX_BYTES="$((snapshot_bytes_limit - snapshot_bytes_used))" \
			_ai_engine_py read-snapshot --workdir "${extra_source}" --dest "${extra_dest}" "${snapshot_args[@]}")" ||
		   ! extra_details="$(_ai_engine_snapshot_summary "${extra_summary}")"; then
			echo "CLAUDE_READ_ISOLATION role=${role} outcome=rejected reason=isolation_snapshot_failed files=${snapshot_files} git=${snapshot_git} extra_dirs=$((${#mounts[@]} / 2 - 1))" >&2
			ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
		fi
		extra_files="${extra_details%% *}"
		snapshot_files=$((snapshot_files + extra_files))
		snapshot_files_used=$((snapshot_files_used + extra_files))
		snapshot_bytes_used=$((snapshot_bytes_used + $(_ai_engine_json_field "${extra_summary}" bytes)))
		mounts+=(--mount "type=bind,src=${extra_dest},dst=${extra_source},readonly")
	done
	git_objects="$(printf '%s' "${snapshot_result}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("git_objects", ""))')" || {
		ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	}
	if [ -n "${git_objects}" ]; then
		ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	# Legacy auxiliary checkouts use the same remaining budget.
	for i in "${!AI_ENGINE_ISOLATION_PATHS[@]}"; do
		extra_snapshot="${run_dir}/source-snapshot-${i}"
		if [ "${snapshot_files_limit}" -le "${snapshot_files_used}" ] || [ "${snapshot_bytes_limit}" -le "${snapshot_bytes_used}" ] ||
		   ! mkdir -m 0700 -- "${extra_snapshot}" ||
		   ! snapshot_result="$(CLAUDE_READ_SNAPSHOT_MAX_FILES="$((snapshot_files_limit - snapshot_files_used))" \
			CLAUDE_READ_SNAPSHOT_MAX_BYTES="$((snapshot_bytes_limit - snapshot_bytes_used))" \
			_ai_engine_py read-snapshot --source "${AI_ENGINE_ISOLATION_PATHS[i]}" --dest "${extra_snapshot}" "${snapshot_args[@]}")"; then
			ai_engine_fallback "${role}" isolation_snapshot_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
		fi
		git_objects="$(_ai_engine_json_field "${snapshot_result}" git_objects)" || { ai_engine_fallback "${role}" isolation_snapshot_failed; return 75; }
		[ -z "${git_objects}" ] || { ai_engine_fallback "${role}" isolation_snapshot_failed; return 75; }
		snapshot_files=$((snapshot_files + $(_ai_engine_json_field "${snapshot_result}" files)))
		snapshot_files_used=$((snapshot_files_used + $(_ai_engine_json_field "${snapshot_result}" files)))
		snapshot_bytes_used=$((snapshot_bytes_used + $(_ai_engine_json_field "${snapshot_result}" bytes)))
		mounts+=(--mount "type=bind,src=${extra_snapshot},dst=${AI_ENGINE_ISOLATION_PATHS[i]},readonly")
	done
	if [ -n "${session_id}" ]; then
		[ ! -L "${RUNNER_TEMP:-/tmp}/claude-read-sessions/${session_id}" ] || { ai_engine_fallback "${role}" isolation_session_dir_unavailable; return 75; }
		mkdir -p "${RUNNER_TEMP:-/tmp}/claude-read-sessions/${session_id}" && chmod 0700 "${RUNNER_TEMP:-/tmp}/claude-read-sessions/${session_id}" || { ai_engine_fallback "${role}" isolation_session_dir_unavailable; return 75; }
		session_mount_root="$(realpath -e -- "${RUNNER_TEMP:-/tmp}/claude-read-sessions/${session_id}")" || { ai_engine_fallback "${role}" isolation_session_dir_unavailable; return 75; }
		pool_mount_root="$(realpath -e -- "${pool_dir}")" || { ai_engine_fallback "${role}" isolation_pool_unavailable; return 75; }
		if [[ "${session_mount_root}/" == "${pool_mount_root}/"* || "${pool_mount_root}/" == "${session_mount_root}/"* ]]; then
			ai_engine_fallback "${role}" isolation_pool_overlap; return 75
		fi
		if compgen -G "${session_mount_root}/*/${session_id}.jsonl" >/dev/null; then
			session_args=(--resume "${session_id}")
		else
			session_args=(--session-id "${session_id}")
		fi
		mounts+=(--mount "type=bind,src=${session_mount_root},dst=/home/agent/.claude/projects")
	fi
	probe_model="$(_ai_engine_py config --key probe_model)" || { ai_engine_fallback "${role}" policy_unavailable; return 75; }
	read_max_secs="${CLAUDE_READ_ISOLATION_MAX_SECS:-14400}"
	if [[ ! "${read_max_secs}" =~ ^[1-9][0-9]{0,5}$ ]]; then
		echo '::warning::invalid CLAUDE_READ_ISOLATION_MAX_SECS; using 14400' >&2
		read_max_secs=14400
	fi
	mapfile -t accounts < <(ai_engine_accounts)
	echo "CLAUDE_ISOLATION role=${role} profile=read mode=container" >&2
	echo "CLAUDE_READ_ISOLATION role=${role} outcome=ready reason=${snapshot_reason} files=${snapshot_files} git=${snapshot_git} extra_dirs=$((${#extra_roots[@]} + ${#AI_ENGINE_ISOLATION_PATHS[@]}))" >&2
	for name in "${accounts[@]}"; do
		attempt=$((attempt + 1))
		token_file="${pool_dir}/tokens/${name}"
		mkdir -p "${run_dir}/sock" && chmod 0700 "${run_dir}/sock" || { ai_engine_fallback "${role}" isolation_relay_unavailable; return 75; }
		rm -f -- "${run_dir}/sock/provider.sock"
		container_name="claude-read-${GITHUB_RUN_ID:-local}-${BASHPID}-${attempt}"
		parent_pid="${BASHPID}"
		env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 python3 "${_AI_ENGINE_DIR}/claude_anthropic_relay.py" broker "${run_dir}/sock/provider.sock" "${token_file}" "${model},${probe_model}" >/dev/null 2>"${run_dir}/relay-${name}.stderr" &
		broker_pid=$!
		# Start the reaper before waiting for the socket: SIGKILL skips shell traps.
		# shellcheck disable=SC2016 # This is the reaper's shell, not the caller's.
		setsid bash -c 'parent=$1; container=$2; broker=$3; snapshot=$4; extras=$5; while kill -0 "$parent" 2>/dev/null && [ "$(ps -o stat= -p "$parent" 2>/dev/null)" != Z ]; do sleep 1; done; docker rm -f "$container" >/dev/null 2>&1; kill "$broker" 2>/dev/null; rm -rf -- "$snapshot" "${snapshot}"-* "$extras"' _ "${parent_pid}" "${container_name}" "${broker_pid}" "${snapshot_dir}" "${extra_snapshot_dir}" >/dev/null 2>&1 < /dev/null &
		reaper_pid=$!
		for ((i=0; i<50; i++)); do
			[ -S "${run_dir}/sock/provider.sock" ] && break
			kill -0 "${broker_pid}" 2>/dev/null || break
			sleep 0.1
		done
		if [ ! -S "${run_dir}/sock/provider.sock" ]; then
			echo "CLAUDE_POOL run role=${role} account=${name} outcome=crashed reason=relay_unavailable" >&2
			kill "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
			wait "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
			relay_failures=$((relay_failures + 1))
			continue
		fi
		cmd=(env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN -u GH_HOST
			-u GH_PAT -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID -u OPENROUTER_API_KEY
			-u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_RUNTIME_TOKEN
			-u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL
			docker run --rm -i --name "${container_name}" --user "$(id -u):$(id -g)"
			--network none --read-only --cap-drop ALL --security-opt no-new-privileges
			--pids-limit 128 --memory 2g --cpus 2
			--tmpfs "/tmp:rw,nosuid,nodev,size=64m" --tmpfs "/home/agent:rw,nosuid,nodev,size=64m,mode=1777"
			"${mounts[@]}"
			--mount "type=bind,src=${run_dir}/sock,dst=/socket,readonly"
			--mount "type=bind,src=${_AI_ENGINE_DIR}/claude_anthropic_relay.py,dst=/relay.py,readonly"
			--mount "type=bind,src=${_AI_ENGINE_DIR}/claude_engine.py,dst=/read-guard.py,readonly"
			--mount "type=bind,src=${instructions},dst=/instructions.md,readonly"
			--mount "type=bind,src=${run_dir}/claude-settings.json,dst=/settings.json,readonly"
			--mount "type=bind,src=${guard_hook},dst=/guard.py,readonly"
			--env HOME=/home/agent --env ANTHROPIC_BASE_URL=http://127.0.0.1:8765
			--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder --env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
			--env DISABLE_AUTOUPDATER=1 --env "CLAUDE_MODEL=${model}" --env "CLAUDE_EFFORT=${effort}"
			--env "CLAUDE_WORKDIR=${workdir}" --workdir "${workdir}" "${image}" /bin/bash -c '
				set -euo pipefail
				python3 -c '\''import json,os; open(os.path.join(os.environ["HOME"], ".claude.json"), "w").write(json.dumps({"projects": {os.environ["CLAUDE_WORKDIR"]: {"hasTrustDialogAccepted": True}}}))'\''
				python3 /relay.py bridge /socket/provider.sock &
				bridge_pid=$!
				trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
				python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
				printf "CLAUDE_READ_CONTAINER_READY\n" >&2
				claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" --system-prompt-file /instructions.md \
					--setting-sources "" --settings /settings.json --strict-mcp-config --disable-slash-commands \
					--exclude-dynamic-system-prompt-sections --tools Read,Grep,Glob,Bash --permission-mode dontAsk \
					--output-format stream-json --verbose "$@"
			' _ "${session_args[@]}")
		transcript="${run_dir}/transcript-${name}.jsonl"
		stderr_file="${run_dir}/stderr-${name}.txt"
		attempt_rc=0
		if [ -f "${_AI_ENGINE_DIR}/codex_stall_guard.sh" ]; then
			timeout --signal=TERM --kill-after=30s "${read_max_secs}s" bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
				--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- "${cmd[@]}" < "${prompt_file}" || attempt_rc=$?
		else
			timeout --signal=TERM --kill-after=30s "${read_max_secs}s" "${cmd[@]}" < "${prompt_file}" > "${transcript}" 2> "${stderr_file}" || attempt_rc=$?
		fi
		env -i PATH="${PATH}" docker rm -f "${container_name}" >/dev/null 2>&1 || true
		kill "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
		wait "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
		# Do not run the checkout's classifier/extractor after the model if its
		# trusted support changed. The caller will unlock and report the mismatch.
		if ! PYTHONDONTWRITEBYTECODE=1 python3 "${run_dir}/claude_engine.py" support-verify --manifest "${run_dir}/support-lock.json"; then
			return "${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"
		fi
		verdict="$(_ai_engine_py classify --transcript "${transcript}" --exit-code "${attempt_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
		outcome="$(_ai_engine_json_field "${verdict}" outcome)"
		reason="$(_ai_engine_json_field "${verdict}" reason)"
		echo "CLAUDE_POOL run role=${role} account=${name} outcome=${outcome} reason=${reason} exit_code=${attempt_rc}" >&2
		if [ "${attempt_rc}" -ne 0 ] && ! grep -Fxq 'CLAUDE_READ_CONTAINER_READY' "${stderr_file}"; then
			ai_engine_fallback "${role}" isolation_container_start_failed
			return "${_AI_ENGINE_EXIT_FALLBACK}"
		fi
		case "${outcome}" in
			success)
				_ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || return 1
				ln -s -- "transcript-${name}.jsonl" "${run_dir}/successful-transcript.jsonl" || return 1
				return 0 ;;
			usage_limit|auth_failed) continue ;;
			timeout) return 124 ;;
			*) _ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || true; return 1 ;;
		esac
	done
	if [ "${relay_failures}" -eq "${#accounts[@]}" ]; then
		ai_engine_fallback "${role}" isolation_relay_unavailable
	else
		ai_engine_fallback "${role}" all_accounts_failed
	fi
	return "${_AI_ENGINE_EXIT_FALLBACK}"
}

claude_run()
{
	local role="${1:-}" prompt_file="${2:-}" out_file="${3:-}" workdir="${4:-}" session_id="${5:-}"
	if ! _ai_engine_valid_role "${role}" || [ ! -s "${prompt_file}" ] || [ -z "${out_file}" ] || [ ! -d "${workdir}" ]; then
		echo "::error::claude_run: usage: claude_run <role> <prompt_file> <out_file> <workdir> [session_id]" >&2
		return 2
	fi
	# The run changes into <workdir>; every path must survive that.
	workdir="$(cd "${workdir}" && pwd)" || return 2
	case "${prompt_file}" in /*) ;; *) prompt_file="${PWD}/${prompt_file}" ;; esac
	case "${out_file}" in /*) ;; *) out_file="${PWD}/${out_file}" ;; esac
	if [ -n "${session_id}" ] && ! [[ "${session_id}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
		echo "::error::claude_run: session_id must be a lower-case UUID" >&2
		return 2
	fi
	local resolved model effort profile hide_claude_md instructions guard_hook cli_version probe_model
	if ! resolved="$(_ai_engine_py resolve --role "${role}" --model-hint "${AI_ENGINE_MODEL_HINT:-}" --effort-hint "${AI_ENGINE_EFFORT_HINT:-}")"; then
		ai_engine_fallback "${role}" resolve_failed
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	model="$(_ai_engine_json_field "${resolved}" model)"
	effort="$(_ai_engine_json_field "${resolved}" effort)"
	profile="$(_ai_engine_json_field "${resolved}" profile)"
	# AI_ENGINE_READ_ONLY=true narrows a write role to the read profile for
	# one call (the review-blocked judge's verdict pass); it never widens one.
	if [ "${AI_ENGINE_READ_ONLY:-false}" = "true" ]; then
		profile="read"
	fi
	hide_claude_md="$(_ai_engine_py config --key hide_claude_md 2>/dev/null || echo false)"
	if ! instructions="$(_ai_engine_instructions_file)"; then
		ai_engine_fallback "${role}" instructions_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	if ! guard_hook="$(_ai_engine_py support-file --name guard-hook)" \
		|| ! cli_version="$(ai_engine_cli_version)" \
		|| ! probe_model="$(_ai_engine_py config --key probe_model)"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	# The helper accepts only an absolute, regular file (it copies it into
	# the container as /support/instructions.md).
	if ! instructions="$(realpath -e -- "${instructions}")" || [ ! -f "${instructions}" ]; then
		ai_engine_fallback "${role}" instructions_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi

	local pool_dir
	pool_dir="$(ai_engine_pool_dir)"
	local -a accounts=()
	local name
	while IFS= read -r name; do
		accounts+=("${name}")
	done < <(ai_engine_accounts)
	if [ "${#accounts[@]}" -eq 0 ]; then
		ai_engine_fallback "${role}" no_credential
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi

	local run_dir rc=0
	run_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/claude-run-XXXXXXXX")" || return 1
	# The caller may read the transcripts (transcript-<NAME>.jsonl) after the run.
	# shellcheck disable=SC2034  # read by callers after claude_run returns
	AI_ENGINE_LAST_RUN_DIR="${run_dir}"
	if [ "${profile}" = read ]; then
		# Keep the verifier outside the checkout that the model reads.
		local locked_files old_int_trap old_term_trap
		if ! cp -- "${_AI_ENGINE_DIR}/claude_engine.py" "${run_dir}/claude_engine.py" ||
		   ! locked_files="$(_ai_engine_py support-lock --manifest "${run_dir}/support-lock.json" --workdir "${workdir}")"; then
			echo "::error::Claude read profile could not lock trusted support." >&2
			echo "AI_ENGINE_SUPPORT_LOCK role=${role} outcome=tampered" >&2
			return "${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"
		fi
		echo "AI_ENGINE_SUPPORT_LOCK role=${role} outcome=locked files=${locked_files}" >&2
		old_int_trap="$(trap -p INT)"
		old_term_trap="$(trap -p TERM)"
		trap '_ai_engine_support_finish "${run_dir}" "${role}" || exit "${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"; rm -rf -- "${run_dir}/source-snapshot" "${run_dir}"/source-snapshot-[0-9]* "${run_dir}/extra-snapshots"; exit 130' INT
		trap '_ai_engine_support_finish "${run_dir}" "${role}" || exit "${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"; rm -rf -- "${run_dir}/source-snapshot" "${run_dir}"/source-snapshot-[0-9]* "${run_dir}/extra-snapshots"; exit 143' TERM
		_ai_engine_claude_run_isolated "${role}" "${prompt_file}" "${out_file}" "${workdir}" "${session_id}" "${model}" "${effort}" "${instructions}" "${pool_dir}" "${run_dir}" "${hide_claude_md}" || rc=$?
		_ai_engine_support_finish "${run_dir}" "${role}" || rc="${_AI_ENGINE_EXIT_SUPPORT_TAMPERED}"
		rm -rf -- "${run_dir}/source-snapshot" "${run_dir}"/source-snapshot-[0-9]* "${run_dir}/extra-snapshots"
		trap - INT TERM
		[ -z "${old_int_trap}" ] || eval "${old_int_trap}"
		[ -z "${old_term_trap}" ] || eval "${old_term_trap}"
		return "${rc}"
	fi
	# Write profiles use the isolated workspace helper, never a host CLI.
	local isolated_exec="${_AI_ENGINE_DIR}/codex_isolated_exec.sh"
	if [ ! -f "${isolated_exec}" ] || [ -L "${isolated_exec}" ]; then
		ai_engine_fallback "${role}" support_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	# The workdir copy is mounted at the same absolute path, so the policy's
	# checkout paths match; the guard hook is copied to /support/guard.py.
	local -a settings_args=(settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile "${profile}" --guard-hook /support/guard.py)
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ] && settings_args+=(--allow-workflow-edits)
	if ! _ai_engine_py "${settings_args[@]}"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	local tools mode isolation_mode
	case "${profile}" in
		read) tools="Read,Grep,Glob,Bash"; mode="dontAsk"; isolation_mode="read-only" ;;
		# An explicit list, not "default": the default set loads ~35 tools whose
		# descriptions push a no-op start-up past the 25,000-token context gate.
		# Keep in sync with PROFILE_TOOLS["write"] in claude_engine.py.
		*) tools="Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch"; mode="bypassPermissions"; isolation_mode="workspace" ;;
	esac
	# The CLI's session store (~/.claude) lives outside the container so a
	# later claude_run in the same job can resume the session (answer Q18 A).
	local claude_home
	claude_home="$(ai_engine_claude_home)"
	local -a isolation_args=(run --engine claude --mode "${isolation_mode}" --workdir "${workdir}"
		--claude-models "${model},${probe_model}" --claude-cli-version "${cli_version}"
		--claude-settings "${run_dir}/claude-settings.json" --claude-guard-hook "${guard_hook}"
		--claude-instructions "${instructions}" --claude-home "${claude_home}")
	# The container copy leaves CLAUDE.md out and never writes one back; the
	# host file is never moved (answer Q19 A).
	[ "${hide_claude_md}" = "true" ] && isolation_args+=(--hide-claude-md)
	# Implement prepares one workspace sandbox per job with the project's
	# dependencies preinstalled (codex_isolated_exec.sh prepare --deps); a
	# write role in that job reuses it, as the codex attempts do.
	if [ "${isolation_mode}" = "workspace" ] && [ -n "${CODEX_ISOLATED_ROOT:-}" ] && [ "${CODEX_ISOLATED_MODE:-}" = "workspace" ]; then
		isolation_args+=(--root "${CODEX_ISOLATED_ROOT}")
	fi

	(
		trap 'exit 130' INT
		trap 'exit 143' TERM
		cd "${workdir}" || exit 1
		for name in "${accounts[@]}"; do
			token_file="${pool_dir}/tokens/${name}"
			session_args=()
			if [ -n "${session_id}" ]; then
				if compgen -G "${claude_home}/projects/*/${session_id}.jsonl" >/dev/null; then
					session_args=(--resume "${session_id}")
				else
					session_args=(--session-id "${session_id}")
				fi
			fi
			transcript="${run_dir}/transcript-${name}.jsonl"
			stderr_file="${run_dir}/stderr-${name}.txt"
			# The token file is read only by the host relay the helper starts.
			cmd=(bash "${isolated_exec}" "${isolation_args[@]}" --claude-token-file "${token_file}" --
				-p --model "${model}" --effort "${effort}"
				--system-prompt-file /support/instructions.md
				--setting-sources "" --settings /support/settings.json
				--strict-mcp-config --disable-slash-commands
				--exclude-dynamic-system-prompt-sections
				--tools "${tools}" --permission-mode "${mode}"
				--output-format stream-json --verbose "${session_args[@]}")
			attempt_rc=0
			if [ -f "${_AI_ENGINE_DIR}/codex_stall_guard.sh" ]; then
				bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
					--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- "${cmd[@]}" < "${prompt_file}" || attempt_rc=$?
			else
				"${cmd[@]}" < "${prompt_file}" > "${transcript}" 2> "${stderr_file}" || attempt_rc=$?
			fi
			case "${attempt_rc}" in
				75)
					# No Docker or no image: no account can run (answer Q20 A).
					# The helper's reason (and any image build output) goes to the job log.
					tail -n 30 "${stderr_file}" >&2 2>/dev/null || true
					echo "CLAUDE_POOL run role=${role} account=${name} outcome=unavailable reason=isolation_unavailable exit_code=${attempt_rc}" >&2
					exit 76
					;;
				73)
					echo "CLAUDE_POOL run role=${role} account=${name} outcome=crashed reason=relay_unavailable exit_code=${attempt_rc}" >&2
					continue
					;;
			esac
			verdict="$(_ai_engine_py classify --transcript "${transcript}" --exit-code "${attempt_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
			outcome="$(_ai_engine_json_field "${verdict}" outcome)"
			reason="$(_ai_engine_json_field "${verdict}" reason)"
			echo "CLAUDE_POOL run role=${role} account=${name} outcome=${outcome} reason=${reason} exit_code=${attempt_rc}" >&2
			case "${outcome}" in
				success)
					_ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || exit 1
					ln -s -- "transcript-${name}.jsonl" "${run_dir}/successful-transcript.jsonl" || exit 1
					exit 0
					;;
				usage_limit|auth_failed)
					continue
					;;
				timeout)
					exit 124
					;;
				*)
					# Keep whatever the run produced for the role's own diagnostics.
					_ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || true
					exit 1
					;;
			esac
		done
		exit "${_AI_ENGINE_EXIT_FALLBACK}"
	) || rc=$?
	if [ "${rc}" -eq 76 ]; then
		ai_engine_fallback "${role}" isolation_unavailable
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	if [ "${rc}" -eq "${_AI_ENGINE_EXIT_FALLBACK}" ]; then
		ai_engine_fallback "${role}" all_accounts_failed
	fi
	return "${rc}"
}

# claude_run_selected <role> <prompt_file> <out_file> <workdir> [session_id]
# For the call sites of a cut-over role (plan Phase 5d): runs claude_run
# only when the job's "Resolve AI engine" step exported
# AI_ENGINE_RESOLVED_<ROLE>=claude; otherwise returns 75 without running
# anything, so the caller runs its unchanged codex command. Same statuses
# as claude_run.
claude_run_selected()
{
	local role="${1:-}" resolved_var
	_ai_engine_valid_role "${role}" || return "${_AI_ENGINE_EXIT_FALLBACK}"
	resolved_var="AI_ENGINE_RESOLVED_${role}"
	[ "${!resolved_var:-codex}" = "claude" ] || return "${_AI_ENGINE_EXIT_FALLBACK}"
	claude_run "$@"
}
