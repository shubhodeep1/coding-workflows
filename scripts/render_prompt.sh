#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "Usage: $0 <prompt-file>" >&2
	exit 1
fi

PROMPT_FILE_ARG="$1"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

resolve_prompt_file()
{
	local prompt_path="$1"
	local candidate=""
	local script_root=""
	local -a candidates=()

	if [ -f "${prompt_path}" ]; then
		printf '%s\n' "${prompt_path}"
		return 0
	fi

	if [[ "${prompt_path}" = /* ]]; then
		return 1
	fi

	script_root="$(dirname -- "${SCRIPT_DIR}")"
	candidates=(
		"${script_root}/${prompt_path}"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

if ! PROMPT_FILE="$(resolve_prompt_file "${PROMPT_FILE_ARG}")"; then
	echo "Prompt file not found: ${PROMPT_FILE_ARG}" >&2
	exit 1
fi

# {{WORKFLOW_EDIT_RESTRICTION}} resolves to one of two lines based on
# ${ALLOW_WORKFLOW_EDITS}. The implement-mode prompt previously hard-coded
# "Do not change CI workflows." which contradicted plans whose `files_touched`
# set legitimately includes `.github/workflows/**` when the workflow runs with
# ALLOW_WORKFLOW_EDITS=true (downstream commit/validate gates already honour
# the same env var). Default is "false" so the prohibitive line stays the
# safe fallback for any caller that forgets to export the env var.
if [ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ]; then
	WORKFLOW_EDIT_RESTRICTION_LINE="- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope."
else
	WORKFLOW_EDIT_RESTRICTION_LINE="- Do not change CI workflows."
fi

# {{SEMBLE_PREFETCH}} resolves from the optional ${SEMBLE_PREFETCH}
# environment variable. Prompt consumers set it per render invocation so the
# bounded Semble block stays in the dynamic prompt section and does not leak
# across different prompt builds in the same shell process.
SEMBLE_PREFETCH_BLOCK="${SEMBLE_PREFETCH:-}"

# {{SERENA_TOOL_HINTS}} resolves from the optional ${SERENA_TOOL_HINTS}
# environment variable. Prompt consumers set it per render invocation so the
# Serena tool-usage guidance stays prompt-local and renders to an empty block
# when Serena is unavailable.
# Editor-only Serena guidance can be injected through the shared renderer
# without touching reviewer or judge prompt assembly.
SERENA_TOOL_HINTS_BLOCK="${SERENA_TOOL_HINTS:-}"

# {{MODEL_FAMILY_OVERLAY}} resolves from the optional
# ${MODEL_FAMILY_OVERLAY} environment variable. Prompt consumers set it per
# render invocation so reviewer-family-specific guidance stays prompt-local
# and renders to an empty block when no overlay applies.
MODEL_FAMILY_OVERLAY_BLOCK="${MODEL_FAMILY_OVERLAY:-}"

# Legacy source-contract sentinels intentionally preserved while the shim now
# delegates rendering to scripts/render_prompt.py.
# "{{WORKFLOW_EDIT_RESTRICTION}}")
# "{{SEMBLE_PREFETCH}}")
# "{{SERENA_TOOL_HINTS}}")
# "{{MODEL_FAMILY_OVERLAY}}")
# Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SEMBLE_PREFETCH placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SERENA_TOOL_HINTS placeholder in rendered output for ${PROMPT_FILE}
# Unresolved MODEL_FAMILY_OVERLAY placeholder in rendered output for ${PROMPT_FILE}

PROMPT_BASENAME="$(basename -- "${PROMPT_FILE}")"
MODE_NAME="${PROMPT_BASENAME%.*}"

resolve_render_prompt_py()
{
	local candidate=""
	local -a candidates=()

	# Only trust renderer backends shipped with the workflow source itself.
	candidates+=(
		"${SCRIPT_DIR}/render_prompt.py"
		"$(pwd)/.codex-workflow-src/scripts/render_prompt.py"
		"$(pwd)/.codex-workflow-src-main/scripts/render_prompt.py"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

resolve_assemble_prompt_sh()
{
	local candidate=""
	local -a candidates=()

	candidates+=(
		"${SCRIPT_DIR}/assemble_prompt.sh"
		"$(pwd)/.codex-workflow-src/scripts/assemble_prompt.sh"
		"$(pwd)/.codex-workflow-src-main/scripts/assemble_prompt.sh"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

resolve_assembly_source_path()
{
	local prompt_path="$1"
	local prompt_dir=""
	local prompt_dir_name=""
	local template_candidate=""

	prompt_dir="$(dirname -- "${prompt_path}")"
	prompt_dir_name="$(basename -- "${prompt_dir}")"
	if [ "${prompt_dir_name}" = "_templates" ]; then
		printf '%s\n' "${prompt_path}"
		return 0
	fi
	if [ "${prompt_dir_name}" = "prompts" ]; then
		template_candidate="${prompt_dir}/_templates/$(basename -- "${prompt_path}")"
		if [ -f "${template_candidate}" ]; then
			printf '%s\n' "${template_candidate}"
			return 0
		fi
	fi
	printf '%s\n' "${prompt_path}"
}

resolve_prompt_root_dir()
{
	local prompt_path="$1"
	local prompt_dir=""
	local prompt_dir_name=""

	prompt_dir="$(dirname -- "${prompt_path}")"
	prompt_dir_name="$(basename -- "${prompt_dir}")"
	if [ "${prompt_dir_name}" = "prompts" ]; then
		printf '%s\n' "${prompt_dir}"
		return 0
	fi
	if [ "${prompt_dir_name}" = "_templates" ] && [ "$(basename -- "$(dirname -- "${prompt_dir}")")" = "prompts" ]; then
		printf '%s\n' "$(dirname -- "${prompt_dir}")"
		return 0
	fi
	printf '%s\n' "${prompt_dir}"
}

cleanup_temp_files()
{
	if [ -n "${ASSEMBLED_PROMPT_FILE:-}" ] && [ -f "${ASSEMBLED_PROMPT_FILE}" ]; then
		rm -f "${ASSEMBLED_PROMPT_FILE}"
	fi
}

collect_prompt_placeholders()
{
	local placeholder_source_file="$1"
	"${PYTHON_BIN}" - <<'PY' "${placeholder_source_file}"
import pathlib
import re
import sys

try:
	text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
except (OSError, UnicodeDecodeError) as exc:
	print(f"ERROR: collect_prompt_placeholders {type(exc).__name__}: {sys.argv[1]}: {exc}", file=sys.stderr)
	sys.exit(1)
for name in sorted(set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", text))):
	print(name)
PY
}

# Security (#9399, #9431, #9465): placeholder names come from the prompt body,
# and callers render composed bodies that embed untrusted PR-diff / comment /
# issue text. Without a guard, a literal `{{GH_PAT}}` in that text would make
# the indirect expansion below substitute the runner's secret into a prompt
# sent to a third-party model. No shipped template uses a placeholder whose
# name looks like a credential, so refusing these names leaves the literal
# `{{NAME}}` in the output (render_prompt.py leaves unknown placeholders
# untouched) and changes no legitimate render.
placeholder_env_name_is_credential_like()
{
	local upper_name="${1^^}"
	local name_word=""
	local -a name_words=()

	case "${upper_name}" in
		*TOKEN*|*SECRET*|*PASSWORD*|*PASSWD*|*PASSPHRASE*|*CREDENTIAL*|*PRIVATE*|*APIKEY*|*COOKIE*|*WEBHOOK*|*CONNECTION_STRING*|*DATABASE_URL*)
			return 0
			;;
		ACTIONS_*|INPUT_*)
			# Runner internals (ACTIONS_RUNTIME_URL, ACTIONS_ID_TOKEN_REQUEST_URL, ...)
			# and action inputs, which can carry secrets; never prompt content.
			return 0
			;;
	esac

	IFS='_' read -r -a name_words <<< "${upper_name}"
	for name_word in "${name_words[@]}"; do
		case "${name_word}" in
			KEY|KEYS|PAT|PASS|PWD|AUTH|SESSION|SIGNING|SIGNATURE|CERT|DSN|URI|CF|OTP)
				return 0
				;;
		esac
	done
	return 1
}

# Second layer: refuse a value that is, as a whole, shaped like a well-known
# credential even when the variable name looks harmless. Only whole-value
# matches count, so a legitimate body that merely quotes a token-like string
# (an issue body, a diff) still renders.
placeholder_env_value_is_credential_like()
{
	local candidate_value="$1"
	if [[ "${candidate_value}" =~ ^[[:space:]]*(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}[[:space:]]*$ ]]; then
		return 0
	fi
	if [[ "${candidate_value}" =~ ^[[:space:]]*sk-[A-Za-z0-9_-]{20,}[[:space:]]*$ ]]; then
		return 0
	fi
	if [[ "${candidate_value}" =~ ^[[:space:]]*-----BEGIN\ [A-Z\ ]*PRIVATE\ KEY----- ]]; then
		return 0
	fi
	return 1
}

append_render_var()
{
	local name="$1"
	local value="$2"
	if [ -n "${RENDER_VARS_SEEN[${name}]+x}" ]; then
		return 0
	fi
	RENDER_VARS_SEEN["${name}"]=1
	RENDER_ARGS+=(--var "${name}=${value}")
}

# Returns 0 when a placeholder name looks like it names a secret, so its value
# must never be forwarded from the process environment into a prompt
# (security finding render-prompt-env-indirect-expansion-secret-leak, refs
# #3576). Upper-cased so mixed-case variants are caught too.
render_prompt_env_name_is_secret_like()
{
	local upper_name="${1^^}"
	case "${upper_name}" in
		*TOKEN*|*KEY*|*SECRET*|*PAT*|*PASSWORD*|*PASSWD*|*CREDENTIAL*|*_CF)
			return 0
			;;
	esac
	return 1
}

if command -v python3 >/dev/null 2>&1; then
	PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
	PYTHON_BIN="python"
else
	echo "Python interpreter not found for render_prompt.py" >&2
	exit 1
fi

if ! RENDER_PROMPT_PY="$(resolve_render_prompt_py)"; then
	echo "render_prompt.py not found for ${PROMPT_FILE}" >&2
	exit 1
fi

RENDER_INPUT_FILE="${PROMPT_FILE}"
PLACEHOLDER_SOURCE_FILE="${PROMPT_FILE}"
ASSEMBLED_PROMPT_FILE=""

if [ "${PROMPT_PRELUDE_REFACTOR_ENABLED:-false}" = "true" ]; then
	ASSEMBLY_SOURCE_FILE="$(resolve_assembly_source_path "${PROMPT_FILE}")"
	if [ "${ASSEMBLY_SOURCE_FILE}" != "${PROMPT_FILE}" ] || [ "$(basename -- "$(dirname -- "${PROMPT_FILE}")")" = "_templates" ]; then
		if ! ASSEMBLE_PROMPT_SH="$(resolve_assemble_prompt_sh)"; then
			echo "assemble_prompt.sh not found for ${PROMPT_FILE}" >&2
			exit 1
		fi
		PROMPT_ROOT_DIR="$(resolve_prompt_root_dir "${ASSEMBLY_SOURCE_FILE}")"
		ASSEMBLED_PROMPT_FILE="$(mktemp "${PROMPT_ROOT_DIR}/.${PROMPT_BASENAME}.assembled.XXXXXX")"
		trap cleanup_temp_files EXIT
		"${ASSEMBLE_PROMPT_SH}" "${PROMPT_FILE}" > "${ASSEMBLED_PROMPT_FILE}"
		RENDER_INPUT_FILE="${ASSEMBLED_PROMPT_FILE}"
		PLACEHOLDER_SOURCE_FILE="${ASSEMBLED_PROMPT_FILE}"
	fi
fi

declare -A RENDER_VARS_SEEN=()
declare -a RENDER_ARGS=()

RENDER_ARGS=(
	"${PYTHON_BIN}" "${RENDER_PROMPT_PY}" "${RENDER_INPUT_FILE}"
	--legacy-mode-name "${MODE_NAME}"
)

INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=false
if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
	RENDER_ARGS+=(--input-already-assembled)
	INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=true
fi

# Opt-in: callers rendering an already-COMPOSED prompt body that embeds
# untrusted content (reviewer/editor bodies concatenate raw PR-diff + comment
# text, which can legitimately contain literal `{% include "..." %}` lines from
# a template-driven consumer repo — Jinja/Django/Nunjucks/Twig/Liquid all use
# that syntax) set RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED=1 so render_prompt.py
# does NOT re-run include-assembly over that body. Without it, a diff/context
# line like `{% include "_partials/site_footer.html" %}` is parsed as a real
# prompt-fragment include, fails to resolve under the prompt search path, and
# hard-fails the whole render with PromptAssemblyError (observed on
# tele-funtoken-msg-scoring run 29182737982). This is distinct from
# RENDER_PROMPT_SKIP_SYNTAX_VALIDATION below: the skip-syntax gate only silences
# validate_supported_template_syntax and does NOT stop the earlier
# assemble_prompt_fragments include expansion. Callers that embed untrusted
# content may need this flag, RENDER_PROMPT_SKIP_SYNTAX_VALIDATION, or both,
# depending on which failure mode they need to suppress. Reference-placeholder
# fail-open requires both flags. Placeholder substitution for the static
# scaffolding still runs. Default (unset) keeps
# include assembly for every trusted template render.
#
# Security (#9399, #9431, #9465): either untrusted-body flag below marks a body
# that embeds PR-diff / comment text, so its `{{NAME}}` tokens are
# attacker-chosen. In that mode the open-ended environment lookup in the
# placeholder loop is skipped entirely; only the explicit vars appended before
# the loop are substituted and every other `{{NAME}}` stays literal. The
# reviewer/editor callers (review_run_reviewers.sh, review_apply_fixes.sh) need
# only those explicit vars.
_render_untrusted_body_mode=false
case "${RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		_render_untrusted_body_mode=true
		if [ "${INPUT_ALREADY_ASSEMBLED_FLAG_ADDED}" != "true" ]; then
			RENDER_ARGS+=(--input-already-assembled)
			INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=true
		fi
		;;
esac

# Opt-in: callers rendering an already-assembled prompt body that embeds
# untrusted content (reviewer/editor bodies carry raw PR-diff + comment text,
# which can legitimately contain literal {{...}} / {%...%} tokens) set
# RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1 so those embedded tokens are not
# mistaken for prompt-authoring errors and the whole render is not hard-failed.
# Default (unset) keeps the strict syntax gate for every static template render.
case "${RENDER_PROMPT_SKIP_SYNTAX_VALIDATION:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		_render_untrusted_body_mode=true
		RENDER_ARGS+=(--skip-syntax-validation)
		;;
esac

append_render_var "WORKFLOW_EDIT_RESTRICTION" "${WORKFLOW_EDIT_RESTRICTION_LINE}"
append_render_var "SEMBLE_PREFETCH" "${SEMBLE_PREFETCH_BLOCK}"
append_render_var "SERENA_TOOL_HINTS" "${SERENA_TOOL_HINTS_BLOCK}"
append_render_var "MODEL_FAMILY_OVERLAY" "${MODEL_FAMILY_OVERLAY_BLOCK}"

# Capture the placeholder names in a checked command substitution instead of a
# process substitution: bash ignores a process substitution's exit status even
# under `set -euo pipefail`, so a collect_prompt_placeholders failure would
# otherwise fall through into render_prompt.py and print a second ERROR line
# (#9341). The helper's stderr is not captured, so its single ERROR line
# reaches the caller; the EXIT trap still removes any assembled temp file.
if ! _render_placeholder_names="$(collect_prompt_placeholders "${PLACEHOLDER_SOURCE_FILE}")"; then
	# The EXIT trap deletes the assembled temp file the ERROR line names, so
	# point at the original prompt (a NOTE, not a second ERROR line).
	if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
		echo "NOTE: ${PLACEHOLDER_SOURCE_FILE} was assembled from ${PROMPT_FILE} and is removed on exit; inspect the source prompt and its includes." >&2
	fi
	exit 1
fi

# Security (finding render-prompt-env-indirect-expansion-secret-leak, refs
# #3576): the loop below forwards `${!NAME}` for every NAME placeholder found in the
# body. Callers that render an untrusted body (raw PR diff, comments,
# check-run text) mark it with RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED or
# RENDER_PROMPT_SKIP_SYNTAX_VALIDATION; there a PR author controls the names,
# so a literal GH_TOKEN placeholder would pull the real secret into a prompt sent to a
# model provider. For such bodies skip env forwarding entirely: only the
# explicit append_render_var values above are passed. This is decided from
# the caller's env only, never from INPUT_ALREADY_ASSEMBLED_FLAG_ADDED, which
# the trusted PROMPT_PRELUDE_REFACTOR_ENABLED assembly path sets internally.
# Trusted templates keep env forwarding, but secret-like names are refused.
RENDER_PROMPT_UNTRUSTED_BODY=false
case "${RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		RENDER_PROMPT_UNTRUSTED_BODY=true
		;;
esac
case "${RENDER_PROMPT_SKIP_SYNTAX_VALIDATION:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		RENDER_PROMPT_UNTRUSTED_BODY=true
		;;
esac

if [ "${RENDER_PROMPT_UNTRUSTED_BODY}" != "true" ]; then
	while IFS= read -r placeholder_name; do
		[ -n "${placeholder_name}" ] || continue
		# Explicitly appended vars above win; skip before touching the environment.
		if [ -n "${RENDER_VARS_SEEN[${placeholder_name}]+x}" ]; then
			continue
		fi
		# Untrusted body: never read the environment for an attacker-chosen name
		# (#9399/#9431/#9465). No NOTE either, so diff text cannot flood stderr.
		if [ "${_render_untrusted_body_mode}" = "true" ]; then
			continue
		fi
		if [ "${!placeholder_name+x}" = "x" ] && render_prompt_env_name_is_secret_like "${placeholder_name}"; then
			# Name only, never the value.
			echo "WARNING: render_prompt.sh: refusing to forward secret-like environment placeholder ${placeholder_name}" >&2
			continue
		fi
		if placeholder_env_name_is_credential_like "${placeholder_name}"; then
			echo "NOTE: render_prompt.sh left {{${placeholder_name}}} unresolved: credential-like names are never read from the environment." >&2
			continue
		fi
		if [ "${!placeholder_name+x}" = "x" ]; then
			if placeholder_env_value_is_credential_like "${!placeholder_name}"; then
				echo "NOTE: render_prompt.sh left {{${placeholder_name}}} unresolved: its environment value looks like a credential." >&2
				continue
			fi
			append_render_var "${placeholder_name}" "${!placeholder_name}"
		fi
	done <<< "${_render_placeholder_names}"
fi

if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
	"${RENDER_ARGS[@]}"
	exit 0
fi

exec "${RENDER_ARGS[@]}"
