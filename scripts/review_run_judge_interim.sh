#!/usr/bin/env bash
set -euo pipefail

SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-scripts}"
if [ -z "${SUPPORT_ROOT_DIR:-}" ]; then
	if [ "$(basename "${SUPPORT_SCRIPTS_DIR}")" = "scripts" ]; then
		SUPPORT_ROOT_DIR="$(dirname "${SUPPORT_SCRIPTS_DIR}")"
	else
		SUPPORT_ROOT_DIR="${SUPPORT_SCRIPTS_DIR}"
	fi
fi
SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${SUPPORT_ROOT_DIR}/prompts}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/review-judge-interim-${RANDOM}}"
mkdir -p "${RUNTIME_DIR}"

judge_interim_log_ok()
{
	printf 'JUDGE_INTERIM_PASS_OK round=%s head_sha=%s remaining=%s path=%s\n' \
		"$1" "$2" "$3" "$4"
}

judge_interim_log_fail()
{
	printf 'JUDGE_INTERIM_PASS_FAIL reason=%s round=%s path=%s\n' \
		"$1" "$2" "$3"
}

extract_and_validate_judge_interim_json()
{
	local src="$1"
	local dst="$2"
	local expected_round="$3"
	local expected_head_sha="$4"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${src}" "${dst}" "${expected_round}" "${expected_head_sha}" <<'PY'
import json
import re
import sys
from json import JSONDecoder, JSONDecodeError

src, dst, expected_round_raw, expected_head_sha = sys.argv[1:5]
expected_round = int(expected_round_raw)

try:
	with open(src, "r", encoding="utf-8", errors="replace") as handle:
		raw = handle.read()
except OSError:
	sys.exit(1)

if not raw.strip():
	sys.exit(1)


def load_candidates(text: str):
	candidates = []
	stripped = text.strip()
	if stripped:
		candidates.append(stripped)
	cleaned = re.sub(r"```(?:json)?\s*", "", text)
	cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()
	if cleaned and cleaned != stripped:
		candidates.append(cleaned)
	decoder = JSONDecoder()
	index = -1
	while True:
		index = text.find("{", index + 1)
		if index == -1:
			break
		try:
			candidate, _ = decoder.raw_decode(text, index)
		except JSONDecodeError:
			continue
		candidates.append(candidate)
	return candidates


def squish(value, limit=None):
	text = re.sub(r"\s+", " ", str(value)).strip()
	if limit is not None and len(text) > limit:
		text = text[: max(limit - 3, 0)].rstrip() + "..."
	return text


def validate(candidate):
	if isinstance(candidate, str):
		candidate = json.loads(candidate)
	if not isinstance(candidate, dict):
		return None
	if "action" in candidate or "status" in candidate:
		return None
	if candidate.get("round") != expected_round:
		return None
	if candidate.get("head_sha") != expected_head_sha:
		return None
	remaining = candidate.get("remaining_issues")
	if not isinstance(remaining, list):
		return None
	normalized = []
	for issue in remaining:
		if not isinstance(issue, dict):
			return None
		required = {
			"id",
			"file",
			"line_start",
			"line_end",
			"symptom",
			"evidence_quote",
			"severity",
		}
		if not required.issubset(issue.keys()):
			return None
		line_start = issue.get("line_start")
		line_end = issue.get("line_end")
		if type(line_start) is not int or type(line_end) is not int:
			return None
		if line_start < 1 or line_end < line_start:
			return None
		severity = issue.get("severity")
		if severity not in {"must-fix", "nice-to-have"}:
			return None
		issue_id = issue.get("id")
		issue_file = issue.get("file")
		symptom = issue.get("symptom")
		evidence_quote = issue.get("evidence_quote")
		if not all(isinstance(value, str) for value in (issue_id, issue_file, symptom, evidence_quote)):
			return None
		issue_id = squish(issue_id)
		issue_file = squish(issue_file)
		symptom = squish(symptom)
		evidence_quote = squish(evidence_quote, 200)
		if not issue_id or not issue_file or not symptom or not evidence_quote:
			return None
		normalized.append(
			{
				"id": issue_id,
				"file": issue_file,
				"line_start": line_start,
				"line_end": line_end,
				"symptom": symptom,
				"evidence_quote": evidence_quote,
				"severity": severity,
			}
		)
	return {
		"round": expected_round,
		"head_sha": expected_head_sha,
		"remaining_issues": normalized,
	}


validated = None
for candidate in load_candidates(raw):
	try:
		validated = validate(candidate)
	except Exception:
		validated = None
	if validated is not None:
		break

if validated is None:
	sys.exit(1)

with open(dst, "w", encoding="utf-8") as handle:
	json.dump(validated, handle, indent=2)
	handle.write("\n")

print(len(validated["remaining_issues"]))
PY
}

PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR}/mode-judge-interim.txt"
PR_NUMBER="${PR_NUMBER:-}"
CURRENT_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
if [ -z "${CURRENT_HEAD_SHA}" ]; then
	CURRENT_HEAD_SHA="${HEAD_SHA:-}"
fi

if [ -z "${PR_NUMBER}" ]; then
	judge_interim_log_fail "missing_pr_number" "0" ".ai/review_runtime/pr-unknown/round-unknown/judge_interim.json"
	exit 0
fi

if [[ ! "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
	judge_interim_log_fail "invalid_pr_number" "0" ".ai/review_runtime/pr-invalid/round-unknown/judge_interim.json"
	exit 0
fi

ROUND_NUMBER_BASE="${ROUND_NUMBER:-}"
if [[ "${ROUND_NUMBER_BASE}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="$((ROUND_NUMBER_BASE + 1))"
elif [[ "${AUTOFIX_ITERATION:-}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="${AUTOFIX_ITERATION}"
else
	CURRENT_ROUND="1"
fi

ARTIFACT_DIR=".ai/review_runtime/pr-${PR_NUMBER}/round-${CURRENT_ROUND}"
ARTIFACT_PATH="${ARTIFACT_DIR}/judge_interim.json"
mkdir -p "${ARTIFACT_DIR}"
rm -f "${ARTIFACT_PATH}"

if [ -z "${CURRENT_HEAD_SHA}" ]; then
	judge_interim_log_fail "missing_head_sha" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi

if [ ! -f "${PROMPT_TEMPLATE}" ]; then
	judge_interim_log_fail "missing_prompt" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi

JUDGE_INTERIM_REASONING="${JUDGE_INTERIM_REASONING:-low}"
case "${JUDGE_INTERIM_REASONING}" in
	xhigh|high|medium|low|none) ;;
	*)
		JUDGE_INTERIM_REASONING="low"
		;;
esac

JUDGE_INTERIM_TIMEOUT_S="${JUDGE_INTERIM_TIMEOUT_S:-120}"
if ! [[ "${JUDGE_INTERIM_TIMEOUT_S}" =~ ^[0-9]+$ ]] || [ "${JUDGE_INTERIM_TIMEOUT_S}" -lt 1 ]; then
	JUDGE_INTERIM_TIMEOUT_S="120"
fi

PROMPT_FILE="${RUNTIME_DIR}/judge_interim_prompt.txt"
RAW_OUTPUT_FILE="${RUNTIME_DIR}/judge_interim_raw.txt"
STDERR_FILE="${RUNTIME_DIR}/judge_interim_stderr.txt"
ISSUE_CONTEXT_FILE="${RUNTIME_DIR}/judge_interim_issue_context.txt"

if [ -s "${LINKED_ISSUE_CONTEXT_FILE:-}" ]; then
	cp "${LINKED_ISSUE_CONTEXT_FILE}" "${ISSUE_CONTEXT_FILE}"
elif [ -s "${PR_META_FILE:-}" ]; then
	cp "${PR_META_FILE}" "${ISSUE_CONTEXT_FILE}"
else
	printf '%s\n' 'No linked issue or PR metadata context available.' > "${ISSUE_CONTEXT_FILE}"
fi

LATEST_COMMIT_DIFF="$(git show --find-renames --stat --patch --format=medium "${CURRENT_HEAD_SHA}" 2>/dev/null || printf '%s\n' '(latest commit diff unavailable)')"

{
	if [ -f ./pre_assembled_static.txt ]; then
		cat ./pre_assembled_static.txt
		echo
	fi
	echo "=== JUDGE INTERIM TASK ==="
	echo
	if [ -x "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" ]; then
		(
			cd "${SUPPORT_ROOT_DIR}"
			bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${PROMPT_TEMPLATE}"
		)
	else
		cat "${PROMPT_TEMPLATE}"
	fi
	echo
	echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE:-50}"
	echo
	echo "=== ROUND CONTEXT ==="
	echo "round: ${CURRENT_ROUND}"
	echo "head_sha: ${CURRENT_HEAD_SHA}"
	echo
	echo "=== REQUIREMENT CONTEXT ==="
	cat "${ISSUE_CONTEXT_FILE}"
	echo
	echo "=== LATEST LOCAL COMMIT DIFF ==="
	printf '%s\n' "${LATEST_COMMIT_DIFF}"
} > "${PROMPT_FILE}"

OPENCODE_HELPERS_PATH="${OPENCODE_HELPERS_PATH:-${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh}"
OPENCODE_CONFIG_WRITER_PATH="${OPENCODE_CONFIG_WRITER_PATH:-${SUPPORT_SCRIPTS_DIR}/write_opencode_config.sh}"
if [ ! -f "${OPENCODE_HELPERS_PATH}" ] || ! source "${OPENCODE_HELPERS_PATH}" 2>/dev/null; then
	judge_interim_log_fail "missing_opencode_helpers" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi
if [ ! -r "${OPENCODE_CONFIG_WRITER_PATH}" ]; then
	opencode_emit_failure_alert review_run_judge_interim reviewer "${MODEL_EDITOR:-openai/gpt-5.6-sol}" 1 config_writer_missing || true
	judge_interim_log_fail "config_writer_missing" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi

JUDGE_INTERIM_OPENCODE_CONFIG="${RUNTIME_DIR}/judge_interim_opencode.json"
JUDGE_INTERIM_OPENCODE_WORKSPACE="$(pwd)"
if ! bash "${OPENCODE_CONFIG_WRITER_PATH}" \
	--role reviewer \
	--model "${MODEL_EDITOR:-openai/gpt-5.6-sol}" \
	--project-path "${JUDGE_INTERIM_OPENCODE_WORKSPACE}" \
	--config-path "${JUDGE_INTERIM_OPENCODE_CONFIG}" \
	--serena off; then
	opencode_emit_failure_alert review_run_judge_interim reviewer "${MODEL_EDITOR:-openai/gpt-5.6-sol}" 1 config_generation || true
	judge_interim_log_fail "config_generation" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi
if ! opencode_require_bootstrap review_run_judge_interim reviewer "${MODEL_EDITOR:-openai/gpt-5.6-sol}" \
	"${JUDGE_INTERIM_OPENCODE_CONFIG}" "${OPENCODE_VERSION:-1.18.23}" "${OPENCODE_CONFIG_WRITER_PATH}"; then
	judge_interim_log_fail "opencode_bootstrap" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
	exit 0
fi

judge_interim_opencode_cmd=(
	bash -c
	# shellcheck disable=SC2016
	'set -euo pipefail; source "$1"; shift; opencode_run_cmd "$@"'
	opencode-judge-interim
	"${OPENCODE_HELPERS_PATH}"
	reviewer
	"${MODEL_EDITOR:-openai/gpt-5.6-sol}"
	"${JUDGE_INTERIM_REASONING}"
	"${JUDGE_INTERIM_OPENCODE_CONFIG}"
	"${JUDGE_INTERIM_OPENCODE_WORKSPACE}"
)

if timeout --signal=TERM --kill-after=30s -- "${JUDGE_INTERIM_TIMEOUT_S}" \
	"${judge_interim_opencode_cmd[@]}" \
	< "${PROMPT_FILE}" > "${RAW_OUTPUT_FILE}" 2> "${STDERR_FILE}"; then
	cmd_rc=0
else
	cmd_rc=$?
fi

judge_interim_clean_output="${RAW_OUTPUT_FILE}.ansi-clean"
if opencode_strip_ansi < "${RAW_OUTPUT_FILE}" > "${judge_interim_clean_output}"; then
	mv "${judge_interim_clean_output}" "${RAW_OUTPUT_FILE}"
else
	rm -f "${judge_interim_clean_output}"
fi
judge_interim_clean_stderr="${STDERR_FILE}.ansi-clean"
if opencode_strip_ansi < "${STDERR_FILE}" > "${judge_interim_clean_stderr}"; then
	mv "${judge_interim_clean_stderr}" "${STDERR_FILE}"
else
	rm -f "${judge_interim_clean_stderr}"
fi
if [ "${cmd_rc}" -ne 0 ]; then
	opencode_emit_failure_alert review_run_judge_interim reviewer "${MODEL_EDITOR:-openai/gpt-5.6-sol}" "${cmd_rc}" invocation_failed || true
fi

remaining_count=""
if remaining_count="$(extract_and_validate_judge_interim_json "${RAW_OUTPUT_FILE}" "${ARTIFACT_PATH}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" 2>/dev/null)" \
	&& [ -s "${ARTIFACT_PATH}" ]; then
	judge_interim_log_ok "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" "${remaining_count}" "${ARTIFACT_PATH}"
	exit 0
fi

if remaining_count="$(extract_and_validate_judge_interim_json "${STDERR_FILE}" "${ARTIFACT_PATH}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" 2>/dev/null)" \
	&& [ -s "${ARTIFACT_PATH}" ]; then
	judge_interim_log_ok "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" "${remaining_count}" "${ARTIFACT_PATH}"
	exit 0
fi

rm -f "${ARTIFACT_PATH}"
failure_reason="json_parse_failed"
if [ "${cmd_rc}" -eq 124 ]; then
	failure_reason="timeout"
elif [ "${cmd_rc}" -eq 137 ]; then
	failure_reason="killed"
elif [ "${cmd_rc}" -ne 0 ]; then
	failure_reason="llm_failed"
fi

judge_interim_log_fail "${failure_reason}" "${CURRENT_ROUND}" "${ARTIFACT_PATH}"
exit 0
