#!/usr/bin/env bash
# promote_main_cycle.sh — the daily promote cycle for `main`.
#
# Runs from the `cycle` job of .github/workflows/promote-main-to-stable.yml.
# A promotion of `main` to `stable` happens only after main has been proven
# end to end, in this order:
#
#   1. code change on main since the last promotion (the `stable` tag), and
#      since the last cycle's baseline and the last failed cycle run — so a
#      known-bad tip is never retried until main moves with real code;
#   2. smoke gate: test-and-mark-stable.yml in gate_only mode on main;
#   3. PROVING apply-analysis run dispatched (one analysis doc, labelled
#      ai:comprehensive-test-pending, marker comment carries the cycle SHAs).
#
# The orchestrator poller carries the cycle from there (verifying run,
# pinned promotion, merge hold) — see README 12f.
#
# Decision order (every exit is a logged, stable prefix):
#   PROMOTE_CYCLE_SKIPPED reason=disabled
#   PROMOTE_CYCLE_SKIPPED reason=another_run_active         a previous cycle job is still running
#   PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight            a labelled tracking issue is open
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes            nothing but non-code paths changed
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle
#   PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run
#   PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor          main diverged from the stable
#                                                           tag / last baseline; cannot
#                                                           fast-forward, so no cycle
#   PROMOTE_CYCLE_SKIPPED reason=insufficient_docs          fewer than PROMOTE_CYCLE_MIN_DOCS docs
#   PROMOTE_CYCLE_SKIPPED reason=guard_unavailable          an API guard failed; fail closed
#   PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=<id>
#   PROMOTE_CYCLE_FAILED reason=smoke_gate_timeout
#   PROMOTE_CYCLE_FAILED reason=smoke_head_checkout_failed  main advanced during the gate and
#                                                           the smoke-tested commit could not
#                                                           be checked out for the dispatch
#   PROMOTE_CYCLE_DISPATCHED doc=<path> baseline=<sha> smoke=<sha>
#
# Non-code paths (do not count as a change): analysis/**, ai-memory/**,
# docs/**, tests/e2e_smoke_canary.txt, CHANGELOG.md and every *.md — except
# any CLAUDE.md and anything under .claude/, which consumers receive and
# therefore count as code.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required), GH_TOKEN (required)
#   GITHUB_RUN_ID                       this run (excluded from the active-run guard)
#   GITHUB_OUTPUT                       receives outcome=/doc=/baseline_sha=/smoke_run_id=
#   PROMOTE_CYCLE_ENABLED               default true
#   PROMOTE_CYCLE_DEFAULT_BRANCH        default main
#   PROMOTE_CYCLE_STABLE_TAG            default stable
#   PROMOTE_CYCLE_WORKFLOW_FILE         default promote-main-to-stable.yml (self)
#   PROMOTE_CYCLE_GATE_WORKFLOW_FILE    default test-and-mark-stable.yml
#   PROMOTE_CYCLE_GATE_WAIT_SECS        default 18000
#   PROMOTE_CYCLE_GATE_POLL_SECS        default 60
#   PROMOTE_CYCLE_MIN_DOCS              default 2 (proving + verifying)
#   PROMOTE_CYCLE_TRACKING_LABEL        default ai:comprehensive-test-pending
#   APPLY_ANALYSIS_DISPATCHER           default scripts/apply_analysis_on_main.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

PROMOTE_CYCLE_ENABLED="${PROMOTE_CYCLE_ENABLED:-true}"
PROMOTE_CYCLE_DEFAULT_BRANCH="${PROMOTE_CYCLE_DEFAULT_BRANCH:-main}"
PROMOTE_CYCLE_STABLE_TAG="${PROMOTE_CYCLE_STABLE_TAG:-stable}"
PROMOTE_CYCLE_WORKFLOW_FILE="${PROMOTE_CYCLE_WORKFLOW_FILE:-promote-main-to-stable.yml}"
PROMOTE_CYCLE_GATE_WORKFLOW_FILE="${PROMOTE_CYCLE_GATE_WORKFLOW_FILE:-test-and-mark-stable.yml}"
PROMOTE_CYCLE_GATE_WAIT_SECS="${PROMOTE_CYCLE_GATE_WAIT_SECS:-18000}"
PROMOTE_CYCLE_GATE_POLL_SECS="${PROMOTE_CYCLE_GATE_POLL_SECS:-60}"
PROMOTE_CYCLE_MIN_DOCS="${PROMOTE_CYCLE_MIN_DOCS:-2}"
PROMOTE_CYCLE_TRACKING_LABEL="${PROMOTE_CYCLE_TRACKING_LABEL:-ai:comprehensive-test-pending}"
APPLY_ANALYSIS_DISPATCHER="${APPLY_ANALYSIS_DISPATCHER:-${SCRIPT_DIR}/apply_analysis_on_main.sh}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"
CYCLE_BASELINE_MARKER="apply-analysis-cycle-baseline-sha"
# Compatibility-only setting retained for existing workflow inputs. Signed
# marker selection requires the immutable Actions producer ID; author
# association grants no authority.
COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS="${COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS:-OWNER,MEMBER,COLLABORATOR}"
COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID="41898282"
COMPREHENSIVE_CYCLE_MARKER_HELPER="${COMPREHENSIVE_CYCLE_MARKER_HELPER:-${SCRIPT_DIR}/orchestrate_state_v2.py}"

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::promote_main_cycle.sh requires ${required_env}."
		exit 1
	fi
done
for numeric_env in PROMOTE_CYCLE_GATE_WAIT_SECS PROMOTE_CYCLE_GATE_POLL_SECS PROMOTE_CYCLE_MIN_DOCS; do
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

skip_cycle()
{
	local reason="$1"
	local detail="${2:-}"
	echo "PROMOTE_CYCLE_SKIPPED reason=${reason}${detail:+ ${detail}}"
	emit_output outcome "skipped:${reason}"
	exit 0
}

fail_cycle()
{
	local reason="$1"
	local detail="${2:-}"
	echo "PROMOTE_CYCLE_FAILED reason=${reason}${detail:+ ${detail}}"
	emit_output outcome "failed:${reason}"
	exit 1
}

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

# is_code_path <path>: 0 when the path counts as a code change.
is_code_path()
{
	local p="$1"
	case "${p}" in
		CLAUDE.md|*/CLAUDE.md|.claude/*) return 0 ;;
	esac
	case "${p}" in
		analysis/*|ai-memory/*|docs/*|tests/e2e_smoke_canary.txt|CHANGELOG.md|*.md) return 1 ;;
	esac
	return 0
}

# code_changes_between <base_sha> <head_sha>
# Sets CODE_CHANGES_OUT to the code paths changed between the two commits
# (one per line), or to "" when only non-code paths changed. Returns 2 on
# API failure. Runs in the main shell (no command substitution) so the
# callers' skip_cycle exits really exit. A compare payload truncated by
# GitHub (300 files / 250 commits) is treated as a code change: better a
# spurious cycle than a missed one.
CODE_CHANGES_OUT=""
code_changes_between()
{
	local base="$1"
	local head="$2"
	local payload
	CODE_CHANGES_OUT=""
	CODE_CHANGES_STATUS=""
	if ! payload="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${base}...${head}")"; then
		return 2
	fi
	local status total_commits file_count
	status="$(printf '%s' "${payload}" | jq -r '.status // empty')"
	total_commits="$(printf '%s' "${payload}" | jq -r '.total_commits // 0')"
	file_count="$(printf '%s' "${payload}" | jq -r '(.files // []) | length')"
	CODE_CHANGES_STATUS="${status}"
	case "${status}" in
		ahead) ;;
		identical|behind)
			return 0
			;;
		*)
			# `diverged` (or an unknown status): head does not descend from
			# base, so the promotion's fast-forward preflight would refuse it
			# anyway. Skip now instead of spending a smoke gate and a proving
			# run on a tip that cannot be promoted.
			return 3
			;;
	esac
	if [ "${file_count}" -ge 300 ] || [ "${total_commits}" -ge 250 ]; then
		CODE_CHANGES_OUT="(compare payload truncated: ${file_count} files, ${total_commits} commits — treated as a code change)"
		return 0
	fi
	local path
	while IFS= read -r path; do
		[ -n "${path}" ] || continue
		if is_code_path "${path}"; then
			CODE_CHANGES_OUT+="${path}"$'\n'
		fi
	done < <(printf '%s' "${payload}" | jq -r '(.files // [])[] | .filename, (.previous_filename // empty)')
	CODE_CHANGES_OUT="${CODE_CHANGES_OUT%$'\n'}"
	return 0
}

resolve_tag_commit()
{
	local tag="$1"
	local ref_json object_type object_sha tag_ref_error_file
	tag_ref_error_file="$(mktemp)"
	if ! ref_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${tag}" 2>"${tag_ref_error_file}")"; then
		if grep -qE '(^|[^0-9])404([^0-9]|$)' "${tag_ref_error_file}"; then
			rm -f "${tag_ref_error_file}"
			return 1
		fi
		cat "${tag_ref_error_file}" >&2
		rm -f "${tag_ref_error_file}"
		return 2
	fi
	rm -f "${tag_ref_error_file}"
	object_type="$(printf '%s' "${ref_json}" | jq -r '.object.type // empty' 2>/dev/null || true)"
	object_sha="$(printf '%s' "${ref_json}" | jq -r '.object.sha // empty' 2>/dev/null || true)"
	if [ "${object_type}" = "tag" ] && [[ "${object_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/tags/${object_sha}" | jq -er '.object.sha | select(test("^[0-9a-f]{40}$"))'
	elif [ "${object_type}" = "commit" ]; then
		[[ "${object_sha}" =~ ^[0-9a-f]{40}$ ]] || return 2
		printf '%s\n' "${object_sha}"
	else
		return 2
	fi
}

# last_cycle_baseline_sha: the baseline SHA recorded by the most recent
# cycle (any outcome), or empty. Exit 2 when the search failed.
# The search matches any comment body, so each candidate issue's comments
# are re-read and only a producer-authenticated signed marker counts; a stray
# comment cannot fake a completed cycle.
last_cycle_baseline_sha()
{
	local query numbers issue_number sha comments_file selected_file
	query="repo:${GITHUB_REPOSITORY} is:issue label:ai:orchestrator-tracking in:comments \"${CYCLE_BASELINE_MARKER}:\""
	if ! numbers="$(gh_retry gh api -X GET search/issues -f "q=${query}" -f sort=created -f order=desc -f per_page=10 2>/dev/null | jq -r '.items[]?.number | select(. != null)')"; then
		return 2
	fi
	[ -n "${numbers}" ] || return 0
	while IFS= read -r issue_number; do
		[[ "${issue_number}" =~ ^[0-9]+$ ]] || continue
		comments_file="$(mktemp)"
		selected_file="$(mktemp)"
		if ! gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments?per_page=100" > "${comments_file}"; then
			rm -f "${comments_file}" "${selected_file}"
			return 2
		fi
		if ! PYTHONDONTWRITEBYTECODE=1 python3 "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" select-comprehensive-marker \
			--comments-json "${comments_file}" \
			--repository "${GITHUB_REPOSITORY}" \
			--producer-id "${COMPREHENSIVE_CYCLE_MARKER_PRODUCER_ID}" \
			--tracking-issue "${issue_number}" \
			--out-file "${selected_file}" >/dev/null 2>&1; then
			rm -f "${comments_file}" "${selected_file}"
			return 2
		fi
		sha="$(jq -r '.marker.cycle_baseline_sha // ""' "${selected_file}" 2>/dev/null || true)"
		untrusted_marker="$(jq -r '.untrusted_marker // false' "${selected_file}" 2>/dev/null || echo false)"
		rm -f "${comments_file}" "${selected_file}"
		if [ -n "${sha}" ]; then
			printf '%s\n' "${sha}"
			return 0
		fi
		if [ "${untrusted_marker}" = "true" ]; then
			echo "::warning::Tracking issue #${issue_number} matched the cycle-baseline marker only in unauthenticated comments; ignoring it." >&2
		fi
	done <<< "${numbers}"
	return 0
}

# require_code_changes <base_sha> <head_sha> <skip_reason>
# Exits the cycle (skip) unless a code path changed between the commits.
require_code_changes()
{
	local rc
	set +e
	code_changes_between "$1" "$2"
	rc=$?
	set -e
	if [ "${rc}" -eq 3 ]; then
		skip_cycle base_not_ancestor "base=$1 head=$2 status=${CODE_CHANGES_STATUS:-unknown}"
	fi
	if [ "${rc}" -ne 0 ]; then
		skip_cycle guard_unavailable "compare=$1...$2"
	fi
	if [ -z "${CODE_CHANGES_OUT}" ]; then
		skip_cycle "$3" "base=$1 head=$2"
	fi
}

if ! is_truthy "${PROMOTE_CYCLE_ENABLED}"; then
	skip_cycle disabled
fi
if [ -z "${ORCHESTRATOR_STATE_AUTH_KEYRING:-}" ] || [ ! -f "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" ]; then
	fail_cycle marker_auth_unavailable
fi
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${COMPREHENSIVE_CYCLE_MARKER_HELPER}" validate-keyring >/dev/null; then
	fail_cycle marker_auth_unavailable
fi

# 1. Another cycle job still running (the schedule fired while a previous
#    tick waits on its smoke gate): skip, do not queue behind it.
self_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_WORKFLOW_FILE}/runs?per_page=20")"
other_active="$(printf '%s' "${self_runs_json}" | jq -r --arg me "${GITHUB_RUN_ID}" '[.workflow_runs[]? | select((.id|tostring) != $me) | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
[[ "${other_active}" =~ ^[0-9]+$ ]] || other_active=0
if [ "${other_active}" -gt 0 ]; then
	skip_cycle another_run_active "active_runs=${other_active}"
fi

# 2. A cycle already in flight (proving or verifying run open).
encoded_labels="$(printf '%s' "ai:orchestrator-tracking,${PROMOTE_CYCLE_TRACKING_LABEL}" | sed 's/:/%3A/g; s/,/%2C/g')"
in_flight="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues?state=open&labels=${encoded_labels}&per_page=100" | jq -r '[.[] | select(has("pull_request") | not) | .number] | join(",")')"
if [ -n "${in_flight}" ]; then
	skip_cycle cycle_in_flight "tracking_issues=${in_flight}"
fi

# 3. Code change since the last promotion.
main_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${PROMOTE_CYCLE_DEFAULT_BRANCH}" | jq -r '.object.sha // empty')"
if ! [[ "${main_tip}" =~ ^[0-9a-f]{40}$ ]]; then
	echo "::error::Could not resolve branch ${PROMOTE_CYCLE_DEFAULT_BRANCH}."
	exit 1
fi
set +e
tag_commit="$(resolve_tag_commit "${PROMOTE_CYCLE_STABLE_TAG}")"
tag_lookup_rc=$?
set -e
case "${tag_lookup_rc}" in
	0)
		require_code_changes "${tag_commit}" "${main_tip}" no_code_changes
		echo "Code changes since ${PROMOTE_CYCLE_STABLE_TAG} (${tag_commit:0:7}):"
		printf '%s\n' "${CODE_CHANGES_OUT}" | sed 's/^/  /'
		;;
	1)
		echo "::warning::Tag ${PROMOTE_CYCLE_STABLE_TAG} does not exist; treating main as never promoted."
		;;
	*)
		skip_cycle guard_unavailable "lookup=tag:${PROMOTE_CYCLE_STABLE_TAG}"
		;;
esac

# 4. Not the same tip the last cycle already covered (Q14: retry only after
#    main moves with code), regardless of that cycle's outcome.
set +e
last_baseline="$(last_cycle_baseline_sha)"
last_baseline_rc=$?
set -e
if [ "${last_baseline_rc}" -ne 0 ]; then
	skip_cycle guard_unavailable "search=${CYCLE_BASELINE_MARKER}"
fi
if [[ "${last_baseline}" =~ ^[0-9a-f]{40}$ ]] && [ "${last_baseline}" != "${tag_commit:-}" ]; then
	require_code_changes "${last_baseline}" "${main_tip}" no_code_changes_since_last_cycle
fi

# 5. Not a tip whose smoke gate or dispatch already failed.
last_failed_head="$(printf '%s' "${self_runs_json}" | jq -r '[.workflow_runs[]? | select(.event == "schedule" and .status == "completed" and (.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out"))] | sort_by(.created_at) | last | .head_sha // empty')"
if [[ "${last_failed_head}" =~ ^[0-9a-f]{40}$ ]]; then
	require_code_changes "${last_failed_head}" "${main_tip}" no_code_changes_since_failed_run
fi

# 6. Enough analysis docs for a proving AND a verifying run.
candidate_count="$(APPLY_ANALYSIS_LIST_ONLY=true GITHUB_OUTPUT="" bash "${APPLY_ANALYSIS_DISPATCHER}" | sed -n 's/^APPLY_ANALYSIS_CANDIDATES count=\([0-9]*\).*/\1/p')"
[[ "${candidate_count}" =~ ^[0-9]+$ ]] || candidate_count=0
if [ "${candidate_count}" -lt "${PROMOTE_CYCLE_MIN_DOCS}" ]; then
	skip_cycle insufficient_docs "available=${candidate_count} required=${PROMOTE_CYCLE_MIN_DOCS}"
fi

# 7. Smoke gate on main.
before_gate_ids="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50" | jq -c '[.workflow_runs[]?.id]')"
echo "Dispatching ${PROMOTE_CYCLE_GATE_WORKFLOW_FILE} on ${PROMOTE_CYCLE_DEFAULT_BRANCH} with gate_only=true (smoke gate for ${main_tip})."
gh_retry gh workflow run "${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "${PROMOTE_CYCLE_DEFAULT_BRANCH}" \
	-f gate_only=true \
	-f skip_e2e=false \
	-f dry_run=false \
	-f test_repo= \
	-f review_workflow_file=internal-review.yml \
	-f "gate_cycle_id=${GITHUB_RUN_ID}"
smoke_sha="${main_tip}"

deadline=$(( $(date +%s) + PROMOTE_CYCLE_GATE_WAIT_SECS ))
gate_run_id=""
gate_conclusion=""
gate_head_sha=""
gate_actor_id=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
	sleep "${PROMOTE_CYCLE_GATE_POLL_SECS}"
	runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50")" || continue
	if [ -z "${gate_run_id}" ]; then
		gate_run_id="$(printf '%s' "${runs_json}" | jq -r --argjson before "${before_gate_ids}" --arg branch "${PROMOTE_CYCLE_DEFAULT_BRANCH}" --arg title "Test & Mark Stable Release [cycle:${GITHUB_RUN_ID};gate-only:true;skip-e2e:false;dry-run:false;test-repo:;review-workflow:internal-review.yml]" '[.workflow_runs[]? | select((.id as $id | ($before | index($id) | not)) and (.head_branch // "") == $branch and (.display_title // "") == $title)] | sort_by(.created_at) | first | .id // empty')"
		[ -n "${gate_run_id}" ] || continue
		echo "Smoke gate run: ${gate_run_id}"
	fi
	run_state="$(printf '%s' "${runs_json}" | jq -r --argjson id "${gate_run_id}" '[.workflow_runs[]? | select(.id == $id)] | first | ((.status // "") + " " + (.conclusion // "") + " " + (.head_sha // "") + " " + ((.actor.id // 0) | tostring))')"
	run_status="$(printf '%s' "${run_state}" | cut -d' ' -f1)"
	run_conclusion="$(printf '%s' "${run_state}" | cut -d' ' -f2)"
	gate_head_sha="$(printf '%s' "${run_state}" | cut -d' ' -f3)"
	gate_actor_id="$(printf '%s' "${run_state}" | cut -d' ' -f4)"
	if [ "${run_status}" = "completed" ]; then
		gate_conclusion="${run_conclusion}"
		break
	fi
done
if [ -z "${gate_run_id}" ]; then
	fail_cycle smoke_gate_timeout "no run appeared within ${PROMOTE_CYCLE_GATE_WAIT_SECS}s"
fi
emit_output smoke_run_id "${gate_run_id}"
if [ -z "${gate_conclusion}" ]; then
	fail_cycle smoke_gate_timeout "run=${gate_run_id} still running after ${PROMOTE_CYCLE_GATE_WAIT_SECS}s"
fi
if [ "${gate_conclusion}" != "success" ]; then
	fail_cycle smoke_gate_failed "run=${gate_run_id} conclusion=${gate_conclusion}"
fi
if ! [[ "${gate_actor_id}" =~ ^[1-9][0-9]*$ ]]; then
	fail_cycle smoke_gate_metadata_invalid "run=${gate_run_id} actor_id=missing"
fi
# The gate checked out whatever main was when its run started; that commit,
# not the tip read before dispatch, is what was smoke-tested. Anything that
# lands after it shows up as untested in the poller's quiescence check.
if [[ "${gate_head_sha:-}" =~ ^[0-9a-f]{40}$ ]]; then
	if [ "${gate_head_sha}" != "${main_tip}" ]; then
		echo "::warning::main advanced from ${main_tip} to ${gate_head_sha} before the smoke gate started; recording the gate's head as the smoke-tested commit."
	fi
	smoke_sha="${gate_head_sha}"
fi
echo "Smoke gate passed on ${smoke_sha} (run ${gate_run_id})."

# The dispatcher reads the analysis docs from this checkout and stamps
# GITHUB_SHA into the project description, so both must be the smoke-tested
# commit, not the tip this job happened to check out. Move the working tree
# there when the gate tested a newer commit; refuse the cycle if that fails.
if [ "${smoke_sha}" != "${main_tip}" ]; then
	if ! git fetch --quiet --depth 1 origin "${smoke_sha}" \
		|| ! git checkout --quiet --detach "${smoke_sha}" \
		|| [ "$(git rev-parse HEAD 2>/dev/null || true)" != "${smoke_sha}" ]; then
		fail_cycle smoke_head_checkout_failed "sha=${smoke_sha}"
	fi
	echo "Checked out the smoke-tested commit ${smoke_sha} for the proving dispatch."
fi

# 8. Proving run.
dispatch_output="$(APPLY_ANALYSIS_ROLE=proving \
	APPLY_ANALYSIS_CYCLE_BASELINE_SHA="${main_tip}" \
	APPLY_ANALYSIS_SMOKE_SHA="${smoke_sha}" \
	APPLY_ANALYSIS_DISPATCHER_RUN_ID="${GITHUB_RUN_ID}" \
	APPLY_ANALYSIS_SMOKE_RUN_ID="${gate_run_id}" \
	APPLY_ANALYSIS_SMOKE_ACTOR_ID="${gate_actor_id}" \
	GITHUB_SHA="${smoke_sha}" \
	GITHUB_OUTPUT="" \
	bash "${APPLY_ANALYSIS_DISPATCHER}")"
printf '%s\n' "${dispatch_output}"
dispatched_doc="$(printf '%s\n' "${dispatch_output}" | sed -n 's/^APPLY_ANALYSIS_DISPATCHED doc=\([^ ]*\).*/\1/p')"
if [ -z "${dispatched_doc}" ]; then
	skip_reason="$(printf '%s\n' "${dispatch_output}" | sed -n 's/^APPLY_ANALYSIS_SKIPPED reason=\([^ ]*\).*/\1/p' | tail -n 1)"
	skip_cycle "dispatcher:${skip_reason:-unknown}" "smoke_run=${gate_run_id}"
fi
echo "PROMOTE_CYCLE_DISPATCHED doc=${dispatched_doc} baseline=${main_tip} smoke=${smoke_sha}"
emit_output outcome dispatched
emit_output doc "${dispatched_doc}"
emit_output baseline_sha "${main_tip}"
