#!/usr/bin/env bash
# Review-run helpers for the E2E smoke job in
# .github/workflows/test-and-mark-stable.yml (issue #5093, security finding
# smoke-review-dispatches-unmerged-workflow).
#
# Phase 3c (Bug B fallback) and Phase 4b (editor retry) dispatch
# ${REVIEW_WORKFLOW_FILE} at TEST_REPO's default branch, never at the smoke
# PR's branch: a dispatch at ai/issue-<N> would run that branch's unmerged
# copy of the wrapper with TEST_REPO's secrets (secrets: inherit). A
# default-branch run carries the default branch's head_branch / head_sha, so
# it is found by the PR run name the wrappers set on workflow_dispatch
# (`Internal: AI Review & Autofix [pr:<N>]`, `AI Review [pr:<N>]`) and tied to
# the smoke PR by the commit its own codex-agent job checked out.
#
# Sourced by the steps; defines functions only. The functions call `gh api`
# once per read and leave retries to their callers, which already poll.
# Every function validates its inputs before any call.

# smoke_review_pr_named_runs <repo> <workflow_file> <dispatch_ref> <pr_number>
#
# Lists the workflow_dispatch runs of <workflow_file> on <dispatch_ref> whose
# run name ends with " [pr:<pr_number>]". The run name alone proves nothing:
# GitHub evaluates run-name from the workflow file at the dispatched ref, so
# any writer can dispatch a run named for this PR from their own branch
# (issue #5094), and that run writes its own codex-agent log, the evidence
# the callers correlate on. Only head_branch == <dispatch_ref> shows the run
# executed the reviewed default-branch wrapper, so a run whose head_branch is
# another branch, null, or empty is dropped (fail closed; none of the 200
# most recent dispatch runs in coding-workflows had a null head_branch). The
# branch is checked here rather than with the API's branch filter.
#   Input:   owner/repo; a workflow basename (*.yml); the dispatch branch;
#            a positive PR number.
#   Output:  a JSON array, oldest id first, of
#            {id, status, conclusion, created_at, updated_at, head_sha, name,
#            path, event, display_title}. Phantom "workflow file issue" runs
#            (name == path) are dropped.
#   Calls:   1 REST read, GET repos/<repo>/actions/workflows/<file>/runs with
#            event=workflow_dispatch, per_page=100 (newest first, so a run
#            dispatched moments ago is on this page).
#   Returns: 0 with the array (possibly []); 1 on invalid input, an API
#            failure, or a malformed payload (nothing printed).
smoke_review_pr_named_runs()
{
	local repo="${1:-}"
	local workflow_file="${2:-}"
	local dispatch_ref="${3:-}"
	local pr_number="${4:-}"
	local response=""
	local runs=""

	if ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| ! [[ "${workflow_file}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*\.yml$ ]] \
		|| ! [[ "${dispatch_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] \
		|| ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then
		return 1
	fi

	# -X GET is required: gh api infers POST from -f parameters.
	if ! response=$(gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"repos/${repo}/actions/workflows/${workflow_file}/runs" \
		-f "event=workflow_dispatch" \
		-f "per_page=100" \
		2>/dev/null); then
		return 1
	fi
	if [ -z "${response}" ]; then
		return 1
	fi

	if ! runs=$(printf '%s' "${response}" | jq -ce --arg pr "${pr_number}" --arg ref "${dispatch_ref}" '
		if type == "object"
			and (.workflow_runs | type == "array")
			and all(.workflow_runs[];
				type == "object"
				and (.id | type == "number") and .id > 0 and .id == (.id | floor)
				and (.status | type == "string")
				and (.created_at | type == "string")
				and ((.conclusion == null) or (.conclusion | type == "string")))
		then
			[
				.workflow_runs[]
				| select(.event == "workflow_dispatch")
				| select(.head_branch == $ref)
				| select((.name // "") != (.path // ""))
				| select((.display_title | type == "string")
					and (.display_title | endswith(" [pr:" + $pr + "]")))
				| {id, status, conclusion, created_at, updated_at, head_sha, name, path, event, display_title}
			]
			| sort_by(.id)
		else
			error("malformed workflow run list")
		end
	' 2>/dev/null); then
		return 1
	fi
	printf '%s\n' "${runs}"
}

# smoke_review_checked_out_sha <repo> <run_id>
#
# Prints the commit a review run checked out, from its own log: the
# "Checkout PR head branch" step of review_autofix.yml's codex-agent job logs
# `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` before any
# reviewer or editor reads PR content, so the FIRST line-anchored match is
# the genuine one; a later line printed from reviewed content cannot win.
# "Line-anchored" means the text follows the line's single prefix token (the
# runner's timestamp) and nothing else. The token's shape is not checked:
# the first-match rule carries the security property, so a change to the
# timestamp format must not turn a genuine run into a false rc=2.
#   Input:   owner/repo; a positive run id.
#   Output:  the 40-hex SHA.
#   Calls:   1 REST read of the run's jobs (per_page=100), then at most 1 read
#            of the codex-agent job's log (the same job-name match Phase 4's
#            live-log probe uses).
#   Returns: 0 found; 1 API failure, malformed jobs payload, or empty/whitespace-only log (retryable);
#            2 a non-empty log was read but has no such line; 3 the run has no
#            codex-agent job; 4 invalid input.
smoke_review_checked_out_sha()
{
	local repo="${1:-}"
	local run_id="${2:-}"
	local jobs_json=""
	local job_id=""
	local log_file=""
	local line=""

	if ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| ! [[ "${run_id}" =~ ^[1-9][0-9]*$ ]]; then
		return 4
	fi

	if ! jobs_json=$(gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"repos/${repo}/actions/runs/${run_id}/jobs" \
		-f "per_page=100" \
		2>/dev/null); then
		return 1
	fi
	if ! job_id=$(printf '%s' "${jobs_json}" | jq -er '
		if type == "object" and (.jobs | type == "array") then
			([.jobs[] | select(type == "object") | select((.name // "") | test("(^| / )codex-agent( \\([^)]*\\))?$")) | .id] | last) // ""
			| tostring
		else
			error("malformed jobs payload")
		end
	' 2>/dev/null); then
		return 1
	fi
	if [ -z "${job_id}" ]; then
		return 3
	fi
	if ! [[ "${job_id}" =~ ^[1-9][0-9]*$ ]]; then
		return 1
	fi

	log_file="$(mktemp)"
	# Job logs carry ANSI colour codes; without --allow-escape-sequences gh
	# refuses the body ("the response contains terminal escape sequences")
	# even when stdout is a file, and every genuine run read as rc=1 (the
	# same fix as scripts/workflow_failure_heal_intake.sh).
	if ! gh api --allow-escape-sequences "repos/${repo}/actions/jobs/${job_id}/logs" > "${log_file}" 2>/dev/null; then
		rm -f "${log_file}"
		return 1
	fi
	# An empty log may still be uploading; only a non-empty log can prove
	# the checkout line is absent. Let the caller's deadline bound retries.
	if ! grep -aq '[^[:space:]]' "${log_file}"; then
		rm -f "${log_file}"
		return 1
	fi
	line=$(tr -d '\r' < "${log_file}" \
		| grep -aE '^[^[:space:]]+ Captured INITIAL_HEAD_SHA=[0-9a-f]{40} for stale-base detection\.$' \
		| head -n 1 || true)
	rm -f "${log_file}"
	if [ -z "${line}" ]; then
		return 2
	fi
	line="${line#* Captured INITIAL_HEAD_SHA=}"
	printf '%s\n' "${line%% *}"
}

# smoke_review_sha_descends_from <repo> <base_sha> <head_sha>
#
# Tells whether <head_sha> is <base_sha> or a descendant of it.
#   Input:   owner/repo; two 40-hex SHAs.
#   Calls:   1 REST read, GET repos/<repo>/compare/<base>...<head> (per_page=1).
#   Returns: 0 when the compare status is identical or ahead; 1 when it is
#            behind or diverged; 2 on invalid input, an API failure, or any
#            other status.
smoke_review_sha_descends_from()
{
	local repo="${1:-}"
	local base_sha="${2:-}"
	local head_sha="${3:-}"
	local compare_status=""

	if ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| ! [[ "${base_sha}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		return 2
	fi

	if ! compare_status=$(gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"repos/${repo}/compare/${base_sha}...${head_sha}" \
		-f "per_page=1" \
		--jq '.status // ""' \
		2>/dev/null); then
		return 2
	fi
	case "${compare_status}" in
		identical|ahead)
			return 0
			;;
		behind|diverged)
			return 1
			;;
		*)
			return 2
			;;
	esac
}
