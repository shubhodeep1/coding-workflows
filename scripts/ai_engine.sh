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
#       Order: work-item labels in AI_ENGINE_LABELS (`ai:codex` beats
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
#   claude_run <role> <prompt_file> <out_file> <workdir> [session_id]
#       Runs `claude -p` in the isolated container and writes the final
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
#   AI_ENGINE_MODEL_HINT, AI_ENGINE_EFFORT_HINT   claude_run's D3 hints
#   CLAUDE_ENGINE_POOL_DIR  account pool written by the token step
#                           (default ${RUNNER_TEMP}/claude-pool): `order`
#                           lists account names, best first; `tokens/<NAME>`
#                           holds each token (0600)
#   ALLOW_WORKFLOW_EDITS    `true` lifts the .github/workflows deny rules (P5)
#   SUPPORT_INSTRUCTIONS_FILE   unattended_system_instructions.md
#
# The OAuth token stays on the host, in the pool directory. Only the host
# relay reads it; the CLI receives a placeholder bearer inside the container.

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
	# Do not run Claude on the runner if the trusted isolation helper is absent.
	if [ ! -f "${_AI_ENGINE_DIR}/codex_isolated_exec.sh" ] || [ -L "${_AI_ENGINE_DIR}/codex_isolated_exec.sh" ]; then
		ai_engine_fallback "${role}" support_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi

	local resolved model effort profile hide_claude_md instructions guard_hook cli_version probe_model claude_home
	if ! resolved="$(_ai_engine_py resolve --role "${role}" --model-hint "${AI_ENGINE_MODEL_HINT:-}" --effort-hint "${AI_ENGINE_EFFORT_HINT:-}")"; then
		ai_engine_fallback "${role}" resolve_failed
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	model="$(_ai_engine_json_field "${resolved}" model)"
	effort="$(_ai_engine_json_field "${resolved}" effort)"
	profile="$(_ai_engine_json_field "${resolved}" profile)"
	hide_claude_md="$(_ai_engine_py config --key hide_claude_md 2>/dev/null || echo false)"
	if ! instructions="$(_ai_engine_instructions_file)"; then
		ai_engine_fallback "${role}" instructions_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	if ! guard_hook="$(_ai_engine_py support-file --name guard-hook)" || ! cli_version="$(ai_engine_cli_version)" || ! probe_model="$(_ai_engine_py config --key probe_model)"; then
		ai_engine_fallback "${role}" support_missing
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
	local -a settings_args=(settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile "${profile}" --guard-hook /support/guard.py)
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ] && settings_args+=(--allow-workflow-edits)
	if ! _ai_engine_py "${settings_args[@]}"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi
	claude_home="${RUNNER_TEMP:-/tmp}/claude-isolated-home"
	mkdir -p -- "${claude_home}" || return 1
	local tools mode
	case "${profile}" in
		read) tools="Read,Grep,Glob,Bash"; mode="dontAsk" ;;
		# An explicit list, not "default": the default set loads ~35 tools whose
		# descriptions push a no-op start-up past the 25,000-token context gate.
		# Keep in sync with PROFILE_TOOLS["write"] in claude_engine.py.
		*) tools="Read,Grep,Glob,Bash,Edit,Write"; mode="bypassPermissions" ;;
	esac

	local rc=0 attempt_rc token_file transcript stderr_file verdict outcome reason isolation_mode
	local -a session_args helper_args cmd
	isolation_mode="workspace"
	[ "${profile}" != "read" ] || isolation_mode="read-only"
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
		helper_args=(run --engine claude --mode "${isolation_mode}" --workdir "${workdir}"
			--claude-token-file "${token_file}" --claude-models "${model},${probe_model}"
			--claude-cli-version "${cli_version}" --claude-settings "${run_dir}/claude-settings.json"
			--claude-guard-hook "${guard_hook}" --claude-instructions "${instructions}" --claude-home "${claude_home}")
		[ "${hide_claude_md}" != "true" ] || helper_args+=(--hide-claude-md)
		[ -z "${CODEX_ISOLATED_ROOT:-}" ] || helper_args+=(--root "${CODEX_ISOLATED_ROOT}")
		cmd=(claude -p --model "${model}" --effort "${effort}"
			--system-prompt-file /support/instructions.md
			--setting-sources "" --settings /support/settings.json
			--strict-mcp-config --disable-slash-commands
			--exclude-dynamic-system-prompt-sections
			--tools "${tools}" --permission-mode "${mode}"
			--output-format stream-json --verbose "${session_args[@]}")
		attempt_rc=0
		if [ -f "${_AI_ENGINE_DIR}/codex_stall_guard.sh" ]; then
			bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase "${role,,}" --engine claude \
				--stdout-file "${transcript}" --stderr-file "${stderr_file}" -- \
					bash "${_AI_ENGINE_DIR}/codex_isolated_exec.sh" "${helper_args[@]}" -- "${cmd[@]}" < "${prompt_file}" || attempt_rc=$?
		else
			bash "${_AI_ENGINE_DIR}/codex_isolated_exec.sh" "${helper_args[@]}" -- "${cmd[@]}" < "${prompt_file}" > "${transcript}" 2> "${stderr_file}" || attempt_rc=$?
		fi
		if [ "${attempt_rc}" -eq 75 ]; then
			ai_engine_fallback "${role}" isolation_unavailable
			return 75
		fi
		if [ "${attempt_rc}" -eq 73 ]; then
			echo "CLAUDE_POOL run role=${role} account=${name} outcome=unavailable reason=relay_unavailable exit_code=73" >&2
			continue
		fi
		verdict="$(_ai_engine_py classify --transcript "${transcript}" --exit-code "${attempt_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
		outcome="$(_ai_engine_json_field "${verdict}" outcome)"
		reason="$(_ai_engine_json_field "${verdict}" reason)"
		echo "CLAUDE_POOL run role=${role} account=${name} outcome=${outcome} reason=${reason} exit_code=${attempt_rc}" >&2
		case "${outcome}" in
			success)
				_ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || return 1
				ln -s -- "transcript-${name}.jsonl" "${run_dir}/successful-transcript.jsonl" || return 1
				return 0
				;;
			usage_limit|auth_failed) continue ;;
			timeout) return 124 ;;
			*)
				_ai_engine_py extract --transcript "${transcript}" --out "${out_file}" >&2 || true
				return 1
				;;
		esac
	done
	rc=75
	if [ "${rc}" -eq "${_AI_ENGINE_EXIT_FALLBACK}" ]; then
		ai_engine_fallback "${role}" all_accounts_failed
	fi
	return "${rc}"
}
