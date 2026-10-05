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
#   AI_ENGINE_MODEL_HINT, AI_ENGINE_EFFORT_HINT   claude_run's D3 hints
#   CLAUDE_ENGINE_POOL_DIR  account pool written by the token step
#                           (default ${RUNNER_TEMP}/claude-pool): `order`
#                           lists account names, best first; `tokens/<NAME>`
#                           holds each token (0600)
#   ALLOW_WORKFLOW_EDITS    `true` lifts the .github/workflows deny rules (P5)
#   SUPPORT_INSTRUCTIONS_FILE   unattended_system_instructions.md
#
# The OAuth token reaches the CLI only through CLAUDE_CODE_OAUTH_TOKEN in a
# subshell, never argv, a log line or a file outside the pool directory.

if [ "${_AI_ENGINE_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_AI_ENGINE_LOADED="true"

_AI_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_AI_ENGINE_EXIT_FALLBACK=75

_ai_engine_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${_AI_ENGINE_DIR}/claude_engine.py" "$@"
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
	engine="$(printf '%s' "${resolved}" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["engine"])')" || engine="codex"
	printf '%s' "${resolved}" | python3 -I -c '
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
	python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"
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
	if ! command -v claude >/dev/null 2>&1; then
		ai_engine_fallback "${role}" cli_missing
		return "${_AI_ENGINE_EXIT_FALLBACK}"
	fi

	local resolved model effort profile hide_claude_md instructions
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
			cmd=(claude -p --model "${model}" --effort "${effort}"
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
