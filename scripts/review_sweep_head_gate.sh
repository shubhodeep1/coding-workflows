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
        baseRefOid
        baseRefName
        mergeStateStatus
        autoMergeRequest { enabledAt }
        commits(last: 1) { nodes { commit { oid committedDate status {
          context(name: "ai-review/head-gate") { state createdAt }
          verdict: context(name: "ai-review/rb-judge-verdict") { state description createdAt }
          mwalert: context(name: "ai-review/merge-wait-alert") { state }
        } } } }
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
			| . as $pr
			| {key: (.number | tostring),
				value: {sha: $c.oid, state: (($c.status.context.state // "absent") | ascii_downcase), committed_at: ($c.committedDate | gate_utc_ts),
					# Verdict-wait fields (issue #9991), same query, no extra call.
					extra: ({}
						+ (($c.status.context.createdAt // null) | gate_utc_ts | if type == "string" then {head_gate_created_at: .} else {} end)
						+ (if (($c.status.verdict.state // "") | ascii_downcase) == "success" and (($c.status.verdict.description // null) | type) == "string"
							then {rb_judge_verdict: $c.status.verdict.description}
								+ (($c.status.verdict.createdAt // null) | gate_utc_ts | if type == "string" then {rb_judge_verdict_at: .} else {} end)
							else {} end)
						+ {merge_wait_alerted: ((($c.status.mwalert.state // "") | ascii_downcase) == "success")}
						+ (if (($pr.baseRefOid // null) | type) == "string" and ($pr.baseRefOid | test("^[0-9a-f]{40}$")) then {base_sha: $pr.baseRefOid} else {} end)
						+ (if (($pr.baseRefName // null) | type) == "string" and $pr.baseRefName != "" then {base_ref: $pr.baseRefName} else {} end)
						+ (if (($pr.mergeStateStatus // null) | type) == "string" then {merge_state_status: ($pr.mergeStateStatus | ascii_downcase)} else {} end)
						+ {auto_merge_enabled: ((($pr.autoMergeRequest // null) | type) == "object")})}}]
			| from_entries) as $gate
		| map(if type == "object" and ($gate[(.number | tostring)] // null) != null
				and $gate[(.number | tostring)].sha == (.head_sha // "")
			then . + {head_gate_state: $gate[(.number | tostring)].state}
				+ (if ($gate[(.number | tostring)].committed_at | type) == "string"
					then {head_committed_at: $gate[(.number | tostring)].committed_at} else {} end)
				+ ($gate[(.number | tostring)].extra // {})
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

# ---------------------------------------------------------------------------
# Verdict wait (issue #9991). The sweep re-dispatched review runs for heads
# that already had a verdict and were only waiting to merge (#9984: 12 runs on
# one unchanged approved head in six hours; #9573: 21 judge runs on one head).
#
# review_sweep_verdict_wait_reason <owner/repo> <row json> [<final ci state>]
#   stdout: a wait reason, or nothing (= dispatch as before). The row is one
#           annotated snapshot row (review_sweep_annotate_head_gate). Reasons:
#             final_ci_merge_pending     gate success and the
#                                        FINAL_CI_MERGE_PENDING_LABEL label
#                                        (default ai:final-ci-merge-pending);
#             auto_merge_checks_pending  gate success, auto-merge enabled and
#                                        the final CI run in flight;
#             auto_merge_checks_missing  gate success, auto-merge enabled and
#                                        the final CI run succeeded, so a
#                                        required check other than the final
#                                        CI holds the merge (#9939);
#             rb_judge_merge_deferred    no gate success, but the head carries
#                                        a judge merge verdict
#                                        (ai-review/rb-judge-verdict) and
#                                        _pr_rb_judge_verdict_waiting
#                                        (scripts/pr_checks_lib.sh) finds no
#                                        relevant change since it.
#           <final ci state> is review_sweep_final_ci_state's word for the head,
#           read by the caller only for a gate-success head with auto-merge
#           enabled; `failure`, `none` and `read_failed` print nothing (the
#           sweep's failed-head skip and the final-CI safety net own those).
#   Switch: VERDICT_WAIT_DISPATCH_SKIP_ENABLED (default true; 1/true/yes/on);
#           anything else prints nothing.
#   API:    none, except _pr_base_freshness's compare + PR-files reads for a
#           judge-verdict head whose base moved since the verdict.
#   Fail-open: a row without the annotation (GraphQL read failed), an
#           unparseable verdict or a missing library prints nothing.
#
# review_sweep_verdict_merge_wait_alert <owner/repo> <row json> <reason> <dry run>
#   G3 of #9991. For a skipped head whose verdict is at least
#   VERDICT_MERGE_WAIT_ALERT_HOURS old (default 6; 0 turns alerts off) and
#   that carries no ai-review/merge-wait-alert status yet, names the blocker
#   (the lease holder when the PR waits for the merge lease, plus
#   _pr_ruleset_required_check_blockers' summary such as `ci/final (missing)`),
#   posts the once-per-head marker status ai-review/merge-wait-alert
#   (state success, never blocks a merge) and only then sends one Telegram
#   WARNING (tg_send_msg, after the same _tg_should_send and secrets checks as
#   the merge train's stall alert). A failed marker write sends nothing, so
#   the alert never repeats per tick. At most
#   REVIEW_SWEEP_VERDICT_ALERT_MAX_PER_TICK (5) alerts per tick.
#   Verdict age: head_gate_created_at for a gate-success head, else
#   rb_judge_verdict_at. API per alert: the ruleset blocker reads (1 to 3),
#   the lease read (2) when the PR waits for the lease, 1 status POST.
#   Log: VERDICT_MERGE_WAIT_ALERT pr=#<n> head_sha=<sha> age_hours=<n>
#        reason=<wait> blocker=<summary|-> outcome=<sent|not_delivered|
#        telegram_unavailable|telegram_unconfigured|below_alert_level|
#        marker_write_failed|dry_run|tick_limit>
# ---------------------------------------------------------------------------
REVIEW_SWEEP_VERDICT_ALERT_MAX_PER_TICK=5
REVIEW_SWEEP_VERDICT_ALERTS_SENT=0
REVIEW_SWEEP_VERDICT_SEP=$'\x1f'

review_sweep_verdict_wait_enabled()
{
	case "$(printf '%s' "${VERDICT_WAIT_DISPATCH_SKIP_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on) return 0 ;;
	esac
	return 1
}

review_sweep_verdict_wait_reason()
{
	local vr_repo="${1:-}" vr_row="${2:-}" vr_final_ci="${3:-}" vr_fields vr_labels
	local vr_pr="" vr_head="" vr_state="" vr_auto="" vr_base_sha="" vr_base_ref="" vr_ms="" vr_verdict=""
	review_sweep_verdict_wait_enabled || return 0
	if ! [[ "${vr_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${vr_repo}" == *..* ]]; then
		return 0
	fi
	vr_fields="$(printf '%s' "${vr_row}" | jq -r --arg sep "${REVIEW_SWEEP_VERDICT_SEP}" '
		if type == "object" then
			[((.number // "") | tostring), (.head_sha // ""), (.head_gate_state // ""),
				(if .auto_merge_enabled == true then "true" else "false" end),
				(.base_sha // ""), (.base_ref // ""), (.merge_state_status // ""), (.rb_judge_verdict // "")]
			| map(tostring | gsub("[[:cntrl:]]"; "")) | join($sep)
		else empty end' 2>/dev/null)" || return 0
	[ -n "${vr_fields}" ] || return 0
	IFS="${REVIEW_SWEEP_VERDICT_SEP}" read -r vr_pr vr_head vr_state vr_auto vr_base_sha vr_base_ref vr_ms vr_verdict <<< "${vr_fields}" || true
	# No annotation for this row: the GraphQL read failed open.
	[ -n "${vr_state}" ] || return 0
	vr_labels="$(printf '%s' "${vr_row}" | jq -c '.labels // [] | if type == "array" then . else [] end' 2>/dev/null || echo '[]')"
	if [ "${vr_state}" = "success" ]; then
		if printf '%s' "${vr_labels}" | jq -e --arg l "${FINAL_CI_MERGE_PENDING_LABEL:-ai:final-ci-merge-pending}" 'index($l) != null' >/dev/null 2>&1; then
			echo "final_ci_merge_pending"
		elif [ "${vr_auto}" = "true" ]; then
			case "${vr_final_ci}" in
				in_flight) echo "auto_merge_checks_pending" ;;
				success) echo "auto_merge_checks_missing" ;;
			esac
		fi
		return 0
	fi
	[ -n "${vr_verdict}" ] || return 0
	type _pr_rb_judge_verdict_waiting >/dev/null 2>&1 || return 0
	if PR_CHECKS_REPOSITORY="${vr_repo}" _pr_rb_judge_verdict_waiting "${vr_pr}" "${vr_head}" "${vr_base_ref}" "${vr_base_sha}" "${vr_ms}" "${vr_labels}" "${vr_auto}" "${vr_verdict}"; then
		echo "rb_judge_merge_deferred"
	fi
	return 0
}

review_sweep_verdict_merge_wait_alert()
{
	local ma_repo="${1:-}" ma_row="${2:-}" ma_reason="${3:-}" ma_dry="${4:-false}" ma_hours="${VERDICT_MERGE_WAIT_ALERT_HOURS:-6}"
	local ma_fields ma_pr="" ma_head="" ma_state="" ma_gate_at="" ma_verdict_at="" ma_alerted="" ma_base_ref="" ma_lease=""
	local ma_since ma_since_epoch ma_now ma_age_h ma_blocker="" ma_desc ma_outcome="" ma_msg
	[[ "${ma_hours}" =~ ^[0-9]{1,4}$ ]] || ma_hours=6
	ma_hours=$((10#${ma_hours}))
	[ "${ma_hours}" -gt 0 ] || return 0
	if ! [[ "${ma_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${ma_repo}" == *..* ]] || ! [[ "${ma_reason}" =~ ^[a-z_]+$ ]]; then
		return 0
	fi
	ma_fields="$(printf '%s' "${ma_row}" | jq -r --arg sep "${REVIEW_SWEEP_VERDICT_SEP}" --arg lease "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" '
		if type == "object" then
			[((.number // "") | tostring), (.head_sha // ""), (.head_gate_state // ""), (.head_gate_created_at // ""),
				(.rb_judge_verdict_at // ""), (if .merge_wait_alerted == true then "true" else "false" end), (.base_ref // ""),
				(if ((.labels // []) | type == "array" and index($lease) != null) then "true" else "false" end)]
			| map(tostring | gsub("[[:cntrl:]]"; "")) | join($sep)
		else empty end' 2>/dev/null)" || return 0
	IFS="${REVIEW_SWEEP_VERDICT_SEP}" read -r ma_pr ma_head ma_state ma_gate_at ma_verdict_at ma_alerted ma_base_ref ma_lease <<< "${ma_fields}" || true
	[[ "${ma_pr}" =~ ^[1-9][0-9]*$ ]] && [[ "${ma_head}" =~ ^[0-9a-f]{40}$ ]] || return 0
	[ "${ma_alerted}" != "true" ] || return 0
	if [ "${ma_state}" = "success" ]; then
		ma_since="${ma_gate_at}"
	else
		ma_since="${ma_verdict_at}"
	fi
	[[ "${ma_since}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 0
	ma_since_epoch="$(date -u -d "${ma_since}" +%s 2>/dev/null || echo "")"
	ma_now="$(date -u +%s)"
	[[ "${ma_since_epoch}" =~ ^[0-9]+$ ]] || return 0
	[ $((ma_now - ma_since_epoch)) -ge $((ma_hours * 3600)) ] || return 0
	ma_age_h=$(((ma_now - ma_since_epoch) / 3600))
	local ma_log="VERDICT_MERGE_WAIT_ALERT pr=#${ma_pr} head_sha=${ma_head} age_hours=${ma_age_h} reason=${ma_reason}"
	if [ "${REVIEW_SWEEP_VERDICT_ALERTS_SENT}" -ge "${REVIEW_SWEEP_VERDICT_ALERT_MAX_PER_TICK}" ]; then
		echo "${ma_log} blocker=- outcome=tick_limit"
		return 0
	fi
	if [ "${ma_lease}" = "true" ] && [ -n "${ma_base_ref}" ] && type _merge_lease_read >/dev/null 2>&1 \
		&& PR_CHECKS_REPOSITORY="${ma_repo}" _merge_lease_read "${ma_base_ref}" >/dev/null 2>&1 \
		&& [ "${MERGE_LEASE_STATE:-}" = "held" ] && [[ "${MERGE_LEASE_PR:-}" =~ ^[1-9][0-9]*$ ]]; then
		ma_blocker="lease held by #${MERGE_LEASE_PR}"
	fi
	if [ -n "${ma_base_ref}" ] && type _pr_ruleset_required_check_blockers >/dev/null 2>&1; then
		PR_RULESET_BLOCKERS_SUMMARY=""
		PR_CHECKS_REPOSITORY="${ma_repo}" _pr_ruleset_required_check_blockers "${ma_head}" "${ma_base_ref}" >/dev/null 2>&1 || true
		if [ -n "${PR_RULESET_BLOCKERS_SUMMARY:-}" ]; then
			ma_blocker="${ma_blocker:+${ma_blocker}; }${PR_RULESET_BLOCKERS_SUMMARY}"
		fi
	fi
	ma_blocker="$(LC_ALL=C printf '%s' "${ma_blocker}" | tr -cd ' -~' | cut -c1-110)"
	[ -n "${ma_blocker}" ] || ma_blocker="-"
	ma_log="${ma_log} blocker=$(printf '%s' "${ma_blocker}" | tr ' ' '_')"
	if [ "${ma_dry}" = "true" ]; then
		echo "${ma_log} outcome=dry_run"
		return 0
	fi
	ma_desc="$(printf 'merge waiting %sh: %s' "${ma_age_h}" "${ma_blocker}" | cut -c1-140)"
	# Marker first: a failed write sends nothing, so the alert never repeats.
	if ! gh api -X POST "repos/${ma_repo}/statuses/${ma_head}" -f state=success \
		-f context=ai-review/merge-wait-alert -f "description=${ma_desc}" >/dev/null 2>&1; then
		echo "${ma_log} outcome=marker_write_failed"
		return 0
	fi
	REVIEW_SWEEP_VERDICT_ALERTS_SENT=$((REVIEW_SWEEP_VERDICT_ALERTS_SENT + 1))
	if ! type tg_send_msg >/dev/null 2>&1 || ! type _tg_should_send >/dev/null 2>&1; then
		ma_outcome="telegram_unavailable"
	elif [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ]; then
		ma_outcome="telegram_unconfigured"
	elif ! _tg_should_send WARNING; then
		ma_outcome="below_alert_level"
	else
		ma_msg="PR #${ma_pr} has had a verdict on head ${ma_head:0:12} for ${ma_age_h}h and has not merged (${ma_reason}). Blocker: ${ma_blocker}. The review sweep no longer re-dispatches it."$'\n'"PR: https://github.com/${ma_repo}/pull/${ma_pr}"
		if tg_send_msg "${ma_msg}" WARNING >/dev/null 2>&1; then
			ma_outcome="sent"
		else
			ma_outcome="not_delivered"
		fi
	fi
	echo "${ma_log} outcome=${ma_outcome}"
	return 0
}
