#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "usage: $0 <mode-prompt-file>" >&2
	exit 2
fi

mode_file="$1"
canonical_file="prompts/serena-efficiency-block.txt"

if [ ! -f "${mode_file}" ]; then
	echo "render_prompt: mode file not found: ${mode_file}" >&2
	exit 1
fi

if [ ! -f "${canonical_file}" ]; then
	echo "render_prompt: canonical Serena block not found: ${canonical_file}" >&2
	exit 1
fi

extract_section() {
	local section="$1"
	local start="[[${section}]]"
	local end="[[/${section}]]"
	awk -v start="${start}" -v end="${end}" '
		$0 == start { in_section = 1; next }
		$0 == end { in_section = 0; exit }
		in_section { print }
	' "${canonical_file}"
}

read_only_block="$(extract_section READ_ONLY)"
read_write_block="$(extract_section READ_WRITE)"

if [ -z "${read_only_block}" ] || [ -z "${read_write_block}" ]; then
	echo "render_prompt: canonical Serena sections are missing or empty" >&2
	exit 1
fi

found_read_only=false
found_read_write=false
unresolved_placeholder=false

while IFS= read -r line || [ -n "${line}" ]; do
	trimmed_line="${line#"${line%%[![:space:]]*}"}"
	trimmed_line="${trimmed_line%"${trimmed_line##*[![:space:]]}"}"
	case "${trimmed_line}" in
		'{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}')
			printf '%s\n' "${read_only_block}"
			found_read_only=true
			;;
		'{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}')
			printf '%s\n' "${read_write_block}"
			found_read_write=true
			;;
		*)
			if [[ "${line}" == *'{{SERENA_EFFICIENCY_BLOCK_'* ]]; then
				unresolved_placeholder=true
			fi
			printf '%s\n' "${line}"
			;;
	esac
done < "${mode_file}"

if [ "${unresolved_placeholder}" = true ]; then
	echo "render_prompt: unresolved Serena placeholder token in ${mode_file}" >&2
	exit 1
fi

if grep -q '{{SERENA_EFFICIENCY_BLOCK_' "${mode_file}"; then
	if [ "${found_read_only}" = false ] && [ "${found_read_write}" = false ]; then
		echo "render_prompt: no Serena placeholders were rendered in ${mode_file}" >&2
		exit 1
	fi
fi
