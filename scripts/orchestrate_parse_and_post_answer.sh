#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

require_env() {
	local name="$1"
	if [ -z "${!name:-}" ]; then
		echo "::error::orchestrate_parse_and_post_answer.sh requires ${name}."
		exit 1
	fi
}

for required_env in GITHUB_REPOSITORY GITHUB_ENV GITHUB_ACTOR GITHUB_RUN_ID GITHUB_RUN_ATTEMPT ISSUE_NUMBER ISSUE_URL CLARIFICATION_COMMENT_ID RUNTIME_DIR CODEX_OUTPUT_FILE; do
	require_env "${required_env}"
done
mkdir -p "${RUNTIME_DIR}"

REPOSITORY="${GITHUB_REPOSITORY}"
MEMORY_HELPERS_AVAILABLE="false"

SKIP_AUTO_ANSWER="false"
LOOP_BLOCKED="false"
CLAIMED="true"
CLARIFY_HASH=""
ANSWER_HASH=""
CYCLE="1"
LOOP_REASON="none"
PREVIOUS_CLARIFY_COMMENT_ID="0"
PREVIOUS_ANSWER_COMMENT_ID="0"
USED_CLARIFY_HASH=""
MAX_CYCLES="${ORCHESTRATOR_MAX_CLARIFY_CYCLES:-3}"
if ! [[ "${MAX_CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
	echo "::warning::Invalid ORCHESTRATOR_MAX_CLARIFY_CYCLES='${MAX_CYCLES}'; defaulting to 3."
	MAX_CYCLES="3"
fi
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

if [ ! -f "${CODEX_OUTPUT_FILE}" ]; then
	echo "::error::Missing or unreadable CODEX_OUTPUT_FILE (${CODEX_OUTPUT_FILE:-})"
	exit 1
fi

# Extract the ANSWERS and RATIONALE sections from Codex output
ANSWERS_BODY="$(cat "${CODEX_OUTPUT_FILE}")"

if [ -z "${ANSWERS_BODY}" ]; then
	echo "::error::Codex output is empty; cannot post answer."
	exit 1
fi

# Detect ESCALATE decisions — treat same as loop-guard block
HAS_ESCALATE="false"
if printf '%s' "${ANSWERS_BODY}" | grep -qE '^Q[0-9]+:\s*ESCALATE'; then
	HAS_ESCALATE="true"
fi

if [ -f "${SCRIPT_DIR}/memory_helpers.sh" ]; then
	# shellcheck source=/dev/null
	source "${SCRIPT_DIR}/memory_helpers.sh"
	MEMORY_HELPERS_AVAILABLE="true"
	memory_ensure_branch

	CHECK_RESULT="$(memory_processed_command_check \
		--issue-number "${ISSUE_NUMBER}" \
		--comment-id "${CLARIFICATION_COMMENT_ID}" \
		--command "answer")"
	CHECK_EXISTS="$(printf '%s' "${CHECK_RESULT}" | jq -r '.exists // false' 2>/dev/null || echo "false")"
	if [ "${CHECK_EXISTS}" = "true" ]; then
		echo "::notice::Clarification comment ${CLARIFICATION_COMMENT_ID} was already processed; skipping duplicate auto-answer."
		echo "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}"
		SKIP_AUTO_ANSWER="true"
	else
		CLAIM_RESULT="$(memory_processed_command_claim \
			--issue-number "${ISSUE_NUMBER}" \
			--comment-id "${CLARIFICATION_COMMENT_ID}" \
			--command "answer" \
			--workflow "orchestrate_clarify_respond" \
			--actor "${GITHUB_ACTOR}" \
			--run-id "${GITHUB_RUN_ID}" \
			--run-attempt "${GITHUB_RUN_ATTEMPT}" \
			--metadata-json "$(jq -cn --arg issue_url "${ISSUE_URL}" --arg run_url "${RUN_URL}" --arg clarify_comment_id "${CLARIFICATION_COMMENT_ID}" '{issue_url: $issue_url, run_url: $run_url, clarify_comment_id: ($clarify_comment_id|tonumber)}')" || true)"
		if [ -z "${CLAIM_RESULT}" ]; then
			echo "::warning::memory_processed_command_claim failed; continuing without claim gate (fail-open)."
			CLAIM_RESULT='{"claimed": true}'
		fi

		memory_enabled="$(printf '%s' "${AI_MEMORY_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
		if [[ "${memory_enabled}" =~ ^(1|true|yes|on)$ ]]; then
			CLAIMED="$(printf '%s' "${CLAIM_RESULT}" | jq -r '.operation_result.claimed // .claimed // false' 2>/dev/null || echo "false")"
		fi
		if [ "${CLAIMED}" != "true" ]; then
			echo "::notice::Clarification comment ${CLARIFICATION_COMMENT_ID} was claimed by another run; skipping duplicate auto-answer."
			echo "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}"
			SKIP_AUTO_ANSWER="true"
		fi
	fi

	if [ "${SKIP_AUTO_ANSWER}" != "true" ]; then
		CLARIFICATION_TEXT=""
		if [ -f "${RUNTIME_DIR}/clarification_comment.txt" ]; then
			CLARIFICATION_TEXT="$(cat "${RUNTIME_DIR}/clarification_comment.txt")"
		fi

		if [ -f "${SCRIPT_DIR}/ai_memory_lib.py" ]; then
			CLARIFY_HASH="$(printf '%s' "${CLARIFICATION_TEXT}" | python3 -c 'from scripts.ai_memory_lib import compute_normalized_sha256; import sys; print(compute_normalized_sha256(sys.stdin.read()))' 2>/dev/null || true)"
			if [ -z "${CLARIFY_HASH}" ]; then
				echo "::warning::Failed to compute CLARIFY_HASH; continuing with empty hash."
			fi
		fi

		LOOP_GUARD_JSON="$(memory_clarify_loop_guard \
			--issue-number "${ISSUE_NUMBER}" \
			--clarify-hash "${CLARIFY_HASH:-${CLARIFICATION_TEXT}}" \
			--max-cycles "${MAX_CYCLES}" \
			--current-comment-id "${CLARIFICATION_COMMENT_ID}")"

		USED_CLARIFY_HASH="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.clarify_hash // empty' 2>/dev/null || echo "")"
		if [ -z "${USED_CLARIFY_HASH}" ]; then
			USED_CLARIFY_HASH="${CLARIFY_HASH}"
		fi

		LOOP_BLOCKED="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.blocked // false' 2>/dev/null || echo "false")"
		LOOP_REASON="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.reason // "none"' 2>/dev/null || echo "none")"
		CYCLE="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.cycle // 1' 2>/dev/null || echo "1")"
		LOOP_MAX_CYCLES="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.max_cycles // empty' 2>/dev/null || echo "")"
		if [[ "${LOOP_MAX_CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
			MAX_CYCLES="${LOOP_MAX_CYCLES}"
		fi
		PREVIOUS_CLARIFY_COMMENT_ID="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.previous_clarify_comment_id // 0' 2>/dev/null || echo "0")"
		PREVIOUS_ANSWER_COMMENT_ID="$(printf '%s' "${LOOP_GUARD_JSON}" | jq -r '.result.previous_answer_comment_id // 0' 2>/dev/null || echo "0")"
	fi
fi

# --- Backup loop guard: comment-count fallback ---
# The memory-based guard may fail open (returns blocked=false) when
# the memory system has errors.  As a secondary check, count actual
# auto-answer comments on the issue thread.  This uses the already-
# fetched thread history (0 extra API calls).  If the count already
# meets or exceeds MAX_CYCLES and the memory guard didn't block,
# force a block to prevent runaway clarification loops.
if [ "${LOOP_BLOCKED}" != "true" ] && [ "${SKIP_AUTO_ANSWER}" != "true" ]; then
	COMMENT_COUNT_GUARD=0
	if [ -f "${THREAD_HISTORY_FILE:-/dev/null}" ]; then
		COMMENT_COUNT_GUARD="$(grep -c '\[auto-answered-by-orchestrator\]' "${THREAD_HISTORY_FILE}" 2>/dev/null || true)"
		COMMENT_COUNT_GUARD="${COMMENT_COUNT_GUARD:-0}"
	fi
	if ! [[ "${COMMENT_COUNT_GUARD}" =~ ^[0-9]+$ ]]; then
		COMMENT_COUNT_GUARD=0
	fi
	if [ "${COMMENT_COUNT_GUARD}" -ge "${MAX_CYCLES}" ]; then
		echo "::warning::Backup loop guard: found ${COMMENT_COUNT_GUARD} prior auto-answer comments (MAX_CYCLES=${MAX_CYCLES}). Blocking to prevent runaway loop."
		LOOP_BLOCKED="true"
		LOOP_REASON="backup_comment_count_guard"
		CYCLE="$((COMMENT_COUNT_GUARD + 1))"
	fi
fi

if [ -f "${SCRIPT_DIR}/ai_memory_lib.py" ]; then
	ANSWER_HASH="$(printf '%s' "${ANSWERS_BODY}" | python3 -c 'from scripts.ai_memory_lib import compute_normalized_sha256; import sys; print(compute_normalized_sha256(sys.stdin.read()))' 2>/dev/null || true)"
	if [ -z "${ANSWER_HASH}" ]; then
		echo "::warning::Failed to compute ANSWER_HASH; continuing with empty hash."
	fi
fi

if [ "${LOOP_BLOCKED}" = "true" ] || [ "${HAS_ESCALATE}" = "true" ]; then
	if [ "${HAS_ESCALATE}" = "true" ] && [ "${LOOP_BLOCKED}" != "true" ]; then
		echo "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=escalate_requested outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} cycle=${CYCLE} max_cycles=${MAX_CYCLES}"
	else
		echo "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=loop_guard_blocked outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} loop_reason=${LOOP_REASON} cycle=${CYCLE} max_cycles=${MAX_CYCLES}"
	fi
	if [ -f "${SCRIPT_DIR}/label_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${SCRIPT_DIR}/label_helpers.sh"
		ensure_label_exists "ai:blocked" "${REPOSITORY}"
	fi

	gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${REPOSITORY}" \
		--add-label 'ai:blocked' --remove-label 'ai:planning' --remove-label 'ai:clarification' >/dev/null 2>&1 || true

	# Build escalation comment — ESCALATE-triggered vs loop-guard-triggered
	if [ "${HAS_ESCALATE}" = "true" ] && [ "${LOOP_BLOCKED}" != "true" ]; then
		ESCALATION_SECTION="$(printf '%s' "${ANSWERS_BODY}" | sed -n '/^ESCALATION/,$ p')"
		{
			echo "Autonomous resolution not possible for issue #${ISSUE_NUMBER}."
			echo
			echo "The clarify-resolve phase determined that one or more questions require data that cannot be derived from the repository."
			echo
			echo "${ESCALATION_SECTION}"
			echo
			echo "- Clarify comment ID: ${CLARIFICATION_COMMENT_ID}"
			echo "- Cycle: ${CYCLE}/${MAX_CYCLES}"
		} > "${RUNTIME_DIR}/loop_break_comment.md"
	else
		{
			echo "Clarification loop guard escalation for issue #${ISSUE_NUMBER}."
			echo
			echo "Auto-answer has been paused to prevent repeated clarification loops."
			echo
			echo "- Current clarify comment ID: ${CLARIFICATION_COMMENT_ID}"
			echo "- Previous clarify comment ID: ${PREVIOUS_CLARIFY_COMMENT_ID}"
			echo "- Previous auto-answer comment ID: ${PREVIOUS_ANSWER_COMMENT_ID}"
			echo "- Loop reason: ${LOOP_REASON}"
			echo "- Cycle: ${CYCLE}/${MAX_CYCLES}"
		} > "${RUNTIME_DIR}/loop_break_comment.md"
	fi

	LOOP_BREAK_RESPONSE="$(gh_retry gh api "repos/${REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
		-f body="$(cat "${RUNTIME_DIR}/loop_break_comment.md")" || true)"
	LOOP_BREAK_COMMENT_ID="$(printf '%s' "${LOOP_BREAK_RESPONSE}" | jq -r '.id // 0' 2>/dev/null || echo "0")"
	if [ "${LOOP_BREAK_COMMENT_ID}" = "0" ]; then
		echo "::warning::Failed to post or parse loop-break comment for issue #${ISSUE_NUMBER}; continuing with comment ID 0."
	fi

	if [ -f "${SCRIPT_DIR}/tg_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${SCRIPT_DIR}/tg_helpers.sh"
		MSG="Orchestrator clarify loop breaker escalated issue #${ISSUE_NUMBER}: ${ISSUE_TITLE:-}"
		MSG+=$'\n'
		MSG+="Issue: ${ISSUE_URL}"
		MSG+=$'\n'
		MSG+="Run: ${RUN_URL}"
		MSG+=$'\n'
		MSG+="Reason: ${LOOP_REASON}"
		MSG+=$'\n'
		MSG+="Clarify comments: ${PREVIOUS_CLARIFY_COMMENT_ID}, ${CLARIFICATION_COMMENT_ID}"
		tg_send_tracked "${ISSUE_NUMBER}" "${MSG}" "WARNING"
	fi

	if [ "${MEMORY_HELPERS_AVAILABLE}" = "true" ] && [ "${CLAIMED}" = "true" ]; then
		memory_processed_command_complete \
			--issue-number "${ISSUE_NUMBER}" \
			--comment-id "${CLARIFICATION_COMMENT_ID}" \
			--command "answer" \
			--status "blocked_loop" \
			--metadata-json "$(jq -cn \
				--arg clarify_hash "${USED_CLARIFY_HASH}" \
				--arg answer_hash "${ANSWER_HASH}" \
				--arg clarify_comment_id "${CLARIFICATION_COMMENT_ID}" \
				--arg cycle "${CYCLE}" \
				--arg max_cycles "${MAX_CYCLES}" \
				--arg previous_clarify_comment_id "${PREVIOUS_CLARIFY_COMMENT_ID}" \
				--arg previous_answer_comment_id "${PREVIOUS_ANSWER_COMMENT_ID}" \
				--arg loop_break_comment_id "${LOOP_BREAK_COMMENT_ID}" \
				--arg loop_block_reason "${LOOP_REASON}" \
				'({
				  clarify_comment_id: ($clarify_comment_id|tonumber),
				  cycle: ($cycle|tonumber),
				  max_cycles: ($max_cycles|tonumber),
				  previous_clarify_comment_id: ($previous_clarify_comment_id|tonumber),
				  previous_answer_comment_id: ($previous_answer_comment_id|tonumber),
				  loop_break_comment_id: ($loop_break_comment_id|tonumber),
				  loop_blocked: true,
				  loop_block_reason: $loop_block_reason
				}
				+ (if ($clarify_hash | test("^[a-f0-9]{64}$")) then {clarify_hash: $clarify_hash} else {} end)
				+ (if ($answer_hash | test("^[a-f0-9]{64}$")) then {answer_hash: $answer_hash} else {} end))')" >/dev/null || echo "::warning::Failed to record blocked-loop completion in processed-command ledger (fail-open)."
	fi

	{
		echo "SKIP_AUTO_ANSWER=true"
		echo "LOOP_BLOCKED=true"
	} >> "$GITHUB_ENV"
	echo "Loop guard blocked auto-answer and escalated issue #${ISSUE_NUMBER}."
	exit 0
fi

if [ "${SKIP_AUTO_ANSWER}" = "true" ]; then
	echo "SKIP_AUTO_ANSWER=true" >> "$GITHUB_ENV"
	exit 0
fi

# Build the /answer comment
{
	echo "/answer [auto-answered-by-orchestrator]"
	echo
	echo "Auto-answered by the orchestrator clarify-respond workflow."
	echo
	echo "${ANSWERS_BODY}"
} > "${RUNTIME_DIR}/answer_comment.md"

ANSWER_RESPONSE="$(gh_retry gh api "repos/${REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
	-f body="$(cat "${RUNTIME_DIR}/answer_comment.md")")"
ANSWER_COMMENT_ID="$(printf '%s' "${ANSWER_RESPONSE}" | jq -r '.id // 0')"

if [ "${MEMORY_HELPERS_AVAILABLE}" = "true" ] && [ "${CLAIMED}" = "true" ]; then
	memory_processed_command_complete \
		--issue-number "${ISSUE_NUMBER}" \
		--comment-id "${CLARIFICATION_COMMENT_ID}" \
		--command "answer" \
		--status "answered" \
		--metadata-json "$(jq -cn \
			--arg clarify_hash "${USED_CLARIFY_HASH}" \
			--arg answer_hash "${ANSWER_HASH}" \
			--arg clarify_comment_id "${CLARIFICATION_COMMENT_ID}" \
			--arg answer_comment_id "${ANSWER_COMMENT_ID}" \
			--arg cycle "${CYCLE}" \
			'({
			  clarify_comment_id: ($clarify_comment_id|tonumber),
			  answer_comment_id: ($answer_comment_id|tonumber),
			  cycle: ($cycle|tonumber),
			  loop_blocked: false,
			  loop_block_reason: "none"
			}
			+ (if ($clarify_hash | test("^[a-f0-9]{64}$")) then {clarify_hash: $clarify_hash} else {} end)
			+ (if ($answer_hash | test("^[a-f0-9]{64}$")) then {answer_hash: $answer_hash} else {} end))')" >/dev/null || echo "::warning::Failed to record answered completion in processed-command ledger (fail-open)."
fi

{
	echo "SKIP_AUTO_ANSWER=false"
	echo "LOOP_BLOCKED=false"
} >> "$GITHUB_ENV"

echo "Posted auto-answer on issue #${ISSUE_NUMBER}"
