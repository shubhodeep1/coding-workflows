#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
	echo "Usage: $0 <output-file> <context-file> [context-file ...]" >&2
	exit 1
fi

OUT_FILE="$1"
shift

CONTRACTS_DIR="db/contracts"
MAX_LINES_PER_CONTRACT=200

mkdir -p "$(dirname "${OUT_FILE}")"
printf '=== DB CONTRACTS (relevant) ===\n' > "${OUT_FILE}"

if [ ! -d "${CONTRACTS_DIR}" ]; then
	echo "[marker] db/contracts directory not found; continuing without snippets." >> "${OUT_FILE}"
	exit 0
fi

mapfile -t CONTRACT_FILES < <(find "${CONTRACTS_DIR}" -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.json' \) | sort)
if [ "${#CONTRACT_FILES[@]}" -eq 0 ]; then
	echo "[marker] no DB contracts found under db/contracts." >> "${OUT_FILE}"
	exit 0
fi

TMP_CONTEXT_TOKENS="$(mktemp)"
TMP_CONTEXT_UNIQUE="$(mktemp)"
TMP_FILE_TOKENS="$(mktemp)"
trap 'rm -f "${TMP_CONTEXT_TOKENS}" "${TMP_CONTEXT_UNIQUE}" "${TMP_FILE_TOKENS}"' EXIT

for context_file in "$@"; do
	if [ -f "${context_file}" ]; then
		tr '[:upper:]' '[:lower:]' < "${context_file}" | tr -cs 'a-z0-9' '\n' >> "${TMP_CONTEXT_TOKENS}"
	fi
done

awk 'length($0) >= 4 && $0 ~ /[a-z]/' "${TMP_CONTEXT_TOKENS}" | sort -u > "${TMP_CONTEXT_UNIQUE}"

MATCHED_FILES=()
for contract_file in "${CONTRACT_FILES[@]}"; do
	: > "${TMP_FILE_TOKENS}"

	printf '%s\n' "$(basename "${contract_file}" ."${contract_file##*.}")" \
		| tr '[:upper:]' '[:lower:]' \
		| tr -cs 'a-z0-9' '\n' \
		| awk 'length($0) >= 4 && $0 ~ /[a-z]/' >> "${TMP_FILE_TOKENS}"

	awk '
		BEGIN { IGNORECASE = 1 }
		/^[[:space:]]*collection[[:space:]]*:/ {
			value = $0
			sub(/^[[:space:]]*collection[[:space:]]*:[[:space:]]*/, "", value)
			sub(/[[:space:]]+#.*$/, "", value)
			gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
			print tolower(value)
			exit
		}
	' "${contract_file}" \
		| tr -cs 'a-z0-9' '\n' \
		| awk 'length($0) >= 4 && $0 ~ /[a-z]/' >> "${TMP_FILE_TOKENS}"

	if [ -s "${TMP_CONTEXT_UNIQUE}" ] && [ -s "${TMP_FILE_TOKENS}" ] && grep -qFxf "${TMP_CONTEXT_UNIQUE}" "${TMP_FILE_TOKENS}"; then
		MATCHED_FILES+=("${contract_file}")
	fi
done

if [ "${#MATCHED_FILES[@]}" -eq 0 ]; then
	AVAILABLE_INDEX="$(printf '%s\n' "${CONTRACT_FILES[@]}" | sed -E 's#^db/contracts/##' | awk 'BEGIN { first = 1 } { if (!first) printf ", "; printf "%s", $0; first = 0 } END { printf "\n" }')"
	echo "[marker] no relevant contracts matched context. Available contracts: ${AVAILABLE_INDEX}" >> "${OUT_FILE}"
	exit 0
fi

for matched_file in "${MATCHED_FILES[@]}"; do
	echo >> "${OUT_FILE}"
	echo "--- ${matched_file#${CONTRACTS_DIR}/} ---" >> "${OUT_FILE}"
	awk -v max_lines="${MAX_LINES_PER_CONTRACT}" '
		NR <= max_lines {
			print
		}
		END {
			if (NR > max_lines) {
				print "[marker] contract snippet truncated at " max_lines " lines."
			}
		}
	' "${matched_file}" >> "${OUT_FILE}"
done
