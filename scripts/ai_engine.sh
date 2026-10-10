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
#   ai_engine_fallback <role> <reason> [class]
#       Logs `AI_ENGINE_FALLBACK role= reason=`, appends one record to
#       AI_ENGINE_FALLBACK_RECORDS_FILE and sets AI_ENGINE_FALLBACK_EXIT
#       (plan D1, item 3e). Capacity reasons (`all_gated`, `all_usage_limit`)
#       or AI_ENGINE_FALLBACK_POLICY=always give 75: the caller runs codex and
#       at most one Telegram note is sent per job. Every other reason under
#       the default policy `capacity` gives 76 and logs
#       `::error::AI_ENGINE_FALLBACK_REFUSED role= reason=`: the caller must
#       fail (never run codex). The optional class can only demote a reason
#       to `non_capacity`. Always returns 0; callers use AI_ENGINE_FALLBACK_EXIT.
#   ai_engine_fallback_policy
#       `capacity` (default) or `always`; an unknown value is `capacity`.
#   ai_engine_fallback_class <reason>
#       `capacity` for all_gated / all_usage_limit, else `non_capacity`.
#   ai_engine_no_account_reason
#       `all_gated` when the pool step gated every account
#       (CLAUDE_POOL_REASON or CLAUDE_POOL_REASON_FILE), else `no_credential`.
#   ai_engine_pool_dir
#       The account pool directory (CLAUDE_ENGINE_POOL_DIR below).
#   ai_engine_claude_home
#       The isolated CLI's session store ($RUNNER_TEMP/claude-isolated-home),
#       where a resumable session's <id>.jsonl lives.
#   ai_engine_accounts
#       The usable account names, best first: each line of `order` that is a
#       valid name and has a regular, non-empty `tokens/<NAME>` file.
#   claude_run <role> <prompt_file> <out_file> <workdir> [session_id]
#       Runs `claude -p` for the role on <workdir> and writes the final
#       result text to <out_file>, the file the codex path writes. The CLI
#       runs in the credential-free, network-isolated container of
#       codex_isolated_exec.sh (`run --engine claude`): a `read` profile sees
#       a read-only copy of <workdir>, a write profile edits a copy whose
#       changed regular files are copied back. Its session store is
#       ${RUNNER_TEMP}/claude-isolated-home, kept across calls in a job.
#       Returns 0 on success; 75 when Claude is out of capacity (every
#       account gated or at its usage limit, or any unavailability under
#       AI_ENGINE_FALLBACK_POLICY=always), after logging AI_ENGINE_FALLBACK,
#       so the caller runs the codex path (D1); 76 when Claude is unavailable
#       for any other reason (no isolation support, Docker or image, no
#       credential, no policy, a rejected token or dead relay) under the
#       default policy `capacity`: the caller fails closed and never runs
#       codex; 124 on a timeout; any other non-zero status on a crash, which
#       follows the role's existing retry rules. Returns 1 without starting
#       the CLI when the account pool directory overlaps <workdir>, the session
#       store (ai_engine_claude_home) or an AI_ENGINE_INCLUDE_PATHS entry (logged `CLAUDE_POOL ... reason=pool_overlap`).
#       AI_ENGINE_LAST_RUN_DIR names the run directory afterwards; it holds
#       transcript-<NAME>.jsonl and stderr-<NAME>.txt for each account tried.
#   claude_run_selected <role> <prompt_file> <out_file> <workdir> [--codex-stdio] -- <codex command...>
#       Resolves the role's engine (ai_engine_for_role). On codex it runs the
#       caller's codex command unchanged (as given, or with
#       `< prompt_file > out_file` under --codex-stdio) and returns its
#       status. On claude it runs claude_run read-only (AI_ENGINE_READ_ONLY)
#       with SUPPORT_ROOT_DIR / SUPPORT_INSTRUCTIONS_FILE pointing at the
#       directory this file was sourced from, wrapped in codex_heartbeat.sh
#       when present; exit 75 (Claude out of capacity) runs the codex command,
#       any other status (including 76, a refused fallback) is returned as is. Sets AI_ENGINE_LAST_SELECTED to
#       `codex`, `claude` or `claude->codex`.
#   ai_engine_stage_support <source_root> <dest_root>
#       Copies the fixed list of engine support files (this file, the
#       policy, relay, isolation helper and its support files, the stall
#       guard, heartbeat, guard hook, engine config and instructions) from a
#       verified checkout into <dest_root>, keeping the repository layout.
#       A missing required file, a symlink or a non-regular file fails the
#       whole copy and removes <dest_root>. Callers source ai_engine.sh only
#       from such a root (default ${RUNNER_TEMP}/claude-engine-support,
#       CLAUDE_ENGINE_SUPPORT_DIR), never from the checkout being worked on.
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
#   AI_ENGINE_FALLBACK_POLICY  `capacity` (default) | `always` (D1 rollback)
#   AI_ENGINE_FALLBACK_RECORDS_FILE  fallback records the job's report step
#                           reads (default
#                           ${RUNNER_TEMP}/ai-engine-fallback-records.tsv)
#   CLAUDE_POOL_REASON, CLAUDE_POOL_REASON_FILE  the pool step's outcome
#                           (file default ${RUNNER_TEMP}/claude-pool-reason)
#   AI_ENGINE_INCLUDE_PATHS newline-separated trusted runtime paths the prompt
#                           names, passed to the container as --include
#                           (default empty; the security audit's oversized-
#                           file export)
#   SUPPORT_INSTRUCTIONS_FILE   unattended_system_instructions.md
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
# Public exit code of a refused (non-capacity) fallback (plan item 3e, D1).
_AI_ENGINE_EXIT_REFUSED=76
# Set by ai_engine_fallback: 75 (run codex) or 76 (refused, fail closed).
AI_ENGINE_FALLBACK_EXIT="${_AI_ENGINE_EXIT_FALLBACK}"
readonly -a _AI_ENGINE_SANDBOX_ONLY_ROLES=(REVIEW_EDITOR REVIEW_CONSOLIDATOR RB_JUDGE CONFLICT_RESOLVER)

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

ai_engine_fallback_policy()
{
	case "${AI_ENGINE_FALLBACK_POLICY:-capacity}" in
		capacity) printf 'capacity\n' ;;
		always) printf 'always\n' ;;
		*)
			# Fail closed: an unknown value never widens the codex fallback.
			echo "::warning::AI engine: unknown AI_ENGINE_FALLBACK_POLICY; using capacity." >&2
			printf 'capacity\n'
			;;
	esac
}

ai_engine_fallback_class()
{
	case "${1:-}" in
		all_gated|all_usage_limit) printf 'capacity\n' ;;
		*) printf 'non_capacity\n' ;;
	esac
}

ai_engine_no_account_reason()
{
	local reason_file="${CLAUDE_POOL_REASON_FILE:-${RUNNER_TEMP:-/tmp}/claude-pool-reason}" token=""
	if [ "${CLAUDE_POOL_REASON:-}" = "all_gated" ]; then
		printf 'all_gated\n'
		return 0
	fi
	if [ -f "${reason_file}" ] && [ ! -L "${reason_file}" ]; then
		token="$(head -c 64 -- "${reason_file}" 2>/dev/null | tr -d '[:space:]')" || token=""
	fi
	if [ "${token}" = "all_gated" ]; then
		printf 'all_gated\n'
	else
		printf 'no_credential\n'
	fi
}

ai_engine_fallback()
{
	local role="${1:-unknown}" reason="${2:-unknown}" demote="${3:-}" class policy action record_role records_file
	reason="$(printf '%s' "${reason}" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-60)"
	echo "AI_ENGINE_FALLBACK role=${role} reason=${reason}" >&2
	class="$(ai_engine_fallback_class "${reason}")"
	# The caller's class can only demote a capacity reason (D3), never promote.
	[ "${demote}" = "non_capacity" ] && class="non_capacity"
	policy="$(ai_engine_fallback_policy)"
	if [ "${policy}" = "always" ] || [ "${class}" = "capacity" ]; then
		action="codex"
		AI_ENGINE_FALLBACK_EXIT="${_AI_ENGINE_EXIT_FALLBACK}"
	else
		action="refused"
		AI_ENGINE_FALLBACK_EXIT="${_AI_ENGINE_EXIT_REFUSED}"
	fi
	# One record per fallback for the job's report step; fail open.
	record_role="$(printf '%s' "${role}" | tr -c 'A-Z0-9_' '_' | cut -c1-40)"
	[[ "${record_role}" =~ ^[A-Z] ]] || record_role="UNKNOWN"
	records_file="${AI_ENGINE_FALLBACK_RECORDS_FILE:-${RUNNER_TEMP:-/tmp}/ai-engine-fallback-records.tsv}"
	if [ ! -L "${records_file}" ]; then
		printf 'v1\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%s)" "${record_role}" "${reason}" "${class}" "${action}" "${policy}" \
			>> "${records_file}" 2>/dev/null || true
	fi
	if [ "${action}" = "refused" ]; then
		echo "::error::AI_ENGINE_FALLBACK_REFUSED role=${role} reason=${reason}" >&2
		return 0
	fi
	# The Claude review-panel slot has no codex path: it retries its pool
	# fallback model or is skipped (skipped_pool), so say so in the alert.
	local fallback_action="running codex"
	[ "${role}" != "PANEL_REVIEWER" ] || fallback_action="the review-panel slot retries its pool fallback model or is skipped, no codex fallback"
	local marker="${RUNNER_TEMP:-/tmp}/ai-engine-fallback-notified"
	[ -e "${marker}" ] && return 0
	# Claude pool capacity (issue #6633): the "Record Claude pool capacity"
	# step already sent the one deduplicated capacity alert for this event.
	if [ "${CLAUDE_POOL_REASON:-}" = "all_gated" ] && [ "${PROVIDER_OUTAGE_CAPACITY_ALERTED:-false}" = "true" ]; then
		return 0
	fi
	: > "${marker}" 2>/dev/null || return 0
	(
		if ! type tg_send_msg >/dev/null 2>&1 && [ -f "${_AI_ENGINE_DIR}/tg_helpers.sh" ]; then
			# shellcheck disable=SC1091
			source "${_AI_ENGINE_DIR}/tg_helpers.sh"
		fi
		if type tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "AI engine: Claude unavailable for ${role} (${reason}) in ${GITHUB_REPOSITORY:-unknown} run ${GITHUB_RUN_ID:-local}; ${fallback_action}." "WARNING" >/dev/null
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

_ai_engine_path_within()
{
	# 0 when <path> is <dir> or lies beneath it; both already resolved.
	local path="${1:-}" dir="${2%/}"
	[ -n "${path}" ] || return 1
	[ "${path}" = "${dir}" ] && return 0
	case "${path}/" in "${dir}/"*) return 0 ;; esac
	return 1
}

_ai_engine_pool_isolated()
{
	# 0 when the account pool <pool_dir> neither contains nor lies inside
	# <workdir> (the model's snapshot), the session store bind-mounted as
	# ~/.claude (ai_engine_claude_home) or any AI_ENGINE_INCLUDE_PATHS entry
	# (a read-only mount). Symlinks are resolved; an unresolvable pool or
	# workdir counts as an overlap, so the caller fails closed (issue #6642).
	local pool workdir include_path include_real home_real
	pool="$(realpath -e -- "${1:-}" 2>/dev/null)" || return 1
	workdir="$(realpath -e -- "${2:-}" 2>/dev/null)" || return 1
	if _ai_engine_path_within "${pool}" "${workdir}" || _ai_engine_path_within "${workdir}" "${pool}"; then
		return 1
	fi
	# The session store may not exist yet (the helper creates it), so -m.
	home_real="$(realpath -m -- "$(ai_engine_claude_home)" 2>/dev/null)" || return 1
	if _ai_engine_path_within "${pool}" "${home_real}" || _ai_engine_path_within "${home_real}" "${pool}"; then
		return 1
	fi
	while IFS= read -r include_path; do
		[ -n "${include_path}" ] || continue
		include_real="$(realpath -m -- "${include_path}" 2>/dev/null)" || return 1
		if _ai_engine_path_within "${include_real}" "${pool}" || _ai_engine_path_within "${pool}" "${include_real}"; then
			return 1
		fi
	done <<< "${AI_ENGINE_INCLUDE_PATHS:-}"
	return 0
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
	local sandbox_only_role
	for sandbox_only_role in "${_AI_ENGINE_SANDBOX_ONLY_ROLES[@]}"; do
		if [ "${role}" = "${sandbox_only_role}" ]; then
			ai_engine_fallback "${role}" host_run_forbidden
			return "${AI_ENGINE_FALLBACK_EXIT}"
		fi
	done
	# The run changes into <workdir>; every path must survive that.
	workdir="$(cd "${workdir}" && pwd)" || return 2
	case "${prompt_file}" in /*) ;; *) prompt_file="${PWD}/${prompt_file}" ;; esac
	case "${out_file}" in /*) ;; *) out_file="${PWD}/${out_file}" ;; esac
	if [ -n "${session_id}" ] && ! [[ "${session_id}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
		echo "::error::claude_run: session_id must be a lower-case UUID" >&2
		return 2
	fi
	# The CLI runs in the credential-free container of codex_isolated_exec.sh
	# (CLAUDE.md answer Q16 A), never on the runner: the job's GH_TOKEN,
	# OpenRouter key and checkout never reach it.
	local isolated_exec="${_AI_ENGINE_DIR}/codex_isolated_exec.sh"
	if [ ! -f "${isolated_exec}" ] || [ -L "${isolated_exec}" ]; then
		ai_engine_fallback "${role}" support_missing
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi

	local resolved model effort profile hide_claude_md instructions guard_hook cli_version probe_model
	if ! resolved="$(_ai_engine_py resolve --role "${role}" --model-hint "${AI_ENGINE_MODEL_HINT:-}" --effort-hint "${AI_ENGINE_EFFORT_HINT:-}")"; then
		ai_engine_fallback "${role}" resolve_failed
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi
	model="$(_ai_engine_json_field "${resolved}" model)"
	effort="$(_ai_engine_json_field "${resolved}" effort)"
	profile="$(_ai_engine_json_field "${resolved}" profile)"
	# Defense in depth: AI_ENGINE_READ_ONLY=true narrows even if resolve
	# changes; an inherited value only removes tools, never grants them.
	if [ "${AI_ENGINE_READ_ONLY:-false}" = "true" ]; then
		profile="read"
	fi
	hide_claude_md="$(_ai_engine_py config --key hide_claude_md 2>/dev/null || echo false)"
	if ! instructions="$(_ai_engine_instructions_file)"; then
		ai_engine_fallback "${role}" instructions_missing
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi
	if ! guard_hook="$(_ai_engine_py support-file --name guard-hook)" \
		|| ! cli_version="$(ai_engine_cli_version)" \
		|| ! probe_model="$(_ai_engine_py config --key probe_model)"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi
	# The helper accepts only an absolute, regular file (it copies it into
	# the container as /support/instructions.md).
	if ! instructions="$(realpath -e -- "${instructions}")" || [ ! -f "${instructions}" ]; then
		ai_engine_fallback "${role}" instructions_missing
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi

	local pool_dir
	pool_dir="$(ai_engine_pool_dir)"
	local -a accounts=()
	local name
	while IFS= read -r name; do
		accounts+=("${name}")
	done < <(ai_engine_accounts)
	if [ "${#accounts[@]}" -eq 0 ]; then
		ai_engine_fallback "${role}" "$(ai_engine_no_account_reason)"
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi
	# The pool's token files must never reach the model's copy of <workdir>,
	# the mounted session store or an --include mount. CLAUDE_ENGINE_POOL_DIR and RUNNER_TEMP are
	# configurable, so the resolved paths are checked before any snapshot.
	# This fails closed with status 1, not the codex-fallback status 75: a
	# misconfigured pool is not ordinary Claude unavailability.
	if ! _ai_engine_pool_isolated "${pool_dir}" "${workdir}"; then
		echo "::error::claude_run: the Claude account pool overlaps the model workdir, the Claude session store or an include path; refusing to run ${role}." >&2
		echo "CLAUDE_POOL run role=${role} account=none outcome=refused reason=pool_overlap exit_code=1" >&2
		return 1
	fi

	local run_dir
	run_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/claude-run-XXXXXXXX")" || return 1
	# The caller may read the transcripts (transcript-<NAME>.jsonl) after the run.
	# shellcheck disable=SC2034  # read by callers after claude_run returns
	AI_ENGINE_LAST_RUN_DIR="${run_dir}"
	# The workdir copy is mounted at the same absolute path, so the policy's
	# checkout paths match; the guard hook is copied to /support/guard.py.
	local -a settings_args=(settings --checkout "${workdir}" --out "${run_dir}/claude-settings.json" --profile "${profile}" --guard-hook /support/guard.py)
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ] && settings_args+=(--allow-workflow-edits)
	if ! _ai_engine_py "${settings_args[@]}"; then
		ai_engine_fallback "${role}" policy_unavailable
		return "${AI_ENGINE_FALLBACK_EXIT}"
	fi
	local tools mode isolation_mode
	case "${profile}" in
		read) tools="Read,Grep,Glob"; mode="dontAsk"; isolation_mode="read-only" ;;
		# An explicit list, not "default": the default set loads ~35 tools whose
		# descriptions push a no-op start-up past the 25,000-token context gate.
		# Keep in sync with PROFILE_TOOLS["write"] in claude_engine.py.
		*) tools="Read,Grep,Glob,Bash,Edit,Write"; mode="bypassPermissions"; isolation_mode="workspace" ;;
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
	local include_path
	while IFS= read -r include_path; do
		if [ -n "${include_path}" ]; then
			isolation_args+=(--include "${include_path}")
		fi
	done <<< "${AI_ENGINE_INCLUDE_PATHS:-}"
	# Implement prepares one workspace sandbox per job with the project's
	# dependencies preinstalled (codex_isolated_exec.sh prepare --deps); a
	# write role in that job reuses it, as the codex attempts do.
	if [ "${isolation_mode}" = "workspace" ] && [ -n "${CODEX_ISOLATED_ROOT:-}" ] && [ "${CODEX_ISOLATED_MODE:-}" = "workspace" ]; then
		isolation_args+=(--root "${CODEX_ISOLATED_ROOT}")
	fi

	local rc=0
	# Subshell exit codes 76 (isolation unavailable) and 78 (every account
	# failed, not all on usage limits) are private sentinels mapped below;
	# they are not the public exit 76 of a refused fallback.
	(
		trap 'exit 130' INT
		trap 'exit 143' TERM
		all_usage_limit=true
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
					all_usage_limit=false
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
				usage_limit)
					continue
					;;
				auth_failed)
					all_usage_limit=false
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
		# Every account at its usage limit is the capacity reason (D1).
		[ "${all_usage_limit}" = "true" ] && exit "${_AI_ENGINE_EXIT_FALLBACK}"
		exit 78
	) || rc=$?
	case "${rc}" in
		76)
			ai_engine_fallback "${role}" isolation_unavailable
			return "${AI_ENGINE_FALLBACK_EXIT}"
			;;
		75)
			ai_engine_fallback "${role}" all_usage_limit
			return "${AI_ENGINE_FALLBACK_EXIT}"
			;;
		78)
			ai_engine_fallback "${role}" all_accounts_failed
			return "${AI_ENGINE_FALLBACK_EXIT}"
			;;
	esac
	return "${rc}"
}

# Engine support files a trusted root needs (ai_engine_stage_support). The
# isolation helper resolves its own support files from its directory, so they
# sit next to it.
readonly -a _AI_ENGINE_SUPPORT_REQUIRED=(
	scripts/ai_engine.sh
	scripts/claude_engine.py
	scripts/claude_anthropic_relay.py
	scripts/claude_settings.json.tmpl
	scripts/codex_isolated_exec.sh
	scripts/codex_isolated_workspace.py
	scripts/clarify_openrouter_broker.py
	scripts/write_codex_config.sh
	scripts/codex_model_catalog.json
	scripts/codex_stall_guard.sh
	scripts/codex_heartbeat.sh
	.claude/hooks/gh_api_write_guard.py
	.github/ai/claude_engine.json
	unattended_system_instructions.md
)
readonly -a _AI_ENGINE_SUPPORT_OPTIONAL=(
	scripts/dependency_registry_proxy.py
	scripts/tg_helpers.sh
)

ai_engine_stage_support()
{
	local source_root="${1:-}" dest_root="${2:-}" rel src mode optional
	if [ -z "${source_root}" ] || [ -z "${dest_root}" ] || [ ! -d "${source_root}" ]; then
		echo "::warning::AI engine: support staging failed reason=usage file=" >&2
		return 1
	fi
	if [ -L "${dest_root}" ]; then
		echo "::warning::AI engine: support staging failed reason=symlink file=<dest_root>" >&2
		return 1
	fi
	rm -rf -- "${dest_root}"
	for optional in false true; do
		local -a list=("${_AI_ENGINE_SUPPORT_REQUIRED[@]}")
		[ "${optional}" = "true" ] && list=("${_AI_ENGINE_SUPPORT_OPTIONAL[@]}")
		for rel in "${list[@]}"; do
			src="${source_root}/${rel}"
			if [ -L "${src}" ]; then
				echo "::warning::AI engine: support staging failed reason=symlink file=${rel}" >&2
				rm -rf -- "${dest_root}"
				return 1
			fi
			if [ ! -e "${src}" ] && [ "${optional}" = "true" ]; then
				continue
			fi
			if [ ! -f "${src}" ]; then
				echo "::warning::AI engine: support staging failed reason=missing file=${rel}" >&2
				rm -rf -- "${dest_root}"
				return 1
			fi
			mode=0644
			case "${rel}" in *.sh) mode=0755 ;; esac
			if ! install -D -m "${mode}" "${src}" "${dest_root}/${rel}"; then
				echo "::warning::AI engine: support staging failed reason=copy file=${rel}" >&2
				rm -rf -- "${dest_root}"
				return 1
			fi
		done
	done
	return 0
}

claude_run_selected()
{
	local usage="claude_run_selected <role> <prompt_file> <out_file> <workdir> [--codex-stdio] -- <codex command...>"
	if [ "$#" -lt 5 ]; then
		echo "::error::claude_run_selected: usage: ${usage}" >&2
		return 2
	fi
	local role="$1" prompt_file="$2" out_file="$3" workdir="$4" codex_stdio="false"
	shift 4
	if [ "${1:-}" = "--codex-stdio" ]; then
		codex_stdio="true"
		shift
	fi
	if [ "${1:-}" != "--" ]; then
		echo "::error::claude_run_selected: usage: ${usage}" >&2
		return 2
	fi
	shift
	if [ "$#" -eq 0 ] || ! _ai_engine_valid_role "${role}" || [ ! -s "${prompt_file}" ] || [ -z "${out_file}" ] || [ ! -d "${workdir}" ]; then
		echo "::error::claude_run_selected: usage: ${usage}" >&2
		return 2
	fi
	# The trusted root this file was sourced from supplies the engine config,
	# guard hook and instructions for both the selection and the run.
	local engine_root engine rc=0
	engine_root="$(cd "${_AI_ENGINE_DIR}/.." && pwd)" || return 2
	engine="$(SUPPORT_ROOT_DIR="${engine_root}" ai_engine_for_role "${role}")" || engine="codex"
	AI_ENGINE_LAST_SELECTED="codex"
	if [ "${engine}" = "claude" ]; then
		AI_ENGINE_LAST_SELECTED="claude"
		local instructions="${engine_root}/unattended_system_instructions.md"
		[ -f "${instructions}" ] || instructions="${SUPPORT_INSTRUCTIONS_FILE:-}"
		local heartbeat="${_AI_ENGINE_DIR}/codex_heartbeat.sh"
		case "${CODEX_HEARTBEAT_ENABLED:-1}" in
			0|false|FALSE|no|off) heartbeat="" ;;
		esac
		if [ -n "${heartbeat}" ] && [ -f "${heartbeat}" ] && [ ! -L "${heartbeat}" ]; then
			AI_ENGINE_READ_ONLY=true SUPPORT_ROOT_DIR="${engine_root}" SUPPORT_INSTRUCTIONS_FILE="${instructions}" \
				bash "${heartbeat}" --phase "${role,,}" --engine claude -- \
				bash -c 'source "$0"; claude_run "$@"' "${_AI_ENGINE_DIR}/ai_engine.sh" \
				"${role}" "${prompt_file}" "${out_file}" "${workdir}" || rc=$?
		else
			AI_ENGINE_READ_ONLY=true SUPPORT_ROOT_DIR="${engine_root}" SUPPORT_INSTRUCTIONS_FILE="${instructions}" \
				claude_run "${role}" "${prompt_file}" "${out_file}" "${workdir}" || rc=$?
		fi
		if [ "${rc}" -ne "${_AI_ENGINE_EXIT_FALLBACK}" ]; then
			return "${rc}"
		fi
		# claude_run already logged AI_ENGINE_FALLBACK: run the codex command.
		AI_ENGINE_LAST_SELECTED="claude->codex"
		rc=0
	fi
	if [ "${codex_stdio}" = "true" ]; then
		"$@" < "${prompt_file}" > "${out_file}" || rc=$?
	else
		"$@" || rc=$?
	fi
	return "${rc}"
}
