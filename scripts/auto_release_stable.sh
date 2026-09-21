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
#   AUTO_RELEASE_DISPATCHED sha=<tip>
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
# and the legacy mark-stable path), at most 1 dispatch.
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
#   GITHUB_OUTPUT                       receives dispatched=/sha=

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

AUTO_RELEASE_STABLE_ENABLED="${AUTO_RELEASE_STABLE_ENABLED:-true}"
AUTO_RELEASE_STABLE_BRANCH="${AUTO_RELEASE_STABLE_BRANCH:-stable}"
AUTO_RELEASE_STABLE_TAG="${AUTO_RELEASE_STABLE_TAG:-stable}"
AUTO_RELEASE_STABLE_WORKFLOW_FILE="${AUTO_RELEASE_STABLE_WORKFLOW_FILE:-test-and-mark-stable.yml}"
AUTO_RELEASE_STABLE_MAX_ATTEMPTS="${AUTO_RELEASE_STABLE_MAX_ATTEMPTS:-3}"

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
# Any branch: the gate's e2e-smoke-test job runs under a per-repository
# cancel-in-progress group, so dispatching a stable gate while the promote
# cycle's gate_only run on main is active would cancel that run.
active_count="$(printf '%s' "${runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
# The gate-runs endpoint above cannot report a promotion that has moved
# `stable` but has not dispatched its gate yet, so this requires a separate
# workflow-scoped read.
promote_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/promote-main-to-stable.yml/runs?per_page=30")"
# Any event: a scheduled cycle tick is about to dispatch (or is waiting on)
# its own gate run, which the group above would otherwise cancel.
promote_active_count="$(printf '%s' "${promote_runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
active_count=$((active_count + promote_active_count))
# The legacy manual release path (mark-stable.yml) also writes refs/tags/stable.
legacy_runs_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/mark-stable.yml/runs?per_page=30")"
legacy_active_count="$(printf '%s' "${legacy_runs_json}" | jq -r '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')"
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

echo "Dispatching ${AUTO_RELEASE_STABLE_WORKFLOW_FILE} on ${AUTO_RELEASE_STABLE_BRANCH} (tip ${branch_tip}, tag at ${tag_commit:-none})."
gh_retry gh workflow run "${AUTO_RELEASE_STABLE_WORKFLOW_FILE}" \
	--repo "${GITHUB_REPOSITORY}" \
	--ref "${AUTO_RELEASE_STABLE_BRANCH}"
echo "AUTO_RELEASE_DISPATCHED sha=${branch_tip}"
emit_output dispatched true
emit_output sha "${branch_tip}"
