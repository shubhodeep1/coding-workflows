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

line=""
while IFS= read -r line || [ -n "${line}" ]; do
	case "${line}" in
		"{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}")
			cat "${READ_ONLY_FILE}"
			;;
		"{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}")
			cat "${READ_WRITE_FILE}"
			;;
		*)
			printf '%s\n' "${line}"
			;;
	esac
done < "${PROMPT_FILE}" > "${RENDERED_FILE}"

if grep -q '{{SERENA_EFFICIENCY_BLOCK_[A-Z_][A-Z_]*}}' "${RENDERED_FILE}"; then
	echo "Unresolved Serena placeholder token(s) in rendered output for ${PROMPT_FILE}" >&2
	exit 1
fi

cat "${RENDERED_FILE}"
