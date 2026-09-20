#!/usr/bin/env bash
#
# workflow_failure_heal_intake.sh
#
# Driven by the Workflow Failure Heal Intake workflow
# (.github/workflows/workflow-failure-heal-intake.yml) in coding-workflows.
# Invoked once per report: a `repository_dispatch` (event `workflow-failure-heal`)
# sent by scripts/workflow_failure_heal_report.sh from a consumer (or from this
# repo's own internal wrapper), a failed release / promotion `workflow_run`, or a
# manual `workflow_dispatch` re-run. It:
#
#   1. Validates the payload (every field is re-checked; the body, comments, and
#      logs stay untrusted data for the model) and applies the skip gates:
#      kill switch, unregistered source repo, smoke-test fixture, self run,
#      downstream release-gate failure already reported by the gate itself.
#   2. Fetches the failed jobs + a filtered tail of their logs for the linked
#      runs (bounded: WORKFLOW_HEAL_MAX_RUNS runs, WORKFLOW_HEAL_MAX_FAILED_JOBS
#      jobs per run, WORKFLOW_HEAL_MAX_RUN_LOG_BYTES per job).
#   3. Fingerprints the failure (workflow + failing step + normalised error
#      signature) and applies the dedup / lineage / budget decision against the
#      open + closed `ai:workflow-heal` issues (see workflow_failure_heal.py):
#      duplicate -> occurrence comment; lineage cap -> escalate;
#      budget cap -> alert; otherwise continue.
#   4. Checks out the coding-workflows source at the release SHA the failing run
#      used, runs the diagnosis model (codex / MODEL_EDITOR) with
#      prompts/mode-workflow-failure-heal.txt, and reads its classification.
#   5. Routes the outcome:
#        workflow-defect, inconclusive -> issue in coding-workflows
#          (label ai:workflow-heal, `Target branch: stable` so the fix is a
#          hotfix on the stable line; a failed release run targets the branch it
#          failed on)
#        consumer-app-defect           -> issue in the source repository
#        consumer-config               -> Telegram ERROR + comment, no issue
#        transient                     -> Telegram DEBUG + comment, no issue
#
# The opened issue is a normal issue, so the existing clarify -> plan ->
# implement -> review pipeline picks it up. This script never pushes code.
# All stable log lines are prefixed WORKFLOW_HEAL.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY            owner/repo of coding-workflows (the intake repo)
#   GH_TOKEN                     GitHub token (GH_PAT) with repo scope on this
#                                repo and every registered consumer
#   WORKFLOW_HEAL_PAYLOAD_FILE   raw payload JSON file
#   RUNTIME_DIR                  scratch dir for intermediate files
#
# Optional env (have defaults):
#   WORKFLOW_HEAL_ENABLED                 "false" to disable; default on
#   WORKFLOW_HEAL_MAX_LINEAGE_DEPTH       max heal generations per fingerprint (default 3)
#   WORKFLOW_HEAL_MAX_OPEN_ISSUES         max open ai:workflow-heal issues (default 10)
#   WORKFLOW_HEAL_MAX_ISSUES_PER_DAY      max ai:workflow-heal issues opened per UTC day (default 20)
#   WORKFLOW_HEAL_TARGET_BRANCH           branch heal fixes target (default stable)
#   WORKFLOW_HEAL_MAX_RUNS                failed runs inspected per report (default 3)
#   WORKFLOW_HEAL_MAX_FAILED_JOBS         failed jobs inspected per run (default 3)
#   WORKFLOW_HEAL_MAX_RUN_LOG_BYTES       filtered log bytes kept per job (default 60000)
#   WORKFLOW_HEAL_LOG_TAIL_LINES          log tail lines kept per job (default 400)
#   WORKFLOW_HEAL_CONSUMER_REGISTRY       consumer registry (default .github/ai/consumer_repos.json)
#   WORKFLOW_HEAL_PY                      path of workflow_failure_heal.py
#   WORKFLOW_HEAL_PROMPT_FILE             diagnosis prompt (default prompts/mode-workflow-failure-heal.txt)
#   WORKFLOW_HEAL_SOURCE_CHECKOUT         "false" to skip the release-SHA worktree (tests)
#   MODEL_EDITOR                          diagnosis model (default openai/gpt-5.6-sol)
#   MODEL_VERBOSITY                       codex verbosity (default low)

set -euo pipefail

log()
{
	echo "WORKFLOW_HEAL $*"
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
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }
source scripts/label_helpers.sh 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { :; }

# --- Config ----------------------------------------------------------------

ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
SELF_REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
PAYLOAD_RAW_FILE="${WORKFLOW_HEAL_PAYLOAD_FILE:?WORKFLOW_HEAL_PAYLOAD_FILE is required}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/workflow-heal-intake-${GITHUB_RUN_ID:-local}}"
mkdir -p "${RUNTIME_DIR}"
HEAL_PY="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
PROMPT_FILE_SRC="${WORKFLOW_HEAL_PROMPT_FILE:-prompts/mode-workflow-failure-heal.txt}"
REGISTRY_FILE="${WORKFLOW_HEAL_CONSUMER_REGISTRY:-.github/ai/consumer_repos.json}"
TARGET_BRANCH_DEFAULT="${WORKFLOW_HEAL_TARGET_BRANCH:-stable}"
SOURCE_CHECKOUT="${WORKFLOW_HEAL_SOURCE_CHECKOUT:-true}"
HEAL_LABEL="ai:workflow-heal"
ESCALATED_LABEL="ai:workflow-heal-escalated"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${SELF_REPO}/actions/runs/${GITHUB_RUN_ID:-0}"
export PYTHONDONTWRITEBYTECODE=1

_positive_or_default()
{
	local value="${1:-}" fallback="$2"
	case "${value}" in
		''|*[!0-9]*) printf '%s' "${fallback}" ;;
		*) printf '%s' "${value}" ;;
	esac
}
MAX_DEPTH="$(_positive_or_default "${WORKFLOW_HEAL_MAX_LINEAGE_DEPTH:-}" 3)"
MAX_OPEN="$(_positive_or_default "${WORKFLOW_HEAL_MAX_OPEN_ISSUES:-}" 10)"
MAX_PER_DAY="$(_positive_or_default "${WORKFLOW_HEAL_MAX_ISSUES_PER_DAY:-}" 20)"
MAX_RUNS="$(_positive_or_default "${WORKFLOW_HEAL_MAX_RUNS:-}" 3)"
MAX_FAILED_JOBS="$(_positive_or_default "${WORKFLOW_HEAL_MAX_FAILED_JOBS:-}" 3)"
MAX_LOG_BYTES="$(_positive_or_default "${WORKFLOW_HEAL_MAX_RUN_LOG_BYTES:-}" 60000)"
LOG_TAIL_LINES="$(_positive_or_default "${WORKFLOW_HEAL_LOG_TAIL_LINES:-}" 400)"

# --- Gates -----------------------------------------------------------------

if [ "${ENABLED,,}" = "false" ]; then
	log "skip reason=disabled (WORKFLOW_HEAL_ENABLED=false)"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "error heal_helper_missing path=${HEAL_PY}"
	exit 1
fi

PAYLOAD_FILE="${RUNTIME_DIR}/payload.json"
if ! python3 "${HEAL_PY}" validate-payload --payload-json "${PAYLOAD_RAW_FILE}" > "${PAYLOAD_FILE}" 2> "${RUNTIME_DIR}/payload_error.txt"; then
	PAYLOAD_ERROR="$(head -c 300 "${RUNTIME_DIR}/payload_error.txt" 2>/dev/null | tr '\n' ' ' || true)"
	log "skip reason=invalid_payload detail=${PAYLOAD_ERROR}"
	tg_send_msg "Workflow failure heal intake received an invalid payload and skipped it: ${PAYLOAD_ERROR}"$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	exit 0
fi

_pf()
{
	jq -r "$1" "${PAYLOAD_FILE}"
}
SOURCE_REPO="$(_pf '.source_repo')"
SOURCE_KIND="$(_pf '.source_kind')"
ISSUE_NUMBER="$(_pf '.issue_number // ""')"
ISSUE_TITLE="$(_pf '.issue_title // ""')"
ISSUE_URL="$(_pf '.issue_url // ""')"
LABEL="$(_pf '.label // ""')"
WRAPPER_SHA="$(_pf '.wrapper_sha // ""')"
HEAD_SHA="$(_pf '.head_sha // ""')"
HEAD_BRANCH="$(_pf '.head_branch // ""')"
PAYLOAD_WORKFLOW_NAME="$(_pf '.workflow_name // ""')"
SOURCE_GEN="$(_pf '.source_gen // ""')"
SOURCE_ROOT="$(_pf '.source_root // ""')"

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --registry-json "${REGISTRY_FILE}" --self-repo "${SELF_REPO}" 2>/dev/null || echo "")"
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none}"
	if [ "${SKIP_REASON}" = "unregistered_source_repo" ]; then
		tg_send_msg "Workflow failure heal intake ignored a report from unregistered repository ${SOURCE_REPO} (add it to .github/ai/consumer_repos.json to opt in)."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	fi
	exit 0
fi

SOURCE_LABEL="${SOURCE_REPO}#${ISSUE_NUMBER:-run}"
log "received source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} label=${LABEL:-none} workflow=${PAYLOAD_WORKFLOW_NAME:-none}"

# --- Collect failed jobs + logs --------------------------------------------

LOG_DIR="${RUNTIME_DIR}/logs"
mkdir -p "${LOG_DIR}"
SUMMARIES_FILE="${RUNTIME_DIR}/run_summaries.json"
printf '[]' > "${SUMMARIES_FILE}"
LOG_FILES=()
RUN_COUNT=0

while IFS=$'\t' read -r run_id run_url; do
	[ -n "${run_id}" ] || continue
	[[ "${run_id}" =~ ^[0-9]+$ ]] || continue
	RUN_COUNT=$((RUN_COUNT + 1))
	if [ "${RUN_COUNT}" -gt "${MAX_RUNS}" ]; then
		break
	fi
	JOBS_FILE="${LOG_DIR}/run-${run_id}-jobs.json"
	if ! gh_api_json_to_file "${JOBS_FILE}" gh api --method GET "repos/${SOURCE_REPO}/actions/runs/${run_id}/jobs" -F per_page=100; then
		log "warn jobs_fetch_failed source=${SOURCE_REPO} run=${run_id}"
		continue
	fi
	if ! jq -e '.jobs | type == "array"' "${JOBS_FILE}" >/dev/null 2>&1; then
		log "warn jobs_fetch_invalid source=${SOURCE_REPO} run=${run_id}"
		continue
	fi
	JOB_COUNT=0
	while IFS=$'\t' read -r job_id job_name workflow_name failing_step; do
		[ -n "${job_id}" ] || continue
		[[ "${job_id}" =~ ^[0-9]+$ ]] || continue
		JOB_COUNT=$((JOB_COUNT + 1))
		if [ "${JOB_COUNT}" -gt "${MAX_FAILED_JOBS}" ]; then
			break
		fi
		RAW_LOG="${LOG_DIR}/run-${run_id}-job-${job_id}.raw"
		FILTERED_LOG="${LOG_DIR}/run-${run_id}-job-${job_id}.txt"
		if gh_retry gh api "repos/${SOURCE_REPO}/actions/jobs/${job_id}/logs" > "${RAW_LOG}" 2>/dev/null && [ -s "${RAW_LOG}" ]; then
			python3 "${HEAL_PY}" filter-log --log-file "${RAW_LOG}" --max-lines "${LOG_TAIL_LINES}" --max-bytes "${MAX_LOG_BYTES}" > "${FILTERED_LOG}" || : > "${FILTERED_LOG}"
			rm -f "${RAW_LOG}"
		else
			log "warn job_log_fetch_failed source=${SOURCE_REPO} run=${run_id} job=${job_id}"
			printf '(job log unavailable)\n' > "${FILTERED_LOG}"
		fi
		LOG_FILES+=("${FILTERED_LOG}")
		jq --arg run_id "${run_id}" --arg url "${run_url}" --arg job_id "${job_id}" --arg job_name "${job_name}" \
			--arg workflow_name "${workflow_name}" --arg failing_step "${failing_step}" --arg log_file "${FILTERED_LOG}" \
			'. + [{run_id: $run_id, url: $url, job_id: $job_id, job_name: $job_name, workflow_name: $workflow_name, failing_step: $failing_step, log_file: $log_file}]' \
			"${SUMMARIES_FILE}" > "${SUMMARIES_FILE}.tmp" && mv "${SUMMARIES_FILE}.tmp" "${SUMMARIES_FILE}"
	done < <(jq -r '.jobs[] | select((.conclusion // "") | IN("failure","timed_out","cancelled"))
		| [(.id|tostring), (.name // ""), (.workflow_name // ""), ((.steps // []) | map(select((.conclusion // "") | IN("failure","timed_out","cancelled"))) | first | .name // "")]
		| map(gsub("[\\t\\n\\r]"; " ")) | @tsv' "${JOBS_FILE}")
done < <(jq -r '.run_refs[] | [.run_id, .url] | @tsv' "${PAYLOAD_FILE}")

SUMMARY_COUNT="$(jq 'length' "${SUMMARIES_FILE}")"
FIRST_WORKFLOW_NAME="$(jq -r 'map(select(.workflow_name != "")) | first | .workflow_name // ""' "${SUMMARIES_FILE}")"
FIRST_FAILING_STEP="$(jq -r 'map(select(.failing_step != "")) | first | .failing_step // ""' "${SUMMARIES_FILE}")"
[ -n "${FIRST_WORKFLOW_NAME}" ] || FIRST_WORKFLOW_NAME="${PAYLOAD_WORKFLOW_NAME}"
if [ -z "${FIRST_WORKFLOW_NAME}" ]; then
	FIRST_WORKFLOW_NAME="label:${LABEL:-unknown}"
fi

# A failed promote / auto-release run whose only failure is "the smoke gate
# failed" duplicates the gate run's own report; the gate run carries the logs.
if [ "${SOURCE_KIND}" = "workflow_run" ] && [ "${#LOG_FILES[@]}" -gt 0 ]; then
	case "${PAYLOAD_WORKFLOW_NAME}" in
		"Promote main to stable"|"Auto release stable")
			if grep -qE 'PROMOTE_CYCLE_FAILED reason=smoke_gate_(failed|timeout)' "${LOG_FILES[@]}" 2>/dev/null; then
				log "skip reason=downstream_gate_failure workflow=${PAYLOAD_WORKFLOW_NAME}"
				exit 0
			fi
			;;
	esac
fi

# --- Fingerprint -------------------------------------------------------------

if [ "${#LOG_FILES[@]}" -gt 0 ]; then
	SIG_ARGS=()
	for f in "${LOG_FILES[@]}"; do
		SIG_ARGS+=(--log-file "${f}")
	done
	SIGNATURE="$(python3 "${HEAL_PY}" error-signature "${SIG_ARGS[@]}" 2>/dev/null || echo "no-error-lines")"
else
	SIGNATURE="label:${LABEL:-unknown}"
fi
FP="$(python3 "${HEAL_PY}" fingerprint --workflow-name "${FIRST_WORKFLOW_NAME}" --failing-step "${FIRST_FAILING_STEP}" --signature "${SIGNATURE}")"
log "fingerprint fp=${FP} workflow=${FIRST_WORKFLOW_NAME} step=${FIRST_FAILING_STEP:-none} runs=${SUMMARY_COUNT}"

# --- Dedup / lineage / budget ----------------------------------------------

SELF_ISSUES_FILE="${RUNTIME_DIR}/heal_issues_self.json"
SOURCE_ISSUES_FILE="${RUNTIME_DIR}/heal_issues_source.json"
ISSUES_FILE="${RUNTIME_DIR}/heal_issues.json"
if ! gh_retry gh api --method GET --paginate "repos/${SELF_REPO}/issues" -F state=all -F labels="${HEAL_LABEL}" -F per_page=100 \
	--jq '.[] | {number, state, body: (.body // ""), created_at, html_url, pull_request: (.pull_request != null)}' 2>/dev/null \
	| jq -s --arg repository "${SELF_REPO}" 'map(. + {repository: $repository})' > "${SELF_ISSUES_FILE}" 2>/dev/null; then
	log "error heal_issue_list_failed"
	tg_send_msg "Workflow failure heal intake could not list ${HEAL_LABEL} issues; report from ${SOURCE_LABEL} not processed."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
	exit 1
fi
# The upstream list cannot see consumer-owned heal issues. Fetch the source
# repository once so deduplication, lineage, and volume budgets cover both
# repositories that can own the issue produced by this intake.
if [ "${SOURCE_REPO}" != "${SELF_REPO}" ]; then
	if ! gh_retry gh api --method GET --paginate "repos/${SOURCE_REPO}/issues" -F state=all -F labels="${HEAL_LABEL}" -F per_page=100 \
		--jq '.[] | {number, state, body: (.body // ""), created_at, html_url, pull_request: (.pull_request != null)}' 2>/dev/null \
		| jq -s --arg repository "${SOURCE_REPO}" 'map(. + {repository: $repository})' > "${SOURCE_ISSUES_FILE}" 2>/dev/null; then
		log "error heal_issue_list_failed repo=${SOURCE_REPO}"
		tg_send_msg "Workflow failure heal intake could not list ${HEAL_LABEL} issues in ${SOURCE_REPO}; report from ${SOURCE_LABEL} not processed."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
		exit 1
	fi
else
	printf '[]' > "${SOURCE_ISSUES_FILE}"
fi
jq -s 'add // []' "${SELF_ISSUES_FILE}" "${SOURCE_ISSUES_FILE}" > "${ISSUES_FILE}"
jq -e 'type == "array"' "${ISSUES_FILE}" >/dev/null 2>&1 || printf '[]' > "${ISSUES_FILE}"

BUDGET_ARGS=(--issues-json "${ISSUES_FILE}" --fingerprint "${FP}" --max-depth "${MAX_DEPTH}" --max-open "${MAX_OPEN}" --max-per-day "${MAX_PER_DAY}")
if [[ "${SOURCE_GEN}" =~ ^[0-9]+$ ]]; then
	BUDGET_ARGS+=(--source-gen "${SOURCE_GEN}" --source-root "${SOURCE_ROOT}")
fi
DECISION_FILE="${RUNTIME_DIR}/decision.json"
python3 "${HEAL_PY}" budget "${BUDGET_ARGS[@]}" > "${DECISION_FILE}"
ACTION="$(jq -r '.action' "${DECISION_FILE}")"
GEN="$(jq -r '.gen // 1' "${DECISION_FILE}")"
ROOT="$(jq -r '.root // ""' "${DECISION_FILE}")"
[ -n "${ROOT}" ] || ROOT="${FP}"

ensure_label_exists "${HEAL_LABEL}" "${SELF_REPO}" || true
ensure_label_exists "${ESCALATED_LABEL}" "${SELF_REPO}" || true

case "${ACTION}" in
	duplicate)
		EXISTING="$(jq -r '.existing_issue' "${DECISION_FILE}")"
		EXISTING_URL="$(jq -r '.existing_url // ""' "${DECISION_FILE}")"
		EXISTING_REPO="$(jq -r '.existing_repo // ""' "${DECISION_FILE}")"
		[ -n "${EXISTING_REPO}" ] || EXISTING_REPO="${SELF_REPO}"
		OCCURRENCE_FILE="${RUNTIME_DIR}/occurrence.md"
		python3 "${HEAL_PY}" compose-occurrence --payload-json "${PAYLOAD_FILE}" --intake-run-url "${RUN_URL}" > "${OCCURRENCE_FILE}"
		if gh_retry gh api "repos/${EXISTING_REPO}/issues/${EXISTING}/comments" -F body=@"${OCCURRENCE_FILE}" >/dev/null 2>&1; then
			log "duplicate existing_issue=${EXISTING} existing_repo=${EXISTING_REPO} fp=${FP} source=${SOURCE_LABEL}"
		else
			log "warn duplicate_comment_failed existing_issue=${EXISTING} existing_repo=${EXISTING_REPO} fp=${FP}"
		fi
		tg_send_msg "Workflow failure heal: ${SOURCE_LABEL} matches open heal issue ${EXISTING_URL:-#${EXISTING}} (recorded as another occurrence)." "DEBUG" >/dev/null 2>&1 || true
		exit 0
		;;
	escalate)
		PRIOR_ISSUE="$(jq -r '.prior_issue // ""' "${DECISION_FILE}")"
		PRIOR_REPO="$(jq -r '.prior_repo // ""' "${DECISION_FILE}")"
		[ -n "${PRIOR_REPO}" ] || PRIOR_REPO="${SELF_REPO}"
		if [[ "${PRIOR_ISSUE}" =~ ^[0-9]+$ ]]; then
			ensure_label_exists "${ESCALATED_LABEL}" "${PRIOR_REPO}" || true
			gh_retry gh issue edit "${PRIOR_ISSUE}" --repo "${PRIOR_REPO}" --add-label "${ESCALATED_LABEL}" >/dev/null 2>&1 || log "warn escalation_label_failed issue=${PRIOR_ISSUE} repo=${PRIOR_REPO}"
		fi
		log "escalate reason=lineage_cap gen=${GEN} max=${MAX_DEPTH} root=${ROOT} fp=${FP} source=${SOURCE_LABEL} prior_issue=${PRIOR_ISSUE:-none} prior_repo=${PRIOR_REPO}"
		tg_send_msg "Workflow failure heal hit the lineage cap (generation ${GEN} > ${MAX_DEPTH}) for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}'). The auto-heal chain has been stopped; a human should look at this."$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}"$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
		exit 0
		;;
	budget_exhausted)
		REASON="$(jq -r '.reason // "budget"' "${DECISION_FILE}")"
		log "skip reason=budget_exhausted detail=${REASON} open=$(jq -r '.open_count' "${DECISION_FILE}") today=$(jq -r '.today_count' "${DECISION_FILE}") source=${SOURCE_LABEL}"
		tg_send_msg "Workflow failure heal budget exhausted (${REASON}); report from ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}') was NOT filed. Close or resolve open ${HEAL_LABEL} issues to resume."$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}"$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
		exit 0
		;;
	open)
		;;
	*)
		log "error unknown_budget_action action=${ACTION}"
		exit 1
		;;
esac

# --- Source checkout at the release SHA the failing run used ---------------

_git_fetch_diagnosis_ref()
{
	local diagnosis_ref="$1"
	local diagnosis_auth_header=""
	if [ -n "${GH_TOKEN:-}" ]; then
		diagnosis_auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
		GIT_CONFIG_COUNT=1 \
			GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
			GIT_CONFIG_VALUE_0="${diagnosis_auth_header}" \
			git fetch --quiet --depth 1 origin "${diagnosis_ref}" >/dev/null 2>&1
	else
		git fetch --quiet --depth 1 origin "${diagnosis_ref}" >/dev/null 2>&1
	fi
}

DIAG_SHA="${WRAPPER_SHA}"
if [ "${SOURCE_KIND}" = "workflow_run" ]; then
	DIAG_SHA="${HEAD_SHA}"
fi
HEAL_SOURCE_DIR="${RUNTIME_DIR}/heal_src"
HEAL_SOURCE_NOTE="unavailable (diagnose against the working directory)"
if [ "${SOURCE_CHECKOUT,,}" != "false" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	DIAG_REF="${DIAG_SHA}"
	if [ -z "${DIAG_REF}" ]; then
		DIAG_REF="${TARGET_BRANCH_DEFAULT}"
		[ "${SOURCE_KIND}" != "workflow_run" ] || DIAG_REF="${HEAD_BRANCH:-${TARGET_BRANCH_DEFAULT}}"
	fi
	if _git_fetch_diagnosis_ref "${DIAG_REF}" \
		&& git worktree add --quiet --detach "${HEAL_SOURCE_DIR}" FETCH_HEAD >/dev/null 2>&1; then
		HEAL_SOURCE_NOTE="${HEAL_SOURCE_DIR} (coding-workflows at ${DIAG_REF})"
		log "source_checkout ref=${DIAG_REF} path=${HEAL_SOURCE_DIR}"
	else
		log "warn source_checkout_failed ref=${DIAG_REF}; diagnosing against the working directory"
	fi
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
	if [ -f agents.md ]; then
		echo "=== REPO ARCHITECTURE (coding-workflows) ==="
		cat agents.md
		echo
	fi
	if [ -f scripts/render_prompt.sh ]; then
		bash scripts/render_prompt.sh "${PROMPT_FILE_SRC}" 2>/dev/null || cat "${PROMPT_FILE_SRC}"
	else
		cat "${PROMPT_FILE_SRC}" 2>/dev/null || true
	fi
	echo
	echo "=== FAILURE CONTEXT ==="
	echo "HEAL_SOURCE_DIR: ${HEAL_SOURCE_NOTE}"
	echo "Source repository: ${SOURCE_REPO}"
	echo "Report kind: ${SOURCE_KIND}"
	if [ -n "${ISSUE_NUMBER}" ]; then
		echo "Escalated ${SOURCE_KIND}: #${ISSUE_NUMBER} -- ${ISSUE_TITLE}"
		echo "URL: ${ISSUE_URL}"
		echo "Escalation label: ${LABEL}"
		echo "Other labels: $(_pf '.labels | join(", ")')"
	else
		echo "Failed workflow: ${PAYLOAD_WORKFLOW_NAME} (conclusion: $(_pf '.conclusion // "unknown"'))"
		echo "Branch: ${HEAD_BRANCH:-unknown}  Head SHA: ${HEAD_SHA:-unknown}"
	fi
	[ -z "${WRAPPER_SHA}" ] || echo "Consumer wrapper release pin: ${WRAPPER_SHA}"
	echo
	if [ -n "${ISSUE_NUMBER}" ]; then
		echo "--- Issue / PR body excerpt (UNTRUSTED) ---"
		_pf '.issue_excerpt'
		echo
		echo "--- Latest comments excerpt (UNTRUSTED) ---"
		_pf '.comments_excerpt'
		echo
	fi
	echo "=== FAILED RUN LOGS (UNTRUSTED) ==="
	if [ "${SUMMARY_COUNT}" -gt 0 ]; then
		while IFS=$'\t' read -r run_url job_name workflow_name failing_step log_file; do
			echo "--- run ${run_url} | workflow: ${workflow_name} | job: ${job_name} | failing step: ${failing_step:-unknown} ---"
			cat "${log_file}" 2>/dev/null || echo "(log unavailable)"
			echo
		done < <(jq -r '.[] | [.url, .job_name, .workflow_name, .failing_step, .log_file] | @tsv' "${SUMMARIES_FILE}")
	else
		echo "(no failed run could be linked to this escalation; diagnose from the issue context and the escalation label semantics)"
	fi
} > "${PROMPT_FILE}"

if command -v codex >/dev/null 2>&1; then
	if env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID \
		codex --ask-for-approval never \
		-c model_verbosity="${MODEL_VERBOSITY:-low}" \
		-c include_apply_patch_tool=false \
		-c 'shell_environment_policy.ignore_default_excludes=false' \
		-c 'shell_environment_policy.filters.OPENROUTER_API_KEY="exclude"' \
		exec --skip-git-repo-check \
		--model "${MODEL_EDITOR:-openai/gpt-5.6-sol}" \
		--sandbox read-only \
		< "${PROMPT_FILE}" \
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

if [ ! -s "${DIAG_FILE}" ]; then
	{
		echo "## Classification"
		echo
		echo "inconclusive"
		echo
		echo "## Summary"
		echo
		echo "Automated diagnosis ${DIAGNOSIS_FALLBACK_REASON}. The raw failure context is included below for follow-up."
		echo
		echo "## Evidence"
		echo
		echo '```'
		if [ "${#LOG_FILES[@]}" -gt 0 ]; then
			head -200 "${LOG_FILES[0]}" 2>/dev/null || echo "(context unavailable)"
		else
			echo "(no failed run logs were linked)"
		fi
		echo '```'
	} > "${DIAG_FILE}"
fi

CLASSIFICATION="$(python3 "${HEAL_PY}" parse-classification --diagnosis-file "${DIAG_FILE}" 2>/dev/null || echo "inconclusive")"
log "classification=${CLASSIFICATION} source=${SOURCE_LABEL} fp=${FP} gen=${GEN}"

# --- Route -----------------------------------------------------------------

_branch_exists()
{
	local repo="$1" branch="$2"
	local encoded_branch=""
	encoded_branch="$(printf '%s' "${branch}" | jq -sRr @uri)"
	[ -n "${encoded_branch}" ] || return 1
	gh_retry gh api "repos/${repo}/branches/${encoded_branch}" >/dev/null 2>&1
}

_comment_on_source()
{
	local body_file="$1"
	[ -n "${ISSUE_NUMBER}" ] || return 0
	gh_retry gh api "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}/comments" -F body=@"${body_file}" >/dev/null 2>&1 \
		|| log "warn source_comment_failed source=${SOURCE_LABEL}"
}

_open_issue()
{
	local repo="$1" target_branch="$2"
	local title_file="${RUNTIME_DIR}/issue_title.txt" body_file="${RUNTIME_DIR}/issue_body.md"
	python3 "${HEAL_PY}" compose-issue \
		--payload-json "${PAYLOAD_FILE}" \
		--diagnosis-file "${DIAG_FILE}" \
		--run-summaries-json "${SUMMARIES_FILE}" \
		--fingerprint "${FP}" \
		--gen "${GEN}" \
		--root "${ROOT}" \
		--classification "${CLASSIFICATION}" \
		--target-branch "${target_branch}" \
		--max-depth "${MAX_DEPTH}" \
		--intake-run-url "${RUN_URL}" \
		--title-out "${title_file}" \
		--body-out "${body_file}"
	local title
	title="$(head -1 "${title_file}")"
	if [ "${repo}" != "${SELF_REPO}" ]; then
		ensure_label_exists "${HEAL_LABEL}" "${repo}" || true
	fi
	local issue_url
	issue_url="$(gh_retry gh issue create --repo "${repo}" --title "${title}" --body-file "${body_file}" --label "${HEAL_LABEL}" 2>/dev/null || echo '')"
	if [ -z "${issue_url}" ]; then
		log "error issue_create_failed repo=${repo} fp=${FP} source=${SOURCE_LABEL}"
		tg_send_msg "Workflow failure heal FAILED to open an issue in ${repo} for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}')."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
		return 1
	fi
	NEW_ISSUE_URL="${issue_url}"
	return 0
}

NEW_ISSUE_URL=""
case "${CLASSIFICATION}" in
	workflow-defect|inconclusive)
		TARGET_BRANCH="${TARGET_BRANCH_DEFAULT}"
		if [ "${SOURCE_KIND}" = "workflow_run" ] && [ -n "${HEAD_BRANCH}" ]; then
			TARGET_BRANCH="${HEAD_BRANCH}"
		fi
		if ! _branch_exists "${SELF_REPO}" "${TARGET_BRANCH}"; then
			log "warn target_branch_missing branch=${TARGET_BRANCH}; issue will target the default branch"
			TARGET_BRANCH=""
		fi
		_open_issue "${SELF_REPO}" "${TARGET_BRANCH}" || exit 1
		log "created issue=${NEW_ISSUE_URL} repo=${SELF_REPO} classification=${CLASSIFICATION} target_branch=${TARGET_BRANCH:-default} fp=${FP} gen=${GEN} root=${ROOT} source=${SOURCE_LABEL}"
		HOTFIX_NOTE=""
		[ -z "${TARGET_BRANCH}" ] || HOTFIX_NOTE=" as a hotfix on \`${TARGET_BRANCH}\`"
		{
			echo "<!-- workflow-failure-heal:outcome -->"
			echo "Workflow failure heal opened ${NEW_ISSUE_URL} in \`${SELF_REPO}\` (classification \`${CLASSIFICATION}\`, generation ${GEN}/${MAX_DEPTH}). The fix will ship through the normal pipeline${HOTFIX_NOTE}."
		} > "${RUNTIME_DIR}/source_comment.md"
		_comment_on_source "${RUNTIME_DIR}/source_comment.md"
		tg_send_msg "Workflow failure heal opened ${NEW_ISSUE_URL} for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}', classification ${CLASSIFICATION}, generation ${GEN}/${MAX_DEPTH}). The pipeline will pick it up." "DEBUG" >/dev/null 2>&1 || true
		;;
	consumer-app-defect)
		if [ -z "${ISSUE_NUMBER}" ]; then
			# A release run has no consumer; keep the diagnosis in this repo.
			_open_issue "${SELF_REPO}" "${HEAD_BRANCH:-}" || exit 1
			log "created issue=${NEW_ISSUE_URL} repo=${SELF_REPO} classification=${CLASSIFICATION} fp=${FP} source=${SOURCE_LABEL}"
		else
			_open_issue "${SOURCE_REPO}" "" || exit 1
			log "created issue=${NEW_ISSUE_URL} repo=${SOURCE_REPO} classification=${CLASSIFICATION} fp=${FP} source=${SOURCE_LABEL}"
			{
				echo "<!-- workflow-failure-heal:outcome -->"
				echo "Workflow failure heal classified this as a defect in this repository and opened ${NEW_ISSUE_URL} for the pipeline to fix."
			} > "${RUNTIME_DIR}/source_comment.md"
			_comment_on_source "${RUNTIME_DIR}/source_comment.md"
		fi
		tg_send_msg "Workflow failure heal opened ${NEW_ISSUE_URL} (consumer-side defect) for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}')." "DEBUG" >/dev/null 2>&1 || true
		;;
	consumer-config|transient)
		{
			echo "<!-- workflow-failure-heal:outcome -->"
			echo "Workflow failure heal classified this escalation as \`${CLASSIFICATION}\`; no fix issue was opened."
			echo
			echo "<details><summary>Automated diagnosis</summary>"
			echo
			cat "${DIAG_FILE}"
			echo
			echo "</details>"
			echo
			echo "Heal intake run: ${RUN_URL}"
		} > "${RUNTIME_DIR}/source_comment.md"
		_comment_on_source "${RUNTIME_DIR}/source_comment.md"
		SUMMARY_LINE="$(sed -n '/^## Summary/,/^## /p' "${DIAG_FILE}" | sed '1d;/^## /d' | tr '\n' ' ' | head -c 400)"
		log "no_issue classification=${CLASSIFICATION} source=${SOURCE_LABEL} fp=${FP}"
		if [ "${CLASSIFICATION}" = "consumer-config" ]; then
			tg_send_msg "Workflow failure heal: ${SOURCE_LABEL} needs an operator (repository configuration), no code fix applies. ${SUMMARY_LINE}"$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}"$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
		else
			tg_send_msg "Workflow failure heal: ${SOURCE_LABEL} looks transient, no issue opened. ${SUMMARY_LINE}"$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}" "DEBUG" >/dev/null 2>&1 || true
		fi
		;;
	*)
		log "error unknown_classification value=${CLASSIFICATION}"
		exit 1
		;;
esac
