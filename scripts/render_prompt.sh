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

cleanup_assembled_prompt()
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

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
for name in sorted(set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", text))):
	print(name)
PY
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
		trap cleanup_assembled_prompt EXIT
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

if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
	RENDER_ARGS+=(--input-already-assembled)
fi

append_render_var "WORKFLOW_EDIT_RESTRICTION" "${WORKFLOW_EDIT_RESTRICTION_LINE}"
append_render_var "SEMBLE_PREFETCH" "${SEMBLE_PREFETCH_BLOCK}"
append_render_var "SERENA_TOOL_HINTS" "${SERENA_TOOL_HINTS_BLOCK}"
append_render_var "MODEL_FAMILY_OVERLAY" "${MODEL_FAMILY_OVERLAY_BLOCK}"

while IFS= read -r placeholder_name; do
	[ -n "${placeholder_name}" ] || continue
	if [ "${!placeholder_name+x}" = "x" ]; then
		append_render_var "${placeholder_name}" "${!placeholder_name}"
	fi
done < <(collect_prompt_placeholders "${PLACEHOLDER_SOURCE_FILE}")

if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
	"${RENDER_ARGS[@]}"
	exit 0
fi

exec "${RENDER_ARGS[@]}"
