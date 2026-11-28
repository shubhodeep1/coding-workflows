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
#       annotated=<n> success=<n> priority=<n>` on stderr (the step prints it).
#
# Priority lane (issue #10048; helpers and env in scripts/pr_checks_lib.sh).
# While PRIORITY_LANE_ENABLED is on and MERGE_TRAIN_PRIORITY_LABELS is not
# empty, review_sweep_annotate_head_gate sends
# REVIEW_SWEEP_HEAD_GATE_PRIORITY_QUERY instead: the same query plus each
# PR's headRefName, isCrossRepository and closingIssuesReferences (with the
# limits of the merge train's _mt_pr_meta_prefetch) and the repository's
# `ai:priority` label. Same number of calls. Each annotated row gains
# `priority_lane` (true only for a verified linked-issue label,
# pr_priority_lane_verified), and the array is reordered priority-first (a
# stable partition). When that label does not exist yet it is created once
# (ensure_label_exists from scripts/label_helpers.sh, in a subshell). With the
# lane off, the query and the output are as before.
#
# review_sweep_priority_lane_hold <owner/repo> <default branch> <snapshot
# json> <hold file>
#   stdout: `<true|false> <priority csv|-> <hold since epoch|->`; the same
#           values go to <hold file> as `hold=<true|false>`, `priority=<csv|->`
#           and `since=<epoch|->` lines (read by
#           scripts/review_final_ci_sweep.sh through
#           FINAL_CI_SWEEP_PRIORITY_LANE_FILE).
#   API (§14): one paginated `GET actions/runs?status=queued` read, made only
#           while the lane is on, PRIORITY_LANE_MAX_DEFER_MINUTES > 0 and the
#           snapshot holds a priority PR. The sweep's existing run snapshots
#           (snapshot_active_review_runs) list only internal-review.yml and
#           review_autofix.yml runs; no existing sweep call lists the
#           security-audit.yml or final ci.yml runs, so this one read covers
#           all three.
#   Fail-open: a failed read prints `false`; no PR is held back.
#   Log: `PRIORITY_LANE_HOLD outcome=active|none|read_failed|disabled
#        [pr=<n> age_min=<m>]` on stderr.

# review_sweep_final_ci_state shares _pr_ensure_final_ci's decision jq; load
# the library beside this file (definitions only) when the caller has not.
if [ -z "${PR_FINAL_CI_JQ_DECISION_DEF:-}" ] && [ -f "${BASH_SOURCE[0]%/*}/pr_checks_lib.sh" ]; then
	# shellcheck source=/dev/null
	source "${BASH_SOURCE[0]%/*}/pr_checks_lib.sh" 2>/dev/null || true
fi

# Priority-lane annotation (issue #10048) also needs the label catalogue;
# label_helpers.sh is sourced only in the subshell that creates the label.
REVIEW_SWEEP_HEAD_GATE_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"

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

# REVIEW_SWEEP_HEAD_GATE_QUERY plus the priority-lane fields (issue #10048).
REVIEW_SWEEP_HEAD_GATE_PRIORITY_QUERY='query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    priorityLabel: label(name: "ai:priority") { name }
    pullRequests(states: OPEN, first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        headRefOid
        headRefName
        isCrossRepository
        closingIssuesReferences(first: 10) { nodes { number labels(first: 50) { nodes { name } } } }
        commits(last: 1) { nodes { commit { oid committedDate status { context(name: "ai-review/head-gate") { state } } } } }
      }
    }
  }
}'

review_sweep_annotate_head_gate()
{
	local gate_repo="${1:-}" gate_input gate_pages gate_output gate_summary
	local gate_query="${REVIEW_SWEEP_HEAD_GATE_QUERY}" gate_prio=false gate_prio_labels='[]'
	# Without scripts/pr_checks_lib.sh the lane stays off; this stub keeps the
	# jq program compiling so the head-gate annotation works as before.
	local gate_jq_def="${PR_PRIORITY_LANE_JQ_DEF:-}"
	[ -n "${gate_jq_def}" ] || gate_jq_def='def pr_priority_lane_verified($p; $w): null;'
	gate_input="$(cat)"
	if ! [[ "${gate_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${gate_repo}" == *..* ]] \
		|| ! printf '%s' "${gate_input}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=invalid_input" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	if [ -n "${PR_PRIORITY_LANE_JQ_DEF:-}" ] && declare -F _pr_priority_lane_enabled >/dev/null 2>&1 \
		&& _pr_priority_lane_enabled && declare -F _pr_priority_lane_settings >/dev/null 2>&1; then
		_pr_priority_lane_settings
		if [ "${PRIORITY_LANE_LABELS_JSON:-[]}" != "[]" ]; then
			gate_prio=true
			gate_prio_labels="${PRIORITY_LANE_LABELS_JSON}"
			gate_query="${REVIEW_SWEEP_HEAD_GATE_PRIORITY_QUERY}"
		fi
	fi
	gate_pages="$(gh api graphql --paginate -f query="${gate_query}" \
		-f owner="${gate_repo%%/*}" -f name="${gate_repo#*/}" 2>/dev/null | jq -s 'if all(.[]; type == "object" and (.data.repository.pullRequests.nodes | type) == "array") and length > 0 then . else error("bad page") end' 2>/dev/null || true)"
	if [ -z "${gate_pages}" ]; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=graphql prs=$(printf '%s' "${gate_input}" | jq 'length')" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	# The `ai:priority` label is created once, when the lane is on, the label
	# is in the list and GitHub answered that it does not exist (issue #10048).
	if [ "${gate_prio}" = "true" ] && printf '%s' "${gate_prio_labels}" | jq -e 'index("ai:priority") != null' >/dev/null 2>&1 \
		&& printf '%s' "${gate_pages}" | jq -e '(.[0].data.repository | has("priorityLabel")) and .[0].data.repository.priorityLabel == null' >/dev/null 2>&1; then
		if ( source "${REVIEW_SWEEP_HEAD_GATE_SCRIPT_DIR}/label_helpers.sh" >/dev/null 2>&1 \
			&& ensure_label_exists "ai:priority" "${gate_repo}" ) >/dev/null 2>&1; then
			echo "PRIORITY_LANE_LABEL outcome=created label=ai:priority" >&2
		else
			echo "::warning::PRIORITY_LANE_LABEL outcome=create_failed label=ai:priority" >&2
		fi
	fi
	gate_output="$(printf '%s' "${gate_input}" | jq -c --argjson pages "${gate_pages}" --argjson prio_on "${gate_prio}" \
		--argjson wanted "${gate_prio_labels}" --arg prefix "${MERGE_TRAIN_HEAD_REF_PREFIX:-ai/issue-}" "${gate_jq_def}"'
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
				value: {sha: $c.oid, state: (($c.status.context.state // "absent") | ascii_downcase), committed_at: ($c.committedDate | gate_utc_ts),
					pl: {gh_head_ref: (.headRefName // null), cross_repo: (if (.isCrossRepository | type) == "boolean" then .isCrossRepository else null end),
						closing: (if (.closingIssuesReferences.nodes | type) == "array"
							then [.closingIssuesReferences.nodes[] | select(type == "object") | {number: .number, labels: [(.labels.nodes // [])[] | .name? // empty]}]
							else null end)}}}]
			| from_entries) as $gate
		| map(if type == "object" and ($gate[(.number | tostring)] // null) != null
				and $gate[(.number | tostring)].sha == (.head_sha // "")
			then . + {head_gate_state: $gate[(.number | tostring)].state}
				+ (if ($gate[(.number | tostring)].committed_at | type) == "string"
					then {head_committed_at: $gate[(.number | tostring)].committed_at} else {} end)
				+ (if $prio_on then
					{priority_lane: ((($gate[(.number | tostring)].pl + {head_ref: (.head_ref // "")})
						| pr_priority_lane_verified($prefix; $wanted)) == true)}
					else {} end)
			else . end)
		| if $prio_on then (map(select(type == "object" and .priority_lane == true)) + map(select((type == "object" and .priority_lane == true) | not))) else . end' 2>/dev/null || true)"
	if [ -z "${gate_output}" ] || ! printf '%s' "${gate_output}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=parse prs=$(printf '%s' "${gate_input}" | jq 'length')" >&2
		printf '%s\n' "${gate_input}"
		return 0
	fi
	gate_summary="$(printf '%s' "${gate_output}" | jq -r '"prs=\(length) annotated=\([.[] | select(.head_gate_state != null)] | length) success=\([.[] | select(.head_gate_state == "success")] | length) priority=\([.[] | select(.priority_lane == true)] | length)"')"
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

review_sweep_priority_lane_hold()
{
	local plh_repo="${1:-}" plh_db="${2:-}" plh_snapshot="${3:-}" plh_file="${4:-}"
	local plh_csv="" plh_now plh_cutoff=0 plh_upper plh_lower plh_runs plh_hit plh_hold=false plh_since=""
	if [ -z "${PR_PRIORITY_LANE_JQ_DEF:-}" ] || ! declare -F _pr_priority_lane_enabled >/dev/null 2>&1 || ! _pr_priority_lane_enabled; then
		echo "PRIORITY_LANE_HOLD outcome=disabled reason=lane_off" >&2
		review_sweep_priority_lane_hold_out false "" "${plh_file}"
		return 0
	fi
	_pr_priority_lane_settings
	plh_csv="$(printf '%s' "${plh_snapshot}" | jq -r '[.[]? | select(type == "object" and .priority_lane == true) | .number | tostring
		| select(test("^[1-9][0-9]*$"))] | unique_by(tonumber) | join(",")' 2>/dev/null || true)"
	[[ "${plh_csv}" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || plh_csv=""
	if [ -z "${plh_csv}" ]; then
		echo "PRIORITY_LANE_HOLD outcome=none reason=no_priority_pr" >&2
		review_sweep_priority_lane_hold_out false "" "${plh_file}"
		return 0
	fi
	if [ "${PRIORITY_LANE_MAX_DEFER_S:-0}" -le 0 ] 2>/dev/null; then
		echo "PRIORITY_LANE_HOLD outcome=disabled reason=max_defer_zero" >&2
		review_sweep_priority_lane_hold_out false "${plh_csv}" "${plh_file}"
		return 0
	fi
	if ! [[ "${plh_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${plh_repo}" == *..* ]]; then
		echo "PRIORITY_LANE_HOLD outcome=read_failed reason=invalid_repo" >&2
		review_sweep_priority_lane_hold_out false "${plh_csv}" "${plh_file}"
		return 0
	fi
	plh_now="$(date -u +%s 2>/dev/null)" || plh_now=""
	[[ "${plh_now}" =~ ^[0-9]+$ ]] || plh_now=0
	if declare -F zombie_queued_cutoff_epoch >/dev/null 2>&1; then
		plh_cutoff="$(zombie_queued_cutoff_epoch 2>/dev/null || echo 0)"
	elif [[ "${ZOMBIE_RUN_MAX_QUEUED_HOURS:-24}" =~ ^0+$ ]]; then
		# As zombie_queued_cutoff_epoch: 0 hours turns the wedged filter off.
		plh_cutoff=0
	elif declare -F _merge_lease_wedged_queued_seconds >/dev/null 2>&1; then
		plh_cutoff=$(( plh_now - $(_merge_lease_wedged_queued_seconds 2>/dev/null || echo "${plh_now}") ))
	fi
	[[ "${plh_cutoff}" =~ ^[0-9]+$ ]] || plh_cutoff=0
	# `created` only narrows the listing; the jq filter stays authoritative.
	plh_upper="$(date -u -d "@$(( plh_now - PRIORITY_LANE_WAIT_S ))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
	plh_lower="*"
	[ "${plh_cutoff}" -gt 0 ] && plh_lower="$(date -u -d "@${plh_cutoff}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '*')"
	plh_runs=""
	if [ "${plh_now}" -gt 0 ] && [ -n "${plh_upper}" ]; then
		if declare -F gh_api_retry >/dev/null 2>&1; then
			plh_runs="$(gh_api_retry --idempotent --paginate -X GET "repos/${plh_repo}/actions/runs" \
				-f status=queued -f per_page=100 -f "created=${plh_lower}..${plh_upper}" 2>/dev/null | jq -sc '.' 2>/dev/null || true)"
		else
			plh_runs="$(gh api --paginate -X GET "repos/${plh_repo}/actions/runs" \
				-f status=queued -f per_page=100 -f "created=${plh_lower}..${plh_upper}" 2>/dev/null | jq -sc '.' 2>/dev/null || true)"
		fi
	fi
	if ! printf '%s' "${plh_runs}" | jq -e 'length > 0 and all(.[]; type == "object" and (.workflow_runs | type) == "array")' >/dev/null 2>&1; then
		echo "PRIORITY_LANE_HOLD outcome=read_failed reason=runs_listing" >&2
		review_sweep_priority_lane_hold_out false "${plh_csv}" "${plh_file}"
		return 0
	fi
	plh_hit="$(_pr_priority_lane_pressure "${plh_runs}" "${plh_csv}" "${plh_cutoff}" "${plh_now}" "${plh_db}")"
	if [[ "${plh_hit}" =~ ^([1-9][0-9]*)\ ([0-9]+)(\ ([1-9][0-9]*))?$ ]]; then
		plh_hold=true
		plh_since="${BASH_REMATCH[4]}"
		echo "PRIORITY_LANE_HOLD outcome=active pr=${BASH_REMATCH[1]} age_min=${BASH_REMATCH[2]}" >&2
	else
		echo "PRIORITY_LANE_HOLD outcome=none reason=no_queued_priority_run" >&2
	fi
	review_sweep_priority_lane_hold_out "${plh_hold}" "${plh_csv}" "${plh_file}" "${plh_since}"
	return 0
}

# review_sweep_priority_lane_hold_out <hold> <csv> <file> [<since_epoch>]:
# prints the hold line (`<hold> <csv|-> <since_epoch|->`) and writes the hold
# file (a failed write only loses the final-CI hold). <since_epoch> is when
# the hold started; the starvation cap is counted from it.
review_sweep_priority_lane_hold_out()
{
	local plo_hold="${1:-false}" plo_csv="${2:-}" plo_since="${4:-}"
	[ -n "${plo_csv}" ] || plo_csv="-"
	[[ "${plo_since}" =~ ^[1-9][0-9]{0,11}$ ]] || plo_since="-"
	if [ -n "${3:-}" ]; then
		printf 'hold=%s\npriority=%s\nsince=%s\n' "${plo_hold}" "${plo_csv}" "${plo_since}" > "$3" 2>/dev/null || true
	fi
	echo "${plo_hold} ${plo_csv} ${plo_since}"
}
