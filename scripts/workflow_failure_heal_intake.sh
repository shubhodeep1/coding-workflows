#!/usr/bin/env bash
#
# workflow_failure_heal_intake.sh
#
# Driven by the Workflow Failure Heal Intake workflow
# (.github/workflows/workflow-failure-heal-intake.yml) in coding-workflows.
# Invoked once per report: a `repository_dispatch` (event `workflow-failure-heal`)
# sent by scripts/workflow_failure_heal_report.sh from a consumer (or from this
# repo's own internal wrapper), by workflow_failure_heal_autofix_report.sh from a
# failed review/autofix run, or by workflow_failure_heal_phase_report.sh from a
# failed clarify / plan / implement run (`phase_failure`), a failed release /
# promotion `workflow_run`, or a manual `workflow_dispatch` re-run. It:
#
#   1. Validates the payload and verifies phase, autofix and release run
#      provenance before reading their logs (label escalations are excluded)
#      (the body, comments, and logs stay untrusted data for the model; an
#      `autofix_failure` report carries its own evidence text), then applies the skip gates:
#      kill switch, unregistered source repo, smoke-test fixture, self run,
#      downstream release-gate failure already reported by the gate itself.
#      A verified autofix_failure with a non-retryable reason
#      (NON_RETRYABLE_FAILURE_REASONS) stops here: one diagnosis comment on the
#      PR, ai:needs-human, a WARNING, `skip reason=non_retryable_<suffix>`.
#   2. Fetches the failed jobs + a filtered tail of their logs for the linked
#      runs (bounded: WORKFLOW_HEAL_MAX_RUNS runs, WORKFLOW_HEAL_MAX_FAILED_JOBS
#      jobs per run, WORKFLOW_HEAL_MAX_RUN_LOG_BYTES per job).
#   3. Fingerprints the failure (workflow + failing step + normalised error
#      signature) and applies the dedup / lineage / budget decision against the
#      open + closed `ai:workflow-heal` issues (see workflow_failure_heal.py):
#      duplicate -> occurrence comment (an `autofix_failure` report also
#      matches an open heal issue filed from the same pull request, a
#      `phase_failure` report one filed from the same issue, and
#      continues the lineage of a closed one or of the heal issue its
#      `ai/issue-<N>` head branch fixes); lineage cap -> escalate;
#      budget cap -> alert; otherwise continue.
#   4. Checks out the coding-workflows source at the release SHA the failing run
#      used, fetches the branch progress since that SHA (one compare call) and
#      the earlier heal issues of the same fingerprint / lineage (from the list
#      already fetched in step 3), runs the diagnosis model (codex /
#      MODEL_EDITOR) with prompts/mode-workflow-failure-heal.txt, and reads its
#      classification. An `already-fixed` answer must cite a commit that landed
#      after the failing SHA, or it is downgraded to `inconclusive`.
#   5. Routes the outcome:
#        workflow-defect, inconclusive -> issue in coding-workflows
#          (label ai:workflow-heal, `Target branch: stable` so the fix is a
#          hotfix on the stable line; a failed release run targets the branch it
#          failed on; a failed review/autofix run on a pull request in this
#          repo targets that pull request's head branch only when the run
#          staged the PR's own scripts (script_ref == head SHA), falling back
#          to stable when the branch no longer exists; otherwise the run hit
#          the shared workflow support and the issue carries no Target
#          branch, so the fix ships on the default branch)
#        consumer-app-defect           -> issue in the source repository
#        consumer-config               -> Telegram ERROR + comment, no issue
#        transient                     -> Telegram DEBUG + comment, no issue
#        already-fixed (verified)      -> Telegram DEBUG + comment, no issue
#        pr-self-inflicted             -> diagnosis comment on the pull request,
#          no issue (the crash is in a file the PR itself changed)
#        base-self-inflicted           -> issue in coding-workflows targeting the
#          PR's base branch, with orchestrator lineage lines when that base is
#          orchestrator/project-<N> (the crash is in a file the base branch
#          changed relative to main and the PR did not)
#      The two self-inflicted tokens apply only to review/autofix reports from
#      this repository with a crash file; the intake computes the ownership
#      (PR diff from the report, base diff from its own `git diff`) before the
#      model runs and routes a token the ownership does not back, or any token
#      while WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false, as
#      workflow-defect.
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
#   WORKFLOW_HEAL_DEFAULT_BRANCH          default branch a CI `workflow_run` report must
#                                         come from (default: the intake event's
#                                         repository.default_branch; empty rejects CI runs)
#   WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED  "false" routes pr-/base-self-inflicted
#                                         as workflow-defect and adds no ownership facts (default true)
#   WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED  "false" (exact value) files workflow-defect /
#                                         inconclusive / base-self-inflicted / consumer-app-defect
#                                         heal issues even when the
#                                         intake-rendered scope marker is missing or invalid; default
#                                         true refuses to file them (skip reason=scope_marker_unresolved,
#                                         CRITICAL). A report that claimed no run at all (a
#                                         label-only or engine_fallback report) can never get a
#                                         scope, so it skips quietly instead
#                                         (skip reason=scope_unavailable_no_runs, exit 0, no alert)
#   WORKFLOW_HEAL_REQUIRE_REPORT_AUTH     "true" skips repository_dispatch reports without an
#                                         OIDC identity; default "false" (transition mode: they
#                                         are accepted after the binding checks). A report that
#                                         carries an identity is always verified (issue #6559).
#   WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS  max age of the identity's iat (default 3600)
#   WORKFLOW_HEAL_REPORT_ORIGIN           repository_dispatch | workflow_run | workflow_dispatch |
#                                         recovery_scan (default GITHUB_EVENT_NAME, else
#                                         repository_dispatch). recovery_scan is set only by
#                                         scripts/workflow_failure_heal_recovery_scan.sh and is
#                                         accepted only on a `schedule` event (#7333)
#   WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP     recovery_scan only: the markerless heal issue's
#                                         `fp` marker; the recomputed fingerprint must equal it
#                                         (default empty, which rejects a recovery_scan report)
#   WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE  recovery_scan only: that heal issue's number in
#                                         this repository (default empty, which rejects)
#   WORKFLOW_HEAL_REPORT_IDENTITY_FILE    0600 file holding the report's OIDC token
#                                         (default ${RUNTIME_DIR}/report_identity.jwt)
#   WORKFLOW_HEAL_REPORT_IDENTITY_STATE   present | absent | malformed (default absent),
#                                         set by the workflow's materialize step
#   WORKFLOW_HEAL_OIDC_JWKS_FILE          test hook: read the JWKS from this file instead of
#                                         fetching it (default empty)
#   HEAL_INTAKE_SCOPE_GUARD_ENABLED       default "true": issue creation is enabled, and then
#                                         only after the composed body's
#                                         ai:workflow-heal-scope:v1 marker verifies; a missing
#                                         or invalid marker logs ::error:: reason=scope_marker_unverified,
#                                         sends a CRITICAL alert and exits nonzero, except for a
#                                         report that claimed no run (skip
#                                         reason=scope_unavailable_no_runs, exit 0, no alert).
#                                         Unset, empty or "true" (any letter case) is on; any other
#                                         value, such as "false", is off: the intake composes the heal
#                                         issue, logs `skip reason=scope_guard_disabled` and creates
#                                         no issue or outcome comment (SELF_REPO heal labels are
#                                         still ensured). Exception:
#                                         a heal-scope recovery replacement (see
#                                         HEAL_SCOPE_RECOVERY_ENABLED) is still verified and
#                                         created while this gate is off.
#   HEAL_SCOPE_RECOVERY_ENABLED           default on (#7191, #7323). Unset, empty or "true"
#                                         (any letter case) lets a verified-identity report
#                                         replace an open, pipeline-authored heal issue that has
#                                         no scope marker (heal-scope status `missing`) instead
#                                         of recording a duplicate; "false" turns off recovery
#                                         only, and any other value also turns it off with a
#                                         warning. Recovery does not need
#                                         HEAL_INTAKE_SCOPE_GUARD_ENABLED: the replacement is
#                                         created only after its scope marker passes the same
#                                         `heal-scope verify` check. The original gets one
#                                         recovery-marker comment and is closed as "not
#                                         planned"; its body is never copied or edited.
#   MODEL_EDITOR                          diagnosis model (default openai/gpt-6-sol)
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
SELF_INFLICTED_ROUTING_ENABLED="${WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED:-true}"
# Only the exact value "false" disables the pre-filing scope-marker check, so
# a typo keeps the guard on (fail closed).
SCOPE_MARKER_GUARD_ENABLED="${WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED:-true}"
# Heal issue creation gate (#6871): on by default; any value other than unset,
# empty or "true" turns it off. See _heal_intake_scope_guard_permits_create.
SCOPE_GUARD_ENABLED="${HEAL_INTAKE_SCOPE_GUARD_ENABLED:-true}"
# Heal-scope recovery (#7191, #7323): on by default; "false" turns it off and
# any other value also turns it off with a warning (the value is not echoed).
# See _heal_scope_recovery_evaluate.
SCOPE_RECOVERY_SWITCH="${HEAL_SCOPE_RECOVERY_ENABLED-}"
case "${SCOPE_RECOVERY_SWITCH,,}" in
	''|true) SCOPE_RECOVERY_ENABLED="true" ;;
	false) SCOPE_RECOVERY_ENABLED="false" ;;
	*)
		SCOPE_RECOVERY_ENABLED="false"
		echo "::warning::WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=invalid_switch" >&2
		;;
esac
unset SCOPE_RECOVERY_SWITCH
# Re-issue lineage (Q147): on by default; "false" turns it off and any other
# value also turns it off with a warning. See _heal_reissue_lookup.
REISSUE_DEDUPE_SWITCH="${HEAL_REISSUE_DEDUPE_ENABLED-}"
case "${REISSUE_DEDUPE_SWITCH,,}" in
	''|true) REISSUE_DEDUPE_ENABLED="true" ;;
	false) REISSUE_DEDUPE_ENABLED="false" ;;
	*)
		REISSUE_DEDUPE_ENABLED="false"
		echo "::warning::WORKFLOW_HEAL reissue_lookup outcome=skip reason=invalid_switch" >&2
		;;
esac
unset REISSUE_DEDUPE_SWITCH
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
REPORT_MAX_AGE="$(_positive_or_default "${WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS:-}" 3600)"
REQUIRE_REPORT_AUTH="${WORKFLOW_HEAL_REQUIRE_REPORT_AUTH:-false}"
case "${REQUIRE_REPORT_AUTH,,}" in
	true|false) REQUIRE_REPORT_AUTH="${REQUIRE_REPORT_AUTH,,}" ;;
	*)
		log "warn invalid_require_report_auth value=$(printf '%s' "${REQUIRE_REPORT_AUTH}" | tr -cd 'A-Za-z0-9_.-' | head -c 40); using false"
		REQUIRE_REPORT_AUTH="false"
		;;
esac
REPORT_ORIGIN="${WORKFLOW_HEAL_REPORT_ORIGIN:-${GITHUB_EVENT_NAME:-repository_dispatch}}"
case "${REPORT_ORIGIN}" in
	repository_dispatch|workflow_run|workflow_dispatch|recovery_scan) ;;
	# Unknown origins take the strictest path.
	*) REPORT_ORIGIN="repository_dispatch" ;;
esac
REPORT_IDENTITY_FILE="${WORKFLOW_HEAL_REPORT_IDENTITY_FILE:-${RUNTIME_DIR}/report_identity.jwt}"
REPORT_IDENTITY_STATE="${WORKFLOW_HEAL_REPORT_IDENTITY_STATE:-absent}"
case "${REPORT_IDENTITY_STATE}" in
	present|absent|malformed) ;;
	*) REPORT_IDENTITY_STATE="malformed" ;;
esac
OIDC_JWKS_FILE_OVERRIDE="${WORKFLOW_HEAL_OIDC_JWKS_FILE:-}"

# --- Gates -----------------------------------------------------------------

if [ "${ENABLED,,}" = "false" ]; then
	log "skip reason=disabled (WORKFLOW_HEAL_ENABLED=false)"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "error heal_helper_missing path=${HEAL_PY}"
	exit 1
fi

# Synced-file security findings (HEAL_SYNCED_SECURITY_FINDING_ENABLED): on when
# unset, empty or "true" (any letter case); "false" turns the kind off, and any
# other value also turns it off with a warning.
RAW_SOURCE_KIND="$(jq -r 'if type == "object" then (.source_kind // "") else "" end' "${PAYLOAD_RAW_FILE}" 2>/dev/null || echo "")"
if [ "${RAW_SOURCE_KIND}" = "synced_security_finding" ]; then
	SYNCED_SECURITY_SWITCH="${HEAL_SYNCED_SECURITY_FINDING_ENABLED:-true}"
	case "${SYNCED_SECURITY_SWITCH,,}" in
		true) ;;
		false)
			log "skip reason=synced_security_finding_disabled"
			exit 0
			;;
		*)
			log "warn invalid_switch name=HEAL_SYNCED_SECURITY_FINDING_ENABLED"
			log "skip reason=synced_security_finding_disabled"
			exit 0
			;;
	esac
	unset SYNCED_SECURITY_SWITCH
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
FAILURE_REASON="$(_pf '.failure_reason // ""')"
FAILURE_STREAK="$(_pf '.failure_streak // ""')"
if [ "${FAILURE_REASON}" = "claude_capacity" ]; then
	# A Claude pool capacity wait is a repo-wide pause, not a defect (#7370).
	log "skip reason=claude_capacity"
	exit 0
fi
PAYLOAD_FAILURE_FINGERPRINT="$(_pf '.failure_fingerprint // ""')"
FAILURE_EVIDENCE_FILE="${RUNTIME_DIR}/failure_evidence.txt"
_pf '.failure_evidence // ""' > "${FAILURE_EVIDENCE_FILE}"
PAYLOAD_BASE_BRANCH="$(_pf '.base_branch // ""')"
PAYLOAD_SCRIPT_REF="$(_pf '.script_ref // ""')"
PAYLOAD_CRASH_FILE="$(_pf '.crash_file // ""')"

# --- Report authentication (issue #6559) -------------------------------------
#
# repository_dispatch: a report carrying an OIDC identity is always verified
# (RS256 against GitHub's JWKS; issuer, audience, iat freshness, repository ==
# source_repo, this repo's reusable reporter workflow, reporter run). Without
# one it is accepted only after the binding checks below, or skipped when
# WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=true. workflow_run payloads are built here
# from GitHub's own event (`event`); manual workflow_dispatch re-runs always
# take the binding checks (`manual`). Every rejection fails closed: WARNING,
# exit 0, no issue or comment written. The token is never logged.

_report_auth_reject()
{
	local reject_reason="${1:-verification_failed}"
	rm -f "${REPORT_IDENTITY_FILE}" 2>/dev/null || true
	log "report_auth=rejected reason=${reject_reason} source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none}"
	tg_send_msg "Workflow failure heal intake rejected a report claiming to come from ${SOURCE_REPO} (reason ${reject_reason}); nothing was filed."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	exit 0
}

REPORT_AUTH=""
# recovery_scan (#7333): the hourly scan rebuilt this autofix_failure report
# from a pipeline-authored, never-edited markerless heal issue. It is not a new
# failure report: REPORT_AUTH=reconstructed is accepted only by
# _heal_scope_recovery_evaluate, the fingerprint and the duplicate decision
# must equal the scan's expected values, and nothing is posted on the source.
# The two expected values come only from the scan's environment, never from
# the payload; a schedule event cannot be produced by a dispatch.
RECOVERY_SCAN_EXPECTED_FP="${WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP:-}"
RECOVERY_SCAN_EXPECTED_ISSUE="${WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE:-}"
case "${REPORT_ORIGIN}" in
	recovery_scan)
		if [ "${GITHUB_EVENT_NAME:-}" != "schedule" ] || [ "${SCOPE_RECOVERY_ENABLED}" != "true" ] \
			|| ! [[ "${RECOVERY_SCAN_EXPECTED_FP}" =~ ^[0-9a-f]{64}$ ]] \
			|| ! [[ "${RECOVERY_SCAN_EXPECTED_ISSUE}" =~ ^[1-9][0-9]*$ ]] \
			|| [ "${SOURCE_KIND}" != "autofix_failure" ]; then
			_report_auth_reject "recovery_scan_invalid"
		fi
		REPORT_AUTH="reconstructed"
		;;
	workflow_run)
		REPORT_AUTH="event"
		;;
	workflow_dispatch)
		REPORT_AUTH="manual"
		;;
	*)
		case "${REPORT_IDENTITY_STATE}" in
			malformed)
				_report_auth_reject "identity_malformed"
				;;
			present)
				if [ ! -s "${REPORT_IDENTITY_FILE}" ]; then
					_report_auth_reject "identity_missing"
				fi
				JWKS_FILE="${RUNTIME_DIR}/github_oidc_jwks.json"
				if [ -n "${OIDC_JWKS_FILE_OVERRIDE}" ]; then
					cp "${OIDC_JWKS_FILE_OVERRIDE}" "${JWKS_FILE}" 2>/dev/null || : > "${JWKS_FILE}"
				elif ! curl -sS --fail --max-time 30 --retry 2 --max-filesize 65536 \
					"https://token.actions.githubusercontent.com/.well-known/jwks" -o "${JWKS_FILE}" 2>/dev/null; then
					: > "${JWKS_FILE}"
				fi
				if ! jq -e '.keys | type == "array"' "${JWKS_FILE}" >/dev/null 2>&1; then
					_report_auth_reject "jwks_unavailable"
				fi
				VERIFY_FILE="${RUNTIME_DIR}/report_identity_verify.json"
				if ! python3 "${HEAL_PY}" verify-report-identity --token-file "${REPORT_IDENTITY_FILE}" --jwks-json "${JWKS_FILE}" \
					--payload-json "${PAYLOAD_FILE}" --self-repo "${SELF_REPO}" --max-age "${REPORT_MAX_AGE}" > "${VERIFY_FILE}" 2>/dev/null; then
					_report_auth_reject "$(jq -r '.reason // "verification_failed"' "${VERIFY_FILE}" 2>/dev/null | tr -cd 'a-z0-9_' | head -c 60 || echo verification_failed)"
				fi
				REPORT_AUTH="verified"
				;;
			*)
				# Every repository_dispatch report, phase_failure included (its
				# heal-report job is granted id-token: write), needs a verified
				# identity when enforcement is on (issue #6834).
				if [ "${REQUIRE_REPORT_AUTH}" = "true" ]; then
					_report_auth_reject "unauthenticated_report"
				fi
				REPORT_AUTH="absent"
				;;
		esac
		;;
esac
# A synced_security_finding report needs a verified identity whatever
# WORKFLOW_HEAL_REQUIRE_REPORT_AUTH says, and its release SHA must be the ref
# the consumer's security-audit.yml ran at.
if [ "${SOURCE_KIND}" = "synced_security_finding" ]; then
	if [ "${REPORT_AUTH}" != "verified" ]; then
		_report_auth_reject "identity_required"
	fi
	SYNCED_SECURITY_WORKFLOW_REF="$(jq -r '.claims.job_workflow_ref // ""' "${VERIFY_FILE}" 2>/dev/null || echo "")"
	if [ -z "${WRAPPER_SHA}" ] || [ "${SYNCED_SECURITY_WORKFLOW_REF##*@}" != "${WRAPPER_SHA}" ]; then
		_report_auth_reject "release_mismatch"
	fi
fi
rm -f "${REPORT_IDENTITY_FILE}" 2>/dev/null || true

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --registry-json "${REGISTRY_FILE}" --self-repo "${SELF_REPO}" 2>/dev/null || echo "")"
# A host-only autofix_failure report (#6745) is not skipped here while the
# non-retryable routing below is on: it must pass binding and provenance first,
# then gets the PR diagnosis (#6756). Every path that does not diagnose it
# still ends in this skip reason after provenance, so no heal issue is filed.
NON_RETRYABLE_ROUTING_ENABLED="${WORKFLOW_HEAL_NON_RETRYABLE_ROUTING_ENABLED:-true}"
HOST_ONLY_DEFERRED=false
if [ "${SKIP_REASON}" = "host_only_conflict_manual_merge" ] && [ "${SOURCE_KIND}" = "autofix_failure" ] && [ "${NON_RETRYABLE_ROUTING_ENABLED,,}" != "false" ]; then
	HOST_ONLY_DEFERRED=true
	SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --registry-json "${REGISTRY_FILE}" --self-repo "${SELF_REPO}" --defer-host-only-autofix 2>/dev/null || echo "host_only_conflict_manual_merge")"
fi
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none}"
	if [ "${SKIP_REASON}" = "unregistered_source_repo" ]; then
		tg_send_msg "Workflow failure heal intake ignored a report from unregistered repository ${SOURCE_REPO} (add it to .github/ai/consumer_repos.json to opt in)."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	fi
	exit 0
fi

# coding-workflows files its own audit findings in-repo.
if [ "${SOURCE_KIND}" = "synced_security_finding" ] && [ "${SOURCE_REPO}" = "${SELF_REPO}" ]; then
	log "skip reason=synced_security_self_repo source=${SOURCE_REPO}"
	exit 0
fi

SOURCE_LABEL="${SOURCE_REPO}#${ISSUE_NUMBER:-run}"

# --- Claim binding (issue #6559) -----------------------------------------------
#
# The claimed issue / PR must exist in source_repo (and carry the claimed
# label); every claimed run must belong to source_repo and have failed (the
# reporter's own verified run may still be in progress). CLAUDE.md §14 audit:
# the intake read no issue or pull request object before this, and the jobs
# reads below are the same per-run reads the log collection used to make, done
# once here and reused there.
LOG_DIR="${RUNTIME_DIR}/logs"
JOBS_DIR="${RUNTIME_DIR}/jobs"
mkdir -p "${LOG_DIR}" "${JOBS_DIR}"
if [ "${REPORT_AUTH}" != "event" ]; then
	BIND_ISSUE_FILE="${RUNTIME_DIR}/bind_issue.json"
	BIND_PULL_FILE="${RUNTIME_DIR}/bind_pull.json"
	BIND_ARGS=(--payload-json "${PAYLOAD_FILE}" --jobs-dir "${JOBS_DIR}")
	if [ "${SOURCE_KIND}" = "issue" ] || [ "${SOURCE_KIND}" = "pull_request" ]; then
		if [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]] && gh_api_json_to_file "${BIND_ISSUE_FILE}" gh api "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}" 2>/dev/null; then
			BIND_ARGS+=(--issue-json "${BIND_ISSUE_FILE}")
			if ! jq -e --arg l "${LABEL}" '[.labels[]? | if type == "object" then .name else . end] | index($l) != null' "${BIND_ISSUE_FILE}" >/dev/null 2>&1; then
				# The label may have been removed since the report; its
				# labeled event still proves the escalation happened.
				BIND_EVENTS_FILE="${RUNTIME_DIR}/bind_events.json"
				if gh_retry gh api --method GET --paginate "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}/events" -F per_page=100 \
					--jq '.[] | select(.event == "labeled") | .label.name // ""' > "${BIND_EVENTS_FILE}" 2>/dev/null; then
					if grep -qxF -- "${LABEL}" "${BIND_EVENTS_FILE}"; then
						BIND_ARGS+=(--labeled-event-found)
					fi
				else
					_report_auth_reject "label_unverifiable"
				fi
			fi
		else
			_report_auth_reject "issue_not_found"
		fi
	elif [ "${SOURCE_KIND}" = "autofix_failure" ]; then
		if [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]] && gh_api_json_to_file "${BIND_PULL_FILE}" gh api "repos/${SOURCE_REPO}/pulls/${ISSUE_NUMBER}" 2>/dev/null; then
			BIND_ARGS+=(--pull-json "${BIND_PULL_FILE}")
			BIND_PR_HEAD="$(jq -r '.head.sha // ""' "${BIND_PULL_FILE}" 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
			if [ -n "${HEAD_SHA}" ] && [[ "${BIND_PR_HEAD}" =~ ^[0-9a-f]{40}$ ]] && [ "${HEAD_SHA}" != "${BIND_PR_HEAD}" ]; then
				# The PR head moved after the failure: the reported head must
				# still be an ancestor of it.
				BIND_COMPARE_FILE="${RUNTIME_DIR}/bind_compare.json"
				if gh_api_json_to_file "${BIND_COMPARE_FILE}" gh api --method GET "repos/${SOURCE_REPO}/compare/${HEAD_SHA}...${BIND_PR_HEAD}" 2>/dev/null \
					&& jq -e '.status == "ahead" or .status == "identical"' "${BIND_COMPARE_FILE}" >/dev/null 2>&1; then
					BIND_ARGS+=(--head-ancestor)
				fi
			fi
		else
			_report_auth_reject "pull_request_not_found"
		fi
	fi
	BIND_RUN_COUNT=0
	# Only label-escalation reports bind their runs here; phase, autofix and
	# workflow_run reports have their runs bound by the provenance gate below,
	# which keeps the current run first and reads no earlier run it rejects.
	BIND_RUNS_HERE=false
	if [ "${SOURCE_KIND}" = "issue" ] || [ "${SOURCE_KIND}" = "pull_request" ]; then
		BIND_RUNS_HERE=true
	fi
	while [ "${BIND_RUNS_HERE}" = "true" ] && IFS= read -r bind_run_id; do
		[[ "${bind_run_id}" =~ ^[0-9]+$ ]] || continue
		BIND_RUN_COUNT=$((BIND_RUN_COUNT + 1))
		[ "${BIND_RUN_COUNT}" -le "${MAX_RUNS}" ] || break
		BIND_JOBS_FILE="${JOBS_DIR}/${bind_run_id}.json"
		if ! gh_api_json_to_file "${BIND_JOBS_FILE}" gh api --method GET "repos/${SOURCE_REPO}/actions/runs/${bind_run_id}/jobs" -F per_page=100 2>/dev/null \
			|| ! jq -e '.jobs | type == "array"' "${BIND_JOBS_FILE}" >/dev/null 2>&1; then
			rm -f "${BIND_JOBS_FILE}"
		fi
	done < <(jq -r '.run_refs[].run_id' "${PAYLOAD_FILE}")
	if [ "${REPORT_AUTH}" = "verified" ]; then
		# Only a verified report's own run (bound to its token) may still be
		# in progress.
		BIND_REPORTER_RUN_ID="$(jq -r '.claims.run_id // ""' "${VERIFY_FILE}" 2>/dev/null || echo "")"
		if [[ "${BIND_REPORTER_RUN_ID}" =~ ^[0-9]+$ ]]; then
			BIND_ARGS+=(--reporter-run-id "${BIND_REPORTER_RUN_ID}")
		fi
		# A verified reporter built the comments excerpt from GitHub; an
		# unauthenticated one may have made it up, so binding clears it.
		BIND_ARGS+=(--trusted-excerpts)
	elif [ "${REPORT_AUTH}" = "reconstructed" ]; then
		# The recovery scan built the excerpts from GitHub reads; no reporter
		# run is bound, so every claimed run must be a completed failure.
		BIND_ARGS+=(--trusted-excerpts)
	fi
	BIND_RESULT_FILE="${RUNTIME_DIR}/bind_result.json"
	if ! python3 "${HEAL_PY}" bind-report "${BIND_ARGS[@]}" > "${BIND_RESULT_FILE}" 2>/dev/null; then
		_report_auth_reject "$(jq -r '.reason // "binding_failed"' "${BIND_RESULT_FILE}" 2>/dev/null | tr -cd 'a-z0-9_' | head -c 60 || echo binding_failed)"
	fi
	# The fetched issue / PR replaces the report's title, URL and body excerpt.
	if ! jq --slurpfile bind "${BIND_RESULT_FILE}" '.run_refs = $bind[0].run_refs | . + ($bind[0].overrides // {})' "${PAYLOAD_FILE}" > "${PAYLOAD_FILE}.tmp" \
		|| ! mv "${PAYLOAD_FILE}.tmp" "${PAYLOAD_FILE}"; then
		_report_auth_reject "binding_failed"
	fi
	ISSUE_TITLE="$(_pf '.issue_title // ""')"
	ISSUE_URL="$(_pf '.issue_url // ""')"
	log "report_auth=${REPORT_AUTH} reason=bound source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} runs_kept=$(jq -r '.run_refs | length' "${BIND_RESULT_FILE}") runs_dropped=$(jq -r '.dropped | length' "${BIND_RESULT_FILE}")"
	if [ "${REPORT_AUTH}" = "absent" ]; then
		# DEBUG: the report is processed normally and the daily wrapper sync
		# (update_workflows.yml) brings the consumer's wrappers up to date.
		tg_send_msg "Workflow failure heal accepted an unauthenticated report from ${SOURCE_REPO} after binding checks; sync its @stable workflow wrappers so reports carry an identity."$'\n'"Run: ${RUN_URL}" "DEBUG" >/dev/null 2>&1 || true
	fi
else
	log "report_auth=event reason=workflow_run_event source=${SOURCE_REPO} kind=${SOURCE_KIND}"
fi

log "received source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} label=${LABEL:-none} workflow=${PAYLOAD_WORKFLOW_NAME:-none}"

# --- Shared intake helpers ---------------------------------------------------
# Defined here, ahead of their first caller: the synced_security_finding branch
# below runs them before the linear path that also uses them.

# The ai:workflow-heal issues of one repository (any state), one record per
# issue tagged with its repository, for the dedup / lineage / budget decision.
_heal_fetch_issue_list()
{
	local list_repo="$1" list_out="$2"
	gh_retry gh api --method GET --paginate "repos/${list_repo}/issues" -F state=all -F labels="${HEAL_LABEL}" -F per_page=100 \
		--jq '.[] | {number, state, state_reason, title, body: (.body // ""), author: (.user.login // ""), created_at, closed_at, html_url, pull_request: (.pull_request != null)}' 2>/dev/null \
		| jq -s --arg repository "${list_repo}" 'map(. + {repository: $repository})' > "${list_out}" 2>/dev/null
}

# HEAL_INTAKE_SCOPE_GUARD_ENABLED (#6871): on when unset, empty or "true"
# (any letter case); any other value is off.
_heal_intake_scope_guard_is_on()
{
	[ "${SCOPE_GUARD_ENABLED,,}" = "true" ]
}

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


# A report that claimed no run (a label-only report, engine_fallback) has no
# verified run to bound a heal scope with, so its scope can never resolve.
# Refusing it is expected, not an intake defect: skip with a log line and
# exit 0 instead of a CRITICAL alert and a failed run. A report that claimed
# runs keeps the CRITICAL refusal, because there an unresolved scope is a fault.
# The raw report is checked too: validate-payload drops malformed or foreign
# refs, and a report whose claimed runs were all dropped is a fault, not a
# run-less report. A heal-scope recovery replacement keeps its documented
# CRITICAL refusal (#7323). A missing jq or unreadable payload prints
# "unknown", which is not "0", so the report keeps the refusal (fail closed).
# A synced_security_finding report carries no run_refs by design but has a
# verified reporter run, so an unverified scope there is a fault: it must not
# exit the whole intake in the middle of its fingerprint-group loop.
_heal_report_claimed_no_runs()
{
	[ "${SOURCE_KIND:-}" != "synced_security_finding" ] \
		&& [ "${HEAL_SCOPE_RECOVERY_ACTIVE:-}" != "true" ] \
		&& [ "$(jq -r '(.run_refs // []) | if type == "array" then length else -1 end' "${PAYLOAD_FILE}" 2>/dev/null || echo unknown)" = "0" ] \
		&& [ "$(jq -r '(.run_refs // []) | if type == "array" then length else -1 end' "${PAYLOAD_RAW_FILE}" 2>/dev/null || echo unknown)" = "0" ]
}

_heal_skip_scope_unavailable()
{
	log "skip reason=scope_unavailable_no_runs stage=$1 fp=${FP} classification=${CLASSIFICATION} source=${SOURCE_LABEL}"
	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		echo "Workflow failure heal filed no issue for ${SOURCE_LABEL}: the report names no failed run, so no heal scope can be set." >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
	fi
	exit 0
}

# Gate in front of the only heal issue-create call (#6871). With
# HEAL_INTAKE_SCOPE_GUARD_ENABLED off it ends the intake with
# exit 0 before the issue-create writes, so no heal issue, target-repo label,
# outcome comment or "created" alert happens (the ai:workflow-heal /
# ai:workflow-heal-escalated labels in SELF_REPO are still ensured earlier,
# as for every report). With it on it re-verifies the composed body's
# ai:workflow-heal-scope:v1 marker through the same `heal-scope verify` the
# implement preflight uses (author / label / edit checks are satisfied
# synthetically: the issue does not exist yet, so only the marker shape is
# checked) and refuses creation, fail closed, on anything but `verified`.
# Heal-scope recovery (#7323): while a recovery replacement is being created
# (HEAL_SCOPE_RECOVERY_ACTIVE=true, set only by _heal_scope_recovery_evaluate)
# the guard-off branch does not exit; it runs the same verify check, so the
# replacement is filed only with a verified scope marker. Every other intake
# path with the guard off still ends with exit 0.
_heal_intake_scope_guard_permits_create()
{
	local scope_guard_repo="$1" scope_guard_body_file="$2" scope_guard_labels="$3" scope_guard_target_branch="${4:-}"
	local scope_guard_input="${RUNTIME_DIR}/scope_guard_input.json" scope_guard_status=""
	if ! _heal_intake_scope_guard_is_on && [ "${HEAL_SCOPE_RECOVERY_ACTIVE}" = "true" ]; then
		log "scope_guard outcome=recovery_only repo=${scope_guard_repo} issue=${HEAL_SCOPE_RECOVERY_ISSUE} fp=${FP} source=${SOURCE_LABEL}"
	elif ! _heal_intake_scope_guard_is_on; then
		log "skip reason=scope_guard_disabled repo=${scope_guard_repo} labels=${scope_guard_labels} target_branch=${scope_guard_target_branch:-default} target_branch_source=${TARGET_BRANCH_SOURCE:-none} classification=${CLASSIFICATION} fp=${FP} source=${SOURCE_LABEL}"
		if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
			echo "Workflow failure heal composed a heal issue for ${SOURCE_LABEL} but did not create it: HEAL_INTAKE_SCOPE_GUARD_ENABLED is off." >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
		fi
		exit 0
	fi
	if [ ! -s "${scope_guard_body_file}" ]; then
		# Name the cause: a missing/empty composed body is not a marker problem.
		scope_guard_status="body_missing"
	elif jq -n --rawfile body "${scope_guard_body_file}" --arg label "${HEAL_LABEL}" \
		'{body: $body, author_login: "heal-intake-precreate", pipeline_login: "heal-intake-precreate", labels: [$label], last_edited_at: null}' \
		> "${scope_guard_input}" 2>/dev/null; then
		scope_guard_status="$(PYTHONDONTWRITEBYTECODE=1 python3 "${HEAL_PY}" heal-scope verify --input-json "${scope_guard_input}" 2>/dev/null \
			| jq -r '.status // empty' 2>/dev/null || true)"
	fi
	rm -f "${scope_guard_input}"
	if [ "${scope_guard_status}" = "verified" ]; then
		log "scope_guard outcome=verified repo=${scope_guard_repo} fp=${FP} source=${SOURCE_LABEL}"
		return 0
	fi
	if _heal_report_claimed_no_runs; then
		_heal_skip_scope_unavailable scope_guard
	fi
	scope_guard_status="$(printf '%s' "${scope_guard_status:-unavailable}" | tr -cd 'a-z_' | head -c 40)"
	[ -n "${scope_guard_status}" ] || scope_guard_status="unavailable"
	echo "::error::WORKFLOW_HEAL error reason=scope_marker_unverified status=${scope_guard_status} repo=${scope_guard_repo} fp=${FP} source=${SOURCE_LABEL}" >&2
	log "error reason=scope_marker_unverified status=${scope_guard_status} repo=${scope_guard_repo} fp=${FP} source=${SOURCE_LABEL}"
	tg_send_msg "Workflow failure heal refused to open an issue in ${scope_guard_repo} for ${SOURCE_LABEL}: the scope marker could not be verified (${scope_guard_status})."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
	return 1
}

# --- Synced-file security findings (synced_security_finding) ----------------
#
# A consumer security audit reports high / critical findings on files the
# @stable sync manages (docs/plans/synced-file-security-findings-heal-intake-plan.md,
# Phase 1). The report names no run, so no log is read and no diagnosis model
# runs (as for engine_fallback). The release SHA was bound to the verified
# identity above; here it must be on `stable`, and every finding's file must be
# a managed path of that release (build_sync_manifest.py managed-digests, run
# from this trusted checkout on the fetched release tree). Findings are grouped
# by fingerprint (path | category | line bucket) and each group takes the
# existing dedupe / occurrence / budget / open path. Only validated fields
# (file, line, category, severity, confidence, finding id, consumer) are
# published (Q245). Counts in the summary line: findings are findings,
# opened / occurrences / skipped are fingerprint groups.
_synced_security_finding_intake()
{
	local ss_dir="${RUNTIME_DIR}/synced_security"
	local ss_bucket="${HEAL_SYNCED_SECURITY_LINE_BUCKET:-25}"
	local ss_findings=0 ss_opened=0 ss_occurrences=0 ss_skipped=0 ss_failed=0 ss_attention=0
	local ss_reporter_run="" ss_compare="" ss_digests="" ss_contract="" ss_groups="" ss_issues=""
	local ss_path_check="" ss_count=0 ss_index=0 ss_links="" ss_action="" ss_reason="" ss_level="DEBUG"
	local ss_group="" ss_decision="" ss_existing="" ss_prior="" ss_gen="" ss_root="" ss_label=""
	local ss_scope="" ss_scope_input="" ss_scope_status="" ss_title="" ss_body="" ss_url="" ss_number=""
	local ss_retired="" ss_group_file="" ss_scope_json="" ss_scope_rc=0
	mkdir -p "${ss_dir}"
	HEAL_SCOPE_RECOVERY_ACTIVE="false"
	CLASSIFICATION="workflow-defect"
	FP="none"
	ss_findings="$(jq -r '.findings | length' "${PAYLOAD_FILE}" 2>/dev/null || echo 0)"
	ss_reporter_run="$(jq -r '.claims.run_id // ""' "${VERIFY_FILE}" 2>/dev/null || echo "")"
	[[ "${ss_reporter_run}" =~ ^[1-9][0-9]*$ ]] || _report_auth_reject "reporter_run_unbound"

	# The release tree the consumer synced from.
	if ! git cat-file -e "${WRAPPER_SHA}^{commit}" 2>/dev/null; then
		_git_fetch_diagnosis_ref "${WRAPPER_SHA}" || true
	fi
	git cat-file -e "${WRAPPER_SHA}^{commit}" 2>/dev/null || _report_auth_reject "release_unavailable"
	# CLAUDE.md §14 audit: no other intake call proves that the release SHA is
	# on `stable` (a fetch by SHA can return a fork-network commit whose tree
	# would forge the managed-paths list). One call per report, not per finding.
	ss_compare="${ss_dir}/release_compare.json"
	if ! gh_api_json_to_file "${ss_compare}" gh api --method GET "repos/${SELF_REPO}/compare/${WRAPPER_SHA}...stable" 2>/dev/null; then
		_report_auth_reject "release_unavailable"
	fi
	jq -e '.status == "ahead" or .status == "identical"' "${ss_compare}" >/dev/null 2>&1 || _report_auth_reject "release_not_on_stable"
	ss_digests="${ss_dir}/managed_digests.json"
	if ! timeout 120 env -i PATH="${PATH}" HOME="${HOME:-/tmp}" PYTHONDONTWRITEBYTECODE=1 \
		python3 -I -B "$(dirname "${HEAL_PY}")/build_sync_manifest.py" managed-digests --repo-root . --commit "${WRAPPER_SHA}" > "${ss_digests}" 2>/dev/null \
		|| ! jq -e 'type == "object" and length > 0' "${ss_digests}" >/dev/null 2>&1; then
		_report_auth_reject "release_unavailable"
	fi
	ss_contract="${ss_dir}/audit_gate_contract.json"
	if ! git show "${WRAPPER_SHA}:workflow-templates/audit-gate/contract.json" > "${ss_contract}" 2>/dev/null \
		|| ! jq -e 'type == "object"' "${ss_contract}" >/dev/null 2>&1; then
		printf '{}' > "${ss_contract}"
	fi
	# Retired files are managed paths (the consumer's sync deletes byte-identical
	# copies), but coding-workflows no longer ships them, so a finding on one has
	# no source to fix. Their consumer paths come from the release's own list
	# and from HEAD's (a file retired after the consumer's pin has no source at
	# HEAD either); an absent or unreadable list means no file is retired.
	ss_retired="${ss_dir}/retired_paths.txt"
	{ git show "${WRAPPER_SHA}:workflow-templates/retired_files.txt" 2>/dev/null || true
		git show "HEAD:workflow-templates/retired_files.txt" 2>/dev/null || true; } \
		| awk '$1 != "" && substr($1, 1, 1) != "#" { print $1 }' > "${ss_retired}" 2>/dev/null || : > "${ss_retired}"
	if ! ss_path_check="$(python3 "${HEAL_PY}" synced-security-check-paths --payload-json "${PAYLOAD_FILE}" --managed-digests-json "${ss_digests}" 2>/dev/null)"; then
		ss_reason="$(jq -r '.reason // "unmanaged_path"' <<< "${ss_path_check}" 2>/dev/null | tr -cd 'a-z_' | head -c 40 || true)"
		_report_auth_reject "${ss_reason:-unmanaged_path}"
	fi

	ss_groups="${ss_dir}/groups.json"
	if ! HEAL_SYNCED_SECURITY_LINE_BUCKET="${ss_bucket}" python3 "${HEAL_PY}" synced-security-groups --payload-json "${PAYLOAD_FILE}" \
		--audit-gate-contract-json "${ss_contract}" > "${ss_groups}"; then
		log "error synced_security_groups_failed source=${SOURCE_REPO}"
		exit 1
	fi

	# The issue lives in this repository, so only its heal list is read.
	ss_issues="${ss_dir}/heal_issues.json"
	if ! _heal_fetch_issue_list "${SELF_REPO}" "${ss_issues}"; then
		log "error heal_issue_list_failed"
		tg_send_msg "Workflow failure heal intake could not list ${HEAL_LABEL} issues; synced-file security report from ${SOURCE_REPO} not processed."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
		exit 1
	fi
	jq -e 'type == "array"' "${ss_issues}" >/dev/null 2>&1 || printf '[]' > "${ss_issues}"

	ss_count="$(jq -r 'length' "${ss_groups}")"
	ss_index=0
	while [ "${ss_index}" -lt "${ss_count}" ]; do
		ss_group="${ss_dir}/group_${ss_index}.json"
		jq ".[${ss_index}]" "${ss_groups}" > "${ss_group}"
		ss_index=$((ss_index + 1))
		FP="$(jq -r '.fingerprint' "${ss_group}")"
		ss_group_file="$(jq -r '.file // empty' "${ss_group}")"
		if [ -z "${ss_group_file}" ]; then
			log "warn empty_group_file fp=${FP} source=${SOURCE_REPO}"
			ss_skipped=$((ss_skipped + 1))
			continue
		fi
		if [ -s "${ss_retired}" ] && grep -Fxq -- "${ss_group_file}" "${ss_retired}"; then
			# Not an attention case: the consumer's next sync removes the file.
			log "skip reason=retired_file file=${ss_group_file} fp=${FP} source=${SOURCE_REPO}"
			ss_skipped=$((ss_skipped + 1))
			continue
		fi
		ss_decision="${ss_dir}/decision_${ss_index}.json"
		python3 "${HEAL_PY}" budget --issues-json "${ss_issues}" --fingerprint "${FP}" --preferred-repo "${SELF_REPO}" \
			--max-depth "${MAX_DEPTH}" --max-open "${MAX_OPEN}" --max-per-day "${MAX_PER_DAY}" > "${ss_decision}" \
			|| { log "error budget_failed fp=${FP} source=${SOURCE_REPO}"; ss_skipped=$((ss_skipped + 1)); ss_failed=1; continue; }
		ss_action="$(jq -r '.action' "${ss_decision}")"
		ss_gen="$(jq -r '.gen // 1' "${ss_decision}")"
		ss_root="$(jq -r '.root // ""' "${ss_decision}")"
		case "${ss_action}" in
			duplicate)
				ss_existing="$(jq -r '.existing_issue // ""' "${ss_decision}")"
				python3 "${HEAL_PY}" compose-synced-security-occurrence --payload-json "${PAYLOAD_FILE}" --group-json "${ss_group}" \
					--intake-run-url "${RUN_URL}" > "${ss_dir}/occurrence_${ss_index}.md" \
					|| { log "error occurrence_compose_failed fp=${FP} source=${SOURCE_REPO}"; ss_skipped=$((ss_skipped + 1)); ss_failed=1; continue; }
				if [[ "${ss_existing}" =~ ^[0-9]+$ ]] && gh_retry gh api "repos/${SELF_REPO}/issues/${ss_existing}/comments" \
					-F body=@"${ss_dir}/occurrence_${ss_index}.md" >/dev/null 2>&1; then
					log "duplicate fp=${FP} existing_issue=${ss_existing} occurrence=recorded source=${SOURCE_REPO}"
					ss_occurrences=$((ss_occurrences + 1))
					ss_links="${ss_links}"$'\n'"- occurrence on #${ss_existing}"
				else
					log "warn occurrence_comment_failed fp=${FP} existing_issue=${ss_existing:-none} source=${SOURCE_REPO}"
					ss_skipped=$((ss_skipped + 1))
					# A lost occurrence is not retried, so the summary alert goes out at WARNING.
					ss_attention=1
				fi
				;;
			escalate)
				ss_prior="$(jq -r '.prior_issue // ""' "${ss_decision}")"
				ss_label="none"
				if [[ "${ss_prior}" =~ ^[0-9]+$ ]]; then
					ss_label="applied"
					gh_retry gh issue edit "${ss_prior}" --repo "${SELF_REPO}" --add-label "${ESCALATED_LABEL}" >/dev/null 2>&1 || ss_label="failed"
				fi
				log "escalate reason=lineage_cap fp=${FP} gen=${ss_gen} prior_issue=${ss_prior:-none} label=${ss_label} source=${SOURCE_REPO}"
				ss_skipped=$((ss_skipped + 1))
				ss_attention=1
				;;
			budget_exhausted)
				log "skip reason=budget_exhausted detail=$(jq -r '.reason // "unknown"' "${ss_decision}" | tr -cd 'a-z_') fp=${FP} source=${SOURCE_REPO}"
				ss_skipped=$((ss_skipped + 1))
				ss_attention=1
				;;
			open)
				ss_scope="${ss_dir}/scope_marker_${ss_index}.txt"
				ss_scope_input="${ss_dir}/scope_input_${ss_index}.json"
				: > "${ss_scope}"
				jq --arg run "${SOURCE_REPO}:${ss_reporter_run}" \
					'{crash_file: null, workflow_paths: .source_paths, changed_files: [], runs: [$run], failing_tests: {names: [], files: [], pairs: []}}' \
					"${ss_group}" > "${ss_scope_input}"
				python3 "${HEAL_PY}" heal-scope render --input-json "${ss_scope_input}" --checkout . --ref HEAD > "${ss_scope}" 2>/dev/null || : > "${ss_scope}"
				# check-marker prints its JSON and exits non-zero for a non-valid
				# marker, so read .status from the captured output (not a pipeline,
				# whose `|| echo` would append a second value under pipefail).
				ss_scope_json=""
				ss_scope_rc=0
				ss_scope_json="$(python3 "${HEAL_PY}" heal-scope check-marker --marker-file "${ss_scope}" 2>/dev/null)" || ss_scope_rc=$?
				ss_scope_status="$(jq -r '.status // "unknown"' <<< "${ss_scope_json}" 2>/dev/null || echo unknown)"
				[[ "${ss_scope_status}" =~ ^[a-z_]{1,20}$ ]] || ss_scope_status="unknown"
				if [ "${ss_scope_rc}" -ne 0 ] && [ "${ss_scope_status}" = "valid" ]; then
					ss_scope_status="unknown"
				fi
				if [ "${ss_scope_status}" != "valid" ]; then
					log "skip reason=scope_unresolved fp=${FP} status=$(printf '%s' "${ss_scope_status}" | tr -cd 'a-z_' | head -c 20) source=${SOURCE_REPO}"
					ss_skipped=$((ss_skipped + 1))
					ss_attention=1
					continue
				fi
				if ! _heal_intake_scope_guard_is_on; then
					log "skip reason=scope_guard_disabled fp=${FP} source=${SOURCE_REPO}"
					ss_skipped=$((ss_skipped + 1))
					ss_attention=1
					continue
				fi
				ss_title="${ss_dir}/issue_title_${ss_index}.txt"
				ss_body="${ss_dir}/issue_body_${ss_index}.md"
				if ! python3 "${HEAL_PY}" compose-synced-security-issue --payload-json "${PAYLOAD_FILE}" --group-json "${ss_group}" \
					--scope-marker-file "${ss_scope}" --gen "${ss_gen}" --root "${ss_root}" --max-depth "${MAX_DEPTH}" \
					--intake-run-url "${RUN_URL}" --title-out "${ss_title}" --body-out "${ss_body}"; then
					log "error issue_compose_failed fp=${FP} source=${SOURCE_REPO}"
					ss_skipped=$((ss_skipped + 1))
					ss_failed=1
					continue
				fi
				if ! _heal_intake_scope_guard_permits_create "${SELF_REPO}" "${ss_body}" "${HEAL_LABEL}" ""; then
					ss_skipped=$((ss_skipped + 1))
					ss_failed=1
					continue
				fi
				ss_url="$(gh_retry gh issue create --repo "${SELF_REPO}" --title "$(head -1 "${ss_title}")" --body-file "${ss_body}" --label "${HEAL_LABEL}" 2>/dev/null || echo '')"
				ss_url="${ss_url%%[[:space:]]*}"
				if [ -z "${ss_url}" ]; then
					log "error issue_create_failed fp=${FP} source=${SOURCE_REPO}"
					ss_skipped=$((ss_skipped + 1))
					ss_failed=1
					continue
				fi
				ss_number=""
				if [[ "${ss_url}" =~ /issues/([1-9][0-9]*)$ ]]; then
					ss_number="${BASH_REMATCH[1]}"
				fi
				log "opened issue=${ss_url} fp=${FP} gen=${ss_gen} source=${SOURCE_REPO}"
				ss_opened=$((ss_opened + 1))
				ss_links="${ss_links}"$'\n'"- opened ${ss_url}"
				# Later groups' dedupe and budgets see the new issue.
				if [ -n "${ss_number}" ]; then
					jq --argjson number "${ss_number}" --rawfile body "${ss_body}" --arg url "${ss_url}" --arg repository "${SELF_REPO}" \
						--arg created "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg title "$(head -1 "${ss_title}")" \
						'. + [{number: $number, state: "open", state_reason: null, title: $title, body: $body, author: "", created_at: $created, closed_at: null, html_url: $url, pull_request: false, repository: $repository}]' \
						"${ss_issues}" > "${ss_issues}.tmp" && mv "${ss_issues}.tmp" "${ss_issues}"
				fi
				;;
			*)
				log "error unknown_decision action=$(printf '%s' "${ss_action}" | tr -cd 'a-z_') fp=${FP}"
				ss_skipped=$((ss_skipped + 1))
				ss_failed=1
				;;
		esac
	done

	log "received kind=synced_security_finding findings=${ss_findings} opened=${ss_opened} occurrences=${ss_occurrences} skipped=${ss_skipped}"
	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		{
			echo "Workflow failure heal: synced-file security report from \`${SOURCE_REPO}\`: findings=${ss_findings} opened=${ss_opened} occurrences=${ss_occurrences} skipped=${ss_skipped}."
			if [ -n "${ss_links}" ]; then printf '%s\n' "${ss_links}"; fi
		} >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
	fi
	if [ "${ss_attention}" -ne 0 ] || [ "${ss_failed}" -ne 0 ]; then
		ss_level="WARNING"
	fi
	tg_send_msg "Workflow failure heal: synced-file security report from ${SOURCE_REPO}: findings=${ss_findings} opened=${ss_opened} occurrences=${ss_occurrences} skipped=${ss_skipped}.${ss_links}"$'\n'"Run: ${RUN_URL}" "${ss_level}" >/dev/null 2>&1 || true
	[ "${ss_failed}" -eq 0 ]
}

if [ "${SOURCE_KIND}" = "synced_security_finding" ]; then
	# A plain call keeps `set -e` active inside the function; a failed group
	# makes it return 1, which ends the run red.
	_synced_security_finding_intake
	exit 0
fi

# --- Provenance (before any log read) --------------------------------------

PENDING_CURRENT_RUN=""
if [[ "${SOURCE_KIND}" == "phase_failure" || "${SOURCE_KIND}" == "autofix_failure" || "${SOURCE_KIND}" == "workflow_run" ]]; then
	PROVENANCE_DIR="${RUNTIME_DIR}/provenance"
	mkdir -p "${PROVENANCE_DIR}"
	PROVENANCE_RUNS="${PROVENANCE_DIR}/runs.json"
	PROVENANCE_COMMENTS=""
	PROVENANCE_RESULT="${PROVENANCE_DIR}/result.json"
	PROVENANCE_LOGIN=""
	printf '{}\n' > "${PROVENANCE_RUNS}"
	# §14 API audit: the existing actions/runs/{id}/jobs call returns no run
	# repository, workflow path, conclusion or PR association, and no issue/PR
	# comments were read here before. At most one /user read, three run GETs,
	# and one paginated comment read; failures reject rather than bypass this gate.
	# The default branch a CI run must come from is read from the intake's own
	# event file (always this repository), so it costs no GitHub call; empty
	# rejects CI runs (fail closed) and leaves release runs unaffected.
	PROVENANCE_DEFAULT_BRANCH="${WORKFLOW_HEAL_DEFAULT_BRANCH:-}"
	if [ -z "${PROVENANCE_DEFAULT_BRANCH}" ] && [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -f "${GITHUB_EVENT_PATH}" ] && [ -r "${GITHUB_EVENT_PATH}" ]; then
		PROVENANCE_DEFAULT_BRANCH="$(jq -r '.repository.default_branch // ""' "${GITHUB_EVENT_PATH}" 2>/dev/null || true)"
	fi
	PROVENANCE_DEFAULT_BRANCH="$(printf '%s' "${PROVENANCE_DEFAULT_BRANCH}" | tr -cd 'A-Za-z0-9._/-' | head -c 255)"
	if [ "${SOURCE_KIND}" = "workflow_run" ] && [ -z "${PROVENANCE_DEFAULT_BRANCH}" ]; then
		# Lets operators tell "default branch unknown" apart from "run not on the
		# default branch"; both still reject CI runs as ci_not_default_branch_push.
		log "provenance_default_branch_unavailable source=${SOURCE_REPO} kind=${SOURCE_KIND} effect=ci_runs_rejected"
	fi
	if [ "${SOURCE_KIND}" != "workflow_run" ]; then
		if [ "${SOURCE_KIND}" != "phase_failure" ] || [ "${SOURCE_REPO,,}" = "${SELF_REPO,,}" ]; then
			PROVENANCE_LOGIN="$(gh_retry gh api --method GET user --jq .login 2>/dev/null || true)"
		fi
		PROVENANCE_COMMENTS="${PROVENANCE_DIR}/comments.json"
		if ! gh_retry gh api --method GET --paginate "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}/comments" -F per_page=100 \
			--jq '.[] | {id, body: (.body // ""), user: (.user // {}), author_association: (.author_association // "")}' 2>/dev/null \
			| jq -s '.' > "${PROVENANCE_COMMENTS}" 2>/dev/null \
			|| ! jq -e 'type == "array"' "${PROVENANCE_COMMENTS}" >/dev/null 2>&1; then
			PROVENANCE_COMMENTS=""
		fi
	fi
	while IFS= read -r provenance_run_id; do
		PROVENANCE_RUN_FILE="${PROVENANCE_DIR}/run-${provenance_run_id}.json"
		if gh_api_json_to_file "${PROVENANCE_RUN_FILE}" gh api --method GET "repos/${SOURCE_REPO}/actions/runs/${provenance_run_id}" 2>/dev/null \
			&& jq -e 'type == "object"' "${PROVENANCE_RUN_FILE}" >/dev/null 2>&1; then
			jq --arg id "${provenance_run_id}" --slurpfile run "${PROVENANCE_RUN_FILE}" '. + {($id): $run[0]}' "${PROVENANCE_RUNS}" > "${PROVENANCE_RUNS}.tmp" \
				&& mv "${PROVENANCE_RUNS}.tmp" "${PROVENANCE_RUNS}"
		fi
	done < <(jq -r '.run_refs[].run_id' "${PAYLOAD_FILE}")
	if ! python3 "${HEAL_PY}" verify-run-provenance --payload-json "${PAYLOAD_FILE}" --runs-json "${PROVENANCE_RUNS}" \
		--comments-json "${PROVENANCE_COMMENTS}" --trusted-login "${PROVENANCE_LOGIN}" --self-repo "${SELF_REPO}" \
		--default-branch "${PROVENANCE_DEFAULT_BRANCH}" > "${PROVENANCE_RESULT}" 2>/dev/null \
		|| ! jq -e 'type == "object" and (.status == "ok" or .status == "rejected") and (.run_refs | type == "array") and (.rejections | type == "array")' "${PROVENANCE_RESULT}" >/dev/null 2>&1; then
		printf '{"status":"rejected","reason":"verifier_error","rejections":[]}\n' > "${PROVENANCE_RESULT}"
	fi
	while IFS=$'\t' read -r provenance_run_id provenance_reason; do
		log "provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} run_id=${provenance_run_id} reason=${provenance_reason}"
	done < <(jq -r '.rejections[] | [.run_id, .reason] | @tsv' "${PROVENANCE_RESULT}")
	if [ "$(jq -r '.status' "${PROVENANCE_RESULT}")" != "ok" ]; then
		PROVENANCE_REASON="$(jq -r '.reason // "verifier_error"' "${PROVENANCE_RESULT}")"
		log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=${PROVENANCE_REASON}"
		tg_send_msg "Workflow failure heal rejected run provenance for ${SOURCE_REPO} (${SOURCE_KIND}, issue ${ISSUE_NUMBER:-none}, reason ${PROVENANCE_REASON})."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
		exit 0
	fi
	PENDING_CURRENT_RUN="$(jq -r '.pending_current_run // ""' "${PROVENANCE_RESULT}")"
	if ! jq --slurpfile result "${PROVENANCE_RESULT}" '.run_refs = $result[0].run_refs' "${PAYLOAD_FILE}" > "${PAYLOAD_FILE}.tmp" \
		|| ! mv "${PAYLOAD_FILE}.tmp" "${PAYLOAD_FILE}"; then
		log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=verifier_error"
		exit 0
	fi
	log "provenance_verified source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} runs=$(jq '.run_refs | length' "${PAYLOAD_FILE}")"
fi

# --- Non-retryable review/autofix failures (after provenance) --------------
#
# A verified autofix_failure whose reason is in NON_RETRYABLE_FAILURE_REASONS
# (scripts/workflow_failure_heal.py; directly, or as the repeated reason of an
# identical_failure_cap report, read from the trusted account's own failure
# markers) cannot be fixed by another run or by a heal issue: the resolver
# sandbox refuses host-executed paths by design (#6750 / PR #6535). Post one
# diagnosis on the PR (marker ai:workflow-heal-non-retryable:v1, keyed on
# pr / head / reason), label it ai:needs-human, send a WARNING, and stop.
# §14 API audit: reuses the comment snapshot and login the provenance gate
# already read; no new GET. A duplicate report makes no API call unless the
# PR read by claim binding lacks ai:needs-human (one label add).
# Fail open: a helper error falls through to the existing heal path (or, for
# a deferred host-only report, to the #6745 skip right after this block).
# _non_retryable_route_apply: the PR-side handling of a routed report (the
# diagnosis comment, ai:needs-human, the WARNING; pr/head/reason dedup).
# Returns 0 when the report was handled (the caller stops), 1 when it does not
# route. Reads the helper result in NON_RETRYABLE_FILE.
NON_RETRYABLE_RECOVERY_DEFERRED=false
NON_RETRYABLE_RECOVERY_DONE=false
_non_retryable_route_apply()
{
	[ -s "${NON_RETRYABLE_FILE:-}" ] || return 1
	[ "$(jq -r '.route' "${NON_RETRYABLE_FILE}" 2>/dev/null || echo false)" = "true" ] || return 1
	NON_RETRYABLE_REASON="$(jq -r '.reason' "${NON_RETRYABLE_FILE}")"
	NON_RETRYABLE_SUFFIX="$(jq -r '.suffix' "${NON_RETRYABLE_FILE}")"
	NON_RETRYABLE_PATH_COUNT="$(jq -r '.paths | length' "${NON_RETRYABLE_FILE}")"
	NON_RETRYABLE_PATHS="$(jq -r '.paths | join(", ")' "${NON_RETRYABLE_FILE}")"
	if [ "${NON_RETRYABLE_REASON}" = "conflict_resolver_sandbox_support_missing" ]; then
		NON_RETRYABLE_ACTION="workflow support lacks the conflict resolver's sandbox files; a maintainer must fix workflow support (or merge the conflict by hand)"
	elif [ -n "${NON_RETRYABLE_PATHS}" ]; then
		NON_RETRYABLE_ACTION="${NON_RETRYABLE_PATHS} need a manual merge"
	else
		NON_RETRYABLE_ACTION="the conflicted path(s) need a manual merge"
	fi
	if [ "$(jq -r '.already_diagnosed' "${NON_RETRYABLE_FILE}")" = "true" ]; then
		# The diagnosis is already posted. Re-add ai:needs-human only when
		# the PR read by claim binding shows it missing (a failed label
		# write earlier); otherwise this costs no API call.
		NON_RETRYABLE_DUP_LABEL="present"
		if [ -f "${BIND_PULL_FILE:-}" ] && jq -e '.labels | type == "array"' "${BIND_PULL_FILE}" >/dev/null 2>&1 \
			&& ! jq -e '[.labels[]? | if type == "object" then .name else . end] | index("ai:needs-human") != null' "${BIND_PULL_FILE}" >/dev/null 2>&1; then
			ensure_label_exists "ai:needs-human" "${SOURCE_REPO}" || true
			if gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${SOURCE_REPO}" --add-label "ai:needs-human" >/dev/null 2>&1; then
				NON_RETRYABLE_DUP_LABEL="reapplied"
			else
				NON_RETRYABLE_DUP_LABEL="failed"
			fi
		fi
		log "skip reason=non_retryable_${NON_RETRYABLE_SUFFIX} outcome=duplicate source=${SOURCE_LABEL} pr=${ISSUE_NUMBER} head=${HEAD_SHA} label=${NON_RETRYABLE_DUP_LABEL}"
		return 0
	fi
	NON_RETRYABLE_COMMENT_FILE="${RUNTIME_DIR}/non_retryable_comment.md"
	jq -r '.comment' "${NON_RETRYABLE_FILE}" > "${NON_RETRYABLE_COMMENT_FILE}"
	# The comment goes first: its marker is what stops the label event
	# below from producing another heal report.
	if ! gh_retry gh api "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}/comments" -F body=@"${NON_RETRYABLE_COMMENT_FILE}" >/dev/null 2>&1; then
		log "error non_retryable_comment_failed source=${SOURCE_LABEL} pr=${ISSUE_NUMBER} head=${HEAD_SHA} reason=${NON_RETRYABLE_REASON}"
		tg_send_msg "Workflow failure heal could not post the non-retryable diagnosis (${NON_RETRYABLE_REASON}) on ${SOURCE_LABEL}; no heal issue was opened and the next report retries."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
		return 0
	fi
	ensure_label_exists "ai:needs-human" "${SOURCE_REPO}" || true
	NON_RETRYABLE_LABEL_STATUS="ai:needs-human applied"
	NON_RETRYABLE_LABEL_TOKEN="applied"
	if ! gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${SOURCE_REPO}" --add-label "ai:needs-human" >/dev/null 2>&1; then
		log "warn non_retryable_label_failed source=${SOURCE_LABEL} pr=${ISSUE_NUMBER}"
		NON_RETRYABLE_LABEL_STATUS="adding ai:needs-human FAILED (the next report for this head retries it)"
		NON_RETRYABLE_LABEL_TOKEN="failed"
	fi
	tg_send_msg "Workflow failure heal: non-retryable review/autofix failure (${NON_RETRYABLE_REASON}) on ${SOURCE_LABEL}: ${NON_RETRYABLE_ACTION}. Diagnosis posted, ${NON_RETRYABLE_LABEL_STATUS}; no heal issue opened."$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}"$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	log "skip reason=non_retryable_${NON_RETRYABLE_SUFFIX} outcome=diagnosed source=${SOURCE_LABEL} pr=${ISSUE_NUMBER} head=${HEAD_SHA} paths=${NON_RETRYABLE_PATH_COUNT} label=${NON_RETRYABLE_LABEL_TOKEN}"
	return 0
}

# Q3 (#7333): a verified sandbox report deferred for heal-scope recovery gets
# the unchanged PR-side handling after recovery, on every exit path (the scope
# guard's refusal included); the exit code is kept.
_non_retryable_recovery_deferred_exit()
{
	local deferred_rc=$?
	trap - EXIT
	if [ "${NON_RETRYABLE_RECOVERY_DONE}" != "true" ]; then
		NON_RETRYABLE_RECOVERY_DONE=true
		set +e
		_non_retryable_route_apply || true
	fi
	exit "${deferred_rc}"
}

if [ "${REPORT_AUTH}" = "reconstructed" ]; then
	# A recovery-scan replay is not a new failure report: it must not re-add
	# ai:needs-human to a PR a person has cleared (#7333).
	log "non_retryable outcome=skip reason=reconstructed source=${SOURCE_LABEL}"
elif [ "${SOURCE_KIND}" = "autofix_failure" ] && [ "${NON_RETRYABLE_ROUTING_ENABLED,,}" != "false" ] \
	&& [ -n "${PROVENANCE_COMMENTS:-}" ] && [ -n "${PROVENANCE_LOGIN:-}" ]; then
	NON_RETRYABLE_FILE="${RUNTIME_DIR}/non_retryable_route.json"
	NON_RETRYABLE_ERR_FILE="${RUNTIME_DIR}/non_retryable_route.err"
	if python3 "${HEAL_PY}" non-retryable-route --payload-json "${PAYLOAD_FILE}" --comments-json "${PROVENANCE_COMMENTS}" \
		--trusted-login "${PROVENANCE_LOGIN}" --intake-run-url "${RUN_URL}" > "${NON_RETRYABLE_FILE}" 2> "${NON_RETRYABLE_ERR_FILE}" \
		&& jq -e 'type == "object" and (.route | type == "boolean") and (.already_diagnosed | type == "boolean") and (.comment | type == "string")' "${NON_RETRYABLE_FILE}" >/dev/null 2>&1; then
		NON_RETRYABLE_ROUTE_REASON="$(jq -r 'if .route == true then .reason else "" end' "${NON_RETRYABLE_FILE}")"
		# Gap 2 (#7333): a verified report with one of the two sandbox reasons
		# first gets the heal-scope recovery check (a matching markerless heal
		# issue is replaced); the host-only stop keeps its routing.
		if [ "${REPORT_AUTH}" = "verified" ] && [ "${SCOPE_RECOVERY_ENABLED}" = "true" ] \
			&& { [ "${NON_RETRYABLE_ROUTE_REASON}" = "conflict_resolver_sandbox_path_unsupported" ] \
				|| [ "${NON_RETRYABLE_ROUTE_REASON}" = "conflict_resolver_sandbox_support_missing" ]; }; then
			NON_RETRYABLE_RECOVERY_DEFERRED=true
			trap '_non_retryable_recovery_deferred_exit' EXIT
			log "non_retryable outcome=deferred reason=heal_scope_recovery route_reason=${NON_RETRYABLE_ROUTE_REASON} source=${SOURCE_LABEL} pr=${ISSUE_NUMBER} head=${HEAD_SHA}"
		elif _non_retryable_route_apply; then
			exit 0
		fi
	else
		log "warn non_retryable_route_unavailable source=${SOURCE_LABEL} detail=$(head -c 200 "${NON_RETRYABLE_ERR_FILE}" 2>/dev/null | tr -c 'A-Za-z0-9_.:=-' ' ' | tr -s ' ' || true)"
	fi
fi
if [ "${HOST_ONLY_DEFERRED}" = "true" ]; then
	# Not diagnosed (no trusted comment snapshot or login, a helper error, or a
	# cap report whose trusted markers do not name the host-only reason): keep
	# #6745's outcome, a manual merge and no heal issue.
	log "skip reason=host_only_conflict_manual_merge source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} stage=after_provenance"
	exit 0
fi

# --- Collect failed jobs + logs --------------------------------------------

SUMMARIES_FILE="${RUNTIME_DIR}/run_summaries.json"
printf '[]' > "${SUMMARIES_FILE}"
LOG_FILES=()
# Job logs actually fetched (placeholders for unreadable logs do not count).
READ_JOB_LOG_COUNT=0
RUN_COUNT=0

while IFS=$'\t' read -r run_id run_url; do
	[ -n "${run_id}" ] || continue
	[[ "${run_id}" =~ ^[0-9]+$ ]] || continue
	RUN_COUNT=$((RUN_COUNT + 1))
	if [ "${RUN_COUNT}" -gt "${MAX_RUNS}" ]; then
		break
	fi
	RUN_INFO_FILE="${LOG_DIR}/run-${run_id}-info.json"
	# Reuse the run already read by the provenance gate (§14); only label
	# escalation reports, which skip that gate, read the run here.
	run_info_ready=false
	if [ -n "${PROVENANCE_DIR:-}" ] && [ -s "${PROVENANCE_DIR}/run-${run_id}.json" ] && cp "${PROVENANCE_DIR}/run-${run_id}.json" "${RUN_INFO_FILE}"; then
		run_info_ready=true
	elif gh_api_json_to_file "${RUN_INFO_FILE}" gh api --method GET "repos/${SOURCE_REPO}/actions/runs/${run_id}"; then
		run_info_ready=true
	fi
	verify_pending_args=()
	if [ -n "${PENDING_CURRENT_RUN}" ] && [ "${run_id}" = "${PENDING_CURRENT_RUN}" ]; then
		verify_pending_args=(--allow-pending)
	fi
	# A review dispatched for the PR runs on the default branch, so its head is
	# not the PR head; verify-run binds it by its `[pr:N]` title instead.
	verify_pr_args=()
	if [ "${SOURCE_KIND}" = autofix_failure ] && [[ "${ISSUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then
		verify_pr_args=(--pr-number "${ISSUE_NUMBER}")
	fi
	if [ "${run_info_ready}" != true ] \
		|| ! python3 "${HEAL_PY}" verify-run --run-json "${RUN_INFO_FILE}" --repo "${SOURCE_REPO}" --target-repo "${SELF_REPO}" --kind "${SOURCE_KIND}" "${verify_pending_args[@]}" "${verify_pr_args[@]}" \
			--head-sha "$({ [ "${SOURCE_KIND}" = autofix_failure ] || [ "${SOURCE_KIND}" = workflow_run ]; } && printf '%s' "${HEAD_SHA}" || true)" > "${RUN_INFO_FILE}.verified" 2> "${RUN_INFO_FILE}.verify_error" \
		|| ! jq -e --argjson id "${run_id}" '.run_id == $id' "${RUN_INFO_FILE}.verified" >/dev/null; then
		log "run_ref_rejected run=${run_id} reason=unverified detail=$(tail -n 1 "${RUN_INFO_FILE}.verify_error" 2>/dev/null | head -c 120 | tr -d '\r\n' | tr -c 'A-Za-z0-9_.:-' '_' || true)"
		continue
	fi
	JOBS_FILE="${LOG_DIR}/run-${run_id}-jobs.json"
	if [ -s "${JOBS_DIR}/${run_id}.json" ]; then
		# Already read (and bound) above.
		cp "${JOBS_DIR}/${run_id}.json" "${JOBS_FILE}"
	elif ! gh_api_json_to_file "${JOBS_FILE}" gh api --method GET "repos/${SOURCE_REPO}/actions/runs/${run_id}/jobs" -F per_page=100; then
		log "warn jobs_fetch_failed source=${SOURCE_REPO} run=${run_id}"
		if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${RUN_COUNT}" -eq 1 ]; then
			log "provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} run_id=${run_id} reason=current_run_jobs_unavailable"
			log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=current_run_jobs_unavailable"
			exit 0
		fi
		continue
	fi
	if ! jq -e '.jobs | type == "array"' "${JOBS_FILE}" >/dev/null 2>&1; then
		log "warn jobs_fetch_invalid source=${SOURCE_REPO} run=${run_id}"
		if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${RUN_COUNT}" -eq 1 ]; then
			log "provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} run_id=${run_id} reason=current_run_jobs_unavailable"
			log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=current_run_jobs_unavailable"
			exit 0
		fi
		continue
	fi
	if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${RUN_COUNT}" -eq 1 ] \
		&& ! jq -e 'any(.jobs[]; (.conclusion == "failure" or .conclusion == "timed_out")
			and (.name | type == "string") and .name != "heal-report"
			and (.name | endswith("/ heal-report") | not))' "${JOBS_FILE}" >/dev/null 2>&1; then
		log "provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} run_id=${run_id} reason=current_run_no_failed_job"
		log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=current_run_no_failed_job"
		exit 0
	fi
	SELECTED_JOBS_FILE="${LOG_DIR}/run-${run_id}-selected.json"
	python3 "${HEAL_PY}" select-evidence-jobs --jobs-json "${JOBS_FILE}" --kind "${SOURCE_KIND}" --limit "${MAX_FAILED_JOBS}" > "${SELECTED_JOBS_FILE}"
	JOB_COUNT=0
	while IFS=$'\t' read -r job_id job_name workflow_name failing_step selection; do
		[ -n "${job_id}" ] || continue
		[[ "${job_id}" =~ ^[0-9]+$ ]] || continue
		JOB_COUNT=$((JOB_COUNT + 1))
		if [ "${JOB_COUNT}" -gt "${MAX_FAILED_JOBS}" ]; then
			break
		fi
		RAW_LOG="${LOG_DIR}/run-${run_id}-job-${job_id}.raw"
		FILTERED_LOG="${LOG_DIR}/run-${run_id}-job-${job_id}.txt"
		# Job logs carry ANSI colour codes; without --allow-escape-sequences gh
		# refuses the body ("the response contains terminal escape sequences")
		# even when stdout is a file, and every job read as "(job log
		# unavailable)": no error signature, no downstream-gate dedup.
		# gh_retry buffers stdout in an unredacted temp file. Fetch directly:
		# the pipe redacts bytes before the first filesystem write.
		if gh api --allow-escape-sequences "repos/${SOURCE_REPO}/actions/jobs/${job_id}/logs" 2>/dev/null \
			| python3 "${HEAL_PY}" redact-stream > "${RAW_LOG}" && [ -s "${RAW_LOG}" ]; then
			if ! python3 "${HEAL_PY}" filter-log --log-file "${RAW_LOG}" --max-lines "${LOG_TAIL_LINES}" --max-bytes "${MAX_LOG_BYTES}" > "${FILTERED_LOG}"; then
				log "warn job_log_filter_failed source=${SOURCE_REPO} run=${run_id} job=${job_id}"
				: > "${FILTERED_LOG}"
			else
				# A log whose filtering failed was emptied above and is not readable evidence.
				READ_JOB_LOG_COUNT=$((READ_JOB_LOG_COUNT + 1))
			fi
			rm -f "${RAW_LOG}"
			if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${RUN_COUNT}" -eq 1 ] && [ -s "${FILTERED_LOG}" ]; then
				LOG_FILES+=("${FILTERED_LOG}")
			fi
		else
			log "warn job_log_fetch_failed source=${SOURCE_REPO} run=${run_id} job=${job_id}"
			printf '(job log unavailable)\n' > "${FILTERED_LOG}"
		fi
		if [ "${SOURCE_KIND}" != "phase_failure" ]; then
			LOG_FILES+=("${FILTERED_LOG}")
		fi
		jq --arg run_id "${run_id}" --arg url "${run_url}" --arg job_id "${job_id}" --arg job_name "${job_name}" --arg selection "${selection}" \
			--arg workflow_name "${workflow_name}" --arg failing_step "${failing_step}" --arg log_file "${FILTERED_LOG}" \
			'. + [{run_id: $run_id, url: $url, job_id: $job_id, job_name: $job_name, workflow_name: $workflow_name, failing_step: $failing_step, log_file: $log_file, selection: $selection}]' \
			"${SUMMARIES_FILE}" > "${SUMMARIES_FILE}.tmp" && mv "${SUMMARIES_FILE}.tmp" "${SUMMARIES_FILE}"
	done < <(jq -r '.[] | [(.id|tostring), (.name // ""), (.workflow_name // ""), ((.steps // []) | map(select((.conclusion // "") | IN("failure","timed_out","cancelled"))) | first | .name // ""), .selection]
		| map(gsub("[\\t\\n\\r]"; " ")) | @tsv' "${SELECTED_JOBS_FILE}")
done < <(jq -r '.run_refs[] | [.run_id, .url] | @tsv' "${PAYLOAD_FILE}")

if [ "${SOURCE_KIND}" = "phase_failure" ] && [ -n "${PENDING_CURRENT_RUN}" ] \
	&& ! jq -e --arg run_id "${PENDING_CURRENT_RUN}" 'any(.[]; .run_id == $run_id)' "${SUMMARIES_FILE}" >/dev/null 2>&1; then
	log "provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} run_id=${PENDING_CURRENT_RUN} reason=current_run_no_failed_job"
	log "skip reason=provenance_rejected source=${SOURCE_REPO} kind=${SOURCE_KIND} issue=${ISSUE_NUMBER:-none} detail=current_run_no_failed_job"
	exit 0
fi

SUMMARY_COUNT="$(jq 'length' "${SUMMARIES_FILE}")"
FIRST_WORKFLOW_NAME="$(jq -r 'map(select(.workflow_name != "")) | first | .workflow_name // ""' "${SUMMARIES_FILE}")"
FIRST_FAILING_STEP="$(jq -r 'map(select(.failing_step != "")) | first | .failing_step // ""' "${SUMMARIES_FILE}")"
if [ "${SOURCE_KIND}" = "phase_failure" ]; then
	# Earlier streak runs provide diagnostic context, not the current run's identity.
	PHASE_FAILED_RUN_ID="$(jq -r '.run_refs[0].run_id' "${PAYLOAD_FILE}")"
	FIRST_WORKFLOW_NAME="$(jq -r --arg current "${PHASE_FAILED_RUN_ID}" 'map(select(.run_id == $current and .workflow_name != "")) | first | .workflow_name // ""' "${SUMMARIES_FILE}")"
	FIRST_FAILING_STEP="$(jq -r --arg current "${PHASE_FAILED_RUN_ID}" 'map(select(.run_id == $current and .failing_step != "")) | first | .failing_step // ""' "${SUMMARIES_FILE}")"
fi
[ -n "${FIRST_WORKFLOW_NAME}" ] || FIRST_WORKFLOW_NAME="${PAYLOAD_WORKFLOW_NAME}"
if [ -z "${FIRST_WORKFLOW_NAME}" ]; then
	FIRST_WORKFLOW_NAME="label:${LABEL:-unknown}"
fi

if [ "${SOURCE_KIND}" = "phase_failure" ]; then
	PHASE_RUN_ID="$(_pf '.run_refs[0].run_id // ""')"
	PHASE_RUN_FILE="${PROVENANCE_DIR}/run-${PHASE_RUN_ID}.json"
	PHASE_JOBS_FILE="${LOG_DIR}/run-${PHASE_RUN_ID}-jobs.json"
	PHASE_COMMENTS_FILE="${PROVENANCE_COMMENTS}"
	PHASE_PROVENANCE_FILE="${RUNTIME_DIR}/phase_provenance.json"
	# Reuse the run, comment and account snapshots already checked before log reads.
	PHASE_COMMENT_AUTHOR="${PROVENANCE_LOGIN}"
	if ! python3 "${HEAL_PY}" verify-phase-provenance --payload-json "${PAYLOAD_FILE}" \
		--run-json "${PHASE_RUN_FILE}" --jobs-json "${PHASE_JOBS_FILE}" \
		--comments-json "${PHASE_COMMENTS_FILE}" --self-repo "${SELF_REPO}" \
		--comment-author "${PHASE_COMMENT_AUTHOR}" > "${PHASE_PROVENANCE_FILE}" \
		|| ! jq -e 'type == "object" and (.verified | type == "boolean") and (.reason | type == "string")' "${PHASE_PROVENANCE_FILE}" >/dev/null 2>&1; then
		printf '{"verified":false,"reason":"verifier_error"}\n' > "${PHASE_PROVENANCE_FILE}"
	fi
	if ! jq -e '.verified == true and .reason == "ok"' "${PHASE_PROVENANCE_FILE}" >/dev/null 2>&1; then
		PHASE_PROVENANCE_REASON="$(jq -r '.reason' "${PHASE_PROVENANCE_FILE}")"
		log "skip reason=phase_report_unverified detail=${PHASE_PROVENANCE_REASON} outcome=skip source=${SOURCE_REPO} issue=${ISSUE_NUMBER} run=${PHASE_RUN_ID}"
		tg_send_msg "Workflow failure heal intake dropped a phase_failure report for ${SOURCE_REPO}#${ISSUE_NUMBER}: run ${PHASE_RUN_ID} could not be verified (${PHASE_PROVENANCE_REASON})."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
		exit 0
	fi
	log "phase_report_verified source=${SOURCE_REPO} issue=${ISSUE_NUMBER} run=${PHASE_RUN_ID}"
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

# A release / default-branch run of this repository that failed only because
# the model provider was unavailable (evidence in its logs confirmed by a live
# probe, scripts/provider_outage.py) is recorded on the repo-wide provider
# outage tracker instead of filing a heal issue; the scheduled sweep reports
# or (opt-in, PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED) re-runs it on recovery
# (issue #6633). Fail-open: without the helper the run is triaged as before.
PROVIDER_OUTAGE_PY="$(dirname "${HEAL_PY}")/provider_outage.py"
# Only stable-release / promotion runs (RELEASE_WORKFLOW_NAMES) go to the
# tracker; CI and scheduled-check runs keep the normal triage path.
OUTAGE_RELEASE_RUN="$(PYTHONDONTWRITEBYTECODE=1 python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import workflow_failure_heal as h; print("true" if sys.argv[2] in h.RELEASE_WORKFLOW_NAMES else "false")' "$(dirname "${HEAL_PY}")" "${PAYLOAD_WORKFLOW_NAME:-}" 2>/dev/null || echo false)"
if [ "${SOURCE_KIND}" = "workflow_run" ] && [ "${SOURCE_REPO}" = "${SELF_REPO}" ] && [ "${OUTAGE_RELEASE_RUN}" = "true" ] && [ "${#LOG_FILES[@]}" -gt 0 ] && [ -f "${PROVIDER_OUTAGE_PY}" ]; then
	OUTAGE_ARGS=()
	for f in "${LOG_FILES[@]}"; do
		OUTAGE_ARGS+=(--log-file "${f}")
	done
	OUTAGE_CLASS="$(PYTHONDONTWRITEBYTECODE=1 python3 "${PROVIDER_OUTAGE_PY}" classify "${OUTAGE_ARGS[@]}" 2>/dev/null | head -n 1 || true)"
	if [[ "${OUTAGE_CLASS}" == reason=provider_unavailable* ]]; then
		OUTAGE_STATUS="$(printf '%s\n' "${OUTAGE_CLASS}" | sed -n 's/.* status=\([a-z0-9_]*\).*/\1/p')"
		OUTAGE_RUN_ID="$(jq -r '.run_refs[0].run_id // ""' "${PAYLOAD_FILE}" 2>/dev/null || true)"
		OUTAGE_WORKFLOW="$(printf '%s' "${PAYLOAD_WORKFLOW_NAME:-unknown}" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-80)"
		OUTAGE_ALERT_FILE="$(mktemp)"
		OUTAGE_OPEN="$(PYTHONDONTWRITEBYTECODE=1 python3 "${PROVIDER_OUTAGE_PY}" open --repo "${SELF_REPO}" --kind outage --provider openrouter \
			--status "${OUTAGE_STATUS:-unknown}" --release-run "${OUTAGE_RUN_ID}" --release-workflow "${OUTAGE_WORKFLOW}" --alert-out "${OUTAGE_ALERT_FILE}" 2>/dev/null | tail -n 1 || true)"
		if [ -s "${OUTAGE_ALERT_FILE}" ]; then
			tg_send_msg "$(cat "${OUTAGE_ALERT_FILE}")" "CRITICAL" >/dev/null 2>&1 || true
		fi
		rm -f -- "${OUTAGE_ALERT_FILE}"
		log "skip reason=provider_unavailable workflow=${OUTAGE_WORKFLOW} run=${OUTAGE_RUN_ID:-none} status=${OUTAGE_STATUS:-unknown} ${OUTAGE_OPEN}"
		exit 0
	fi
fi

# --- Fingerprint -------------------------------------------------------------

if [ "${SOURCE_KIND}" = "engine_fallback" ]; then
	# A refused AI engine fallback (plan item 3e): one heal issue per engine
	# role and reason, across runs, workflows and repos. The values were
	# validated by validate-payload; no log was read. These three inputs give
	# the same hash as `workflow_failure_heal.py engine-fallback-fingerprint`.
	ENGINE_ROLE="$(_pf '.engine_role // ""')"
	ENGINE_REASON="$(_pf '.engine_reason // ""')"
	FIRST_WORKFLOW_NAME="ai-engine-fallback"
	FIRST_FAILING_STEP="${ENGINE_ROLE}"
	SIGNATURE="${ENGINE_REASON}"
elif [ "${SOURCE_KIND}" = "autofix_failure" ]; then
	# The review job's log ends the same way for every failure class
	# ("Process completed with exit code 1"), so the reporter's own reason and
	# evidence identify the failure; the job logs still go to the model below.
	[ -n "${PAYLOAD_WORKFLOW_NAME}" ] && FIRST_WORKFLOW_NAME="${PAYLOAD_WORKFLOW_NAME}"
	FIRST_FAILING_STEP="autofix:${FAILURE_REASON:-unknown}"
	# The reporter's own header lines are not evidence: its `flags:` line
	# matched the signature patterns on every report, so unrelated failures
	# shared one fingerprint and lineage (PR #5892 escalated at generation 4).
	# An identical_failure_cap report's evidence is the gate's marker data,
	# which matches no signature pattern, so every cap report would share
	# `no-error-lines`; its validated failure_fingerprint (the repeated
	# failure's review-side fp) names the failure instead.
	if [ "${FAILURE_REASON}" = "identical_failure_cap" ] && [[ "${PAYLOAD_FAILURE_FINGERPRINT}" =~ ^[0-9a-f]{64}$ ]]; then
		SIGNATURE="autofix-fp:${PAYLOAD_FAILURE_FINGERPRINT}"
	elif [ -s "${FAILURE_EVIDENCE_FILE}" ]; then
		SIGNATURE="$(python3 "${HEAL_PY}" error-signature --strip-autofix-header --log-file "${FAILURE_EVIDENCE_FILE}" 2>/dev/null || echo "no-error-lines")"
	else
		SIGNATURE="autofix:${FAILURE_REASON:-unknown}"
	fi
# An issue / PR report whose linked runs' logs were all unreadable carries only
# "(job log unavailable)" placeholders, whose `no-error-lines` signature is the
# same for every item; key it on the escalated item like a run-less report.
elif [ "${#LOG_FILES[@]}" -gt 0 ] && { [ "${READ_JOB_LOG_COUNT}" -gt 0 ] \
	|| { [ "${SOURCE_KIND}" != "issue" ] && [ "${SOURCE_KIND}" != "pull_request" ]; }; }; then
	SIG_ARGS=()
	for f in "${LOG_FILES[@]}"; do
		SIG_ARGS+=(--log-file "${f}")
	done
	SIGNATURE="$(python3 "${HEAL_PY}" error-signature "${SIG_ARGS[@]}" 2>/dev/null || echo "no-error-lines")"
elif [ "${SOURCE_KIND}" = "phase_failure" ]; then
	# The failed run's logs could not be read: key on the phase instead of
	# the (absent) escalation label.
	SIGNATURE="phase:${FAILURE_REASON:-unknown}"
else
	# No run (or no readable run log) to read: key on the escalated item as well as the label. A bare
	# `label:<label>` signature gave every run-less report of that label, in
	# every repository, one fingerprint and one lineage, so three unrelated
	# harness fixes capped binance-blessings#249's first report at
	# generation 4.
	SIGNATURE="label:${LABEL:-unknown}|source:${SOURCE_REPO}#${ISSUE_NUMBER:-none}"
fi
FP="$(python3 "${HEAL_PY}" fingerprint --workflow-name "${FIRST_WORKFLOW_NAME}" --failing-step "${FIRST_FAILING_STEP}" --signature "${SIGNATURE}")"
log "fingerprint fp=${FP} workflow=${FIRST_WORKFLOW_NAME} step=${FIRST_FAILING_STEP:-none} runs=${SUMMARY_COUNT}"
if [ "${REPORT_AUTH}" = "reconstructed" ] && [ "${FP}" != "${RECOVERY_SCAN_EXPECTED_FP}" ]; then
	log "heal_scope_recovery outcome=skip reason=fingerprint_mismatch issue=${RECOVERY_SCAN_EXPECTED_ISSUE} fp=${FP} expected_fp=${RECOVERY_SCAN_EXPECTED_FP}"
	exit 0
fi

# --- Dedup / lineage / budget ----------------------------------------------

# A deferred non-retryable report (#7333) whose heal-issue list cannot be read
# skips recovery (the hourly recovery scan retries it) and ends with today's
# PR-side outcome through the EXIT trap, instead of a red run.
_heal_issue_list_failed_deferred_exit()
{
	[ "${NON_RETRYABLE_RECOVERY_DEFERRED}" = "true" ] || return 0
	log "heal_scope_recovery outcome=skip reason=heal_issue_list_failed source=${SOURCE_LABEL} fp=${FP}"
	exit 0
}

SELF_ISSUES_FILE="${RUNTIME_DIR}/heal_issues_self.json"
SOURCE_ISSUES_FILE="${RUNTIME_DIR}/heal_issues_source.json"
ISSUES_FILE="${RUNTIME_DIR}/heal_issues.json"
if ! _heal_fetch_issue_list "${SELF_REPO}" "${SELF_ISSUES_FILE}"; then
	_heal_issue_list_failed_deferred_exit
	log "error heal_issue_list_failed"
	tg_send_msg "Workflow failure heal intake could not list ${HEAL_LABEL} issues; report from ${SOURCE_LABEL} not processed."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
	exit 1
fi
# The upstream list cannot see consumer-owned heal issues. Fetch the source
# repository once so deduplication, lineage, and volume budgets cover both
# repositories that can own the issue produced by this intake.
if [ "${SOURCE_REPO}" != "${SELF_REPO}" ]; then
	if ! _heal_fetch_issue_list "${SOURCE_REPO}" "${SOURCE_ISSUES_FILE}"; then
		_heal_issue_list_failed_deferred_exit
		log "error heal_issue_list_failed repo=${SOURCE_REPO}"
		tg_send_msg "Workflow failure heal intake could not list ${HEAL_LABEL} issues in ${SOURCE_REPO}; report from ${SOURCE_LABEL} not processed."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
		exit 1
	fi
else
	printf '[]' > "${SOURCE_ISSUES_FILE}"
fi
jq -s 'add // []' "${SELF_ISSUES_FILE}" "${SOURCE_ISSUES_FILE}" > "${ISSUES_FILE}"
jq -e 'type == "array"' "${ISSUES_FILE}" >/dev/null 2>&1 || printf '[]' > "${ISSUES_FILE}"

HEAL_TRUSTED_AUTHOR=""
if [[ "${SOURCE_GEN}" =~ ^[0-9]+$ ]]; then
	HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-}"
	if [ -z "${HEAL_TRUSTED_AUTHOR}" ]; then
		# §14 audit: heal issue lists identify issue authors, not the token's
		# account; the only other /user read is for self-repo phase reports.
		HEAL_TRUSTED_AUTHOR="$(gh_retry gh api --method GET user --jq '.login // empty' 2>/dev/null || true)"
	fi
	if [ -z "${HEAL_TRUSTED_AUTHOR}" ]; then
		log "warn heal_identity_unavailable source=${SOURCE_LABEL}"
	fi
fi
BUDGET_ARGS=(--issues-json "${ISSUES_FILE}" --fingerprint "${FP}" --preferred-repo "${SOURCE_REPO}" --max-depth "${MAX_DEPTH}" --max-open "${MAX_OPEN}" --max-per-day "${MAX_PER_DAY}")
if [[ "${SOURCE_GEN}" =~ ^[0-9]+$ ]]; then
	BUDGET_ARGS+=(--source-gen "${SOURCE_GEN}" --source-root "${SOURCE_ROOT}")
	if [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]]; then
		BUDGET_ARGS+=(--source-issue "${SOURCE_REPO}#${ISSUE_NUMBER}" --trusted-author "${HEAL_TRUSTED_AUTHOR}")
	fi
fi
# A review/autofix report is keyed on its pull request as well as its
# fingerprint: the evidence (and so the fingerprint) differs run to run, which
# let one PR open a new heal issue on every failed run. The PR's head branch
# links the heal issue it fixes (ai/issue-<N>), so a heal fix PR whose own
# review fails continues that issue's lineage and reaches the cap. A
# clarify / plan / implement report is keyed on its issue the same way (it has
# no head branch; a heal issue's own failed run carries its gen / root markers
# as source_gen / source_root instead).
if { [ "${SOURCE_KIND}" = "autofix_failure" ] || [ "${SOURCE_KIND}" = "phase_failure" ]; } && [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]]; then
	BUDGET_ARGS+=(--source-key "${SOURCE_REPO}#${ISSUE_NUMBER}" --source-head-branch "${HEAD_BRANCH}")
fi
DECISION_FILE="${RUNTIME_DIR}/decision.json"
python3 "${HEAL_PY}" budget "${BUDGET_ARGS[@]}" > "${DECISION_FILE}"

# --- Re-issue lineage (Q147) -------------------------------------------------
# The unblock judge re-issues a blocked heal issue as an ordinary, unlabelled
# issue (no heal markers) and closes the original, so the list above cannot
# see that the lineage is still being worked on: a later report from the same
# failure opened a new generation (#7082 next to #7059). When the budget found
# a closed heal issue of this lineage and no open duplicate, look for an open
# re-issue of it and record the report there instead.
# §14 audit: no call above returns issues outside the ai:workflow-heal label,
# so this adds one search read per repository that can own the heal issue (at
# most two), and only for a report whose lineage has a closed heal issue. A
# failed read keeps the first decision (fail open: today's behaviour).
_heal_reissue_lookup()
{
	[ "${REISSUE_DEDUPE_ENABLED}" = "true" ] || return 0
	# A recovery replay must match its own issue; it never looks elsewhere.
	if [ "${REPORT_AUTH}" = "reconstructed" ]; then
		return 0
	fi
	[ "$(jq -r '.action // ""' "${DECISION_FILE}")" != "duplicate" ] || return 0
	[ "$(jq -r '.lineage_source // "none"' "${DECISION_FILE}")" != "none" ] || return 0
	local reissues_file="${RUNTIME_DIR}/heal_reissues.json" reissue_decision="${RUNTIME_DIR}/decision_reissue.json"
	local reissue_repo reissue_part reissue_index=0 reissue_repos=("${SELF_REPO}")
	[ "${SOURCE_REPO}" = "${SELF_REPO}" ] || reissue_repos+=("${SOURCE_REPO}")
	local reissue_parts=()
	for reissue_repo in "${reissue_repos[@]}"; do
		reissue_part="${RUNTIME_DIR}/heal_reissues_${reissue_index}.json"
		reissue_index=$((reissue_index + 1))
		if ! gh_retry gh api --method GET search/issues -f q="repo:${reissue_repo} is:issue in:body \"Re-issued by the unblock judge from\"" \
			-f sort=created -f order=desc -f per_page=100 \
			--jq '[.items[]? | {number, state, title, body: (.body // ""), author: (.user.login // ""), html_url, pull_request: (.pull_request != null)}]' 2>/dev/null \
			| jq --arg repository "${reissue_repo}" 'map(. + {repository: $repository})' > "${reissue_part}" 2>/dev/null \
			|| ! jq -e 'type == "array"' "${reissue_part}" >/dev/null 2>&1; then
			log "reissue_lookup outcome=skip reason=search_failed repo=${reissue_repo} fp=${FP}"
			return 0
		fi
		reissue_parts+=("${reissue_part}")
	done
	jq -s 'add // []' "${reissue_parts[@]}" > "${reissues_file}"
	if ! python3 "${HEAL_PY}" budget "${BUDGET_ARGS[@]}" --reissues-json "${reissues_file}" > "${reissue_decision}" 2>/dev/null \
		|| ! jq -e '.action | type == "string"' "${reissue_decision}" >/dev/null 2>&1; then
		log "reissue_lookup outcome=skip reason=budget_failed fp=${FP}"
		return 0
	fi
	if [ "$(jq -r '.match // ""' "${reissue_decision}")" != "reissue" ]; then
		log "reissue_lookup outcome=none candidates=$(jq 'length' "${reissues_file}") fp=${FP}"
		return 0
	fi
	mv "${reissue_decision}" "${DECISION_FILE}"
	log "reissue_lookup outcome=found existing_issue=$(jq -r '.existing_issue' "${DECISION_FILE}") existing_repo=$(jq -r '.existing_repo // ""' "${DECISION_FILE}") reissue_of=$(jq -r '.reissue_of' "${DECISION_FILE}") fp=${FP}"
}
_heal_reissue_lookup

ACTION="$(jq -r '.action' "${DECISION_FILE}")"
GEN="$(jq -r '.gen // 1' "${DECISION_FILE}")"
ROOT="$(jq -r '.root // ""' "${DECISION_FILE}")"
[ -n "${ROOT}" ] || ROOT="${FP}"
LINEAGE_IGNORED_REASON="$(jq -r '.lineage_ignored_reason // "none"' "${DECISION_FILE}")"
log "lineage source=$(jq -r '.lineage_source // "none"' "${DECISION_FILE}") reason=${LINEAGE_IGNORED_REASON} source_gen=${SOURCE_GEN:-none} gen=${GEN}"
if [ "${REPORT_AUTH}" = "reconstructed" ] && { [ "${ACTION}" != "duplicate" ] \
	|| [ "$(jq -r '.existing_issue // ""' "${DECISION_FILE}")" != "${RECOVERY_SCAN_EXPECTED_ISSUE}" ] \
	|| [ "$(jq -r --arg self "${SELF_REPO}" '(.existing_repo // "") | if . == "" then $self else . end' "${DECISION_FILE}")" != "${SELF_REPO}" ]; }; then
	log "heal_scope_recovery outcome=skip reason=decision_mismatch issue=${RECOVERY_SCAN_EXPECTED_ISSUE} action=${ACTION} fp=${FP}"
	exit 0
fi
case "${LINEAGE_IGNORED_REASON}" in
	untrusted_author|not_heal_issue|gen_mismatch|root_mismatch)
		tg_send_msg "Workflow failure heal ignored an unverified lineage marker from ${SOURCE_LABEL} (${LINEAGE_IGNORED_REASON}). Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
		;;
esac

ensure_label_exists "${HEAL_LABEL}" "${SELF_REPO}" || true
ensure_label_exists "${ESCALATED_LABEL}" "${SELF_REPO}" || true

# --- Heal-scope recovery (#7191) --------------------------------------------
# A pipeline-authored heal issue filed without an ai:workflow-heal-scope:v1
# marker (#7082) is refused by the implement preflight, and retrying cannot
# make its body verifiable. With HEAL_SCOPE_RECOVERY_ENABLED on (the default),
# a report whose OIDC identity verified may replace it: a duplicate decision is
# turned into an open, so the new issue goes through the normal scope freeze,
# diagnosis, scope render and the scope guard, which runs its unchanged
# `heal-scope verify` check for this path even while
# HEAL_INTAKE_SCOPE_GUARD_ENABLED is off (#7323). The
# original body is never an input; its markers are left out of the lineage by
# re-running the budget without it. Once the replacement exists, the original
# gets one recovery-marker comment and is closed as "not planned".
# §14 audit: the heal issue list carries no comments and no token identity, so
# this path adds at most one /user read (when no earlier /user result exists)
# and one paginated comments read, the latter only after the local
# scope-status and issue-author checks passed.
HEAL_SCOPE_RECOVERY_ACTIVE="false"
HEAL_SCOPE_RECOVERY_ISSUE=""
HEAL_SCOPE_RECOVERY_REPO=""
HEAL_SCOPE_RECOVERY_URL=""

_heal_scope_recovery_evaluate()
{
	[ "${SCOPE_RECOVERY_ENABLED,,}" = "true" ] || return 0
	[ "${ACTION}" = "duplicate" ] || return 0
	# A re-issue is not a heal issue; it has no scope marker to recover (Q147).
	[ "$(jq -r '.match // ""' "${DECISION_FILE}")" != "reissue" ] || return 0
	local recovery_issue recovery_repo recovery_url recovery_login
	local recovery_record="${RUNTIME_DIR}/heal_scope_recovery_issue.json"
	local recovery_comments="${RUNTIME_DIR}/heal_scope_recovery_comments.json"
	local recovery_check="${RUNTIME_DIR}/heal_scope_recovery_check.json"
	local recovery_issues="${RUNTIME_DIR}/heal_issues_recovery.json"
	local recovery_decision="${RUNTIME_DIR}/decision_recovery.json"
	local recovery_reason recovery_action recovery_replaced recovery_replaced_repo recovery_close
	recovery_issue="$(jq -r '.existing_issue // ""' "${DECISION_FILE}")"
	recovery_repo="$(jq -r '.existing_repo // ""' "${DECISION_FILE}")"
	[ -n "${recovery_repo}" ] || recovery_repo="${SELF_REPO}"
	recovery_url="$(jq -r '.existing_url // ""' "${DECISION_FILE}")"
	if [ "${REPORT_AUTH}" != "verified" ] && [ "${REPORT_AUTH}" != "reconstructed" ]; then
		log "heal_scope_recovery outcome=skip reason=report_unauthenticated report_auth=${REPORT_AUTH:-none} issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	if ! [[ "${recovery_issue}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${recovery_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		log "heal_scope_recovery outcome=skip reason=issue_invalid fp=${FP}"
		return 0
	fi
	# A heal issue whose own phase run failed keeps the existing
	# heal_issue_failed_itself escalation below (assumption: only phase reports
	# have that path, so only they are excluded here).
	if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${recovery_issue}" = "${ISSUE_NUMBER:-}" ] && [ "${recovery_repo}" = "${SOURCE_REPO}" ]; then
		log "heal_scope_recovery outcome=skip reason=own_issue_report issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	if ! jq -e --argjson n "${recovery_issue}" --arg r "${recovery_repo}" \
		'[.[] | select(.number == $n and (.repository // "") == $r)] | first | type == "object"' "${ISSUES_FILE}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=issue_record_missing issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	jq --argjson n "${recovery_issue}" --arg r "${recovery_repo}" \
		'[.[] | select(.number == $n and (.repository // "") == $r)] | first' "${ISSUES_FILE}" > "${recovery_record}"
	# Every candidate below is a /user result for the intake's own token.
	recovery_login="${PROVENANCE_LOGIN:-}"
	[ -n "${recovery_login}" ] || recovery_login="${HEAL_TRUSTED_AUTHOR:-}"
	if [ -z "${recovery_login}" ]; then
		recovery_login="$(gh_retry gh api --method GET user --jq '.login // empty' 2>/dev/null || true)"
	fi
	if [ -z "${recovery_login}" ]; then
		log "heal_scope_recovery outcome=skip reason=identity_unavailable issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	# Local pre-check (scope status, issue author) before the comments read.
	if ! python3 "${HEAL_PY}" heal-scope-recovery check --issue-json "${recovery_record}" \
		--pipeline-login "${recovery_login}" > "${recovery_check}" 2>/dev/null; then
		log "heal_scope_recovery outcome=skip reason=check_failed issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	if [ "$(jq -r '.eligible // false' "${recovery_check}")" != "true" ]; then
		recovery_reason="$(jq -r '.reason // "unknown"' "${recovery_check}" | tr -cd 'a-z_' | head -c 40)"
		log "heal_scope_recovery outcome=skip reason=${recovery_reason:-unknown} issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	if ! gh_retry gh api --method GET --paginate "repos/${recovery_repo}/issues/${recovery_issue}/comments" -F per_page=100 \
		--jq '.[] | {id, body: (.body // ""), user: (.user // {})}' 2>/dev/null \
		| jq -s '.' > "${recovery_comments}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${recovery_comments}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=comments_unavailable issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	if ! python3 "${HEAL_PY}" heal-scope-recovery check --issue-json "${recovery_record}" \
		--comments-json "${recovery_comments}" --pipeline-login "${recovery_login}" > "${recovery_check}" 2>/dev/null; then
		log "heal_scope_recovery outcome=skip reason=check_failed issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	recovery_reason="$(jq -r '.reason // "unknown"' "${recovery_check}" | tr -cd 'a-z_' | head -c 40)"
	if [ "${recovery_reason}" = "already_replaced" ]; then
		recovery_replaced="$(jq -r '.replaced_by // ""' "${recovery_check}")"
		recovery_replaced_repo="$(jq -r '.replaced_repo // ""' "${recovery_check}")"
		if [[ "${recovery_replaced}" =~ ^[1-9][0-9]*$ ]] && [[ "${recovery_replaced_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
			# Record this report on the replacement, and retry the close a
			# failed finalize left open.
			jq --argjson n "${recovery_replaced}" --arg r "${recovery_replaced_repo}" --argjson from "${recovery_issue}" --arg from_repo "${recovery_repo}" \
				'.existing_issue = $n | .existing_repo = $r | .existing_url = ("https://github.com/" + $r + "/issues/" + ($n | tostring)) | .redirected_from = {issue: $from, repo: $from_repo}' \
				"${DECISION_FILE}" > "${DECISION_FILE}.recovery" && mv "${DECISION_FILE}.recovery" "${DECISION_FILE}"
			recovery_close="failed"
			if gh_retry gh issue close "${recovery_issue}" --repo "${recovery_repo}" --reason "not planned" >/dev/null 2>&1; then
				recovery_close="done"
			fi
			log "heal_scope_recovery outcome=redirect issue=${recovery_issue} repo=${recovery_repo} replaced_by=${recovery_replaced} replaced_repo=${recovery_replaced_repo} close=${recovery_close} fp=${FP}"
			if [ "${recovery_close}" != "done" ]; then
				tg_send_msg "Workflow failure heal could not close ${recovery_url:-https://github.com/${recovery_repo}/issues/${recovery_issue}}, already replaced by https://github.com/${recovery_replaced_repo}/issues/${recovery_replaced}. Close it by hand if it is still open."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
			fi
		else
			log "heal_scope_recovery outcome=skip reason=marker_invalid issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		fi
		return 0
	fi
	if [ "$(jq -r '.eligible // false' "${recovery_check}")" != "true" ]; then
		log "heal_scope_recovery outcome=skip reason=${recovery_reason:-unknown} issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	# Re-run the budget without the original, so its unverified markers add
	# nothing to the lineage and every cap (including an already open
	# replacement for this fingerprint) still applies.
	jq --argjson n "${recovery_issue}" --arg r "${recovery_repo}" \
		'[.[] | select((.number == $n and (.repository // "") == $r) | not)]' "${ISSUES_FILE}" > "${recovery_issues}"
	local recovery_budget_args=() recovery_arg_index=0
	while [ "${recovery_arg_index}" -lt "${#BUDGET_ARGS[@]}" ]; do
		if [ "${BUDGET_ARGS[${recovery_arg_index}]}" = "--issues-json" ]; then
			recovery_budget_args+=(--issues-json "${recovery_issues}")
			recovery_arg_index=$((recovery_arg_index + 2))
			continue
		fi
		recovery_budget_args+=("${BUDGET_ARGS[${recovery_arg_index}]}")
		recovery_arg_index=$((recovery_arg_index + 1))
	done
	if ! python3 "${HEAL_PY}" budget "${recovery_budget_args[@]}" > "${recovery_decision}" 2>/dev/null; then
		log "heal_scope_recovery outcome=skip reason=budget_failed issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	recovery_action="$(jq -r '.action // "unknown"' "${recovery_decision}" | tr -cd 'a-z_' | head -c 40)"
	if [ "${recovery_action}" != "open" ]; then
		log "heal_scope_recovery outcome=skip reason=budget_${recovery_action:-unknown} issue=${recovery_issue} repo=${recovery_repo} fp=${FP}"
		return 0
	fi
	DECISION_FILE="${recovery_decision}"
	ACTION="open"
	GEN="$(jq -r '.gen // 1' "${DECISION_FILE}")"
	ROOT="$(jq -r '.root // ""' "${DECISION_FILE}")"
	[ -n "${ROOT}" ] || ROOT="${FP}"
	HEAL_SCOPE_RECOVERY_ACTIVE="true"
	HEAL_SCOPE_RECOVERY_ISSUE="${recovery_issue}"
	HEAL_SCOPE_RECOVERY_REPO="${recovery_repo}"
	HEAL_SCOPE_RECOVERY_URL="${recovery_url:-https://github.com/${recovery_repo}/issues/${recovery_issue}}"
	log "heal_scope_recovery outcome=eligible issue=${recovery_issue} repo=${recovery_repo} fp=${FP} gen=${GEN}"
	return 0
}

_heal_scope_recovery_evaluate

if [ "${NON_RETRYABLE_RECOVERY_DEFERRED}" = "true" ] && [ "${HEAL_SCOPE_RECOVERY_ACTIVE}" != "true" ]; then
	# No eligible markerless heal issue: the EXIT trap applies today's PR-side
	# outcome; no occurrence, escalation or heal issue (#7333).
	log "heal_scope_recovery outcome=skip reason=not_eligible_non_retryable source=${SOURCE_LABEL} action=${ACTION} fp=${FP}"
	exit 0
fi
if [ "${REPORT_AUTH}" = "reconstructed" ] && { [ "${HEAL_SCOPE_RECOVERY_ACTIVE}" != "true" ] \
	|| [ "${HEAL_SCOPE_RECOVERY_ISSUE}" != "${RECOVERY_SCAN_EXPECTED_ISSUE}" ] || [ "${HEAL_SCOPE_RECOVERY_REPO}" != "${SELF_REPO}" ]; }; then
	# The scan records no occurrence: only a recovery may follow a replay.
	log "heal_scope_recovery outcome=skip reason=not_recovered issue=${RECOVERY_SCAN_EXPECTED_ISSUE} fp=${FP}"
	exit 0
fi

case "${ACTION}" in
	duplicate)
		EXISTING="$(jq -r '.existing_issue' "${DECISION_FILE}")"
		EXISTING_URL="$(jq -r '.existing_url // ""' "${DECISION_FILE}")"
		EXISTING_REPO="$(jq -r '.existing_repo // ""' "${DECISION_FILE}")"
		[ -n "${EXISTING_REPO}" ] || EXISTING_REPO="${SELF_REPO}"
		DUPLICATE_MATCH="$(jq -r '.match // "fingerprint"' "${DECISION_FILE}")"
		OCCURRENCE_FILE="${RUNTIME_DIR}/occurrence.md"
		python3 "${HEAL_PY}" compose-occurrence --payload-json "${PAYLOAD_FILE}" --intake-run-url "${RUN_URL}" > "${OCCURRENCE_FILE}"
		if gh_retry gh api "repos/${EXISTING_REPO}/issues/${EXISTING}/comments" -F body=@"${OCCURRENCE_FILE}" >/dev/null 2>&1; then
			log "duplicate existing_issue=${EXISTING} existing_repo=${EXISTING_REPO} fp=${FP} source=${SOURCE_LABEL} match=${DUPLICATE_MATCH}"
		else
			log "warn duplicate_comment_failed existing_issue=${EXISTING} existing_repo=${EXISTING_REPO} fp=${FP}"
		fi
		if [ "${SOURCE_KIND}" = "phase_failure" ] && [ "${DUPLICATE_MATCH}" = "fingerprint" ] && [ "${EXISTING}" = "${ISSUE_NUMBER}" ] && [ "${EXISTING_REPO}" = "${SOURCE_REPO}" ]; then
			# The heal issue's own clarify / plan / implement run failed the way
			# the issue was filed for: the pipeline cannot run the fix, and every
			# retry would only add another occurrence here. Hand it to a human.
			ensure_label_exists "${ESCALATED_LABEL}" "${EXISTING_REPO}" || true
			gh_retry gh issue edit "${EXISTING}" --repo "${EXISTING_REPO}" --add-label "${ESCALATED_LABEL}" >/dev/null 2>&1 || log "warn escalation_label_failed issue=${EXISTING} repo=${EXISTING_REPO}"
			log "escalate reason=heal_issue_failed_itself issue=${EXISTING} repo=${EXISTING_REPO} fp=${FP} failure=${FAILURE_REASON}"
			tg_send_msg "Workflow failure heal: heal issue ${EXISTING_URL:-${EXISTING_REPO}#${EXISTING}} failed its own ${FAILURE_REASON%_failed} run with the failure it was filed for, so the pipeline cannot fix it; a human should look at this."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
			exit 0
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
		elif [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]] && [ -n "${SOURCE_REPO}" ]; then
			# No prior heal issue to mark: label the failure report itself so
			# the unblock scan (replace-claude-sessions plan Phase 7) picks the
			# stopped chain up instead of it ending in a Telegram alert only.
			ensure_label_exists "${ESCALATED_LABEL}" "${SOURCE_REPO}" || true
			gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${SOURCE_REPO}" --add-label "${ESCALATED_LABEL}" >/dev/null 2>&1 || log "warn escalation_label_failed issue=${ISSUE_NUMBER} repo=${SOURCE_REPO}"
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

# --- "Is this already fixed?" context ---------------------------------------
#
# Branch progress: one `GET repos/<self>/compare/<failing sha>...<branch>` call.
# CLAUDE.md §15 audit: the jobs, job-log, heal-issue-list and branch-existence
# calls in this script carry no commit history, so none of them can say whether
# the branch moved past the code that failed. Reference per report kind: a
# release run or a review/autofix run in this repo -> its head SHA vs its
# branch; a consumer report -> its wrapper release pin vs the heal target
# branch. Fail open: without a usable response the model is told progress is
# unavailable, and an `already-fixed` answer is then downgraded.
#
# Earlier heals: rendered from ${ISSUES_FILE} (already fetched for the budget
# decision), so no extra API call.

PROGRESS_SHA=""
PROGRESS_BRANCH=""
if [ "${SOURCE_KIND}" = "workflow_run" ] || { [ "${SOURCE_KIND}" = "autofix_failure" ] && [ "${SOURCE_REPO}" = "${SELF_REPO}" ]; }; then
	PROGRESS_SHA="${HEAD_SHA}"
	PROGRESS_BRANCH="${HEAD_BRANCH}"
elif [ -n "${WRAPPER_SHA}" ]; then
	PROGRESS_SHA="${WRAPPER_SHA}"
	PROGRESS_BRANCH="${TARGET_BRANCH_DEFAULT}"
fi
PROGRESS_COMPARE_FILE="${RUNTIME_DIR}/branch_compare.json"
PROGRESS_FILE="${RUNTIME_DIR}/branch_progress.json"
PROGRESS_REASON=""
: > "${PROGRESS_COMPARE_FILE}"
if [ -z "${PROGRESS_SHA}" ] || [ -z "${PROGRESS_BRANCH}" ]; then
	PROGRESS_REASON="no_reference_sha"
elif ! gh_api_json_to_file "${PROGRESS_COMPARE_FILE}" gh api --method GET "repos/${SELF_REPO}/compare/${PROGRESS_SHA}...${PROGRESS_BRANCH}" 2>/dev/null; then
	PROGRESS_REASON="compare_fetch_failed"
fi
if ! python3 "${HEAL_PY}" branch-progress --compare-json "${PROGRESS_COMPARE_FILE}" --failed-sha "${PROGRESS_SHA:-none}" --branch "${PROGRESS_BRANCH:-none}" --reason "${PROGRESS_REASON}" > "${PROGRESS_FILE}" 2>/dev/null \
	|| ! jq -e 'type == "object"' "${PROGRESS_FILE}" >/dev/null 2>&1; then
	printf '{"available": false, "reason": "branch_progress_failed"}\n' > "${PROGRESS_FILE}"
fi
log "branch_progress available=$(jq -r '.available' "${PROGRESS_FILE}") branch=${PROGRESS_BRANCH:-none} sha=${PROGRESS_SHA:-none} ahead_by=$(jq -r '.ahead_by // "none"' "${PROGRESS_FILE}") reason=$(jq -r '.reason // "none"' "${PROGRESS_FILE}")"
# Branch tip checkout (git fetch, no API call) so the model can diff the code
# that failed against the code that runs now; the working directory is the
# intake's own checkout, which is not the progress branch for every report.
HEAL_BRANCH_TIP_DIR="${RUNTIME_DIR}/heal_branch_tip"
HEAL_BRANCH_TIP_NOTE="unavailable"
PROGRESS_TIP_SHA="$(jq -r 'if .available == true and (.ahead_by // 0) > 0 then .tip_sha else "" end' "${PROGRESS_FILE}" 2>/dev/null || echo "")"
if [ -n "${PROGRESS_TIP_SHA}" ] && [ "${SOURCE_CHECKOUT,,}" != "false" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	if _git_fetch_diagnosis_ref "${PROGRESS_TIP_SHA}" \
		&& git worktree add --quiet --detach "${HEAL_BRANCH_TIP_DIR}" FETCH_HEAD >/dev/null 2>&1; then
		HEAL_BRANCH_TIP_NOTE="${HEAL_BRANCH_TIP_DIR} (coding-workflows ${PROGRESS_BRANCH} at ${PROGRESS_TIP_SHA})"
		log "branch_tip_checkout ref=${PROGRESS_TIP_SHA} branch=${PROGRESS_BRANCH} path=${HEAL_BRANCH_TIP_DIR}"
	else
		log "warn branch_tip_checkout_failed ref=${PROGRESS_TIP_SHA} branch=${PROGRESS_BRANCH}"
	fi
fi
LINEAGE_FILE="${RUNTIME_DIR}/heal_lineage.txt"
python3 "${HEAL_PY}" lineage-context --issues-json "${ISSUES_FILE}" --fingerprint "${FP}" --root "${ROOT}" > "${LINEAGE_FILE}" 2>/dev/null \
	|| printf '(earlier heal issues unavailable)\n' > "${LINEAGE_FILE}"

# --- Crash ownership (self-repo review/autofix failures) -------------------

# Who changed the file the review/autofix run crashed in: the PR itself (its
# changed files travel in the report), its base branch relative to main (the
# intake's own `git diff` on two depth-1 fetches, no GitHub API call), or
# neither. The facts go to the model; the routing below only honours a
# self-inflicted token the facts back. Fail open: any git failure -> none.
_git_fetch_ownership_refs()
{
	local ownership_auth_header=""
	if [ -n "${GH_TOKEN:-}" ]; then
		ownership_auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
		GIT_CONFIG_COUNT=1 \
			GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
			GIT_CONFIG_VALUE_0="${ownership_auth_header}" \
		git fetch --quiet --depth "${HEAL_OWNERSHIP_FETCH_DEPTH:-1}" origin "$@" >/dev/null 2>&1
	else
		git fetch --quiet --depth "${HEAL_OWNERSHIP_FETCH_DEPTH:-1}" origin "$@" >/dev/null 2>&1
	fi
}

CRASH_OWNERSHIP="none"
OWNERSHIP_FACTS_FILE=""
PIPELINE_OWNERSHIP_FILES=""
if [ "${SELF_INFLICTED_ROUTING_ENABLED,,}" != "false" ] \
	&& [ "${SOURCE_KIND}" = "autofix_failure" ] \
	&& [ "${SOURCE_REPO}" = "${SELF_REPO}" ] \
	&& [ -n "${PAYLOAD_CRASH_FILE}" ]; then
	BASE_CHANGED_FILES_FILE="${RUNTIME_DIR}/base_changed_files.txt"
	: > "${BASE_CHANGED_FILES_FILE}"
	OWNERSHIP_BASE_NOTE="not compared"
	if [ -z "${PAYLOAD_BASE_BRANCH}" ] || [ "${PAYLOAD_BASE_BRANCH}" = "main" ]; then
		# A PR against main has no base-branch changes to own the crash.
		OWNERSHIP_BASE_NOTE="base is main"
	elif [ "${PAYLOAD_BASE_BRANCH}" = "${TARGET_BRANCH_DEFAULT}" ]; then
		# Never route a heal fix to the stable line through this path.
		OWNERSHIP_BASE_NOTE="base is ${TARGET_BRANCH_DEFAULT}; not compared"
	elif ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		log "warn crash_ownership_base_diff_failed reason=no_git_checkout base=${PAYLOAD_BASE_BRANCH}"
		OWNERSHIP_BASE_NOTE="unavailable (no git checkout)"
	elif _git_fetch_ownership_refs "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/${PAYLOAD_BASE_BRANCH}:refs/remotes/origin/${PAYLOAD_BASE_BRANCH}" \
		&& git diff --name-only "refs/remotes/origin/main" "refs/remotes/origin/${PAYLOAD_BASE_BRANCH}" > "${BASE_CHANGED_FILES_FILE}" 2>/dev/null; then
		OWNERSHIP_BASE_NOTE="$(wc -l < "${BASE_CHANGED_FILES_FILE}" | tr -d ' ') file(s) differ between main and ${PAYLOAD_BASE_BRANCH}"
	else
		log "warn crash_ownership_base_diff_failed reason=fetch_or_diff_failed base=${PAYLOAD_BASE_BRANCH}"
		: > "${BASE_CHANGED_FILES_FILE}"
		OWNERSHIP_BASE_NOTE="unavailable (fetch or diff failed)"
	fi
	CRASH_OWNERSHIP="$(python3 "${HEAL_PY}" classify-crash-ownership --payload-json "${PAYLOAD_FILE}" --base-changed-files "${BASE_CHANGED_FILES_FILE}" 2>/dev/null || echo none)"
	case "${CRASH_OWNERSHIP}" in
		pr|base|none) ;;
		*) CRASH_OWNERSHIP="none" ;;
	esac
	CRASH_FILE_IN_PR_DIFF="no"
	if jq -e --arg f "${PAYLOAD_CRASH_FILE}" '(.changed_files // []) | index($f) != null' "${PAYLOAD_FILE}" >/dev/null 2>&1; then
		CRASH_FILE_IN_PR_DIFF="yes"
	fi
	OWNERSHIP_FACTS_FILE="${RUNTIME_DIR}/ownership_facts.md"
	{
		echo "## Ownership facts"
		echo
		echo "- Crash file: ${PAYLOAD_CRASH_FILE}"
		echo "- Ownership: ${CRASH_OWNERSHIP}"
		echo "- Pull request head: ${HEAD_BRANCH:-unknown} @ ${HEAD_SHA:-unknown}"
		echo "- Pull request base: ${PAYLOAD_BASE_BRANCH:-unknown} (${OWNERSHIP_BASE_NOTE})"
		echo "- Script ref the run staged: ${PAYLOAD_SCRIPT_REF:-unknown}"
		echo "- Crash file in the pull request diff: ${CRASH_FILE_IN_PR_DIFF}"
	} > "${OWNERSHIP_FACTS_FILE}"
	log "crash_ownership=${CRASH_OWNERSHIP} crash_file=${PAYLOAD_CRASH_FILE} base=${PAYLOAD_BASE_BRANCH:-unknown} source=${SOURCE_LABEL}"
elif [ "${SELF_INFLICTED_ROUTING_ENABLED,,}" != "false" ] \
	&& [ "${SOURCE_KIND}" = "autofix_failure" ] \
	&& [ "${SOURCE_REPO}" = "${SELF_REPO}" ]; then
	# No crash file: some self-inflicted failures never name one (PR #4376's
	# sandbox made every model process exit with systemd status 226 before it
	# wrote anything). Second basis: the run staged the PR head's own scripts
	# and the PR changes pipeline files. Only `pr` is derived here; base
	# ownership still needs a crash file. Fail open: helper error -> none.
	PIPELINE_OWNERSHIP_OUTPUT="$(python3 "${HEAL_PY}" classify-pipeline-ownership --payload-json "${PAYLOAD_FILE}" 2>/dev/null || echo none)"
	PIPELINE_OWNERSHIP="$(head -n 1 <<<"${PIPELINE_OWNERSHIP_OUTPUT}")"
	if [ "${PIPELINE_OWNERSHIP}" = "pr" ]; then
		CRASH_OWNERSHIP="pr"
		PIPELINE_OWNERSHIP_FILES="$(printf '%s\n' "${PIPELINE_OWNERSHIP_OUTPUT}" | tail -n +2)"
		OWNERSHIP_FACTS_FILE="${RUNTIME_DIR}/ownership_facts.md"
		{
			echo "## Ownership facts"
			echo
			echo "- Crash file: none (the evidence names no file)"
			echo "- Ownership: pr"
			echo "- Ownership basis: pipeline files (the run staged the pull request head's own scripts, and the pull request changes files the review/autofix pipeline executes)"
			echo "- Pull request head: ${HEAD_BRANCH:-unknown} @ ${HEAD_SHA:-unknown}"
			echo "- Pull request base: ${PAYLOAD_BASE_BRANCH:-unknown}"
			echo "- Script ref the run staged: ${PAYLOAD_SCRIPT_REF:-unknown}"
			echo "- Pipeline files the pull request changes:"
			printf '%s\n' "${PIPELINE_OWNERSHIP_FILES}" | sed 's/^/  - /'
		} > "${OWNERSHIP_FACTS_FILE}"
		log "crash_ownership=pr crash_file=none basis=pipeline_files files=$(printf '%s\n' "${PIPELINE_OWNERSHIP_FILES}" | wc -l | tr -d ' ') base=${PAYLOAD_BASE_BRANCH:-unknown} source=${SOURCE_LABEL}"
	fi
fi

# Freeze the heal-scope inputs before the diagnosis prompt assembles any
# untrusted evidence (#6463): only validated report fields, authenticated run metadata
# and the ownership facts computed above. _open_issue reads this snapshot,
# never the diagnosis, so neither evidence nor a model verdict can widen it.
SCOPE_INPUTS_FROZEN="${RUNTIME_DIR}/scope_inputs_frozen.json"
# Failing tests reported by the verified runs' own job logs (Q37: A): the
# render step resolves them to test files and the subjects they cover at the
# scope commit, so a CI heal may fix the code a test exercises instead of
# being confined to tests/**. Logs are run metadata, not diagnosis output.
FAILING_TESTS_JSON="$(python3 "${HEAL_PY}" heal-scope failing-tests --log-files "${LOG_DIR}"/run-*-job-*.txt 2>/dev/null || echo '{"names":[],"files":[]}')"
jq -e 'type == "object" and (.names | type == "array") and (.files | type == "array")' <<< "${FAILING_TESTS_JSON}" >/dev/null 2>&1 || FAILING_TESTS_JSON='{"names":[],"files":[]}'
# Slurp the verified run records through one jq filter. A failed jq call
# yields `[]` in place of its output: with no *.verified file (a report that
# names no run) jq still prints `[]` for the empty slurp and then exits 2 on
# the unmatched glob, so appending a fallback would leave two JSON documents
# for --argjson and the freeze would always fail.
_heal_slurp_verified_runs()
{
	local slurp_filter="$1" slurped_json=""
	slurped_json="$(jq -s "${slurp_filter}" "${LOG_DIR}"/*.verified 2>/dev/null)" || slurped_json='[]'
	printf '%s\n' "${slurped_json}"
}
jq -n --arg crash "${PAYLOAD_CRASH_FILE}" \
	--argjson failing_tests "${FAILING_TESTS_JSON}" \
	--argjson runs "$(_heal_slurp_verified_runs '[.[] | select(.run_id != null) | "'"${SOURCE_REPO}"'" + ":" + (.run_id | tostring)] | unique')" \
	--argjson self_workflows "$(_heal_slurp_verified_runs '[.[] | .referenced_paths[]?] | unique')" \
	--argjson consumer_workflows "$(_heal_slurp_verified_runs '[.[] | .path | select(type == "string" and . != "")] | unique')" \
	--argjson changed "$(if [ "${CRASH_OWNERSHIP}" = pr ]; then jq '.changed_files // []' "${PAYLOAD_FILE}"; elif [ "${CRASH_OWNERSHIP}" = base ]; then jq -R -s 'split("\n") | map(select(. != ""))' "${BASE_CHANGED_FILES_FILE}"; else echo '[]'; fi)" \
	'{crash_file: $crash, runs: $runs, self_workflows: $self_workflows, consumer_workflows: $consumer_workflows, changed_files: $changed, failing_tests: $failing_tests}' > "${SCOPE_INPUTS_FROZEN}" \
	&& jq -e '(.runs | type == "array") and (.changed_files | type == "array") and (.self_workflows | type == "array") and (.consumer_workflows | type == "array")' "${SCOPE_INPUTS_FROZEN}" >/dev/null 2>&1 \
	&& chmod 0444 "${SCOPE_INPUTS_FROZEN}" || { rm -f "${SCOPE_INPUTS_FROZEN}"; log "warn scope_inputs_freeze_failed"; }

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
	echo "HEAL_BRANCH_TIP_DIR: ${HEAL_BRANCH_TIP_NOTE}"
	echo "Source repository: ${SOURCE_REPO}"
	echo "Report kind: ${SOURCE_KIND}"
	if [ "${SOURCE_KIND}" = "autofix_failure" ]; then
		echo "Failed review/autofix run on pull request #${ISSUE_NUMBER} -- ${ISSUE_TITLE}"
		echo "URL: ${ISSUE_URL}"
		echo "Workflow: ${PAYLOAD_WORKFLOW_NAME}"
		echo "Failure reason: ${FAILURE_REASON}"
		echo "Consecutive failed review runs on this PR: ${FAILURE_STREAK:-1}"
		echo "PR labels: $(_pf '.labels | join(", ")')"
	elif [ "${SOURCE_KIND}" = "phase_failure" ]; then
		echo "Failed ${FAILURE_REASON%_failed} run on issue #${ISSUE_NUMBER} -- ${ISSUE_TITLE}"
		echo "URL: ${ISSUE_URL}"
		echo "Workflow: ${PAYLOAD_WORKFLOW_NAME}"
		echo "Failure reason: ${FAILURE_REASON}"
		echo "Consecutive failed ${FAILURE_REASON%_failed} runs on this issue: ${FAILURE_STREAK:-1}"
		echo "Issue labels: $(_pf '.labels | join(", ")')"
	elif [ "${SOURCE_KIND}" = "engine_fallback" ]; then
		echo "Refused AI engine fallback: role $(_pf '.engine_role') reason $(_pf '.engine_reason') (Claude unavailable for a non-capacity reason; the role failed instead of running codex)"
		echo "Workflow: ${PAYLOAD_WORKFLOW_NAME}"
	elif [ -n "${ISSUE_NUMBER}" ]; then
		echo "Escalated ${SOURCE_KIND}: #${ISSUE_NUMBER} -- ${ISSUE_TITLE}"
		echo "URL: ${ISSUE_URL}"
		echo "Escalation label: ${LABEL}"
		echo "Other labels: $(_pf '.labels | join(", ")')"
		if [ "${LABEL}" = "ai:scope-blocked" ]; then
			# Validated logins and association only (Q81): the automation-path
			# guard grants a pipeline-authored issue without files_touched
			# differently from any other author.
			echo "Issue author: $(_pf '.issue_author_login // "unknown"') (association: $(_pf '.issue_author_association // "unknown"'))"
			echo "Pipeline account (the reporter's identity): $(_pf '.reporter_pipeline_login // "unknown"')"
		fi
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
	if [ "${SOURCE_KIND}" = "autofix_failure" ] || [ "${SOURCE_KIND}" = "engine_fallback" ]; then
		echo "--- Failure evidence from the reporting run (UNTRUSTED) ---"
		cat "${FAILURE_EVIDENCE_FILE}" 2>/dev/null || true
		echo
	fi
	echo "=== BRANCH PROGRESS SINCE THE FAILING SHA ==="
	python3 "${HEAL_PY}" render-branch-progress --summary-json "${PROGRESS_FILE}" 2>/dev/null || echo "(branch progress unavailable)"
	echo
	echo "=== EARLIER HEAL ISSUES FOR THIS FAILURE (UNTRUSTED) ==="
	cat "${LINEAGE_FILE}" 2>/dev/null || true
	echo
	if [ -n "${OWNERSHIP_FACTS_FILE}" ] && [ -s "${OWNERSHIP_FACTS_FILE}" ]; then
		cat "${OWNERSHIP_FACTS_FILE}"
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

# The failed-run logs, issue and comment excerpts are untrusted, so the
# agent runs in the credential-free, network-isolated container
# (scripts/codex_isolated_exec.sh, read-only): beyond the env -u below it
# never holds the OpenRouter key or a checkout's .git. The two source
# worktrees the prompt names are mounted read-only at their own paths.
heal_isolated_args=(run --mode read-only)
for heal_include_dir in "${HEAL_SOURCE_DIR}" "${HEAL_BRANCH_TIP_DIR}"; do
	if [ -d "${heal_include_dir}" ]; then
		heal_isolated_args+=(--include "${heal_include_dir}")
	fi
done
# The WORKFLOW_HEAL role picks its engine with claude_run_selected from this
# script's own (trusted) directory: on codex, or when Claude is unavailable
# (exit 75), it runs the unchanged isolated codex command below; the Claude
# engine runs in the same credential-free container with the source worktrees
# mounted read-only (AI_ENGINE_INCLUDE_PATHS). Without the engine helper the
# codex command runs as before.
heal_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
heal_codex_cmd=(bash "${heal_script_dir}/codex_isolated_exec.sh" "${heal_isolated_args[@]}" --
	--ask-for-approval never
	-c model_verbosity="${MODEL_VERBOSITY:-low}"
	-c include_apply_patch_tool=false
	-c 'shell_environment_policy.ignore_default_excludes=false'
	-c 'shell_environment_policy.filters.OPENROUTER_API_KEY="exclude"'
	exec --skip-git-repo-check
	--model "${MODEL_EDITOR:-openai/gpt-6-sol}"
	--sandbox read-only)
heal_engine_helper="${heal_script_dir}/ai_engine.sh"
heal_include_paths=""
heal_include_newline=$'\n'
for heal_include_dir in "${HEAL_SOURCE_DIR}" "${HEAL_BRANCH_TIP_DIR}"; do
	if [ -d "${heal_include_dir}" ]; then
		heal_include_paths="${heal_include_paths:+${heal_include_paths}${heal_include_newline}}${heal_include_dir}"
	fi
done
heal_engine_ready=false
if [ -f "${heal_engine_helper}" ] && [ ! -L "${heal_engine_helper}" ] \
	&& bash -c 'source "$0" >/dev/null 2>&1 && declare -F claude_run_selected >/dev/null' "${heal_engine_helper}" 2>/dev/null; then
	heal_engine_ready=true
fi
if command -v codex >/dev/null 2>&1; then
	heal_model_rc=0
	if [ "${heal_engine_ready}" = "true" ]; then
		env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID \
			AI_ENGINE_INCLUDE_PATHS="${heal_include_paths}" \
			AI_ENGINE_MODEL_HINT="${MODEL_EDITOR:-}" AI_ENGINE_EFFORT_HINT="${MODEL_REASONING_EFFORT:-}" \
			bash -c 'source "$0"; claude_run_selected "$@"' "${heal_engine_helper}" \
			WORKFLOW_HEAL "${PROMPT_FILE}" "${DIAG_FILE}" "${PWD}" --codex-stdio -- "${heal_codex_cmd[@]}" \
			2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2) || heal_model_rc=$?
	else
		echo "AI_ENGINE_FALLBACK role=WORKFLOW_HEAL reason=engine_support_missing" >&2
		env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID \
			"${heal_codex_cmd[@]}" \
			< "${PROMPT_FILE}" \
			> "${DIAG_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2) || heal_model_rc=$?
	fi
	if [ "${heal_model_rc}" -eq 0 ]; then
		:
	elif [ "${heal_model_rc}" -eq 80 ]; then
		# Claude capacity wait (issue #7370): no codex diagnosis and no issue
		# filed; the run is recorded for the capacity-resume pass to re-run.
		log "skip reason=claude_capacity_wait"
		exit 0
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

# An `already-fixed` answer suppresses the issue, so it has to be checkable:
# the `## Fixed by` section must cite a commit that landed on the branch after
# the failing SHA. Anything less is filed as `inconclusive`, with a note.
ALREADY_FIXED_COMMITS=""
if [ "${CLASSIFICATION}" = "already-fixed" ]; then
	ALREADY_FIXED_CHECK_FILE="${RUNTIME_DIR}/already_fixed_check.json"
	python3 "${HEAL_PY}" check-already-fixed --diagnosis-file "${DIAG_FILE}" --summary-json "${PROGRESS_FILE}" > "${ALREADY_FIXED_CHECK_FILE}" 2>/dev/null \
		|| printf '{"ok": false, "reason": "check_failed", "commits": []}\n' > "${ALREADY_FIXED_CHECK_FILE}"
	if jq -e '.ok == true' "${ALREADY_FIXED_CHECK_FILE}" >/dev/null 2>&1; then
		ALREADY_FIXED_COMMITS="$(jq -r '[.commits[] | .[0:12]] | join(",")' "${ALREADY_FIXED_CHECK_FILE}")"
		log "already_fixed_verified commits=${ALREADY_FIXED_COMMITS} branch=${PROGRESS_BRANCH} source=${SOURCE_LABEL} fp=${FP}"
	else
		ALREADY_FIXED_REJECT_REASON="$(jq -r '.reason // "check_failed"' "${ALREADY_FIXED_CHECK_FILE}" 2>/dev/null || echo check_failed)"
		log "warn already_fixed_unverified reason=${ALREADY_FIXED_REJECT_REASON} source=${SOURCE_LABEL} fp=${FP}; filing as inconclusive"
		CLASSIFICATION="inconclusive"
		{
			echo "> **Heal intake note:** the diagnosis below classified this failure as \`already-fixed\`, but the claim could not be verified (\`${ALREADY_FIXED_REJECT_REASON}\`): it must cite, under \`## Fixed by\`, a commit that landed on the branch after the failing SHA. It was filed as \`inconclusive\` instead. Confirm whether the failure still reproduces on the current branch before planning a fix."
			echo
			cat "${DIAG_FILE}"
		} > "${DIAG_FILE}.tmp" && mv "${DIAG_FILE}.tmp" "${DIAG_FILE}"
	fi
fi

# A deterministic failure (the identical-failure cap tripped, or an earlier
# heal of this lineage merged a fix and it came back) is never `transient`:
# a re-run cannot clear it, and a `transient` verdict opens no issue. File it
# as `inconclusive` with a note instead (PR #4443: six identical failures,
# then a generation-2 heal that only added a retry).
if [ "${CLASSIFICATION}" = "transient" ] \
	&& [ "$(python3 "${HEAL_PY}" is-deterministic --payload-json "${PAYLOAD_FILE}" --gen "${GEN}" 2>/dev/null || echo false)" = "true" ]; then
	log "classification_remapped from=transient to=inconclusive reason=deterministic_failure source=${SOURCE_LABEL} fp=${FP} gen=${GEN}"
	CLASSIFICATION="inconclusive"
	{
		echo "> **Heal intake note:** the diagnosis below classified this failure as \`transient\`, but it is deterministic (the same fingerprint repeated, or it came back after an earlier heal of this lineage). A re-run cannot clear it, so it was filed as \`inconclusive\`. Find the deterministic cause; a retry is not an accepted fix."
		echo
		cat "${DIAG_FILE}"
	} > "${DIAG_FILE}.tmp" && mv "${DIAG_FILE}.tmp" "${DIAG_FILE}"
fi

# A self-inflicted token is routed as such only when the ownership computed
# above backs it (a crash file in the PR or base diff, or, with no crash file,
# pipeline files the PR changes); otherwise (routing disabled, consumer report,
# ownership none or the other side) it takes today's workflow-defect route.
case "${CLASSIFICATION}" in
	pr-self-inflicted|base-self-inflicted)
		SELF_INFLICTED_EXPECTED="pr"
		[ "${CLASSIFICATION}" = "pr-self-inflicted" ] || SELF_INFLICTED_EXPECTED="base"
		SELF_INFLICTED_REMAP_REASON=""
		if [ "${SELF_INFLICTED_ROUTING_ENABLED,,}" = "false" ]; then
			SELF_INFLICTED_REMAP_REASON="routing_disabled"
		elif [ "${CRASH_OWNERSHIP}" != "${SELF_INFLICTED_EXPECTED}" ]; then
			SELF_INFLICTED_REMAP_REASON="ownership_${CRASH_OWNERSHIP}"
		fi
		if [ -n "${SELF_INFLICTED_REMAP_REASON}" ]; then
			log "classification_remapped from=${CLASSIFICATION} to=workflow-defect reason=${SELF_INFLICTED_REMAP_REASON} source=${SOURCE_LABEL} fp=${FP}"
			CLASSIFICATION="workflow-defect"
		fi
		;;
esac

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
	if [ "${REPORT_AUTH}" = "reconstructed" ]; then
		# A recovery-scan replay never posts on the source PR (#7333).
		log "source_comment=skipped_reconstructed source=${SOURCE_LABEL}"
		return 0
	fi
	gh_retry gh api "repos/${SOURCE_REPO}/issues/${ISSUE_NUMBER}/comments" -F body=@"${body_file}" >/dev/null 2>&1 \
		|| log "warn source_comment_failed source=${SOURCE_LABEL}"
}

# Fetch the consumer commit a heal issue will be implemented against (the
# failing head when known, else its default branch) into a private bare repo.
# No credential prompt; any failure leaves the consumer scope unresolved.
_git_fetch_consumer_scope_ref()
{
	local consumer_repo="$1" scope_dir="$2" consumer_ref="HEAD" consumer_auth_header=""
	[[ "${consumer_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
	if [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then consumer_ref="${HEAD_SHA}"; fi
	rm -rf -- "${scope_dir}"
	GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init --bare -q "${scope_dir}" >/dev/null 2>&1 || return 1
	if [ -n "${GH_TOKEN:-}" ]; then
		consumer_auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
	fi
	GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
		GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" GIT_CONFIG_VALUE_0="${consumer_auth_header}" \
		timeout 120 git --git-dir "${scope_dir}" fetch --quiet --depth 1 "https://github.com/${consumer_repo}.git" "${consumer_ref}" >/dev/null 2>&1
}

# Runs only after the replacement issue exists (#7191). Points the original at
# it with a recovery marker comment, then closes the original as "not planned"
# (attempted even when the comment fails, so it stops matching new reports).
# Never fails the intake: the replacement is already filed.
_heal_scope_recovery_finalize()
{
	local finalize_repo="$1" finalize_url="${2%%[[:space:]]*}" finalize_number="" finalize_marker=""
	local finalize_comment="${RUNTIME_DIR}/heal_scope_recovery_comment.md"
	local finalize_comment_state="failed" finalize_close_state="failed"
	if [[ "${finalize_url}" =~ /issues/([1-9][0-9]*)$ ]]; then
		finalize_number="${BASH_REMATCH[1]}"
	fi
	if [ -n "${finalize_number}" ]; then
		finalize_marker="$(python3 "${HEAL_PY}" heal-scope-recovery marker --replaced-by "${finalize_number}" --repo "${finalize_repo}" 2>/dev/null || true)"
	fi
	if [ -n "${finalize_marker}" ]; then
		{
			echo "Workflow failure heal replaced this issue with ${finalize_url}: this issue has no verifiable \`ai:workflow-heal-scope:v1\` marker, so the implement step cannot run it. The replacement was filed from an authenticated failure report with a freshly rendered scope."
			echo
			echo "Heal intake run: ${RUN_URL}"
			echo
			echo "${finalize_marker}"
		} > "${finalize_comment}"
		if gh_retry gh api "repos/${HEAL_SCOPE_RECOVERY_REPO}/issues/${HEAL_SCOPE_RECOVERY_ISSUE}/comments" -F body=@"${finalize_comment}" >/dev/null 2>&1; then
			finalize_comment_state="posted"
		fi
	fi
	if gh_retry gh issue close "${HEAL_SCOPE_RECOVERY_ISSUE}" --repo "${HEAL_SCOPE_RECOVERY_REPO}" --reason "not planned" >/dev/null 2>&1; then
		finalize_close_state="done"
	fi
	log "heal_scope_recovery outcome=replaced issue=${HEAL_SCOPE_RECOVERY_ISSUE} repo=${HEAL_SCOPE_RECOVERY_REPO} replacement=${finalize_number:-unknown} replacement_repo=${finalize_repo} comment=${finalize_comment_state} close=${finalize_close_state} fp=${FP}"
	if [ "${finalize_comment_state}" != "posted" ] || [ "${finalize_close_state}" != "done" ]; then
		tg_send_msg "Workflow failure heal replaced ${HEAL_SCOPE_RECOVERY_URL} (no scope marker) with ${finalize_url}, but could not finish on the original (comment=${finalize_comment_state}, close=${finalize_close_state}). Close it by hand if it is still open."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	fi
	return 0
}

_open_issue()
{
	local repo="$1" target_branch="$2" integration_branch="${3:-}"
	local title_file="${RUNTIME_DIR}/issue_title.txt" body_file="${RUNTIME_DIR}/issue_body.md"
	local label_args=(--label "${HEAL_LABEL}")
	local scope_marker_file="${RUNTIME_DIR}/scope_marker.txt"
	: > "${scope_marker_file}"
	# Scope is derived only from validated report fields and authenticated run
	# metadata. The diagnosis and its files_touched prose are never inputs.
	# This selection is fixed by the validated report, not the diagnosis's
	# classification or target branch: a model verdict cannot expand scope.
	local checkout_ref="${WRAPPER_SHA:-${TARGET_BRANCH_DEFAULT}}"
	if [ "${SOURCE_KIND}" = workflow_run ]; then checkout_ref="${HEAD_SHA:-${TARGET_BRANCH_DEFAULT}}"; fi
	if [ "${SOURCE_KIND}" = autofix_failure ] && [ -n "${PAYLOAD_SCRIPT_REF}" ]; then checkout_ref="${PAYLOAD_SCRIPT_REF}"; fi
	local scope_ref=""
	if [[ "${checkout_ref}" =~ ^[0-9a-f]{40}$ ]]; then
		_git_fetch_ownership_refs "${checkout_ref}" || true
		scope_ref="${checkout_ref}"
	elif [[ "${checkout_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] && git check-ref-format --branch "${checkout_ref}" >/dev/null 2>&1; then
		_git_fetch_ownership_refs "+refs/heads/${checkout_ref}:refs/remotes/origin/${checkout_ref}" || true
		scope_ref="refs/remotes/origin/${checkout_ref}"
	fi
	local scope_checkout="." scope_workflows_key=self_workflows
	if [ "${repo}" != "${SELF_REPO}" ]; then
		# The issue is implemented in the consumer checkout, so its scope must be
		# checked against the consumer's own tree, never coding-workflows'. Only
		# the run's own workflow path applies there (referenced_paths name
		# coding-workflows files). An unreadable consumer tree leaves the scope
		# unresolved, which implement refuses at ai:needs-human (fail closed).
		scope_checkout="${RUNTIME_DIR}/consumer-scope.git"
		scope_workflows_key=consumer_workflows
		scope_ref=""
		if _git_fetch_consumer_scope_ref "${repo}" "${scope_checkout}"; then
			scope_ref="$(git --git-dir "${scope_checkout}" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
		fi
	fi
	# A missing pre-diagnosis snapshot leaves the scope unresolved (fail closed).
	if [ -n "${scope_ref}" ] && [ -s "${SCOPE_INPUTS_FROZEN:-}" ] && git -C "${scope_checkout}" rev-parse --verify "${scope_ref}^{commit}" >/dev/null 2>&1; then
		jq --arg key "${scope_workflows_key}" '{crash_file: .crash_file, runs: .runs, workflow_paths: .[$key], changed_files: .changed_files, failing_tests: (.failing_tests // {names: [], files: []})}' \
			"${SCOPE_INPUTS_FROZEN}" > "${RUNTIME_DIR}/scope_inputs.json"
		python3 "${HEAL_PY}" heal-scope render --input-json "${RUNTIME_DIR}/scope_inputs.json" --checkout "${scope_checkout}" --ref "${scope_ref}" > "${scope_marker_file}" || : > "${scope_marker_file}"
	fi
	log "scope paths=$(grep -o 'paths=[^ ]*' "${scope_marker_file}" | tr ',' '\n' | wc -l | tr -d ' ') runs=${SUMMARY_COUNT} outcome=$([ -s "${scope_marker_file}" ] && grep -q '<!--' "${scope_marker_file}" && echo written || echo unresolved)"
	# Fail closed on an unresolved scope (#7059): an issue the heal implementer
	# must fix is refused by implement preflight without a valid intake-authored
	# marker (#6866 was filed with an empty one). consumer-app-defect issues carry
	# the same heal label and preflight, so they are checked too. Check it with the same parser
	# implement uses before filing; nothing is persisted before this point, so
	# the next report of this fingerprint retries.
	if [ "${SCOPE_MARKER_GUARD_ENABLED}" != "false" ]; then
		case "${CLASSIFICATION}" in
			workflow-defect|inconclusive|base-self-inflicted|consumer-app-defect)
				local scope_check_json="" scope_check_status="unknown" scope_check_rc=0
				scope_check_json="$(python3 "${HEAL_PY}" heal-scope check-marker --marker-file "${scope_marker_file}" 2>/dev/null)" || scope_check_rc=$?
				scope_check_status="$(jq -r '.status // "unknown"' <<< "${scope_check_json}" 2>/dev/null || echo unknown)"
				[[ "${scope_check_status}" =~ ^[a-z_]{1,20}$ ]] || scope_check_status="unknown"
				if [ "${scope_check_rc}" -ne 0 ] || [ "${scope_check_status}" != "valid" ]; then
					[ "${scope_check_status}" != "valid" ] || scope_check_status="unknown"
					if _heal_report_claimed_no_runs; then
						_heal_skip_scope_unavailable marker_guard
					fi
					log "skip reason=scope_marker_unresolved fp=${FP} classification=${CLASSIFICATION} status=${scope_check_status} source=${SOURCE_LABEL}"
					tg_send_msg "Workflow failure heal refused to file an issue without a valid intake-authored scope marker (intake defect, status ${scope_check_status}) for ${SOURCE_LABEL} (classification ${CLASSIFICATION}, fingerprint ${FP})."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
					return 1
				fi
				;;
		esac
	fi
	rm -f "${title_file}" "${body_file}"
	if ! python3 "${HEAL_PY}" compose-issue \
		--payload-json "${PAYLOAD_FILE}" \
		--diagnosis-file "${DIAG_FILE}" \
		--run-summaries-json "${SUMMARIES_FILE}" \
		--fingerprint "${FP}" \
		--gen "${GEN}" \
		--root "${ROOT}" \
		--classification "${CLASSIFICATION}" \
		--target-branch "${target_branch}" \
		--integration-branch "${integration_branch}" \
		--scope-marker-file "${scope_marker_file}" \
		--max-depth "${MAX_DEPTH}" \
		--intake-run-url "${RUN_URL}" \
		--title-out "${title_file}" \
		--body-out "${body_file}"; then
		log "error issue_compose_failed repo=${repo} fp=${FP} source=${SOURCE_LABEL}"
		tg_send_msg "Workflow failure heal FAILED to compose a safe issue in ${repo} for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}')."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
		return 1
	fi
	local title
	title="$(head -1 "${title_file}")"
	local issue_label_names="${HEAL_LABEL}"
	if [[ "${integration_branch}" =~ ^orchestrator/project-[0-9]+$ ]]; then
		# Same lineage convention as the review-blocked judge's
		# merge_with_followup issues: the orchestrator treats it as a child.
		label_args+=(--label "ai:orchestrator-managed")
		issue_label_names="${issue_label_names},ai:orchestrator-managed"
	fi
	# Nothing below this gate may run unless it permits creation (#6871).
	_heal_intake_scope_guard_permits_create "${repo}" "${body_file}" "${issue_label_names}" "${target_branch}" || return 1
	if [ "${repo}" != "${SELF_REPO}" ]; then
		ensure_label_exists "${HEAL_LABEL}" "${repo}" || true
	fi
	if [[ "${integration_branch}" =~ ^orchestrator/project-[0-9]+$ ]]; then
		ensure_label_exists "ai:orchestrator-managed" "${repo}" || true
	fi
	local issue_url
	issue_url="$(gh_retry gh issue create --repo "${repo}" --title "${title}" --body-file "${body_file}" "${label_args[@]}" 2>/dev/null || echo '')"
	if [ -z "${issue_url}" ]; then
		log "error issue_create_failed repo=${repo} fp=${FP} source=${SOURCE_LABEL}"
		tg_send_msg "Workflow failure heal FAILED to open an issue in ${repo} for ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}')."$'\n'"Run: ${RUN_URL}" "CRITICAL" >/dev/null 2>&1 || true
		return 1
	fi
	NEW_ISSUE_URL="${issue_url}"
	if [ "${HEAL_SCOPE_RECOVERY_ACTIVE}" = "true" ]; then
		_heal_scope_recovery_finalize "${repo}" "${issue_url}"
	fi
	return 0
}

NEW_ISSUE_URL=""
case "${CLASSIFICATION}" in
	workflow-defect|inconclusive)
		TARGET_BRANCH="${TARGET_BRANCH_DEFAULT}"
		TARGET_BRANCH_SOURCE="default"
		if [ "${SOURCE_KIND}" = "workflow_run" ] && [ -n "${HEAD_BRANCH}" ]; then
			TARGET_BRANCH="${HEAD_BRANCH}"
			TARGET_BRANCH_SOURCE="failed_run_branch"
		elif [ "${SOURCE_KIND}" = "autofix_failure" ] && [ "${SOURCE_REPO}" = "${SELF_REPO}" ]; then
			# A review/autofix run in this repo normally executes the verified
			# workflow-support commit (main's shared scripts), not the pull
			# request's own copies: PR-head scripts are review data. A defect in
			# that shared code must be fixed on the default branch; aiming the
			# fix at the blocked PR's head branch strands it there (issue #6623
			# was filed against ai/issue-5152 and could not be planned). Only a
			# run that provably staged the PR head's own scripts (script_ref
			# equal to the PR head SHA) routes the fix to that head branch.
			# A verified support ref on stable (or a SHA on main) redirects the
			# fix to that branch (`support_ref`). Otherwise no Target-branch
			# header is written and the issue follows the default branch
			# (`shared_workflow`), instead of guessing a branch from untrusted
			# report prose.
			if [ -n "${HEAD_BRANCH}" ] && [[ "${PAYLOAD_SCRIPT_REF}" =~ ^[0-9a-f]{40}$ ]] && [ "${PAYLOAD_SCRIPT_REF}" = "${HEAD_SHA}" ]; then
				TARGET_BRANCH="${HEAD_BRANCH}"
				TARGET_BRANCH_SOURCE="source_pr_head"
			elif [ "${PAYLOAD_SCRIPT_REF}" = stable ]; then
				TARGET_BRANCH=stable
				TARGET_BRANCH_SOURCE=support_ref
			elif [[ "${PAYLOAD_SCRIPT_REF}" =~ ^[0-9a-f]{40}$ ]] && [ "${PAYLOAD_SCRIPT_REF}" != "${HEAD_SHA}" ] \
				&& HEAL_OWNERSHIP_FETCH_DEPTH=1024 _git_fetch_ownership_refs "+refs/heads/main:refs/remotes/origin/main" \
				&& git merge-base --is-ancestor "${PAYLOAD_SCRIPT_REF}" refs/remotes/origin/main 2>/dev/null; then
				TARGET_BRANCH=main
				TARGET_BRANCH_SOURCE=support_ref
			else
				TARGET_BRANCH=""
				TARGET_BRANCH_SOURCE="shared_workflow"
			fi
		fi
		# A consumer pinned to a release commit that the default heal target
		# (stable) does not contain ran code only the default branch has: this
		# repository's own projects validate on main. A stable hotfix then has
		# nothing to edit and the plan blocks (#6902 -> #7063). The branch-progress
		# compare above (`compare/<pin>...<target>`, no extra API call) already
		# says whether the target contains the pin: `ahead`/`identical` do,
		# `behind`/`diverged` do not. An unavailable compare keeps the target.
		if [ "${TARGET_BRANCH_SOURCE}" = default ] && [ -n "${WRAPPER_SHA}" ] \
			&& [ "${PROGRESS_SHA}" = "${WRAPPER_SHA}" ] && [ "${PROGRESS_BRANCH}" = "${TARGET_BRANCH}" ]; then
			HEAL_PIN_STATUS="$(jq -r 'if .available == true then (.status // "") else "" end' "${PROGRESS_FILE}" 2>/dev/null || true)"
			case "${HEAL_PIN_STATUS}" in
				behind|diverged)
					# The provenance block only resolves the default branch for
					# phase/autofix/workflow_run reports; consumer issue and PR
					# reports skip it, so resolve it here the same way (no API call).
					HEAL_RETARGET_BRANCH="${PROVENANCE_DEFAULT_BRANCH:-${WORKFLOW_HEAL_DEFAULT_BRANCH:-}}"
					if [ -z "${HEAL_RETARGET_BRANCH}" ] && [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -f "${GITHUB_EVENT_PATH}" ] && [ -r "${GITHUB_EVENT_PATH}" ]; then
						HEAL_RETARGET_BRANCH="$(jq -r '.repository.default_branch // ""' "${GITHUB_EVENT_PATH}" 2>/dev/null || true)"
					fi
					HEAL_RETARGET_BRANCH="$(printf '%s' "${HEAL_RETARGET_BRANCH}" | tr -cd 'A-Za-z0-9._/-' | head -c 255)"
					HEAL_RETARGET_BRANCH="${HEAL_RETARGET_BRANCH:-main}"
					log "target_branch_retarget from=${TARGET_BRANCH} to=${HEAL_RETARGET_BRANCH} reason=wrapper_pin_not_on_target pin=${WRAPPER_SHA} compare_status=${HEAL_PIN_STATUS}"
					TARGET_BRANCH="${HEAL_RETARGET_BRANCH}"
					TARGET_BRANCH_SOURCE=wrapper_pin_not_on_target
					;;
			esac
		fi
		if [ -n "${TARGET_BRANCH}" ] && ! _branch_exists "${SELF_REPO}" "${TARGET_BRANCH}"; then
			if [ "${TARGET_BRANCH_SOURCE}" = "source_pr_head" ]; then
				log "warn source_pr_branch_missing branch=${TARGET_BRANCH}; falling back to ${TARGET_BRANCH_DEFAULT}"
				TARGET_BRANCH="${TARGET_BRANCH_DEFAULT}"
				TARGET_BRANCH_SOURCE="default"
				if ! _branch_exists "${SELF_REPO}" "${TARGET_BRANCH}"; then
					log "warn target_branch_missing branch=${TARGET_BRANCH}; issue will target the default branch"
					TARGET_BRANCH=""
				fi
			else
				log "warn target_branch_missing branch=${TARGET_BRANCH}; issue will target the default branch"
				TARGET_BRANCH=""
			fi
		fi
		_open_issue "${SELF_REPO}" "${TARGET_BRANCH}" || exit 1
		log "created issue=${NEW_ISSUE_URL} repo=${SELF_REPO} classification=${CLASSIFICATION} target_branch=${TARGET_BRANCH:-default} fp=${FP} gen=${GEN} root=${ROOT} source=${SOURCE_LABEL} target_branch_source=${TARGET_BRANCH_SOURCE}"
		HOTFIX_NOTE=""
		if [ -n "${TARGET_BRANCH}" ] && [ "${TARGET_BRANCH_SOURCE}" = "source_pr_head" ]; then
			HOTFIX_NOTE=" on this pull request's own branch \`${TARGET_BRANCH}\`"
		elif [ -n "${TARGET_BRANCH}" ]; then
			HOTFIX_NOTE=" as a hotfix on \`${TARGET_BRANCH}\`"
		elif [ "${TARGET_BRANCH_SOURCE}" = "shared_workflow" ]; then
			HOTFIX_NOTE=" on the default branch, because the failing code is the shared workflow support this pull request's review ran, not this pull request's own changes"
		fi
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
	pr-self-inflicted)
		# The crash is in a file this PR changed: the fix belongs in the PR
		# itself, so the diagnosis goes to the PR (for the review-blocked
		# judge and the author) and no heal issue is opened.
		{
			echo "**Workflow failure heal: this failure is caused by this pull request's own changes**"
			echo "<!-- workflow-failure-heal:outcome -->"
			echo
			if [ -n "${PAYLOAD_CRASH_FILE}" ]; then
				echo "The review/autofix run crashed in \`${PAYLOAD_CRASH_FILE}\`, which this pull request changes (classification \`${CLASSIFICATION}\`). Fix it on this branch; no heal issue was opened."
			else
				echo "The review/autofix run executed this pull request's own scripts and failed without naming a file. This pull request changes pipeline files it runs (classification \`${CLASSIFICATION}\`): $(printf '%s\n' "${PIPELINE_OWNERSHIP_FILES}" | sed 's/.*/`&`/' | paste -sd ',' - | sed 's/,/, /g'). Fix it on this branch; no heal issue was opened."
			fi
			echo
			cat "${DIAG_FILE}"
			echo
			echo "Heal intake run: ${RUN_URL}"
		} > "${RUNTIME_DIR}/source_comment.md"
		_comment_on_source "${RUNTIME_DIR}/source_comment.md"
		log "no_issue classification=${CLASSIFICATION} source=${SOURCE_LABEL} fp=${FP} crash_file=${PAYLOAD_CRASH_FILE}"
		tg_send_msg "Workflow failure heal: ${SOURCE_LABEL} fails in ${PAYLOAD_CRASH_FILE:-pipeline files}, which the pull request itself changes; diagnosis posted on the PR, no issue opened."$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}" "DEBUG" >/dev/null 2>&1 || true
		;;
	base-self-inflicted)
		# The crash is in a file the PR's base branch changed relative to main
		# and the PR did not: the fix belongs on that base branch. The branch
		# was just fetched by the ownership check, so it exists.
		_open_issue "${SELF_REPO}" "${PAYLOAD_BASE_BRANCH}" "${PAYLOAD_BASE_BRANCH}" || exit 1
		log "created issue=${NEW_ISSUE_URL} repo=${SELF_REPO} classification=${CLASSIFICATION} target_branch=${PAYLOAD_BASE_BRANCH} fp=${FP} gen=${GEN} root=${ROOT} source=${SOURCE_LABEL} target_branch_source=base_branch crash_file=${PAYLOAD_CRASH_FILE}"
		{
			echo "<!-- workflow-failure-heal:outcome -->"
			echo "Workflow failure heal opened ${NEW_ISSUE_URL} in \`${SELF_REPO}\` (classification \`${CLASSIFICATION}\`, generation ${GEN}/${MAX_DEPTH}). The run crashed in \`${PAYLOAD_CRASH_FILE}\`, which the base branch \`${PAYLOAD_BASE_BRANCH}\` changed and this pull request did not; the fix will ship through the normal pipeline on \`${PAYLOAD_BASE_BRANCH}\`."
		} > "${RUNTIME_DIR}/source_comment.md"
		_comment_on_source "${RUNTIME_DIR}/source_comment.md"
		tg_send_msg "Workflow failure heal opened ${NEW_ISSUE_URL} on base branch ${PAYLOAD_BASE_BRANCH} for ${SOURCE_LABEL} (crash in ${PAYLOAD_CRASH_FILE}, classification ${CLASSIFICATION}, generation ${GEN}/${MAX_DEPTH})." "DEBUG" >/dev/null 2>&1 || true
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
	already-fixed)
		{
			echo "<!-- workflow-failure-heal:outcome -->"
			echo "Workflow failure heal classified this failure as \`already-fixed\`: \`${PROGRESS_BRANCH}\` gained commit(s) ${ALREADY_FIXED_COMMITS//,/, } after the failing SHA \`${PROGRESS_SHA}\`, and the diagnosis attributes the fix to them. No fix issue was opened."
			if [ "${PROGRESS_SHA}" = "${WRAPPER_SHA}" ] && [ -n "${WRAPPER_SHA}" ]; then
				echo
				echo "This repository's workflow wrappers are pinned to the older release; the next wrapper sync to \`${PROGRESS_BRANCH}\` picks up the fix."
			fi
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
		log "no_issue classification=${CLASSIFICATION} fixed_by=${ALREADY_FIXED_COMMITS} branch=${PROGRESS_BRANCH} source=${SOURCE_LABEL} fp=${FP}"
		tg_send_msg "Workflow failure heal: ${SOURCE_LABEL} (workflow '${FIRST_WORKFLOW_NAME}') is already fixed on ${PROGRESS_BRANCH} by ${ALREADY_FIXED_COMMITS}; no issue opened. ${SUMMARY_LINE}"$'\n'"Source: ${ISSUE_URL:-${SOURCE_REPO}}"$'\n'"Run: ${RUN_URL}" "DEBUG" >/dev/null 2>&1 || true
		;;
	*)
		log "error unknown_classification value=${CLASSIFICATION}"
		exit 1
		;;
esac

# Recovery was active but this classification opened no replacement (#7191):
# record the report on the original as the duplicate path would have.
if [ "${HEAL_SCOPE_RECOVERY_ACTIVE}" = "true" ] && [ -z "${NEW_ISSUE_URL}" ]; then
	RECOVERY_OCCURRENCE_FILE="${RUNTIME_DIR}/occurrence_recovery.md"
	RECOVERY_OCCURRENCE_STATE="failed"
	if [ "${REPORT_AUTH}" = "reconstructed" ]; then
		# A scan replay is not a new occurrence (#7333).
		RECOVERY_OCCURRENCE_STATE="skipped_reconstructed"
	elif python3 "${HEAL_PY}" compose-occurrence --payload-json "${PAYLOAD_FILE}" --intake-run-url "${RUN_URL}" > "${RECOVERY_OCCURRENCE_FILE}" \
		&& gh_retry gh api "repos/${HEAL_SCOPE_RECOVERY_REPO}/issues/${HEAL_SCOPE_RECOVERY_ISSUE}/comments" -F body=@"${RECOVERY_OCCURRENCE_FILE}" >/dev/null 2>&1; then
		RECOVERY_OCCURRENCE_STATE="posted"
	fi
	log "heal_scope_recovery outcome=no_replacement reason=classification_$(printf '%s' "${CLASSIFICATION}" | tr -cd 'a-z-' | tr '-' '_') issue=${HEAL_SCOPE_RECOVERY_ISSUE} repo=${HEAL_SCOPE_RECOVERY_REPO} occurrence=${RECOVERY_OCCURRENCE_STATE} fp=${FP}"
fi
