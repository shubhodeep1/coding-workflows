#!/usr/bin/env bash
# auto_release_stable.sh — release the `stable` branch when it is ahead of the
# `stable` tag.
#
# Runs from .github/workflows/auto-release-stable.yml on a schedule. Patch
# fixes merged straight into the `stable` branch (the `claude/*-stable` PRs)
# used to sit unreleased until an operator dispatched test-and-mark-stable.yml
# by hand; consumers pin the *tag*, so they never saw the fix. This script
# dispatches the same release gate automatically, and only when there is
# something to release.
#
# Decision order (every exit is a logged, stable prefix):
#   AUTO_RELEASE_SKIPPED reason=disabled          kill switch
#   AUTO_RELEASE_SKIPPED reason=up_to_date        branch tip == tag commit
#   AUTO_RELEASE_SKIPPED reason=branch_not_ahead   branch is behind or diverged
#                                                 from the tag; fail closed
#   AUTO_RELEASE_SKIPPED reason=release_in_flight the gate (any branch), a
#                                                 promote-main-to-stable run
#                                                 or a legacy mark-stable run
#                                                 is queued/running
#   AUTO_RELEASE_SKIPPED reason=last_gate_failed  the gate already failed or
#                                                 timed out on this exact tip
#                                                 AUTO_RELEASE_STABLE_MAX_ATTEMPTS
#                                                 times; a human must look (a
#                                                 cancelled gate is retried and
#                                                 never counts as an attempt)
#   AUTO_RELEASE_SKIPPED reason=runner_queue_deep the last stable gate failed
#                                                 recently and the Actions
#                                                 queue is deep; a new cycle
#                                                 would wait on the same queue
#   AUTO_RELEASE_DISPATCHED sha=<tip>
#
# Runner-queue back-off: every gate cycle files five to nine fixture issues and
# PRs and starts their clarify / plan / implement / review runs. Between
# 2026-11-10 and 2026-11-17 the gate ran 16 times and passed twice; the failed
# cycles (43445515854, 43590943288, 43708007145, 43878140217, 43934729786) all
# timed out waiting for child runs that sat in the queue, and each retry added
# its own fixtures to that queue. The gate's own jobs can start promptly while
# its child runs wait, so the signal is the queue itself: before a dispatch,
# when the newest completed stable gate run (cancelled runs aside) failed or
# timed out less than AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS ago, the script
# reads the number of queued runs in this repository and skips while it is at
# or above AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS. The skipped tick spends no
# attempt. Once the failure is older than the window, one cycle is dispatched
# whatever the queue, so a deep queue delays a release (a hotfix on a new tip
# included) by at most the window (rounded up to the next 6-hour tick) and
# never holds it for good; if that cycle
# fails too, the window starts again. Only `queued` runs of this repository are
# counted: `pending` runs wait for a concurrency group, not a runner, and other
# repositories' queues are not readable from here.
# Fail-open: an unreadable queue count dispatches as before
# (AUTO_RELEASE_QUEUE_CHECK outcome=read_failed).
#
# Fixture cleanup before a retry: when a completed gate run in the listing
# (any branch, so the promote cycle's gate_only runs count too) did not
# succeed, open issues and PRs it left behind (title starting `[E2E ` and
# naming that run as `(run <id>)` or `E2E Orchestrate Smoke <id>`, written by
# this token's login) are closed before the back-off and the dispatch, so a
# retry never stacks its fixtures on the previous cycle's and a tick that
# waits for the queue still takes them out of it. A fixture issue's pipeline
# PR (`AI implementation for issue #<N>`, label e2e-smoke-test) is closed by
# the review sweep's smoke-PR backstop on its next tick once the issue is
# closed. Orchestrator projects (ai:orchestrator-tracking /
# ai:orchestrator-managed) are left to
# scripts/orchestrate_poll_smoke_fixture_reap.sh, which closes a project with
# its children. Nothing is deleted and no comment is posted (eleven workflows
# start on issue_comment); the log line is the record. Each search reads one
# page of 100. Fail-open: any failed read or close is logged
# (AUTO_RELEASE_FIXTURE_CLEANUP) and the dispatch decision goes ahead.
#
# Retry budget: a gate that failed on the current tip is re-dispatched on the
# next tick until AUTO_RELEASE_STABLE_MAX_ATTEMPTS completed runs on that tip
# have ended in failure / timed_out / startup_failure. A transient failure (a
# GitHub-side push rejection, a runner loss after the tests ran) therefore
# releases on a later tick by itself; a deterministic failure stops costing
# gate runs after the budget and waits for the branch to move (a workflow-heal
# hotfix on `stable` is such a move) or for a human re-run. Attempts are
# counted from the 30 most recent gate runs the workflow-runs read returns.
#
# API calls per run (§15): 2 ref reads (+1 to dereference an annotated tag),
# 1 compare, 3 workflow-runs lists (the release gate, promote-main-to-stable,
# and the legacy mark-stable path), at most 1 dispatch. Only when the newest
# completed stable gate run failed inside the back-off window: +1 queued-runs
# count. Only when a completed gate run in the listing did not succeed: +1 user
# read and +2 searches (open `[E2E` issues, open `[E2E` PRs; the search API
# needs the type), then 1 PATCH per closed fixture (at most
# AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX). The gate-run listing already
# fetched answers "is that run completed", so no run is read again.
#
# Environment (all optional unless stated):
#   GITHUB_REPOSITORY (required)        owner/repo
#   GH_TOKEN (required)                 token able to dispatch workflows
#   AUTO_RELEASE_STABLE_ENABLED         default true
#   AUTO_RELEASE_STABLE_BRANCH          default stable
#   AUTO_RELEASE_STABLE_TAG             default stable
#   AUTO_RELEASE_STABLE_WORKFLOW_FILE   default test-and-mark-stable.yml
#   AUTO_RELEASE_STABLE_MAX_ATTEMPTS    default 3 (failed gate runs per tip
#                                       before the tip is held)
#   AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS default 100 (queued runs in the
#                                       repository at or above which a retry
#                                       after a recent failed gate waits;
#                                       0 disables the back-off)
#   AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS
#                                       default 24 (how long after a failed
#                                       gate the back-off applies; after it
#                                       one cycle runs whatever the queue)
#   AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED
#                                       default true
#   AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX
#                                       default 30 (fixtures closed per tick)
#   GITHUB_OUTPUT                       receives dispatched=/sha=

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
# Issue #9473: wedged-queued run filter from gh_helpers.sh. When the helpers
# could not be sourced, these no-op fallbacks keep the pre-#9473 behaviour
# (every queued run counts as active).
declare -F zombie_queued_cutoff_epoch >/dev/null 2>&1 || zombie_queued_cutoff_epoch()
{
	printf '0'
}
declare -F zombie_queued_jq_def >/dev/null 2>&1 || zombie_queued_jq_def()
{
	printf '%s\n' 'def is_wedged_queued($cutoff): false;'
}
declare -F zombie_queued_log_ignored >/dev/null 2>&1 || zombie_queued_log_ignored()
{
	cat >/dev/null 2>&1 || true
	return 0
}

AUTO_RELEASE_STABLE_ENABLED="${AUTO_RELEASE_STABLE_ENABLED:-true}"
AUTO_RELEASE_STABLE_BRANCH="${AUTO_RELEASE_STABLE_BRANCH:-stable}"
AUTO_RELEASE_STABLE_TAG="${AUTO_RELEASE_STABLE_TAG:-stable}"
AUTO_RELEASE_STABLE_WORKFLOW_FILE="${AUTO_RELEASE_STABLE_WORKFLOW_FILE:-test-and-mark-stable.yml}"
AUTO_RELEASE_STABLE_MAX_ATTEMPTS="${AUTO_RELEASE_STABLE_MAX_ATTEMPTS:-3}"
AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS="${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS:-100}"
AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS="${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS:-24}"
AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED="${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED:-true}"
AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX="${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX:-30}"

for required_env in GITHUB_REPOSITORY GH_TOKEN; do
	if [ -z "${!required_env:-}" ]; then
		echo "::error::auto_release_stable.sh requires ${required_env}."
		exit 1
	fi
done
if ! [[ "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" -lt 1 ]; then
	echo "::error::AUTO_RELEASE_STABLE_MAX_ATTEMPTS must be a positive integer (got '${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}')."
	exit 1
fi

# A malformed knob falls back to its default with a warning: the back-off and
# the cleanup are additions to the dispatch decision and must not stop it.
# The digit caps keep the 10# arithmetic below far from overflow.
if ! [[ "${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS}" =~ ^[0-9]{1,6}$ ]]; then
	echo "::warning::AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS is not a non-negative integer of at most 6 digits; using 100."
	AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS=100
fi
AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS=$((10#${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS}))
if ! [[ "${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}" =~ ^[0-9]{1,5}$ ]]; then
	echo "::warning::AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS is not a non-negative integer of at most 5 digits; using 24."
	AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS=24
fi
AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS=$((10#${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}))
if ! [[ "${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX}" =~ ^[0-9]{1,3}$ ]]; then
	echo "::warning::AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX is not a non-negative integer of at most 3 digits; using 30."
	AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX=30
fi
AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX=$((10#${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX}))

emit_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT}"
	fi
}

skip_release()
{
	local reason="$1"
	local detail="${2:-}"
	echo "AUTO_RELEASE_SKIPPED reason=${reason}${detail:+ ${detail}}"
	emit_output dispatched false
	emit_output sha ""
	exit 0
}

is_truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

if ! is_truthy "${AUTO_RELEASE_STABLE_ENABLED}"; then
	skip_release disabled
fi

branch_tip="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${AUTO_RELEASE_STABLE_BRANCH}" | jq -r '.object.sha // empty')"
if ! [[ "${branch_tip}" =~ ^[0-9a-f]{40}$ ]]; then
	echo "::error::Could not resolve branch ${AUTO_RELEASE_STABLE_BRANCH} on ${GITHUB_REPOSITORY}."
	exit 1
fi

# The stable tag is annotated (`git tag -a` in the release job), so the ref
# points at a tag object that must be dereferenced to its commit.
tag_ref_error_file="$(mktemp)"
tag_missing="false"
if ! tag_ref_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${AUTO_RELEASE_STABLE_TAG}" 2>"${tag_ref_error_file}")"; then
	if grep -qE '(^|[^0-9])404([^0-9]|$)' "${tag_ref_error_file}"; then
		tag_ref_json=""
		tag_missing="true"
	else
		cat "${tag_ref_error_file}" >&2
		rm -f "${tag_ref_error_file}"
		skip_release guard_unavailable "lookup=tag:${AUTO_RELEASE_STABLE_TAG}"
	fi
fi
rm -f "${tag_ref_error_file}"
tag_object_type="$(printf '%s' "${tag_ref_json}" | jq -r '.object.type // empty' 2>/dev/null || true)"
tag_object_sha="$(printf '%s' "${tag_ref_json}" | jq -r '.object.sha // empty' 2>/dev/null || true)"
tag_commit=""
if [ "${tag_object_type}" = "tag" ] && [[ "${tag_object_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	if ! tag_commit="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/tags/${tag_object_sha}" | jq -er '.object.sha | select(test("^[0-9a-f]{40}$"))')"; then
		skip_release guard_unavailable "lookup=tag-object:${tag_object_sha}"
	fi
elif [ "${tag_object_type}" = "commit" ]; then
	tag_commit="${tag_object_sha}"
fi
if [ -z "${tag_commit}" ]; then
	if [ "${tag_missing}" = "true" ]; then
		echo "::warning::Tag ${AUTO_RELEASE_STABLE_TAG} does not exist on ${GITHUB_REPOSITORY}; treating the branch as unreleased."
	else
		skip_release guard_unavailable "lookup=tag:${AUTO_RELEASE_STABLE_TAG}"
	fi
fi

if [ "${branch_tip}" = "${tag_commit}" ]; then
	skip_release up_to_date "sha=${branch_tip}"
fi
if [[ "${tag_commit}" =~ ^[0-9a-f]{40}$ ]]; then
	# A differing tip only proves movement. Release only when the branch is
	# strictly ahead of the tag commit; a rewound or diverged branch would
	# retag consumers from history the release gate never validated.
	compare_status="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/compare/${tag_commit}...${branch_tip}" | jq -r '.status // empty')"
	case "${compare_status}" in
		ahead) ;;
		identical) skip_release up_to_date "sha=${branch_tip}" ;;
		*)
			echo "::warning::${AUTO_RELEASE_STABLE_BRANCH} (${branch_tip}) is '${compare_status:-unknown}' relative to the ${AUTO_RELEASE_STABLE_TAG} tag commit (${tag_commit}); refusing to release a rewound or diverged branch."
			skip_release branch_not_ahead "sha=${branch_tip} tag=${tag_commit} status=${compare_status:-unknown}"
			;;
	esac
fi

runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${AUTO_RELEASE_STABLE_WORKFLOW_FILE}/runs?per_page=30")"
# Issue #9473: a queued run older than ZOMBIE_RUN_MAX_QUEUED_HOURS is wedged
# (zero jobs, cannot be cancelled) and does not count as in flight.
# stderr is kept so an invalid ZOMBIE_RUN_MAX_QUEUED_HOURS warning shows.
zq_cutoff="$(zombie_queued_cutoff_epoch || echo 0)"
[[ "${zq_cutoff}" =~ ^[0-9]+$ ]] || zq_cutoff=0
zq_def="$(zombie_queued_jq_def 2>/dev/null || printf '%s' 'def is_wedged_queued($cutoff): false;')"
zombie_queued_log_ignored auto_release_gate "${zq_cutoff}" <<< "${runs_json}"
# Any branch: the gate's e2e-smoke-test job runs under a per-repository
# cancel-in-progress group, so dispatching a stable gate while the promote
# cycle's gate_only run on main is active would cancel that run.
active_count="$(printf '%s' "${runs_json}" | jq -r --argjson zq_cutoff "${zq_cutoff}" "${zq_def}"'[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending") | select(is_wedged_queued($zq_cutoff) | not)] | length')"
# The gate-runs endpoint above cannot report a promotion that has moved
# `stable` but has not dispatched its gate yet, so this requires a separate
# workflow-scoped read.
promote_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/promote-main-to-stable.yml/runs?per_page=30")"
# Any event: a scheduled cycle tick is about to dispatch (or is waiting on)
# its own gate run, which the group above would otherwise cancel.
zombie_queued_log_ignored auto_release_promote "${zq_cutoff}" <<< "${promote_runs_json}"
promote_active_count="$(printf '%s' "${promote_runs_json}" | jq -r --argjson zq_cutoff "${zq_cutoff}" "${zq_def}"'[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending") | select(is_wedged_queued($zq_cutoff) | not)] | length')"
active_count=$((active_count + promote_active_count))
# The legacy manual release path (mark-stable.yml) also writes refs/tags/stable.
legacy_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/mark-stable.yml/runs?per_page=30")"
zombie_queued_log_ignored auto_release_legacy "${zq_cutoff}" <<< "${legacy_runs_json}"
legacy_active_count="$(printf '%s' "${legacy_runs_json}" | jq -r --argjson zq_cutoff "${zq_cutoff}" "${zq_def}"'[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending") | select(is_wedged_queued($zq_cutoff) | not)] | length')"
active_count=$((active_count + legacy_active_count))
if [ "${active_count}" -gt 0 ]; then
	skip_release release_in_flight "active_runs=${active_count}"
fi
# Restrict to runs on the stable branch: the promote cycle runs the same
# workflow in gate_only mode on the default branch, and when the two branches
# share a tip a failed smoke gate there must not read as a failed release.
# `cancelled` is deliberately not counted: a cancelled gate proved nothing
# (concurrency, a runner loss, an operator), so the next tick simply tries
# again instead of spending an attempt.
failed_attempts_on_tip="$(printf '%s' "${runs_json}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure"))] | length')"
[[ "${failed_attempts_on_tip}" =~ ^[0-9]+$ ]] || failed_attempts_on_tip=0
if [ "${failed_attempts_on_tip}" -ge "${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}" ]; then
	last_conclusion_on_tip="$(printf '%s' "${runs_json}" | jq -r --arg sha "${branch_tip}" --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_sha == $sha and .head_branch == $branch and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure"))] | sort_by(.created_at) | last | .conclusion // empty')"
	echo "::warning::${AUTO_RELEASE_STABLE_WORKFLOW_FILE} already failed ${failed_attempts_on_tip} time(s) on ${AUTO_RELEASE_STABLE_BRANCH}@${branch_tip} (last '${last_conclusion_on_tip}', budget ${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}); not re-dispatching until the branch moves or a human re-runs the gate."
	skip_release last_gate_failed "sha=${branch_tip} conclusion=${last_conclusion_on_tip} attempts=${failed_attempts_on_tip} max=${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}"
fi
if [ "${failed_attempts_on_tip}" -gt 0 ]; then
	echo "Retrying ${AUTO_RELEASE_STABLE_WORKFLOW_FILE} on ${AUTO_RELEASE_STABLE_BRANCH}@${branch_tip}: ${failed_attempts_on_tip} failed attempt(s) so far, budget ${AUTO_RELEASE_STABLE_MAX_ATTEMPTS}."
fi

# Fixture cleanup before a retry (see the header). Runs before the back-off
# below, so a tick that waits for the queue still takes the previous cycle's
# fixtures out of it. Never fails or blocks the dispatch decision.
auto_release_cleanup_previous_fixtures()
{
	local done_run_ids fixture_login fixture_kind fixture_query fixture_search_json fixture_page fixture_rows=""
	local fixture_number fixture_type fixture_run fixture_closed=0 fixture_failed=0 fixture_skipped=0
	# Completed gate runs that did not succeed, from the listing already read.
	done_run_ids="$(printf '%s' "${runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "completed" and (.conclusion // "") != "success") | .id | select(type == "number") | tostring] | join(" ")' 2>/dev/null || true)"
	if [ -z "${done_run_ids}" ]; then
		return 0
	fi
	# Only fixtures this token's login wrote: the gate files them with the
	# same token, and the qualifier keeps other people's `E2E` issues out of
	# the one page each search reads.
	fixture_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
	if ! [[ "${fixture_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
		echo "::warning::AUTO_RELEASE_FIXTURE_CLEANUP outcome=read_failed lookup=user"
		return 0
	fi
	for fixture_kind in issue pull-request; do
		fixture_query="repo:${GITHUB_REPOSITORY} is:${fixture_kind} is:open in:title \"[E2E\" author:${fixture_login} -label:ai:orchestrator-tracking -label:ai:orchestrator-managed"
		if ! fixture_search_json="$(gh_retry gh api -X GET search/issues -f q="${fixture_query}" -f per_page=100 2>/dev/null)" \
			|| ! fixture_page="$(printf '%s' "${fixture_search_json}" | jq -r --arg login "${fixture_login}" 'if (.items | type) != "array" then error("no items") else .items[] end | select((.title // "") | startswith("[E2E ")) | select((.user.login // "") == $login) | select([.labels[]?.name] | (index("ai:orchestrator-tracking") == null and index("ai:orchestrator-managed") == null)) | [(.number | tostring), (if .pull_request then "pr" else "issue" end), ((.title // "") | (capture("\\(run (?<id>[1-9][0-9]{4,19})\\)") // capture("E2E Orchestrate Smoke (?<id>[1-9][0-9]{4,19})") // {id: "-"}) | .id)] | @tsv' 2>/dev/null)"; then
			echo "::warning::AUTO_RELEASE_FIXTURE_CLEANUP outcome=read_failed lookup=search kind=${fixture_kind}"
			return 0
		fi
		if [ -n "${fixture_page}" ]; then
			fixture_rows+="${fixture_page}"$'\n'
		fi
	done
	while IFS=$'\t' read -r fixture_number fixture_type fixture_run; do
		[[ "${fixture_number}" =~ ^[1-9][0-9]*$ ]] || continue
		[[ "${fixture_run}" =~ ^[1-9][0-9]*$ ]] || continue
		# Only a fixture of a gate run this listing shows as finished and not
		# successful; a running gate still owns its fixtures.
		case " ${done_run_ids} " in
			*" ${fixture_run} "*) ;;
			*) continue ;;
		esac
		if [ "$((fixture_closed + fixture_failed))" -ge "${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX}" ]; then
			fixture_skipped=$((fixture_skipped + 1))
			continue
		fi
		# No comment is posted: eleven workflows start on issue_comment, and
		# this runs when the queue is already deep. The log line is the record.
		if [ "${fixture_type}" = "pr" ]; then
			if ! gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/pulls/${fixture_number}" -f state=closed >/dev/null 2>&1; then
				fixture_failed=$((fixture_failed + 1))
				echo "::warning::AUTO_RELEASE_FIXTURE_CLEANUP outcome=close_failed number=${fixture_number} type=${fixture_type} gate_run=${fixture_run}"
				continue
			fi
		elif ! gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/${fixture_number}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1; then
			fixture_failed=$((fixture_failed + 1))
			echo "::warning::AUTO_RELEASE_FIXTURE_CLEANUP outcome=close_failed number=${fixture_number} type=${fixture_type} gate_run=${fixture_run}"
			continue
		fi
		fixture_closed=$((fixture_closed + 1))
		echo "AUTO_RELEASE_FIXTURE_CLEANUP outcome=closed number=${fixture_number} type=${fixture_type} gate_run=${fixture_run}"
	done <<< "${fixture_rows}"
	echo "AUTO_RELEASE_FIXTURE_CLEANUP outcome=done closed=${fixture_closed} failed=${fixture_failed} skipped=${fixture_skipped}"
	return 0
}
if is_truthy "${AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED}"; then
	auto_release_cleanup_previous_fixtures || true
fi

# Runner-queue back-off (see the header). The newest completed stable gate run
# that was not cancelled decides: a cancelled gate proved nothing either way.
last_gate_run="$(printf '%s' "${runs_json}" | jq -r --arg branch "${AUTO_RELEASE_STABLE_BRANCH}" '[.workflow_runs[]? | select(.status == "completed" and .head_branch == $branch and (.conclusion // "") != "cancelled" and (.conclusion // "") != "skipped")] | sort_by(.created_at) | last | if . == null then "" else "\(.id)\t\(.conclusion // "-")\t\((.updated_at // "") | (try fromdateiso8601 catch 0))" end' 2>/dev/null || true)"
IFS=$'\t' read -r last_gate_run_id last_gate_conclusion last_gate_ended_epoch <<< "${last_gate_run}" || true
if [ "${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS}" -gt 0 ] && [[ "${last_gate_run_id:-}" =~ ^[1-9][0-9]*$ ]] \
	&& { [ "${last_gate_conclusion:-}" = "failure" ] || [ "${last_gate_conclusion:-}" = "timed_out" ]; }; then
	now_epoch="$(date -u +%s)"
	[[ "${last_gate_ended_epoch:-}" =~ ^[0-9]+$ ]] || last_gate_ended_epoch=0
	last_gate_age_hours=$(( (now_epoch - last_gate_ended_epoch) / 3600 ))
	if [ "${last_gate_ended_epoch}" -eq 0 ] || [ "${last_gate_age_hours}" -lt 0 ]; then
		# No readable end time: the failure cannot be dated, so do not hold a
		# release on it.
		echo "AUTO_RELEASE_QUEUE_CHECK outcome=not_recent last_run=${last_gate_run_id} last_age_hours=unknown window_hours=${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}"
	elif [ "${last_gate_age_hours}" -ge "${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}" ]; then
		# The failure is older than the window: one cycle may test the queue again.
		echo "AUTO_RELEASE_QUEUE_CHECK outcome=not_recent last_run=${last_gate_run_id} last_age_hours=${last_gate_age_hours} window_hours=${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}"
	else
		queued_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=queued&per_page=1" 2>/dev/null || true)"
		queued_runs="$(printf '%s' "${queued_runs_json}" | jq -r 'if (.total_count | type) == "number" then .total_count else empty end' 2>/dev/null || true)"
		if ! [[ "${queued_runs}" =~ ^[0-9]+$ ]]; then
			echo "::warning::AUTO_RELEASE_QUEUE_CHECK outcome=read_failed lookup=queued_runs last_run=${last_gate_run_id} — dispatching without the runner-queue back-off."
		elif [ "${queued_runs}" -ge "${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS}" ]; then
			echo "::warning::${AUTO_RELEASE_STABLE_WORKFLOW_FILE} run ${last_gate_run_id} ended '${last_gate_conclusion}' ${last_gate_age_hours} hour(s) ago and ${queued_runs} run(s) are queued now (limit ${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS}); not starting another gate cycle until the queue drains or the failure is ${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS} hour(s) old."
			skip_release runner_queue_deep "sha=${branch_tip} queued=${queued_runs} max=${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS} last_run=${last_gate_run_id} last_age_hours=${last_gate_age_hours} window_hours=${AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS}"
		else
			echo "AUTO_RELEASE_QUEUE_CHECK outcome=clear queued=${queued_runs} max=${AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS} last_run=${last_gate_run_id} last_age_hours=${last_gate_age_hours}"
		fi
	fi
fi

echo "Dispatching ${AUTO_RELEASE_STABLE_WORKFLOW_FILE} on ${AUTO_RELEASE_STABLE_BRANCH} (tip ${branch_tip}, tag at ${tag_commit:-none})."
# Fully qualified: `stable` is also a tag, and the release gate accepts
# only refs/heads/stable.
gh_retry gh workflow run "${AUTO_RELEASE_STABLE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "refs/heads/${AUTO_RELEASE_STABLE_BRANCH}"
echo "AUTO_RELEASE_DISPATCHED sha=${branch_tip}"
emit_output dispatched true
emit_output sha "${branch_tip}"
