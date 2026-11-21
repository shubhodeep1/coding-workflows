#!/usr/bin/env bash
# Head-gate state for the review sweep (review_autofix_sweep.yml).
#
# Sourced by the "Enumerate open PRs and dispatch internal-review.yml" step
# from the trusted default-branch checkout (.ci-rerun-support). Not a manual
# script: nothing here runs when the file is executed directly.
#
# Why: the sweep dispatched internal-review.yml for every open non-draft PR
# without an active review run, including heads whose ai-review/head-gate is
# already success. On 2026-11-17 08:30 (run 44047353968) 5 of 15 dispatches
# were such heads; PR #9239 collected 40 head-gate statuses on one head since
# 2026-11-11 because its final CI `lint` failed and every review run ended
# `required_checks_failed`. The final-CI safety net
# (scripts/review_final_ci_sweep.sh) also spent its per-tick cap on
# gate-pending heads (`checked=10 gate_not_success=10 ensured=0`), because it
# could only learn a head's gate state by reading it, one head at a time.
#
# review_sweep_annotate_head_gate <owner/repo>
#   stdin:  the sweep's open-PR snapshot, a JSON array of objects that carry
#           `number` and `head_sha`.
#   stdout: the same array; each row gains `head_gate_state`, the lowercase
#           state of the `ai-review/head-gate` commit status on that PR's
#           head (`success`, `pending`, `failure`, `error`, `expected`, or
#           `absent` when the head has no such status). A row whose head moved
#           between the two reads, or that the read did not return, gets no
#           field (callers treat that as unknown).
#           An annotated row also gains `head_committed_at`, the head commit's
#           committer date normalised to UTC (`YYYY-MM-DDTHH:MM:SSZ`), when
#           GitHub returned a parseable one; the same query field, so no extra
#           call. scripts/review_sweep_round_cap.sh counts a PR's review rounds
#           from that time, so dispatches of an earlier head do not count
#           against a new push.
#   API:    one GraphQL query per 100 open PRs (`gh api graphql --paginate`),
#           aliased over `repository.pullRequests(states: OPEN)`, so a tick
#           with N open PRs costs ceil(N / 100) calls (§15). It replaces the
#           per-head `GET commits/<sha>/status` reads the final-CI sweep made
#           for every annotated head.
#   Fail-open: any read or parse failure prints the input unchanged and logs
#           `REVIEW_SWEEP_HEAD_GATE outcome=read_failed`; callers then behave
#           exactly as before this helper existed.
#
# review_sweep_final_ci_state <owner/repo> <head sha> [<workflow file>]
#   Needs scripts/pr_checks_lib.sh sourced (it uses that file's
#   PR_FINAL_CI_JQ_DECISION_DEF; without it the answer is read_failed).
#   stdout: one word for the final-CI runs of <workflow file> (default
#           ci.yml) on <head sha>, the same decision `_pr_ensure_final_ci` makes: `in_flight` (a run not completed),
#           `success`, `failure` (a full, non-sync-quick run failed and none
#           succeeded), `none` (no run, or only cancelled / timed-out /
#           sync-quick-failed runs: the final CI will be dispatched again), or
#           `read_failed`.
#   API:    one `GET actions/workflows/<file>/runs?head_sha=<sha>&per_page=20`.
#           The sweep calls it only for a gate-success head that is about to
#           be dispatched, so at most one call per such head per tick.
#   Fail-open: `read_failed` never suppresses a dispatch.
#
# Logs: `REVIEW_SWEEP_HEAD_GATE outcome=<annotated|read_failed> prs=<n>
#       annotated=<n> success=<n>` on stderr (the step prints it).

# review_sweep_final_ci_state shares _pr_ensure_final_ci's decision jq; load
# the library beside this file (definitions only) when the caller has not.
if [ -z "${PR_FINAL_CI_JQ_DECISION_DEF:-}" ] && [ -f "${BASH_SOURCE[0]%/*}/pr_checks_lib.sh" ]; then
	# shellcheck source=/dev/null
	source "${BASH_SOURCE[0]%/*}/pr_checks_lib.sh" 2>/dev/null || true
fi

REVIEW_SWEEP_HEAD_GATE_QUERY='query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        headRefOid
        commits(last: 1) { nodes { commit { oid committedDate status { context(name: "ai-review/head-gate") { state } } } } }
      }
    }
  }
}'

review_sweep_annotate_head_gate()
{
	local gate_repo="${1:-}" gate_input gate_pages gate_output gate_summary
	gate_input="$(cat)"
	if ! [[ "${gate_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${gate_repo}" == *..* ]] \
		|| ! printf '%s' "${gate_input}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=invalid_input" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	gate_pages="$(gh api graphql --paginate -f query="${REVIEW_SWEEP_HEAD_GATE_QUERY}" \
		-f owner="${gate_repo%%/*}" -f name="${gate_repo#*/}" 2>/dev/null | jq -s 'if all(.[]; type == "object" and (.data.repository.pullRequests.nodes | type) == "array") and length > 0 then . else error("bad page") end' 2>/dev/null || true)"
	if [ -z "${gate_pages}" ]; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=graphql prs=$(printf '%s' "${gate_input}" | jq 'length')" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	gate_output="$(printf '%s' "${gate_input}" | jq -c --argjson pages "${gate_pages}" '
		# committedDate is a GitTimestamp: ISO-8601 in the committer offset,
		# not UTC. Normalise it so it compares with run created_at.
		def gate_utc_ts:
			try (if type == "string" then
				((capture("^(?<d>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\\.[0-9]+)?(?<z>Z|(?<s>[+-])(?<h>[0-9]{2}):?(?<m>[0-9]{2}))$")) // null) as $t
				| if $t == null then null
					else ((($t.d + "Z") | fromdateiso8601)
						- (if $t.z == "Z" then 0 else (($t.h | tonumber) * 3600 + ($t.m | tonumber) * 60) * (if $t.s == "-" then -1 else 1 end) end))
						| todateiso8601 end
			else null end) catch null;
		([$pages[].data.repository.pullRequests.nodes[]
			| select(type == "object" and ((.number // null) | type) == "number")
			| ((.commits.nodes // [])[0].commit // {}) as $c
			| select(($c.oid // "") == (.headRefOid // "") and ($c.oid // "") != "")
			| {key: (.number | tostring),
				value: {sha: $c.oid, state: (($c.status.context.state // "absent") | ascii_downcase), committed_at: ($c.committedDate | gate_utc_ts)}}]
			| from_entries) as $gate
		| map(if type == "object" and ($gate[(.number | tostring)] // null) != null
				and $gate[(.number | tostring)].sha == (.head_sha // "")
			then . + {head_gate_state: $gate[(.number | tostring)].state}
				+ (if ($gate[(.number | tostring)].committed_at | type) == "string"
					then {head_committed_at: $gate[(.number | tostring)].committed_at} else {} end)
			else . end)' 2>/dev/null || true)"
	if ! printf '%s' "${gate_output}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=parse prs=$(printf '%s' "${gate_input}" | jq 'length')" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	gate_summary="$(printf '%s' "${gate_output}" | jq -r '"prs=\(length) annotated=\([.[] | select(.head_gate_state != null)] | length) success=\([.[] | select(.head_gate_state == "success")] | length)"')"
	echo "REVIEW_SWEEP_HEAD_GATE outcome=annotated ${gate_summary}" >&2
	printf '%s\n' "${gate_output}"
	return 0
}

review_sweep_final_ci_state()
{
	local fci_repo="${1:-}" fci_sha="${2:-}" fci_wf="${3:-ci.yml}" fci_runs
	if ! [[ "${fci_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${fci_repo}" == *..* ]] \
		|| ! [[ "${fci_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${fci_wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]]; then
		echo "read_failed"
		return 0
	fi
	fci_runs="$(gh api "repos/${fci_repo}/actions/workflows/${fci_wf}/runs?head_sha=${fci_sha}&per_page=20" 2>/dev/null || echo "")"
	# The decision jq is _pr_ensure_final_ci's own (PR_FINAL_CI_JQ_DECISION_DEF
	# in scripts/pr_checks_lib.sh); without that library nothing is decided.
	if [ -z "${PR_FINAL_CI_JQ_DECISION_DEF:-}" ]; then
		echo "read_failed"
		return 0
	fi
	case "$(printf '%s' "${fci_runs}" | jq -r --arg path ".github/workflows/${fci_wf}" --arg sha "${fci_sha}" "${PR_FINAL_CI_JQ_DECISION_DEF}"' _pr_final_ci_decision' 2>/dev/null | tail -n1)" in
		"exists in_flight") echo "in_flight" ;;
		"exists success") echo "success" ;;
		"exists failure") echo "failure" ;;
		"dispatch "*) echo "none" ;;
		*) echo "read_failed" ;;
	esac
	return 0
}
