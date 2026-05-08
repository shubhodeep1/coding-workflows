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

RENDERED_FILE="$(mktemp)"
trap 'rm -f "${RENDERED_FILE}"' EXIT

emit_semble_prefetch()
{
	if [ -z "${SEMBLE_PREFETCH_FILE:-}" ] || [ ! -r "${SEMBLE_PREFETCH_FILE}" ]; then
		return 0
	fi

	local semble_line=""
	while IFS= read -r semble_line || [ -n "${semble_line}" ]; do
		printf '%s\n' "${semble_line}"
	done < "${SEMBLE_PREFETCH_FILE}"
}

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

line=""
while IFS= read -r line || [ -n "${line}" ]; do
	trimmed_line="${line#"${line%%[![:space:]]*}"}"
	case "${trimmed_line}" in
		"{{WORKFLOW_EDIT_RESTRICTION}}")
			printf '%s\n' "${WORKFLOW_EDIT_RESTRICTION_LINE}"
			;;
		"{{SEMBLE_PREFETCH}}")
			emit_semble_prefetch
			;;
		*)
			printf '%s\n' "${line}"
			;;
	esac
done < "${PROMPT_FILE}" > "${RENDERED_FILE}"

if grep -qE '^[[:space:]]*\{\{(WORKFLOW_EDIT_RESTRICTION|SEMBLE_PREFETCH)\}\}[[:space:]]*$' "${RENDERED_FILE}"; then
	echo "Unresolved prompt placeholder in rendered output for ${PROMPT_FILE}" >&2
	exit 1
fi

cat "${RENDERED_FILE}"
