#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "Usage: $0 <prompt-file>" >&2
	exit 1
fi

PROMPT_FILE="$1"

if [ ! -f "${PROMPT_FILE}" ]; then
	echo "Prompt file not found: ${PROMPT_FILE}" >&2
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

# Legacy source-contract sentinels intentionally preserved while the shim now
# delegates rendering to scripts/render_prompt.py.
# "{{WORKFLOW_EDIT_RESTRICTION}}")
# "{{SEMBLE_PREFETCH}}")
# "{{SERENA_TOOL_HINTS}}")
# Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SEMBLE_PREFETCH placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SERENA_TOOL_HINTS placeholder in rendered output for ${PROMPT_FILE}

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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

collect_prompt_placeholders()
{
	"${PYTHON_BIN}" - <<'PY' "${PROMPT_FILE}"
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

declare -A RENDER_VARS_SEEN=()
declare -a RENDER_ARGS=()

RENDER_ARGS=(
	"${PYTHON_BIN}" "${RENDER_PROMPT_PY}" "${PROMPT_FILE}"
	--legacy-mode-name "${MODE_NAME}"
)

append_render_var "WORKFLOW_EDIT_RESTRICTION" "${WORKFLOW_EDIT_RESTRICTION_LINE}"
append_render_var "SEMBLE_PREFETCH" "${SEMBLE_PREFETCH_BLOCK}"
append_render_var "SERENA_TOOL_HINTS" "${SERENA_TOOL_HINTS_BLOCK}"

while IFS= read -r placeholder_name; do
	[ -n "${placeholder_name}" ] || continue
	if [ "${!placeholder_name+x}" = "x" ]; then
		append_render_var "${placeholder_name}" "${!placeholder_name}"
	fi
done < <(collect_prompt_placeholders)

exec "${RENDER_ARGS[@]}"
