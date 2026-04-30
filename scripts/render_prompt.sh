#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "Usage: $0 <prompt-file>" >&2
	exit 1
fi

PROMPT_FILE="$1"
SERENA_BLOCK_FILE="prompts/serena-efficiency-block.txt"

if [ ! -f "${PROMPT_FILE}" ]; then
	echo "Prompt file not found: ${PROMPT_FILE}" >&2
	exit 1
fi

if [ ! -s "${SERENA_BLOCK_FILE}" ]; then
	echo "Canonical Serena block file is missing or empty: ${SERENA_BLOCK_FILE}" >&2
	exit 1
fi

READ_ONLY_FILE="$(mktemp)"
READ_WRITE_FILE="$(mktemp)"
RENDERED_FILE="$(mktemp)"
trap 'rm -f "${READ_ONLY_FILE}" "${READ_WRITE_FILE}" "${RENDERED_FILE}"' EXIT

extract_section() {
	local section_name="$1"
	local out_file="$2"

	awk -v section="${section_name}" '
		$0 == "[" section "]" {
			in_section = 1
			next
		}
		in_section && /^\[[A-Z_]+\]$/ {
			exit
		}
		in_section {
			print
		}
	' "${SERENA_BLOCK_FILE}" > "${out_file}"

	if [ ! -s "${out_file}" ]; then
		echo "Missing or empty section [${section_name}] in ${SERENA_BLOCK_FILE}" >&2
		exit 1
	fi
}

extract_section "READ_ONLY" "${READ_ONLY_FILE}"
extract_section "READ_WRITE" "${READ_WRITE_FILE}"

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
	# Strip leading whitespace before matching so placeholders embedded in
	# indented heredocs (e.g. the inline template in .github/workflows/
	# implement.yml) substitute the same way as left-justified placeholders
	# in prompts/*.txt. The substitution body is emitted unindented; the
	# prompt is plain text consumed by the model so indentation drift on
	# substituted lines is immaterial.
	trimmed_line="${line#"${line%%[![:space:]]*}"}"
	case "${trimmed_line}" in
		"{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}")
			cat "${READ_ONLY_FILE}"
			;;
		"{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}")
			cat "${READ_WRITE_FILE}"
			;;
		"{{WORKFLOW_EDIT_RESTRICTION}}")
			printf '%s\n' "${WORKFLOW_EDIT_RESTRICTION_LINE}"
			;;
		*)
			printf '%s\n' "${line}"
			;;
	esac
done < "${PROMPT_FILE}" > "${RENDERED_FILE}"

if grep -qE '^[[:space:]]*\{\{SERENA_EFFICIENCY_BLOCK_[A-Z_][A-Z_]*\}\}[[:space:]]*$' "${RENDERED_FILE}"; then
	echo "Unresolved Serena placeholder token(s) in rendered output for ${PROMPT_FILE}" >&2
	exit 1
fi

if grep -qE '^[[:space:]]*\{\{WORKFLOW_EDIT_RESTRICTION\}\}[[:space:]]*$' "${RENDERED_FILE}"; then
	echo "Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ${PROMPT_FILE}" >&2
	exit 1
fi

cat "${RENDERED_FILE}"
