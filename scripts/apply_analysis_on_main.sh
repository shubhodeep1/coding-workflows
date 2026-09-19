#!/usr/bin/env bash
# apply_analysis_on_main.sh — hand ONE pending analysis doc to the orchestrator.
#
# Runs from .github/workflows/apply-analysis-on-main.yml on every push to the
# default branch. It is the unattended equivalent of the interactive
# `/apply-analysis` command, deliberately limited to a single source doc per
# dispatch so later pushes to main still have analysis docs left to execute.
#
# Decision order (every exit is a logged, stable prefix):
#   APPLY_ANALYSIS_SKIPPED reason=disabled          kill switch
#   APPLY_ANALYSIS_SKIPPED reason=project_in_flight  an open tracking issue still
#                                                    carries the pending label
#   APPLY_ANALYSIS_SKIPPED reason=no_docs            nothing under the doc glob
#   APPLY_ANALYSIS_SKIPPED reason=all_docs_processed every doc is already recorded
#                                                    in the processing report or
#                                                    was dispatched before
#   APPLY_ANALYSIS_SKIPPED reason=guard_unavailable  the dispatch-history search
#                                                    failed; fail closed, the next
#                                                    push retries
#   APPLY_ANALYSIS_DISPATCHED doc=<path> tracking_issue=<n>
#
# Loop guard: the tracking-issue title and body are model-written, so the doc a
# project came from is recorded as a visible marker comment on the tracking
# issue ("<marker>: <path>"). Before dispatching a doc the script searches
# tracking issues (open or closed) for that marker and skips docs that were
# dispatched before, whatever the outcome — a project that failed, or that
# merged without deleting its doc, is a human decision, not a credit burner.
#
# API calls per run (§15): 1 issue list (in-flight guard), ≤1 search per
# candidate doc until the first unprocessed one, 1 dispatch, then one issue
# list per poll tick until the new tracking issue appears, 1 label edit and
# 1 comment. Nothing here runs per poll cycle of the orchestrator.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required)  owner/repo
#   GH_TOKEN (required)           token able to dispatch workflows + edit issues
#   GITHUB_REF_NAME               branch to dispatch on (default: main)
#   GITHUB_SHA                    commit the docs were read at (for the reference block)
#   GITHUB_SERVER_URL             default https://github.com
#   GITHUB_RUN_ID                 for the "dispatched by" line
#   GITHUB_OUTPUT                 receives dispatched=/doc=/tracking_issue=
#   APPLY_ANALYSIS_ON_MAIN_ENABLED           default true
#   APPLY_ANALYSIS_DOC_GLOB                  default analysis/workflow-optimization-*.md
#   APPLY_ANALYSIS_REPORT_PATH               default analysis/recommendation-processing-report.md
#   APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE default internal-orchestrate.yml
#   APPLY_ANALYSIS_TRACKING_LABEL            default ai:comprehensive-test-pending
#   APPLY_ANALYSIS_TRACKING_WAIT_SECS        default 1800
#   APPLY_ANALYSIS_TRACKING_POLL_SECS        default 30

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

APPLY_ANALYSIS_SOURCE_DOC_MARKER="apply-analysis-source-doc"
APPLY_ANALYSIS_ON_MAIN_ENABLED="${APPLY_ANALYSIS_ON_MAIN_ENABLED:-true}"
APPLY_ANALYSIS_DOC_GLOB="${APPLY_ANALYSIS_DOC_GLOB:-analysis/workflow-optimization-*.md}"
APPLY_ANALYSIS_REPORT_PATH="${APPLY_ANALYSIS_REPORT_PATH:-analysis/recommendation-processing-report.md}"
APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE="${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE:-internal-orchestrate.yml}"
APPLY_ANALYSIS_TRACKING_LABEL="${APPLY_ANALYSIS_TRACKING_LABEL:-ai:comprehensive-test-pending}"
APPLY_ANALYSIS_TRACKING_WAIT_SECS="${APPLY_ANALYSIS_TRACKING_WAIT_SECS:-1800}"
APPLY_ANALYSIS_TRACKING_POLL_SECS="${APPLY_ANALYSIS_TRACKING_POLL_SECS:-30}"
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
for numeric_env in APPLY_ANALYSIS_TRACKING_WAIT_SECS APPLY_ANALYSIS_TRACKING_POLL_SECS; do
	if ! [[ "${!numeric_env}" =~ ^[0-9]+$ ]] || [ "${!numeric_env}" -lt 1 ]; then
		echo "::error::${numeric_env} must be a positive integer (got '${!numeric_env}')."
		exit 1
	fi
done

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
	emit_output tracking_issue ""
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
# Prints the JSON array of open ai:orchestrator-tracking issue numbers,
# optionally narrowed to issues that also carry <extra-label>. One API call.
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

# doc_dispatched_before <path>
# Returns 0 when a tracking issue (open or closed) already carries this doc's
# marker comment, 1 when none does, 2 when the search itself failed.
doc_dispatched_before()
{
	local doc_path="$1"
	local query total
	query="repo:${GITHUB_REPOSITORY} is:issue label:ai:orchestrator-tracking in:comments \"${APPLY_ANALYSIS_SOURCE_DOC_MARKER}: ${doc_path}\""
	if ! total="$(gh_retry gh api -X GET search/issues -f "q=${query}" -f per_page=1 2>/dev/null | jq -r '.total_count // empty')"; then
		return 2
	fi
	if ! [[ "${total}" =~ ^[0-9]+$ ]]; then
		return 2
	fi
	if [ "${total}" -gt 0 ]; then
		return 0
	fi
	return 1
}

if ! is_truthy "${APPLY_ANALYSIS_ON_MAIN_ENABLED}"; then
	skip_dispatch disabled
fi

in_flight_json="$(open_tracking_issue_numbers "${APPLY_ANALYSIS_TRACKING_LABEL}")"
if [ "$(printf '%s' "${in_flight_json}" | jq -r 'length')" -gt 0 ]; then
	skip_dispatch project_in_flight "tracking_issues=$(printf '%s' "${in_flight_json}" | jq -r 'join(",")')"
fi

# Oldest doc first: the filenames embed the collection date
# (workflow-optimization-YYYY-MM-DD[-N].md), so a version sort orders them
# chronologically and keeps -2/-3 suffixes after their base date.
candidate_docs=()
while IFS= read -r candidate; do
	[ -n "${candidate}" ] && candidate_docs+=("${candidate}")
done < <(compgen -G "${APPLY_ANALYSIS_DOC_GLOB}" | sort -V || true)
if [ "${#candidate_docs[@]}" -eq 0 ]; then
	skip_dispatch no_docs "glob=${APPLY_ANALYSIS_DOC_GLOB}"
fi

selected_doc=""
for candidate in "${candidate_docs[@]}"; do
	candidate_basename="$(basename "${candidate}")"
	if [ -f "${APPLY_ANALYSIS_REPORT_PATH}" ] && grep -qF -- "${candidate_basename}" "${APPLY_ANALYSIS_REPORT_PATH}"; then
		echo "::warning::${candidate} is already listed in ${APPLY_ANALYSIS_REPORT_PATH} but still exists; skipping it (delete it by hand or via a normal PR)."
		continue
	fi
	set +e
	doc_dispatched_before "${candidate}"
	dispatched_rc=$?
	set -e
	case "${dispatched_rc}" in
		0)
			echo "::warning::${candidate} was dispatched to the orchestrator before (marker comment found) but still exists; skipping it — decide by hand whether to re-run it."
			continue
			;;
		1)
			selected_doc="${candidate}"
			break
			;;
		*)
			skip_dispatch guard_unavailable "doc=${candidate}"
			;;
	esac
done
if [ -z "${selected_doc}" ]; then
	skip_dispatch all_docs_processed "docs=${#candidate_docs[@]}"
fi

selected_basename="$(basename "${selected_doc}")"
ref_sha="${GITHUB_SHA:-${GITHUB_REF_NAME}}"
doc_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/blob/${ref_sha}/${selected_doc}"
run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

project_description="$(cat <<DESC
Apply analysis recommendations from ${selected_basename}

These are analysis recommendation docs, NOT a pre-approved spec. Read the referenced doc in full from the repository, then validate every recommendation against the current code on the default branch: implement only the recommendations that are still correct, still applicable, and safe under the repository rules (security and correctness first; never rename or remove existing identifiers without an alias; MongoDB changes ship their /db/contracts update; no standalone manual scripts, wire work into existing automation; never use auto-close keywords against ai:orchestrator-tracking issues). Skip recommendations that are already implemented, obsolete, or unsafe, and record why.

Scope: process ONLY the single source doc referenced below. Do not read, modify, or delete any other file under analysis/. In the final PR of this project: delete the source doc and append an entry for it to ${APPLY_ANALYSIS_REPORT_PATH} (path, date, and a per-recommendation verdict: applied, already-implemented, or rejected with the reason). Never delete or modify analysis/last_collection_timestamp.txt or analysis/validation-selftest-status.json.

Source doc: ${selected_doc} (branch ${GITHUB_REF_NAME} at ${ref_sha})
Source doc URL: ${doc_url}
Dispatched by apply-analysis-on-main: ${run_url}
DESC
)"

before_json="$(open_tracking_issue_numbers)"

echo "Dispatching ${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE} on ${GITHUB_REF_NAME} for ${selected_doc}."
gh_retry gh workflow run "${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "${GITHUB_REF_NAME}" \
	-f "project_description=${project_description}"

# The orchestrator creates the tracking issue after its own clarify and
# decompose steps, so wait for a NEW open tracking issue rather than a title
# (the title is model-written).
deadline=$(( $(date +%s) + APPLY_ANALYSIS_TRACKING_WAIT_SECS ))
tracking_issue=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
	sleep "${APPLY_ANALYSIS_TRACKING_POLL_SECS}"
	after_json="$(open_tracking_issue_numbers)" || continue
	new_json="$(jq -cn --argjson before "${before_json}" --argjson after "${after_json}" '$after - $before')"
	new_count="$(printf '%s' "${new_json}" | jq -r 'length')"
	if [ "${new_count}" -eq 0 ]; then
		continue
	fi
	if [ "${new_count}" -eq 1 ]; then
		tracking_issue="$(printf '%s' "${new_json}" | jq -r '.[0]')"
		break
	fi
	echo "::error::${new_count} tracking issues appeared while waiting for the apply-analysis project (${new_json}); refusing to guess which one to label."
	emit_output dispatched true
	emit_output doc "${selected_doc}"
	emit_output tracking_issue ""
	exit 1
done
if [ -z "${tracking_issue}" ]; then
	echo "::error::No new tracking issue appeared within ${APPLY_ANALYSIS_TRACKING_WAIT_SECS}s after dispatching ${APPLY_ANALYSIS_ORCHESTRATE_WORKFLOW_FILE} for ${selected_doc}. The project may still start; label it ${APPLY_ANALYSIS_TRACKING_LABEL} by hand to keep the release callback."
	emit_output dispatched true
	emit_output doc "${selected_doc}"
	emit_output tracking_issue ""
	exit 1
fi

gh_retry gh issue edit "${tracking_issue}" --repo "${GITHUB_REPOSITORY}" \
	--add-label "${APPLY_ANALYSIS_TRACKING_LABEL}"

marker_comment="$(cat <<COMMENT
<!-- ${APPLY_ANALYSIS_SOURCE_DOC_MARKER} -->
## Apply-analysis dispatch

${APPLY_ANALYSIS_SOURCE_DOC_MARKER}: ${selected_doc}

This project was dispatched by apply-analysis-on-main (${run_url}) for the single source doc above. The tracking issue carries \`${APPLY_ANALYSIS_TRACKING_LABEL}\`, so the orchestrator poller promotes \`${GITHUB_REF_NAME}\` to \`stable\` when the project completes cleanly. The marker line is the loop guard: the doc is never dispatched automatically again.
COMMENT
)"
gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${tracking_issue}/comments" -f "body=${marker_comment}" >/dev/null

echo "APPLY_ANALYSIS_DISPATCHED doc=${selected_doc} tracking_issue=${tracking_issue}"
emit_output dispatched true
emit_output doc "${selected_doc}"
emit_output tracking_issue "${tracking_issue}"
