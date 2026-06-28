#!/usr/bin/env bash
#
# check_failure_triage.sh
#
# Driven by the AI Check Failure Triage workflow
# (.github/workflows/check_failure_triage.yml). Invoked once per failing PR
# check-run. It:
#
#   1. De-duplicates against any already-open triage issue for the same
#      repo + PR + check (so a check that is already in triage does not get a
#      second issue while the first is open).
#   2. Computes the auto-fix lineage generation. A triage issue spawns an AI
#      fix PR (branch ai/issue-<N>); if a check fails on that fix PR the new
#      triage links back to its parent and increments the generation. Once the
#      generation exceeds CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH the chain is
#      stopped and escalated to a human instead of opening yet another issue.
#   3. Collects the failing check-run logs (collect_pr_check_runs_context.py),
#      runs the diagnosis model (codex / openai/gpt-5.4 by default), and opens
#      a GitHub issue describing the failure + root cause + suggested fix.
#
# The opened issue is a normal issue, so the existing clarify -> plan ->
# implement -> review pipeline (which triggers on issues: opened) picks it up
# automatically. This script never pushes code itself -- the fix flows through
# the normal, gated pipeline (the "safest possible" path).
#
# All stable log lines are prefixed CHECK_TRIAGE so workflow-log-analysis and
# operators can grep them.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY            owner/repo of the PR being triaged
#   GH_TOKEN                     GitHub token (GH_PAT) with repo+issues scope
#   RUNTIME_DIR                  scratch dir for intermediate files
#   CHECK_TRIAGE_PR_NUMBER       PR number associated with the failing check
#   CHECK_TRIAGE_CHECK_NAME      failing check-run name
#   CHECK_TRIAGE_CHECK_CONCLUSION  conclusion (failure|timed_out|...)
#   CHECK_TRIAGE_HEAD_SHA        head SHA the check ran against
#   CHECK_TRIAGE_DETAILS_URL     check-run details URL
#   CHECK_TRIAGE_CHECK_RUN_ID    check-run id
#
# Optional env (have defaults):
#   CHECK_FAILURE_TRIAGE_ENABLED             "true" to act; anything else = no-op
#   CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH   max auto-fix generations (default 3)
#   MODEL_EDITOR                             diagnosis model (default openai/gpt-5.4)
#   MODEL_VERBOSITY                          codex verbosity (default low)
#   CHECK_RUNS_WAIT_TIMEOUT_SECS             context collector wait (default 60)
#   CHECK_TRIAGE_SELF_CHECK_NAME_FRAGMENT    self-loop guard fragment
#                                            (default "Check Failure Triage")

set -euo pipefail

log()
{
	echo "CHECK_TRIAGE $*"
}

# --- Helpers (fail open if unavailable) ------------------------------------

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
type gh_api_json_to_file >/dev/null 2>&1 || gh_api_json_to_file()
{
	local _gh_api_json_outfile="$1"
	shift
	"$@" > "${_gh_api_json_outfile}"
}
type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq()
{
	local _safe_gh_jq_tmp
	if ! _safe_gh_jq_tmp=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then
		return 1
	fi
	if gh api "$@" > "${_safe_gh_jq_tmp}"; then
		cat "${_safe_gh_jq_tmp}"
		rm -f "${_safe_gh_jq_tmp}"
		return 0
	fi
	rm -f "${_safe_gh_jq_tmp}"
	return 1
}
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

# --- Config ----------------------------------------------------------------

ENABLED="${CHECK_FAILURE_TRIAGE_ENABLED:-false}"
MAX_DEPTH="${CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH:-3}"
case "${MAX_DEPTH}" in
	''|*[!0-9]*)
		MAX_DEPTH=3
		;;
esac

TRIAGE_LABEL="ai:check-triage"
ESCALATED_LABEL="ai:check-triage-escalated"
MARKER_PREFIX="check-failure-triage:"
SELF_FRAGMENT="${CHECK_TRIAGE_SELF_CHECK_NAME_FRAGMENT:-Check Failure Triage}"

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/check-triage-${GITHUB_RUN_ID:-local}}"
mkdir -p "${RUNTIME_DIR}"

PR_NUMBER="${CHECK_TRIAGE_PR_NUMBER:-}"
CHECK_NAME="${CHECK_TRIAGE_CHECK_NAME:-}"
CHECK_CONCLUSION="${CHECK_TRIAGE_CHECK_CONCLUSION:-}"
HEAD_SHA="${CHECK_TRIAGE_HEAD_SHA:-}"
CHECK_DETAILS_URL="${CHECK_TRIAGE_DETAILS_URL:-}"
CHECK_RUN_ID="${CHECK_TRIAGE_CHECK_RUN_ID:-}"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${GITHUB_RUN_ID:-0}"

# --- Gates -----------------------------------------------------------------

if [ "${ENABLED}" != "true" ]; then
	log "skip reason=disabled (set CHECK_FAILURE_TRIAGE_ENABLED=true to enable) repo=${REPO}"
	exit 0
fi

# Conclusion filter: only act on genuine failures. action_required /
# cancelled / skipped / neutral / stale / success are not actionable
# code-failure signals.
case "${CHECK_CONCLUSION}" in
	failure|timed_out)
		;;
	*)
		log "skip reason=non_actionable_conclusion conclusion=${CHECK_CONCLUSION} check=${CHECK_NAME}"
		exit 0
		;;
esac

if [ -z "${PR_NUMBER}" ] || [ "${PR_NUMBER}" = "null" ]; then
	log "skip reason=no_associated_pr check=${CHECK_NAME}"
	exit 0
fi

# Self-loop guard: never triage this workflow's own check-run, otherwise a
# triage run that itself fails would trigger triage on itself forever. This is
# a self-reference guard, NOT a check-type exclusion.
case "${CHECK_NAME}" in
	*"${SELF_FRAGMENT}"*)
		log "skip reason=self_check check=${CHECK_NAME}"
		exit 0
		;;
esac

# --- Fingerprint (in-process / duplicate-issue dedup key) ------------------

FP="$(printf '%s' "${REPO}|pr=${PR_NUMBER}|check=${CHECK_NAME}" | sha256sum | awk '{print $1}')"
FP_MARKER="<!-- ${MARKER_PREFIX}fp=${FP} -->"

# --- Resolve PR + lineage generation ---------------------------------------

PR_JSON_FILE="${RUNTIME_DIR}/pr_payload.json"
if gh_api_json_to_file "${PR_JSON_FILE}" gh api "repos/${REPO}/pulls/${PR_NUMBER}"; then
	PR_JSON="$(cat "${PR_JSON_FILE}")"
else
	log "warn pr_fetch_failed pr=${PR_NUMBER}; proceeding with minimal context"
	printf '{}' > "${PR_JSON_FILE}"
	PR_JSON='{}'
fi
if ! printf '%s' "${PR_JSON}" | jq -e . >/dev/null 2>&1; then
	log "warn pr_fetch_invalid_json pr=${PR_NUMBER}; proceeding with minimal context"
	printf '{}' > "${PR_JSON_FILE}"
	PR_JSON='{}'
fi
PR_STATE="$(printf '%s' "${PR_JSON}" | jq -r '.state // ""')"
HEAD_REF="$(printf '%s' "${PR_JSON}" | jq -r '.head.ref // ""')"
PR_TITLE="$(printf '%s' "${PR_JSON}" | jq -r '.title // ""')"
PR_URL="$(printf '%s' "${PR_JSON}" | jq -r '.html_url // ""')"
HEAD_REPO_FULL_NAME="$(printf '%s' "${PR_JSON}" | jq -r '.head.repo.full_name // ""')"
[ -n "${PR_URL}" ] || PR_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/pull/${PR_NUMBER}"
if [ -n "${PR_STATE}" ] && [ "${PR_STATE}" != "open" ]; then
	log "skip reason=pr_not_open pr=${PR_NUMBER} state=${PR_STATE}"
	exit 0
fi
if [ -n "${HEAD_REPO_FULL_NAME}" ] && [ "${HEAD_REPO_FULL_NAME}" != "${REPO}" ]; then
	log "skip reason=fork_pr pr=${PR_NUMBER} head_repo=${HEAD_REPO_FULL_NAME}"
	exit 0
fi
printf '%s' "${PR_JSON}" | jq -r '.body // ""' > "${RUNTIME_DIR}/pr_body.txt" 2>/dev/null || : > "${RUNTIME_DIR}/pr_body.txt"

# A fix PR opened by the pipeline uses branch ai/issue-<N>. If this failing PR
# is such a branch, read its source issue's triage markers to derive the
# lineage generation/root.
GEN=1
ROOT="${FP}"
PARENT_ISSUE=""
case "${HEAD_REF}" in
	ai/issue-*)
		PARENT_ISSUE="${HEAD_REF#ai/issue-}"
		;;
esac
case "${PARENT_ISSUE}" in
	''|*[!0-9]*)
		PARENT_ISSUE=""
		;;
esac

if [ -n "${PARENT_ISSUE}" ]; then
	PARENT_ISSUE_JSON_FILE="${RUNTIME_DIR}/parent_issue_${PARENT_ISSUE}.json"
	if ! gh_api_json_to_file "${PARENT_ISSUE_JSON_FILE}" gh api "repos/${REPO}/issues/${PARENT_ISSUE}"; then
		log "error parent_body_fetch_failed issue=${PARENT_ISSUE}"
		exit 1
	fi
	if ! PARENT_BODY="$(jq -r '.body // ""' "${PARENT_ISSUE_JSON_FILE}" 2>/dev/null)"; then
		log "error parent_body_parse_failed issue=${PARENT_ISSUE}"
		exit 1
	fi
	PGEN="$(printf '%s' "${PARENT_BODY}" | sed -n "s/.*${MARKER_PREFIX}gen=\([0-9]\{1,\}\).*/\1/p" | head -1)"
	PROOT="$(printf '%s' "${PARENT_BODY}" | sed -n "s/.*${MARKER_PREFIX}root=\([0-9a-f]\{64\}\).*/\1/p" | head -1)"
	if [[ "${PGEN}" =~ ^[0-9]+$ ]]; then
		GEN=$((PGEN + 1))
		[ -n "${PROOT}" ] && ROOT="${PROOT}"
		log "lineage parent_issue=${PARENT_ISSUE} parent_gen=${PGEN} gen=${GEN} root=${ROOT}"
	else
		log "error parent_generation_missing_or_malformed issue=${PARENT_ISSUE}"
		exit 1
	fi
fi

# --- Duplicate-issue dedup -------------------------------------------------

OPEN_TRIAGE="$(gh_retry gh issue list --repo "${REPO}" --state open --label "${TRIAGE_LABEL}" --limit 200 --json number,body 2>/dev/null || echo '[]')"
EXISTING="$(printf '%s' "${OPEN_TRIAGE}" | jq -r --arg fp "fp=${FP}" '[.[] | select((.body // "") | contains($fp)) | .number] | first // empty' 2>/dev/null || echo '')"
if [ -n "${EXISTING}" ]; then
	log "skip reason=duplicate_open_issue issue=${EXISTING} fp=${FP} pr=${PR_NUMBER} check=${CHECK_NAME}"
	exit 0
fi

# --- Lineage cap / escalation ----------------------------------------------

ensure_triage_labels()
{
	gh_retry gh label create "${TRIAGE_LABEL}" --repo "${REPO}" --color "d876e3" --description "Issue auto-filed from a failing PR check by check-failure triage" >/dev/null 2>&1 || true
	gh_retry gh label create "${ESCALATED_LABEL}" --repo "${REPO}" --color "b60205" --description "Check-failure auto-fix chain hit the lineage cap; needs human attention" >/dev/null 2>&1 || true
}

ensure_triage_labels

if [ "${GEN}" -gt "${MAX_DEPTH}" ]; then
	log "escalate reason=lineage_cap gen=${GEN} max=${MAX_DEPTH} root=${ROOT} pr=${PR_NUMBER} check=${CHECK_NAME}"
	# PRs are issues for the labels API, so issue edit works on the PR number.
	if ! gh_retry gh issue edit "${PR_NUMBER}" --repo "${REPO}" --add-label "${ESCALATED_LABEL}" >/dev/null 2>&1; then
		log "error escalation_label_failed pr=${PR_NUMBER} label=${ESCALATED_LABEL}"
		tg_send_msg "Check-failure auto-triage hit the lineage cap for ${REPO} PR #${PR_NUMBER}, but failed to apply label '${ESCALATED_LABEL}'."$'\n'"PR: ${PR_URL}"$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
		exit 1
	fi
	tg_send_msg "Check-failure auto-triage hit the lineage cap (generation ${GEN} > ${MAX_DEPTH}) for ${REPO} PR #${PR_NUMBER}, check '${CHECK_NAME}'."$'\n'"The auto-fix chain has been stopped; a human should look at this PR."$'\n'"PR: ${PR_URL}"$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
	exit 0
fi

# --- Collect failing check-run context (logs) ------------------------------

PR_PAYLOAD_FILE="${PR_JSON_FILE}"
PR_CHECK_RUNS_CONTEXT_FILE="${RUNTIME_DIR}/pr_check_runs_context.txt"
: > "${PR_CHECK_RUNS_CONTEXT_FILE}"
if [ -f scripts/collect_pr_check_runs_context.py ]; then
	if PR_PAYLOAD_FILE="${PR_PAYLOAD_FILE}" \
		PR_CHECK_RUNS_CONTEXT_FILE="${PR_CHECK_RUNS_CONTEXT_FILE}" \
		CHECK_RUNS_WAIT_TIMEOUT_SECS="${CHECK_RUNS_WAIT_TIMEOUT_SECS:-60}" \
		PYTHONDONTWRITEBYTECODE=1 python3 scripts/collect_pr_check_runs_context.py; then
		:
	else
		: > "${PR_CHECK_RUNS_CONTEXT_FILE}"
		log "warn context_collection_failed pr=${PR_NUMBER}"
	fi
else
	log "warn context_collector_missing"
fi

# --- Run the diagnosis model -----------------------------------------------

PROMPT_FILE="${RUNTIME_DIR}/codex_prompt.txt"
DIAG_FILE="${RUNTIME_DIR}/diagnosis.md"
DIAGNOSIS_FALLBACK_REASON="produced no output"
: > "${DIAG_FILE}"

{
	echo "=== SYSTEM INSTRUCTIONS ==="
	cat unattended_system_instructions.md 2>/dev/null || true
	echo
	if [ -f agents_canonical.md ]; then
		echo "=== REPO ARCHITECTURE (coding-workflows canonical) ==="
		cat agents_canonical.md
		echo
	fi
	if [ -f agents.md ]; then
		echo "=== REPO ARCHITECTURE (this repository) ==="
		cat agents.md
		echo
	fi
	if [ -f scripts/render_prompt.sh ]; then
		bash scripts/render_prompt.sh prompts/mode-check-failure-triage.txt 2>/dev/null || cat prompts/mode-check-failure-triage.txt
	else
		cat prompts/mode-check-failure-triage.txt 2>/dev/null || true
	fi
	echo
	echo "=== FAILURE CONTEXT ==="
	echo "Repository: ${REPO}"
	echo "Pull request: #${PR_NUMBER} -- ${PR_TITLE}"
	echo "PR URL: ${PR_URL}"
	echo "Head branch: ${HEAD_REF}"
	echo "Head SHA: ${HEAD_SHA}"
	echo "Failing check: ${CHECK_NAME}"
	echo "Conclusion: ${CHECK_CONCLUSION}"
	echo "Check details URL: ${CHECK_DETAILS_URL}"
	echo
	echo "--- PR description ---"
	cat "${RUNTIME_DIR}/pr_body.txt" 2>/dev/null || true
	echo
	echo "--- Check-run failure context (logs) ---"
	if [ -s "${PR_CHECK_RUNS_CONTEXT_FILE}" ]; then
		cat "${PR_CHECK_RUNS_CONTEXT_FILE}"
	else
		echo "(check-run log context unavailable; inspect ${CHECK_DETAILS_URL})"
	fi
} > "${PROMPT_FILE}"

if command -v codex >/dev/null 2>&1; then
	if cat "${PROMPT_FILE}" | codex --ask-for-approval never \
		-c model_verbosity="${MODEL_VERBOSITY:-low}" \
		-c include_apply_patch_tool=true \
		exec --skip-git-repo-check \
		--model "${MODEL_EDITOR:-openai/gpt-5.4}" \
		--sandbox danger-full-access \
		> "${DIAG_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2); then
		:
	else
		log "warn codex_exec_nonzero"
		DIAGNOSIS_FALLBACK_REASON="failed (codex exited non-zero)"
		: > "${DIAG_FILE}"
	fi
else
	log "warn codex_unavailable; filing raw context only"
	DIAGNOSIS_FALLBACK_REASON="could not run (codex unavailable)"
fi

# Fallback body if the model produced nothing usable.
if [ ! -s "${DIAG_FILE}" ]; then
	{
		echo "## Summary"
		echo
		echo "Automated diagnosis ${DIAGNOSIS_FALLBACK_REASON}. The raw failure context is included below for follow-up."
		echo
		echo "## Evidence"
		echo
		echo '```'
		head -200 "${PR_CHECK_RUNS_CONTEXT_FILE}" 2>/dev/null || echo "(context unavailable)"
		echo '```'
	} > "${DIAG_FILE}"
fi

# --- Compose and open the issue --------------------------------------------

TITLE="CI failure: ${CHECK_NAME} on PR #${PR_NUMBER}"
BODY_FILE="${RUNTIME_DIR}/issue_body.md"
{
	echo "${FP_MARKER}"
	echo "<!-- ${MARKER_PREFIX}gen=${GEN} -->"
	echo "<!-- ${MARKER_PREFIX}root=${ROOT} -->"
	echo "<!-- ${MARKER_PREFIX}pr=${PR_NUMBER} -->"
	echo
	echo "## Automated CI failure triage (generation ${GEN} of max ${MAX_DEPTH})"
	echo
	echo "A check failed on PR #${PR_NUMBER}. This issue was filed automatically so the AI pipeline can implement a fix through the normal clarify -> plan -> implement -> review path."
	echo
	echo "- **Repository:** \`${REPO}\`"
	echo "- **Pull request:** ${PR_URL} (\`${HEAD_REF}\`)"
	echo "- **Failing check:** \`${CHECK_NAME}\` (conclusion: \`${CHECK_CONCLUSION}\`)"
	if [ -n "${CHECK_RUN_ID}" ]; then
		echo "- **Check run id:** \`${CHECK_RUN_ID}\`"
	fi
	echo "- **Check details:** ${CHECK_DETAILS_URL}"
	echo "- **Head SHA:** \`${HEAD_SHA}\`"
	echo "- **Triage run:** ${RUN_URL}"
	echo
	echo "---"
	echo
	cat "${DIAG_FILE}"
	echo
	echo "---"
	echo
	echo "_Filed by the AI check-failure-triage workflow. Auto-fix lineage generation ${GEN} (cap ${MAX_DEPTH}); the chain escalates to a human at the cap. Re-runs for the same PR + check are de-duplicated while this issue stays open._"
} > "${BODY_FILE}"

ISSUE_URL_NEW="$(gh_retry gh issue create --repo "${REPO}" --title "${TITLE}" --body-file "${BODY_FILE}" --label "${TRIAGE_LABEL}" 2>/dev/null || echo '')"
if [ -z "${ISSUE_URL_NEW}" ]; then
	log "error issue_create_failed pr=${PR_NUMBER} check=${CHECK_NAME} fp=${FP}"
	tg_send_msg "Check-failure auto-triage FAILED to open an issue for ${REPO} PR #${PR_NUMBER}, check '${CHECK_NAME}'."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
	exit 1
fi

log "created issue=${ISSUE_URL_NEW} fp=${FP} gen=${GEN} root=${ROOT} pr=${PR_NUMBER} check=${CHECK_NAME}"
tg_send_msg "Check-failure auto-triage opened ${ISSUE_URL_NEW} for ${REPO} PR #${PR_NUMBER} (check '${CHECK_NAME}', generation ${GEN}/${MAX_DEPTH}). The pipeline will pick it up."$'\n'"PR: ${PR_URL}" "DEBUG" >/dev/null 2>&1 || true
