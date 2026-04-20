#!/usr/bin/env bash
set -euo pipefail

review_log()
{
	printf 'stage=consolidator %s\n' "$*" >&2
}

REVIEW_CONSOLIDATOR_MODEL="${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-5.4-mini}"
REVIEW_CONSOLIDATOR_REASONING="${REVIEW_CONSOLIDATOR_REASONING:-medium}"
REVIEW_CONSOLIDATOR_TIMEOUT_SECS="${REVIEW_CONSOLIDATOR_TIMEOUT_SECS:-300}"
REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT="${REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT:-16000}"

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR:-prompts}/review-consolidator.txt"
REVIEWER_BUNDLE_FILE="${RUNTIME_DIR}/reviewer_bundle.txt"
PR_CHANGED_FILES_FILE="${PR_CHANGED_FILES_FILE:-${RUNTIME_DIR}/pr_changed_files.txt}"
LAST_RUN_DIFF_STAT_FILE="${LAST_RUN_DIFF_STAT_FILE:-${RUNTIME_DIR}/last_run_diff_stat.txt}"
CONSOLIDATOR_PROMPT_FILE="${RUNTIME_DIR}/review_consolidator_prompt.txt"
CONSOLIDATOR_RAW_FILE="${RUNTIME_DIR}/consolidator_raw.txt"

if [ "${REVIEW_CONSOLIDATOR_ENABLED:-1}" = "0" ]; then
	: > "${CONSOLIDATOR_RAW_FILE}"
	review_log "model=${REVIEW_CONSOLIDATOR_MODEL} reasoning=${REVIEW_CONSOLIDATOR_REASONING} disabled=1 failopen=0 output_bytes=0"
	exit 0
fi

for required in "${PROMPT_TEMPLATE}" "${REVIEWER_BUNDLE_FILE}"; do
	if [ ! -f "${required}" ]; then
		: > "${CONSOLIDATOR_RAW_FILE}"
		review_log "model=${REVIEW_CONSOLIDATOR_MODEL} reasoning=${REVIEW_CONSOLIDATOR_REASONING} missing=$(basename "${required}") failopen=1 output_bytes=0"
		exit 0
	fi
done

{
	if [ -s ./pre_assembled_static.txt ]; then
		cat ./pre_assembled_static.txt
		echo
	fi
	echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE:-50}"
	echo
	echo "=== CONSOLIDATOR PROMPT ==="
	echo
	cat "${PROMPT_TEMPLATE}"
	echo
	echo "=== PR METADATA: CHANGED FILES ==="
	if [ -s "${PR_CHANGED_FILES_FILE}" ]; then
		cat "${PR_CHANGED_FILES_FILE}"
	else
		echo "(no changed files metadata available)"
	fi
	echo
	echo "=== PR METADATA: LAST RUN DIFF STAT ==="
	if [ -s "${LAST_RUN_DIFF_STAT_FILE}" ]; then
		cat "${LAST_RUN_DIFF_STAT_FILE}"
	else
		echo "(no last-run diff stat available)"
	fi
	echo
	echo "=== REVIEWER BUNDLE ==="
	cat "${REVIEWER_BUNDLE_FILE}"
} > "${CONSOLIDATOR_PROMPT_FILE}"

input_bytes="$(wc -c < "${CONSOLIDATOR_PROMPT_FILE}" | tr -d '[:space:]')"
start_epoch="$(date +%s)"
tmp_out="$(mktemp)"
tmp_err="$(mktemp)"
tmp_cap="$(mktemp)"
consolidator_codex_home=""

# Isolated CODEX_HOME overlay so consolidator reasoning effort can be set
# without mutating the shared editor CODEX_HOME. Mirrors the pattern in
# scripts/summarize_reviewer_consensus.sh (copy base CODEX_HOME, sed-patch
# model_reasoning_effort in config.toml). codex exec does not accept a
# --reasoning CLI flag, so the overlay is the supported mechanism.
codex_bin="$(command -v codex || true)"
if [ -z "${codex_bin}" ]; then
	: > "${CONSOLIDATOR_RAW_FILE}"
	review_log "model=${REVIEW_CONSOLIDATOR_MODEL} reasoning=${REVIEW_CONSOLIDATOR_REASONING} missing=codex_bin failopen=1 output_bytes=0"
	rm -f "${tmp_out}" "${tmp_err}" "${tmp_cap}"
	exit 0
fi

consolidator_codex_root="${RUNNER_TEMP:-${RUNTIME_DIR}}/codex_home_consolidator"
mkdir -p "${consolidator_codex_root}"
consolidator_codex_home="$(mktemp -d "${consolidator_codex_root}/consolidator.XXXXXX")"
trap 'rm -f "${tmp_out}" "${tmp_err}" "${tmp_cap}"; if [ -n "${consolidator_codex_home}" ]; then rm -rf "${consolidator_codex_home}"; fi; rmdir "${consolidator_codex_root}" 2>/dev/null || true' EXIT INT TERM

if [ -d "${CODEX_HOME:-}" ]; then
	cp -r "${CODEX_HOME}/." "${consolidator_codex_home}/" || review_log "cp_failed=1 source_codex_home=${CODEX_HOME}"
fi
mkdir -p "${consolidator_codex_home}/bin"

for cfg in "${consolidator_codex_home}/config.toml" "${consolidator_codex_home}/.codex/config.toml"; do
	if [ -f "${cfg}" ]; then
		if ! grep -Eq '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "${cfg}"; then
			printf 'model_reasoning_effort = "%s"\n' "${REVIEW_CONSOLIDATOR_REASONING}" >> "${cfg}"
		else
			sed -i \
				-e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*\".*\"/model_reasoning_effort = \"${REVIEW_CONSOLIDATOR_REASONING}\"/" \
				-e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*'[^']*'/model_reasoning_effort = \"${REVIEW_CONSOLIDATOR_REASONING}\"/" \
				"${cfg}" 2>/dev/null || true
		fi
	fi
done

cmd_rc=0

if ! CODEX_HOME="${consolidator_codex_home}" \
	timeout "${REVIEW_CONSOLIDATOR_TIMEOUT_SECS}" \
	"${codex_bin}" exec --model "${REVIEW_CONSOLIDATOR_MODEL}" --full-auto \
	< "${CONSOLIDATOR_PROMPT_FILE}" > "${tmp_out}" 2> "${tmp_err}"; then
	cmd_rc=$?
fi

raw_bytes="$(wc -c < "${tmp_out}" | tr -d "[:space:]")"
max_bytes="${REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT}"
if ! [[ "${max_bytes}" =~ ^[0-9]+$ ]]; then
	max_bytes=16000
fi
# Conversion: tokens -> bytes (approx 4 bytes per token for safety)
max_bytes_actual=$((max_bytes * 4))

if [ "${raw_bytes}" -gt "${max_bytes_actual}" ]; then
	# Truncate to byte cap while preserving leading complete lines.
	head -c "${max_bytes_actual}" "${tmp_out}" > "${tmp_cap}"
	last_byte_hex="$(tail -c 1 "${tmp_cap}" | od -An -tx1 | tr -d '[:space:]' || true)"
	if [ "${last_byte_hex}" = "0a" ]; then
		cp "${tmp_cap}" "${CONSOLIDATOR_RAW_FILE}"
	else
		sed '$d' "${tmp_cap}" > "${CONSOLIDATOR_RAW_FILE}"
	fi
	printf '\n... [TRUNCATED_BY_OUTPUT_CAP] ...\n' >> "${CONSOLIDATOR_RAW_FILE}"
	capped=1
else
	cp "${tmp_out}" "${CONSOLIDATOR_RAW_FILE}"
	capped=0
fi

output_bytes="$(wc -c < "${CONSOLIDATOR_RAW_FILE}" | tr -d '[:space:]')"
wall_secs="$(( $(date +%s) - start_epoch ))"
failopen=0
if [ "${cmd_rc}" -ne 0 ] || [ ! -s "${CONSOLIDATOR_RAW_FILE}" ]; then
	failopen=1
	if [ "${cmd_rc}" -ne 0 ]; then
		: > "${CONSOLIDATOR_RAW_FILE}"
		output_bytes=0
	fi
fi

review_log "model=${REVIEW_CONSOLIDATOR_MODEL} reasoning=${REVIEW_CONSOLIDATOR_REASONING} input_bytes=${input_bytes} output_bytes=${output_bytes} wall_secs=${wall_secs} exit_code=${cmd_rc} capped=${capped} failopen=${failopen}"

if [ -s "${tmp_err}" ]; then
	sed 's/^/stage=consolidator stderr=/g' "${tmp_err}" >&2 || true
fi

rm -f "${tmp_out}" "${tmp_err}" "${tmp_cap}"
exit 0
