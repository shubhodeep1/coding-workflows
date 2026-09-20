#!/usr/bin/env bash
# apply_analysis_on_main.sh — hand ONE pending analysis doc to the orchestrator.
#
# The unattended equivalent of the interactive `/apply-analysis` command,
# limited to a single source doc per dispatch. Two callers:
#
#   * scripts/promote_main_cycle.sh (the daily promote cycle) dispatches the
#     PROVING run with APPLY_ANALYSIS_ROLE=proving after the smoke gate passed.
#   * The orchestrator poller dispatches the VERIFYING run with
#     APPLY_ANALYSIS_ROLE=verifying once the proving run has merged.
#
# The project is bound at dispatch time: orchestrate.yml's optional
# `tracking_labels` / `tracking_comment` inputs apply the tracking label and
# post the marker comment when the tracking issue is created, so nothing here
# has to guess which (model-titled) tracking issue is its own.
#
# Decision order (every exit is a logged, stable prefix):
#   APPLY_ANALYSIS_SKIPPED reason=disabled                   kill switch
#   APPLY_ANALYSIS_SKIPPED reason=project_in_flight          an open tracking issue
#                                                            still carries the label
#   APPLY_ANALYSIS_SKIPPED reason=orchestrate_run_in_flight  the orchestrator
#                                                            workflow is queued or
#                                                            running (its tracking
#                                                            issue does not exist yet)
#   APPLY_ANALYSIS_SKIPPED reason=no_docs                    nothing under the glob
#   APPLY_ANALYSIS_SKIPPED reason=all_docs_processed         every doc is recorded in
#                                                            the processing report or
#                                                            was dispatched before
#   APPLY_ANALYSIS_SKIPPED reason=guard_unavailable          the dispatch-history
#                                                            search, a candidate's
#                                                            comment read, or the
#                                                            workflow-runs list
#                                                            failed; fail closed
#   APPLY_ANALYSIS_CANDIDATES count=<n> docs=<a,b>           list-only mode
#   APPLY_ANALYSIS_DISPATCHED doc=<path> role=<role>
#
# Loop guard: the marker comment carries "apply-analysis-source-doc: <path>";
# before dispatching a doc the script searches tracking issues (open or
# closed) for that marker and skips docs dispatched before, whatever the
# outcome. A project that failed, or merged without deleting its doc, is a
# human decision, not a credit burner.
#
# API calls per run (§15): 1 issue list, 1 workflow-runs list, then per
# candidate doc until the first unprocessed one: 1 search plus 1
# issue-comments read per search hit (the trusted-author check; up to 20
# hits per doc), and 1 dispatch. The common marker-present path therefore
# costs 2 + 2·(docs already dispatched) + 1 calls.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required)  owner/repo
#   GH_TOKEN (required)           token able to dispatch workflows
#   GITHUB_REF_NAME               branch to dispatch on (default: main)
#   GITHUB_SHA                    commit the docs were read at
#   GITHUB_SERVER_URL             default https://github.com
#   GITHUB_RUN_ID                 for the "dispatched by" line
#   GITHUB_OUTPUT                 receives dispatched=/doc=/role=/candidate_count=
#   APPLY_ANALYSIS_ON_MAIN_ENABLED             default true
#   APPLY_ANALYSIS_ROLE                        proving (default) | verifying
#   APPLY_ANALYSIS_CYCLE_BASELINE_SHA          main tip when the cycle started (marker line)
#   APPLY_ANALYSIS_SMOKE_SHA                   main tip the smoke gate ran on (marker line)
#   APPLY_ANALYSIS_PROMOTE_SHA                 commit a verifying run promotes (marker line)
#   APPLY_ANALYSIS_PROVING_MERGE_SHA           the proving run's merge commit (marker line)
#   APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE     tracking issue number the in-flight
#                                              guard ignores (the completing proving run)
#   APPLY_ANALYSIS_LIST_ONLY                   true: print the unprocessed docs, no dispatch
#   APPLY_ANALYSIS_DOC_GLOB                    default analysis/workflow-optimization-*.md
#   APPLY_ANALYSIS_REPORT_PATH                 default analysis/recommendation-processing-report.md
#   APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE   default internal-orchestrate.yml
#   APPLY_ANALYSIS_TRACKING_LABEL              default ai:comprehensive-test-pending

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

APPLY_ANALYSIS_SOURCE_DOC_MARKER="apply-analysis-source-doc"
APPLY_ANALYSIS_ON_MAIN_ENABLED="${APPLY_ANALYSIS_ON_MAIN_ENABLED:-true}"
APPLY_ANALYSIS_ROLE="${APPLY_ANALYSIS_ROLE:-proving}"
APPLY_ANALYSIS_CYCLE_BASELINE_SHA="${APPLY_ANALYSIS_CYCLE_BASELINE_SHA:-}"
APPLY_ANALYSIS_SMOKE_SHA="${APPLY_ANALYSIS_SMOKE_SHA:-}"
APPLY_ANALYSIS_PROMOTE_SHA="${APPLY_ANALYSIS_PROMOTE_SHA:-}"
APPLY_ANALYSIS_PROVING_MERGE_SHA="${APPLY_ANALYSIS_PROVING_MERGE_SHA:-}"
APPLY_ANALYSIS_DISPATCHER_RUN_ID="${APPLY_ANALYSIS_DISPATCHER_RUN_ID:-${GITHUB_RUN_ID:-0}}"
APPLY_ANALYSIS_SMOKE_RUN_ID="${APPLY_ANALYSIS_SMOKE_RUN_ID:-}"
APPLY_ANALYSIS_SMOKE_ACTOR_ID="${APPLY_ANALYSIS_SMOKE_ACTOR_ID:-}"
APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE="${APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE:-}"
APPLY_ANALYSIS_LIST_ONLY="${APPLY_ANALYSIS_LIST_ONLY:-false}"
APPLY_ANALYSIS_DOC_GLOB="${APPLY_ANALYSIS_DOC_GLOB:-analysis/workflow-optimization-*.md}"
APPLY_ANALYSIS_REPORT_PATH="${APPLY_ANALYSIS_REPORT_PATH:-analysis/recommendation-processing-report.md}"
APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE="${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE:-internal-orchestrate.yml}"
APPLY_ANALYSIS_TRACKING_LABEL="${APPLY_ANALYSIS_TRACKING_LABEL:-ai:comprehensive-test-pending}"
# Compatibility-only setting retained for existing workflow inputs. Signed
# marker selection requires the immutable Actions producer ID; author
# association grants no authority.
COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS="${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS:-OWNER,MEMBER,COLLABORATOR}"
COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID="41898282"
COMPREHENSIVE_CYCLE_MARKER_HELPER="${COMPREHENSIVE_CYCLE_MARKER_HELPER:-${SCRIPT_DIR}/orchestrate_state_v2.py}"
GITHUB_REF_NAME="${GITHUB_REF_NAME:-main}"
GITHUB_SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
GITHUB_SHA="${GITHUB_SHA:-}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::apply_analysis_on_main.sh requires ${required_env}."
		exit 1
	fi
done
case "${APPLY_ANALYSIS_ROLE}" in
	proving|verifying) ;;
	*)
		echo "::error::APPLY_ANALYSIS_ROLE must be 'proving' or 'verifying' (got '${APPLY_ANALYSIS_ROLE}')."
		exit 1
		;;
esac
for sha_env in APPLY_ANALYSIS_CYCLE_BASELINE_SHA APPLY_ANALYSIS_SMOKE_SHA APPLY_ANALYSIS_PROMOTE_SHA APPLY_ANALYSIS_PROVING_MERGE_SHA; do
	if [ -n "${!sha_env}" ] && ! [[ "${!sha_env}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "::error::${sha_env} must be a 40-hex commit SHA (got '${!sha_env}')."
		exit 1
	fi
done
if [ -n "${APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE}" ] && ! [[ "${APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE}" =~ ^[0-9]+$ ]]; then
	echo "::error::APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE must be an issue number."
	exit 1
fi

emit_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT}"
	fi
}

skip_dispatch()
{
	local reason="$1"
	local detail="${2:-}"
	echo "APPLY_ANALYSIS_SKIPPED reason=${reason}${detail:+ ${detail}}"
	emit_output dispatched false
	emit_output doc ""
	emit_output role "${APPLY_ANALYSIS_ROLE}"
	exit 0
}

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# open_tracking_issue_numbers [extra-label]
# JSON array of open ai:orchestrator-tracking issue numbers, optionally
# narrowed to issues that also carry <extra-label>. One API call.
open_tracking_issue_numbers()
{
	local labels="ai:orchestrator-tracking"
	if [ -n "${1:-}" ]; then
		labels="${labels},$1"
	fi
	local encoded_labels
	encoded_labels="$(printf '%s' "${labels}" | sed 's/:/%3A/g; s/,/%2C/g')"
	gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues?state=open&labels=${encoded_labels}&per_page=100" \
		| jq -c '[.[] | select(has("pull_request") | not) | .number] | sort'
}

# trusted_marker_comment_present <issue-number> <marker-line>
# 0 when the issue carries a producer-authenticated signed marker for the
# source document, 1 when only untrusted (or no) comments carry it, and 2
# when comments or signature verification could not be read.
trusted_marker_comment_present()
{
	local issue_number="$1"
	local marker_line="$2"
	local comments_file selected_file source_doc selected_doc
	comments_file="$(mktemp)"
	selected_file="$(mktemp)"
	if ! gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments?per_page=100" > "${comments_file}" 2>/dev/null; then
		rm -f "${comments_file}" "${selected_file}"
		return 2
	fi
	if ! PYTHONDONTWRITEBYTECODE=1 python3 "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" select-comprehensive-marker \
		--comments-json "${comments_file}" \
		--repository "${GITHUB_REPOSITORY}" \
		--producer-id "${COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID}" \
		--out-file "${selected_file}" >/dev/null 2>&1; then
		rm -f "${comments_file}" "${selected_file}"
		return 2
	fi
	source_doc="${marker_line#${APPLY_ANALYSIS_SOURCE_DOC_MARKER}: }"
	selected_doc="$(jq -r '.marker.source_doc // ""' "${selected_file}" 2>/dev/null || true)"
	rm -f "${comments_file}" "${selected_file}"
	[ "${selected_doc}" = "${source_doc}" ]
}

# doc_dispatched_before <path>
# 0 when a tracking issue (open or closed) carries this doc's marker in a
# producer-authenticated signed comment, 1 when none does, 2 when lookup failed.
# The search only finds candidates (it matches any comment body); the
# signature and immutable producer-ID check make the answer trustworthy, so a
# stray comment cannot make the dispatcher skip a doc forever.
doc_dispatched_before()
{
	local doc_path="$1"
	local marker_line="${APPLY_ANALYSIS_SOURCE_DOC_MARKER}: ${doc_path}"
	local query numbers issue_number rc
	query="repo:${GITHUB_REPOSITORY} is:issue label:ai:orchestrator-tracking in:comments \"${marker_line}\""
	if ! numbers="$(gh_retry gh api -X GET search/issues -f "q=${query}" -f per_page=20 2>/dev/null | jq -r '.items[]?.number | select(. != null)')"; then
		return 2
	fi
	[ -n "${numbers}" ] || return 1
	while IFS= read -r issue_number; do
		[[ "${issue_number}" =~ ^[0-9]+$ ]] || continue
		# No set -e toggling here: the caller runs this under its own
		# set +e window and re-enabling errexit inside it would abort the
		# script on the return 1 below.
		if trusted_marker_comment_present "${issue_number}" "${marker_line}"; then
			return 0
		else
			rc=$?
		fi
		[ "${rc}" -eq 2 ] && return 2
		echo "::warning::Tracking issue #${issue_number} carries the marker for ${doc_path} only in unauthenticated comments; ignoring it." >&2
	done <<< "${numbers}"
	return 1
}

# orchestrate_run_in_flight
# 0 when the orchestrator workflow has a queued or running run, 1 when it has
# none, 2 when the runs list could not be read (callers fail closed).
orchestrate_run_in_flight()
{
	local runs_json active
	if ! runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE}/runs?per_page=20")"; then
		return 2
	fi
	active="$(printf '%s' "${runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length' 2>/dev/null || true)"
	[[ "${active}" =~ ^[0-9]+$ ]] || return 2
	[ "${active}" -gt 0 ] && return 0
	return 1
}

# unprocessed_docs
# Prints one unprocessed doc path per line, oldest first. Docs listed in the
# processing report or carrying a dispatch marker are skipped with a warning.
# Returns 2 when the marker search is unavailable.
unprocessed_docs()
{
	local candidate candidate_basename dispatched_rc
	local candidates=()
	while IFS= read -r candidate; do
		[ -n "${candidate}" ] && candidates+=("${candidate}")
	done < <(compgen -G "${APPLY_ANALYSIS_DOC_GLOB}" | sort -V || true)
	for candidate in "${candidates[@]}"; do
		candidate_basename="$(basename "${candidate}")"
		if [ -f "${APPLY_ANALYSIS_REPORT_PATH}" ] && grep -qF -- "${candidate_basename}" "${APPLY_ANALYSIS_REPORT_PATH}"; then
			echo "::warning::${candidate} is already listed in ${APPLY_ANALYSIS_REPORT_PATH} but still exists; skipping it (delete it by hand or via a normal PR)." >&2
			continue
		fi
		set +e
		doc_dispatched_before "${candidate}"
		dispatched_rc=$?
		set -e
		case "${dispatched_rc}" in
			0)
				echo "::warning::${candidate} was dispatched to the orchestrator before (marker comment found) but still exists; skipping it — decide by hand whether to re-run it." >&2
				continue
				;;
			1)
				printf '%s\n' "${candidate}"
				;;
			*)
				return 2
				;;
		esac
	done
	return 0
}

if ! is_truthy "${APPLY_ANALYSIS_ON_MAIN_ENABLED}"; then
	skip_dispatch disabled
fi

if is_truthy "${APPLY_ANALYSIS_LIST_ONLY}"; then
	set +e
	docs_output="$(unprocessed_docs)"
	docs_rc=$?
	set -e
	if [ "${docs_rc}" -ne 0 ]; then
		skip_dispatch guard_unavailable "mode=list_only"
	fi
	candidate_count=0
	candidate_csv=""
	while IFS= read -r line; do
		[ -n "${line}" ] || continue
		candidate_count=$((candidate_count + 1))
		candidate_csv="${candidate_csv:+${candidate_csv},}${line}"
	done <<< "${docs_output}"
	echo "APPLY_ANALYSIS_CANDIDATES count=${candidate_count} docs=${candidate_csv}"
	emit_output dispatched false
	emit_output candidate_count "${candidate_count}"
	emit_output candidate_docs "${candidate_csv}"
	exit 0
fi

in_flight_json="$(open_tracking_issue_numbers "${APPLY_ANALYSIS_TRACKING_LABEL}")"
if [ -n "${APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE}" ]; then
	in_flight_json="$(printf '%s' "${in_flight_json}" | jq -c --argjson n "${APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE}" '[.[] | select(. != $n)]')"
fi
if [ "$(printf '%s' "${in_flight_json}" | jq -r 'length')" -gt 0 ]; then
	skip_dispatch project_in_flight "tracking_issues=$(printf '%s' "${in_flight_json}" | jq -r 'join(",")')"
fi

set +e
orchestrate_run_in_flight
orchestrate_run_in_flight_rc=$?
set -e
case "${orchestrate_run_in_flight_rc}" in
	0) skip_dispatch orchestrate_run_in_flight "workflow=${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE}" ;;
	1) ;;
	*) skip_dispatch guard_unavailable "lookup=workflow-runs:${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE}" ;;
esac

if ! compgen -G "${APPLY_ANALYSIS_DOC_GLOB}" >/dev/null; then
	skip_dispatch no_docs "glob=${APPLY_ANALYSIS_DOC_GLOB}"
fi

set +e
docs_output="$(unprocessed_docs)"
docs_rc=$?
set -e
if [ "${docs_rc}" -ne 0 ]; then
	skip_dispatch guard_unavailable
fi
selected_doc="$(printf '%s\n' "${docs_output}" | sed -n '1p')"
if [ -z "${selected_doc}" ]; then
	skip_dispatch all_docs_processed
fi

selected_basename="$(basename "${selected_doc}")"
ref_sha="${GITHUB_SHA:-${GITHUB_REF_NAME}}"
doc_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/blob/${ref_sha}/${selected_doc}"
run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

if [ -z "${ORCHESTRATOR_STATE_AUTH_KEYRING:-}" ] || [ ! -f "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" ]; then
	echo "::error::A comprehensive-cycle marker requires ORCHESTRATOR_STATE_AUTH_KEYRING and ${COMPREHENSIVE_CYCLE_MARKER_HELPER}."
	exit 1
fi
for marker_id_name in APPLY_ANALYSIS_DISPATCHER_RUN_ID APPLY_ANALYSIS_SMOKE_RUN_ID APPLY_ANALYSIS_SMOKE_ACTOR_ID COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID; do
	if ! [[ "${!marker_id_name}" =~ ^[1-9][0-9]*$ ]]; then
		echo "::error::${marker_id_name} must be a positive integer."
		exit 1
	fi
done
if [ -z "${APPLY_ANALYSIS_CYCLE_BASELINE_SHA}" ] || [ -z "${APPLY_ANALYSIS_SMOKE_SHA}" ]; then
	echo "::error::Comprehensive-cycle dispatches require baseline and smoke SHAs."
	exit 1
fi

project_description="$(cat <<DESC
Apply analysis recommendations from ${selected_basename}

These are analysis recommendation docs, NOT a pre-approved spec. Read the referenced doc in full from the repository, then validate every recommendation against the current code on the default branch: implement only the recommendations that are still correct, still applicable, and safe under the repository rules (security and correctness first; never rename or remove existing identifiers without an alias; MongoDB changes ship their /db/contracts update; no standalone manual scripts, wire work into existing automation; never use auto-close keywords against ai:orchestrator-tracking issues). Skip recommendations that are already implemented, obsolete, or unsafe, and record why.

Scope: process ONLY the single source doc referenced below. Do not read, modify, or delete any other file under analysis/. In the final PR of this project: delete the source doc and append an entry for it to ${APPLY_ANALYSIS_REPORT_PATH} (path, date, and a per-recommendation verdict: applied, already-implemented, or rejected with the reason). Never delete or modify analysis/last_collection_timestamp.txt or analysis/validation-selftest-status.json.

Source doc: ${selected_doc} (branch ${GITHUB_REF_NAME} at ${ref_sha})
Source doc URL: ${doc_url}
Dispatched by the promote cycle (${APPLY_ANALYSIS_ROLE} run): ${run_url}
DESC
)"

marker_lines="${APPLY_ANALYSIS_SOURCE_DOC_MARKER}: ${selected_doc}
apply-analysis-role: ${APPLY_ANALYSIS_ROLE}
apply-analysis-dispatcher-run-id: ${APPLY_ANALYSIS_DISPATCHER_RUN_ID}
apply-analysis-smoke-run-id: ${APPLY_ANALYSIS_SMOKE_RUN_ID}"
[ -n "${APPLY_ANALYSIS_CYCLE_BASELINE_SHA}" ] && marker_lines+=$'\n'"apply-analysis-cycle-baseline-sha: ${APPLY_ANALYSIS_CYCLE_BASELINE_SHA}"
[ -n "${APPLY_ANALYSIS_SMOKE_SHA}" ] && marker_lines+=$'\n'"apply-analysis-smoke-sha: ${APPLY_ANALYSIS_SMOKE_SHA}"
[ -n "${APPLY_ANALYSIS_PROMOTE_SHA}" ] && marker_lines+=$'\n'"apply-analysis-promote-sha: ${APPLY_ANALYSIS_PROMOTE_SHA}"
[ -n "${APPLY_ANALYSIS_PROVING_MERGE_SHA}" ] && marker_lines+=$'\n'"apply-analysis-proving-merge-sha: ${APPLY_ANALYSIS_PROVING_MERGE_SHA}"

marker_candidate_file="$(mktemp)"
marker_envelope_file="$(mktemp)"
trap 'rm -f "${marker_candidate_file:-}" "${marker_envelope_file:-}"' EXIT
jq -n \
	--arg source_doc "${selected_doc}" \
	--arg role "${APPLY_ANALYSIS_ROLE}" \
	--argjson dispatcher_run_id "${APPLY_ANALYSIS_DISPATCHER_RUN_ID}" \
	--argjson smoke_run_id "${APPLY_ANALYSIS_SMOKE_RUN_ID}" \
	--argjson smoke_actor_id "${APPLY_ANALYSIS_SMOKE_ACTOR_ID}" \
	--arg smoke_head_sha "${APPLY_ANALYSIS_SMOKE_SHA}" \
	--arg cycle_baseline_sha "${APPLY_ANALYSIS_CYCLE_BASELINE_SHA}" \
	--arg promote_sha "${APPLY_ANALYSIS_PROMOTE_SHA}" \
	--arg proving_merge_sha "${APPLY_ANALYSIS_PROVING_MERGE_SHA}" '
	{
		source_doc: $source_doc,
		role: $role,
		dispatcher_run_id: $dispatcher_run_id,
		smoke_run_id: $smoke_run_id,
		smoke_actor_id: $smoke_actor_id,
		smoke_workflow_path: ".github/workflows/test-and-mark-stable.yml",
		smoke_event: "workflow_dispatch",
		smoke_display_title: ("Test & Mark Stable Release [cycle:" + ($dispatcher_run_id | tostring) + ";gate-only:true;skip-e2e:false;dry-run:false;test-repo:;review-workflow:internal-review.yml]"),
		smoke_inputs: {
			gate_only: "true",
			gate_cycle_id: ($dispatcher_run_id | tostring),
			skip_e2e: "false",
			dry_run: "false",
			test_repo: "",
			review_workflow_file: "internal-review.yml"
		},
		smoke_conclusion: "success",
		smoke_head_sha: $smoke_head_sha,
		cycle_baseline_sha: $cycle_baseline_sha,
		promote_sha: $promote_sha,
		proving_merge_sha: $proving_merge_sha
	}' > "${marker_candidate_file}"
PYTHONDONTWRITEBYTECODE=1 python3 "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" sign-comprehensive-marker \
	--candidate-file "${marker_candidate_file}" \
	--repository "${GITHUB_REPOSITORY}" \
	--producer-id "${COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID}" \
	--out-file "${marker_envelope_file}"
marker_envelope="$(cat "${marker_envelope_file}")"

if [ "${APPLY_ANALYSIS_ROLE}" = "verifying" ]; then
	role_prose="This is the VERIFYING run of a promote cycle: it re-proves the pipeline on top of the proving run's merged changes. When it reaches ready-to-merge, the orchestrator poller promotes \`apply-analysis-promote-sha\` to \`stable\` and holds this project's final merge until that release finishes. Its own merge never promotes anything; the next daily cycle covers it."
else
	role_prose="This is the PROVING run of a promote cycle: once it merges, the orchestrator poller dispatches the verifying run on the next analysis doc, and that run's ready-to-merge point promotes \`main\` (including this project's merge) to \`stable\`."
fi
marker_comment="$(cat <<COMMENT
<!-- ${APPLY_ANALYSIS_SOURCE_DOC_MARKER} -->
## Apply-analysis dispatch

${marker_lines}

<!-- COMPREHENSIVE_CYCLE_MARKER_V1
${marker_envelope}
COMPREHENSIVE_CYCLE_MARKER_V1 -->

Dispatched by ${run_url} for the single source doc above; the tracking issue carries \`${APPLY_ANALYSIS_TRACKING_LABEL}\`. ${role_prose} The marker lines are machine-read by the poller and are the loop guard: this doc is never dispatched automatically again.
COMMENT
)"

echo "Dispatching ${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE} on ${GITHUB_REF_NAME} for ${selected_doc} (role=${APPLY_ANALYSIS_ROLE})."
gh_retry gh workflow run "${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "${GITHUB_REF_NAME}" \
	-f "project_description=${project_description}" \
	-f "tracking_labels=${APPLY_ANALYSIS_TRACKING_LABEL}" \
	-f "tracking_comment=${marker_comment}"

echo "APPLY_ANALYSIS_DISPATCHED doc=${selected_doc} role=${APPLY_ANALYSIS_ROLE}"
emit_output dispatched true
emit_output doc "${selected_doc}"
emit_output role "${APPLY_ANALYSIS_ROLE}"
