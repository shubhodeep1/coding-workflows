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
#   ai_engine_accounts
#       The usable account names, best first: each line of `order` that is a
#       valid name and has a regular, non-empty `tokens/<NAME>` file.
#   claude_run_selected <role> <prompt_file> <out_file> <workdir> [session_id]
#       claude_run when AI_ENGINE_RESOLVED_<ROLE>=claude, else 75 at once.
#   claude_run <role> <prompt_file> <out_file> <workdir> [session_id]
#       Runs `claude -p` for the role in <workdir> and writes the final
#       result text to <out_file>, the file the codex path writes.
#       Returns 0 on success; 75 when Claude is unavailable (no CLI, no
#       credential, no policy, or every account hit its usage limit or was
#       rejected), after logging AI_ENGINE_FALLBACK, so the caller runs the
#       codex path (D1); 124 on a timeout; any other non-zero status on a
#       crash, which follows the role's existing retry rules.
#       AI_ENGINE_LAST_RUN_DIR names the run directory afterwards; it holds
#       transcript-<NAME>.jsonl and stderr-<NAME>.txt for each account tried.
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
#   AI_ENGINE_ISOLATED_READ_PATHS  colon-separated extra read-only directories
#                                   for read-profile calls (default empty)
#
# Write-profile calls pass the OAuth token to the host CLI in a subshell.
# Read-profile calls instead run in a networkless container with a placeholder
# token; only the host relay reads the pool. Isolation failures return 75.

if [ "${_AI_ENGINE_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_AI_ENGINE_LOADED="true"

_AI_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_AI_ENGINE_EXIT_FALLBACK=75

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

_ai_engine_isolation_preflight()
{
	local pool_dir="$1" workdir="$2" prompt_file="$3" instructions="$4" entry
	AI_ENGINE_ISOLATION_FAILURE=""
	command -v docker >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_docker_missing; return 1; }
	command -v python3 >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_python_missing; return 1; }
	for entry in setsid ps git realpath sha256sum; do
		command -v "${entry}" >/dev/null 2>&1 || { AI_ENGINE_ISOLATION_FAILURE=isolation_support_missing; return 1; }
	done
	if [ ! -f "${_AI_ENGINE_DIR}/claude_anthropic_relay.py" ] || [ ! -f "${_AI_ENGINE_DIR}/clarify_sandbox/Dockerfile" ]; then
		AI_ENGINE_ISOLATION_FAILURE=isolation_support_missing; return 1
	fi
	AI_ENGINE_ISOLATION_GUARD="$(_ai_engine_py support-file --name guard-hook 2>/dev/null)" || { AI_ENGINE_ISOLATION_FAILURE=policy_unavailable; return 1; }
	[ -f "${AI_ENGINE_ISOLATION_GUARD}" ] || { AI_ENGINE_ISOLATION_FAILURE=policy_unavailable; return 1; }
	AI_ENGINE_ISOLATION_PATHS=()
	if [ -n "${AI_ENGINE_ISOLATED_READ_PATHS:-}" ]; then
		local -a entries=()
		IFS=: read -r -a entries <<< "${AI_ENGINE_ISOLATED_READ_PATHS}"
		if [ "${#entries[@]}" -gt 8 ] || [[ "${AI_ENGINE_ISOLATED_READ_PATHS}" == *: ]]; then
			AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
		fi
		for entry in "${entries[@]}"; do
			if [[ "${entry}" != /* ]] || [ ! -d "${entry}" ] || [ -L "${entry}" ] || [[ "${entry}" == *,* ]]; then
				AI_ENGINE_ISOLATION_FAILURE=isolation_read_path_invalid; return 1
			fi
			AI_ENGINE_ISOLATION_PATHS+=("$(realpath -e -- "${entry}")")
		done
	fi
	local pool_real candidate
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
	for directory in "${AI_ENGINE_ISOLATION_WORKDIR}" "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
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
	local guard_hook image probe_model reason name token_file transcript stderr_file verdict outcome attempt_rc broker_pid reaper_pid container_name parent_pid attempt=0 relay_failures=0 i
	local -a mounts=() session_args=() cmd=() accounts=()
	AI_ENGINE_ISOLATION_WORKDIR="${workdir}"
	if ! _ai_engine_isolation_preflight "${pool_dir}" "${workdir}" "${prompt_file}" "${instructions}"; then
		ai_engine_fallback "${role}" "${AI_ENGINE_ISOLATION_FAILURE}"; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	guard_hook="${AI_ENGINE_ISOLATION_GUARD}"
	if ! _ai_engine_py settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile read --guard-hook /guard.py; then
		ai_engine_fallback "${role}" policy_unavailable; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	chmod 0644 "${run_dir}/claude-settings.json"
	if ! image="$(_ai_engine_isolation_image)"; then
		ai_engine_fallback "${role}" isolation_image_build_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	if ! _ai_engine_git_mask_configs "${run_dir}"; then
		ai_engine_fallback "${role}" isolation_mask_failed; return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	mounts=(--mount "type=bind,src=${workdir},dst=${workdir},readonly")
	for name in "${AI_ENGINE_ISOLATION_PATHS[@]}"; do
		mounts+=(--mount "type=bind,src=${name},dst=${name},readonly")
	done
	mounts+=("${AI_ENGINE_ISOLATION_MASKS[@]}")
	if [ "${hide_claude_md}" = true ] && [ -f "${workdir}/CLAUDE.md" ] && [ ! -L "${workdir}/CLAUDE.md" ]; then
		: > "${run_dir}/empty-claude-md"
		mounts+=(--mount "type=bind,src=${run_dir}/empty-claude-md,dst=${workdir}/CLAUDE.md,readonly")
	fi
	if [ -n "${session_id}" ]; then
		mkdir -p "${RUNNER_TEMP:-/tmp}/claude-read-sessions" && chmod 0700 "${RUNNER_TEMP:-/tmp}/claude-read-sessions" || { ai_engine_fallback "${role}" isolation_mask_failed; return 75; }
		if compgen -G "${RUNNER_TEMP:-/tmp}/claude-read-sessions/*/${session_id}.jsonl" >/dev/null; then
			session_args=(--resume "${session_id}")
		else
			session_args=(--session-id "${session_id}")
		fi
		mounts+=(--mount "type=bind,src=${RUNNER_TEMP:-/tmp}/claude-read-sessions,dst=/home/agent/.claude/projects")
	fi
	probe_model="$(_ai_engine_py config --key probe_model)" || { ai_engine_fallback "${role}" policy_unavailable; return 75; }
	mapfile -t accounts < <(ai_engine_accounts)
	echo "CLAUDE_ISOLATION role=${role} profile=read mode=container" >&2
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
		setsid bash -c 'parent=$1; container=$2; broker=$3; while kill -0 "$parent" 2>/dev/null && [ "$(ps -o stat= -p "$parent" 2>/dev/null)" != Z ]; do sleep 1; done; docker rm -f "$container" >/dev/null 2>&1; kill "$broker" 2>/dev/null' _ "${parent_pid}" "${container_name}" "${broker_pid}" >/dev/null 2>&1 < /dev/null &
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
			bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
				--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- "${cmd[@]}" < "${prompt_file}" || attempt_rc=$?
		else
			"${cmd[@]}" < "${prompt_file}" > "${transcript}" 2> "${stderr_file}" || attempt_rc=$?
		fi
		env -i PATH="${PATH}" docker rm -f "${container_name}" >/dev/null 2>&1 || true
		kill "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
		wait "${broker_pid}" "${reaper_pid}" 2>/dev/null || true
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
	local resolved model effort profile hide_claude_md instructions
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
	if [ "${profile}" != read ] && ! command -v claude >/dev/null 2>&1; then
		ai_engine_fallback "${role}" cli_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	hide_claude_md="$(_ai_engine_py config --key hide_claude_md 2>/dev/null || echo false)"
	if ! instructions="$(_ai_engine_instructions_file)"; then
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

	local run_dir
	run_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/claude-run-XXXXXXXX")" || return 1
	# The caller may read the transcripts (transcript-<NAME>.jsonl) after the run.
	# shellcheck disable=SC2034  # read by callers after claude_run returns
	AI_ENGINE_LAST_RUN_DIR="${run_dir}"
	if [ "${profile}" = read ]; then
		_ai_engine_claude_run_isolated "${role}" "${prompt_file}" "${out_file}" "${workdir}" "${session_id}" "${model}" "${effort}" "${instructions}" "${pool_dir}" "${run_dir}" "${hide_claude_md}"
		return $?
	fi
	local -a settings_args=(settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile "${profile}")
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ] && settings_args+=(--allow-workflow-edits)
	if ! _ai_engine_py "${settings_args[@]}"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	# Spike S10: the CLI ignores the allow list of an untrusted workspace.
	_ai_engine_py trust --workdir "${workdir}" || echo "::warning::claude_run: could not mark ${workdir} trusted" >&2
	local tools mode
	case "${profile}" in
		read) tools="Read,Grep,Glob,Bash"; mode="dontAsk" ;;
		# An explicit list, not "default": the default set loads ~35 tools whose
		# descriptions push a no-op start-up past the 25,000-token context gate.
		# Keep in sync with PROFILE_TOOLS["write"] in claude_engine.py.
		*) tools="Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch"; mode="bypassPermissions" ;;
	esac

	local rc=0
	(
		trap 'exit 130' INT
		trap 'exit 143' TERM
		unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN
		hidden=""
		if [ "${hide_claude_md}" = "true" ] && [ -f "${workdir}/CLAUDE.md" ] && [ ! -L "${workdir}/CLAUDE.md" ]; then
			hidden="${run_dir}/CLAUDE.md.hidden"
			mv -- "${workdir}/CLAUDE.md" "${hidden}" || exit 1
			trap 'if [ -e "${workdir}/CLAUDE.md" ] || [ -L "${workdir}/CLAUDE.md" ]; then
				if original_backup_path="$(mktemp "${workdir}/CLAUDE.md.original.XXXXXXXX")" && mv -- "${hidden}" "${original_backup_path}"; then
					echo "::warning::claude_run: CLAUDE.md created while hidden; original preserved at ${original_backup_path}" >&2
				else
					echo "::error::claude_run: could not restore original CLAUDE.md; preserved at ${hidden}" >&2
				fi
			else mv -- "${hidden}" "${workdir}/CLAUDE.md"; fi' EXIT
		fi
		cd "${workdir}" || exit 1
		credential_prefix=()
		if [ "${profile}" = "read" ]; then
			# An inherited gh login or host override must not replace the stripped job token.
			mkdir -m 0700 -- "${run_dir}/gh-read-config" || exit 1
			# Keep the runner/stall guard environment intact; only the model and
			# its child tools lose credentials. The Claude OAuth token is retained.
			credential_prefix=(env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN -u GH_HOST
				-u GH_PAT -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID -u OPENROUTER_API_KEY
				-u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_RUNTIME_TOKEN
				GH_CONFIG_DIR="${run_dir}/gh-read-config")
		fi
		for name in "${accounts[@]}"; do
			token_file="${pool_dir}/tokens/${name}"
			session_args=()
			if [ -n "${session_id}" ]; then
				if compgen -G "${HOME}/.claude/projects/*/${session_id}.jsonl" >/dev/null; then
					session_args=(--resume "${session_id}")
				else
					session_args=(--session-id "${session_id}")
				fi
			fi
			transcript="${run_dir}/transcript-${name}.jsonl"
			stderr_file="${run_dir}/stderr-${name}.txt"
			cmd=("${credential_prefix[@]}" claude -p --model "${model}" --effort "${effort}"
				--system-prompt-file "${instructions}"
				--setting-sources "" --settings "${run_dir}/claude-settings.json"
				--strict-mcp-config --disable-slash-commands
				--exclude-dynamic-system-prompt-sections
				--tools "${tools}" --permission-mode "${mode}"
				--output-format stream-json --verbose "${session_args[@]}")
			CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '[:space:]' < "${token_file}")"
			export CLAUDE_CODE_OAUTH_TOKEN
			attempt_rc=0
			if [ -f "${_AI_ENGINE_DIR}/codex_stall_guard.sh" ]; then
				bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
					--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- "${cmd[@]}" < "${prompt_file}" || attempt_rc=$?
			else
				"${cmd[@]}" < "${prompt_file}" > "${transcript}" 2> "${stderr_file}" || attempt_rc=$?
			fi
			unset CLAUDE_CODE_OAUTH_TOKEN
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
