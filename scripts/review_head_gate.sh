#!/usr/bin/env bash
# SHA-bound review status and stale auto-merge withdrawal for review_autofix.
# The gate CLI runs only from the verified workflow-support checkout.

REVIEW_HEAD_GATE_CONTEXT="ai-review/head-gate"
: "${REVIEW_HEAD_GATE_STATUS_ENABLED:=true}"
: "${REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED:=true}"

review_head_gate_valid_repo()
{
	[[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && [[ "$1" != *..* ]]
}

review_head_gate_post_status()
{
	local status_repo="${1:-}" status_sha="${2:-}" status_state="${3:-}" status_description="${4:-}"
	# Case-insensitive, like review_head_gate_updater_exempt and the updater
	# verify job: "False"/"FALSE" must not post a pending that no success replaces.
	local status_flag="${REVIEW_HEAD_GATE_STATUS_ENABLED:-true}"
	if [ "${status_flag,,}" = "false" ]; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha:-?} state=${status_state:-?} outcome=disabled"
		return 0
	fi
	if ! review_head_gate_valid_repo "${status_repo}" || ! [[ "${status_sha}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${status_state}" =~ ^(pending|success|failure)$ ]]; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha:-?} state=${status_state:-?} outcome=invalid"
		return 0
	fi
	status_description="$(LC_ALL=C printf '%s' "${status_description}" | tr -cd ' -~' | cut -c1-140)"
	local -a status_args=(-X POST "repos/${status_repo}/statuses/${status_sha}" -f "state=${status_state}" -f "context=${REVIEW_HEAD_GATE_CONTEXT}" -f "description=${status_description}")
	if [[ "${RUN_URL:-}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$ ]]; then
		status_args+=(-f "target_url=${RUN_URL}")
	fi
	# The gate's existing PR GET supplies the head and enrollment, but cannot
	# publish a status; exactly one POST is needed for each state transition.
	if gh_retry gh api "${status_args[@]}" >/dev/null; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha} state=${status_state} outcome=posted"
	else
		echo "::warning::REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha} state=${status_state} outcome=failed"
	fi
	return 0
}

# With an unknown enrollment, verify the live PR and expose its JSON via
# REVIEW_HEAD_GATE_LIVE_PR_JSON for the judge's head comparison. A known
# enrollment comes from the gate's existing authenticated PR read.
review_head_gate_withdraw_auto_merge()
{
	local withdraw_repo="${1:-}" withdraw_pr="${2:-}" known_enabled="${3:-}" withdraw_json=""
	REVIEW_HEAD_GATE_LIVE_PR_JSON=""
	if ! review_head_gate_valid_repo "${withdraw_repo}" || ! [[ "${withdraw_pr}" =~ ^[1-9][0-9]*$ ]]; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr:-?} outcome=failed reason=invalid_input"
		return 1
	fi
	if [ "${known_enabled}" = "false" ]; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=not_enabled"
		return 0
	fi
	if [ "${known_enabled}" != "true" ]; then
		# The judge's earlier PR reads predate its decision; it has no live
		# enrollment cache. The gate passes its existing read when known.
		if ! withdraw_json="$(gh_retry gh api "repos/${withdraw_repo}/pulls/${withdraw_pr}" 2>/dev/null)"; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=read_failed"
			return 1
		fi
		if ! jq -e '.state == "open" and (.head.sha | type == "string") and has("auto_merge") and (.auto_merge == null or (.auto_merge | type == "object"))' <<< "${withdraw_json}" >/dev/null 2>&1; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=invalid_response"
			return 1
		fi
		REVIEW_HEAD_GATE_LIVE_PR_JSON="${withdraw_json}"
		if jq -e '.auto_merge == null' <<< "${withdraw_json}" >/dev/null 2>&1; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=not_enabled"
			return 0
		fi
	fi
	if ! gh_retry gh pr merge "${withdraw_pr}" --repo "${withdraw_repo}" --disable-auto >/dev/null 2>&1; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=disable_failed"
		return 1
	fi
	echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=disabled"
}

# Updater-PR exemption (#9483, AD-1: A). Consumer-updater PRs
# (auto/update-workflows-<sha12>[-r2..-r5]) are never merged by review: the updater's
# verify job owns the merge, and review_enable_auto_merge.sh leaves before
# the review tail's success post, so the gate used to stay `pending` forever
# (btc_sweeper#264, atlas-bridge.gd#134). Post `success` for the gated head
# only when every signal holds; anything missing, failed, stale or unreadable
# keeps the gate pending (fail closed):
#   - the head ref matches ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ (hint
#     and live; -r2..-r5 are updater resend branches, as in the other review
#     guards in review_enable_auto_merge.sh and review_rb_judge.sh);
#   - the PR is open and its head repository is the base repository (no fork);
#   - the live head SHA equals the gated SHA;
#   - the combined status of that SHA has ai-update-workflows/verify=success
#     and coding-workflows/workflow-integrity=success (the integrity check is
#     published as a commit status by workflow_integrity.yml, not a check run).
# Usage: review_head_gate_updater_exempt <repo> <pr> <gated sha> <head ref hint> [live pr json]
# API budget (§14): none when the hint is empty or not an updater branch; one
# PR read when no live PR JSON is passed (review_enable_auto_merge.sh passes
# its own fresh read); one combined-status read. Logs exactly one
# REVIEW_HEAD_GATE mode=updater_exempt line. Returns 0 only when posted.
: "${REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED:=true}"
REVIEW_HEAD_GATE_UPDATER_BRANCH_RE='^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$'
REVIEW_HEAD_GATE_UPDATER_VERIFY_CONTEXT="ai-update-workflows/verify"
REVIEW_HEAD_GATE_UPDATER_INTEGRITY_CONTEXT="coding-workflows/workflow-integrity"

review_head_gate_updater_exempt()
{
	local ue_repo="${1:-}" ue_pr="${2:-}" ue_sha="${3:-}" ue_hint="${4:-}" ue_pr_json="${5:-}" ue_status_json="" ue_reason=""
	ue_log()
	{
		echo "REVIEW_HEAD_GATE mode=updater_exempt pr=${ue_pr:-?} head=${ue_sha:-?} outcome=$1 reason=$2"
	}
	# Case-insensitive, like the workflow's other switches: False/FALSE also disable.
	local ue_exempt_flag="${REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED:-true}" ue_status_flag="${REVIEW_HEAD_GATE_STATUS_ENABLED:-true}"
	if [ "${ue_exempt_flag,,}" = "false" ]; then
		ue_log skipped disabled
		return 1
	fi
	if [ "${ue_status_flag,,}" = "false" ]; then
		ue_log skipped status_disabled
		return 1
	fi
	if ! review_head_gate_valid_repo "${ue_repo}" || ! [[ "${ue_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${ue_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		ue_log skipped invalid_input
		return 1
	fi
	if [ -z "${ue_hint}" ]; then
		ue_log skipped head_ref_unavailable
		return 1
	fi
	if ! [[ "${ue_hint}" =~ ${REVIEW_HEAD_GATE_UPDATER_BRANCH_RE} ]]; then
		ue_log skipped not_updater_branch
		return 1
	fi
	if [ -z "${ue_pr_json}" ]; then
		if ! ue_pr_json="$(gh_retry gh api "repos/${ue_repo}/pulls/${ue_pr}" 2>/dev/null)"; then
			ue_log skipped pr_read_failed
			return 1
		fi
	fi
	ue_reason="$(jq -r --arg repo "${ue_repo}" --arg sha "${ue_sha}" --arg re "${REVIEW_HEAD_GATE_UPDATER_BRANCH_RE}" '
		if type != "object" then "invalid_response"
		elif .state != "open" then "pr_not_open"
		elif ((.head.ref // "") | type) != "string" or ((.head.ref // "") | test($re) | not) then "not_updater_branch"
		elif (.head.repo.full_name // "") != $repo or (.base.repo.full_name // "") != $repo then "fork_head"
		elif (.head.sha // "") != $sha then "head_moved"
		else "ok" end' <<< "${ue_pr_json}" 2>/dev/null)" || ue_reason=""
	if [ "${ue_reason}" != "ok" ]; then
		ue_log skipped "${ue_reason:-invalid_response}"
		return 1
	fi
	if ! ue_status_json="$(gh_retry gh api "repos/${ue_repo}/commits/${ue_sha}/status?per_page=100" 2>/dev/null)"; then
		ue_log skipped status_read_failed
		return 1
	fi
	ue_reason="$(jq -r --arg v "${REVIEW_HEAD_GATE_UPDATER_VERIFY_CONTEXT}" --arg i "${REVIEW_HEAD_GATE_UPDATER_INTEGRITY_CONTEXT}" '
		def state_of($c): [ .statuses[] | select(type == "object" and .context == $c) | .state ] | first;
		if type != "object" or ((.statuses // null) | type) != "array" then "invalid_response"
		else
			state_of($v) as $vs | state_of($i) as $is
			| ((.total_count // 0) > (.statuses | length)) as $incomplete
			| if $vs == null then (if $incomplete then "status_page_incomplete" else "verify_missing" end)
			elif $vs != "success" then "verify_not_success"
			elif $is == null then (if $incomplete then "status_page_incomplete" else "integrity_missing" end)
			elif $is != "success" then "integrity_not_success"
			else "ok" end
		end' <<< "${ue_status_json}" 2>/dev/null)" || ue_reason=""
	if [ "${ue_reason}" != "ok" ]; then
		ue_log skipped "${ue_reason:-invalid_response}"
		return 1
	fi
	# review_head_gate_post_status always returns 0 (its other callers rely
	# on that), so read its own outcome line to tell a failed POST apart.
	local ue_post_out=""
	ue_post_out="$(review_head_gate_post_status "${ue_repo}" "${ue_sha}" success "updater PR verified against the attested release manifest")"
	[ -n "${ue_post_out}" ] && printf '%s\n' "${ue_post_out}"
	if [[ "${ue_post_out}" != *"state=success outcome=posted"* ]]; then
		ue_log skipped post_failed
		return 1
	fi
	ue_log posted verified
	return 0
}

review_head_gate_main()
{
	if [ "${EVENT_NAME:-}" = "pull_request" ]; then
		if [ "${EVENT_ACTION:-}" = "opened" ] || [ "${EVENT_ACTION:-}" = "synchronize" ]; then
			local event_sha="${PR_EVENT_HEAD_SHA:-${GATE_HEAD_SHA:-}}"
			review_head_gate_post_status "${REPOSITORY:-}" "${event_sha}" pending "review pending for this head"
			if [ -n "${GATE_HEAD_SHA:-}" ] && [ "${event_sha}" != "${GATE_HEAD_SHA}" ]; then
				review_head_gate_post_status "${REPOSITORY:-}" "${GATE_HEAD_SHA}" pending "review pending for this head"
			fi
		fi
		if [ "${EVENT_ACTION:-}" = "synchronize" ] && [ "${REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED}" != "false" ]; then
			if ! review_head_gate_withdraw_auto_merge "${REPOSITORY:-}" "${PR_NUMBER:-}" "${GATE_AUTO_MERGE_ENABLED:-}"; then
				echo "::error::REVIEW_HEAD_GATE stale auto-merge withdrawal failed; refusing review gate" >&2
				return 1
			fi
		fi
	fi
	if [ "${DETERMINISTIC_SKIP:-false}" = "true" ]; then
		# #10151: name the gate's skip reason (a fixed token set by the gate)
		# in the one success POST; empty keeps the legacy text. The status
		# helper strips the text to printable ASCII and 140 characters.
		local det_skip_description="deterministic skip evaluated this head"
		if [ -n "${GATE_DET_SKIP_REASON:-}" ]; then
			det_skip_description="deterministic skip: ${GATE_DET_SKIP_REASON}"
		fi
		review_head_gate_post_status "${REPOSITORY:-}" "${GATE_HEAD_SHA:-}" success "${det_skip_description}"
	elif [ -n "${GATE_HEAD_SHA:-}" ] && [ -n "${GATE_HEAD_REF:-}" ]; then
		# Posted after any pending status above for the same SHA, so it wins.
		# The early gate call passes no GATE_HEAD_REF, so it skips this (no
		# API call and no misleading mode=updater_exempt log line).
		review_head_gate_updater_exempt "${REPOSITORY:-}" "${PR_NUMBER:-}" "${GATE_HEAD_SHA}" "${GATE_HEAD_REF:-}" || true
	fi
}

# Freshness-update fast path (#7084, Q60: A; hardened by security-pass
# finding freshness-sync-signed-merge-review-bypass). A merge path that finds
# the base moved under the PR's files updates the branch, waits for the commit
# GitHub produced, verifies it, and posts
#   <!-- ai:merge-base-sync:v1 pr=<n> from=<approved head> sync=<k> base=<base tip> merge=<update commit> -->
# (scripts/pr_checks_lib.sh). The synchronize run may skip the reviewer panel
# only for exactly that recorded update commit:
#   - the newest marker by the workflow's own login for <pr> whose `merge`
#     equals the head; legacy markers without `base`/`merge` never skip;
#   - the head commit has exactly two parents, `from` first and `base`
#     second, and GitHub made it (committer `web-flow`, verified signature);
#   - the base parent is contained in <base> (`compare/<base parent>...<base>`
#     is `ahead` or `identical`), so the only new code is already on the base.
# Any other head, such as a later web conflict-resolver merge on top of a
# branch reset to the approved head, has a different SHA or base parent and
# is reviewed normally.
# Usage: freshness-sync <repo> <pr> <head sha> <base ref> <author login> <comments json file>
# The comments file is the review gate's existing marker-comment read (an array
# of {author_login, author_type, body}; author_type is optional and is only
# needed to trust a github-actions[bot] final-CI-failed marker); this adds no
# comments call. API budget (§15): one
# `commits/<head>` and one compare read, both only when a bound marker names
# the head. Exit 0 = skip the reviewers; 1 = review normally. Every outcome
# logs one REVIEW_HEAD_GATE mode=freshness_sync line. On exit 0 it also sets
# REVIEW_HEAD_GATE_FRESHNESS_SYNC_FROM to the verified approved head (`from`),
# empty otherwise; the single-issue security pass reads it to reuse that
# head's clean audit (scripts/review_single_issue_security_pass.sh).
review_head_gate_freshness_sync()
{
	local fs_repo="${1:-}" fs_pr="${2:-}" fs_head="${3:-}" fs_base="${4:-}" fs_login="${5:-}" fs_comments="${6:-}"
	local fs_markers fs_marker fs_from fs_sync fs_base_parent fs_commit fs_parent_count fs_first fs_second fs_verified fs_committer fs_status
	# Issue #9533: on a skip, expose the verified approved head and base
	# parent to a sourcing caller (the single-issue security pass), so it
	# needs no second marker parser. Empty on every other outcome.
	REVIEW_HEAD_GATE_FRESHNESS_FROM=""
	REVIEW_HEAD_GATE_FRESHNESS_BASE_PARENT=""
	REVIEW_HEAD_GATE_FRESHNESS_SYNC_FROM=""
	fs_log()
	{
		echo "REVIEW_HEAD_GATE mode=freshness_sync pr=${fs_pr:-?} head=${fs_head:-?} outcome=$1 reason=$2${3:+ $3}"
	}
	if ! review_head_gate_valid_repo "${fs_repo}" || ! [[ "${fs_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${fs_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${fs_base}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || [[ "${fs_base}" == *..* ]] \
		|| [ -z "${fs_login}" ] || [ ! -f "${fs_comments}" ]; then
		fs_log review invalid_input
		return 1
	fi
	# One line per trusted marker for this PR: "<from> <sync> <base|-> <merge|->".
	fs_markers="$(jq -r --arg login "${fs_login}" --arg pr "${fs_pr}" '
		.[]? | select(type == "object" and (.author_login // "") == $login)
		| (.body // "") | select(type == "string" and startswith("**Branch updated from `"))
		| capture("<!-- ai:merge-base-sync:v1 pr=(?<pr>[0-9]+) from=(?<from>[0-9a-f]{40}) sync=(?<sync>[0-9]+)( base=(?<base>[0-9a-f]{40}) merge=(?<merge>[0-9a-f]{40})( inline_run=(?<inline_run>[0-9]+))?)? -->\\s*$")?
		| select(.pr == $pr) | "\(.from) \(.sync) \(.base // "-") \(.merge // "-")"' "${fs_comments}" 2>/dev/null || true)"
	if [ -z "${fs_markers}" ]; then
		fs_log review no_sync_marker
		return 1
	fi
	fs_marker="$(printf '%s\n' "${fs_markers}" | awk -v head="${fs_head}" '$4 == head { m = $0 } END { if (m != "") print m }')"
	if [ -z "${fs_marker}" ]; then
		if printf '%s\n' "${fs_markers}" | awk '$4 != "-" { found = 1 } END { exit found ? 0 : 1 }'; then
			fs_log review marker_not_for_head
		else
			fs_log review unbound_marker
		fi
		return 1
	fi
	read -r fs_from fs_sync fs_base_parent _ <<< "${fs_marker}"
	# CI already failed on this head (scripts/review_freshness_ci_failed.sh,
	# Q103): review it normally so autofix sees the failing checks.
	if jq -e --arg login "${fs_login}" --arg pr "${fs_pr}" --arg head "${fs_head}" '
		[ .[]? | select(type == "object" and (.author_login // "") == $login)
		  | (.body // "") | select(type == "string")
		  | test("<!-- ai:freshness-ci-failed:v1 pr=" + $pr + " head=" + $head + " -->\\s*$") ] | any' \
		"${fs_comments}" >/dev/null 2>&1; then
		fs_log review ci_failed_on_head "from=${fs_from} sync=${fs_sync}"
		return 1
	fi
	# The full final CI failed on this head (scripts/review_final_ci_failed.sh,
	# ci.yml's `final-ci-failed` job, or the review-blocked judge's repair
	# request, issue #10063). That hand-back is a different marker, posted by
	# github-actions[bot] or the GH_PAT login: the same trust rule as the review
	# gate's doc-only / small-diff check. Without this the update head was
	# skipped and re-approved on every review run while its final CI stayed
	# red (PR #10152, 2026-11-28).
	if jq -e --arg login "${fs_login}" --arg pr "${fs_pr}" --arg head "${fs_head}" '
		[ .[]? | select(type == "object")
		  | select(((.author_login // "") | ascii_downcase) == ($login | ascii_downcase)
		    or ((.author_login // "") == "github-actions[bot]" and (.author_type // "") == "Bot"))
		  | (.body // "") | select(type == "string")
		  | test("<!-- ai:(final-ci-failed|final-ci-repair-request):v1 pr=" + $pr + " head=" + $head + " -->\\s*$") ] | any' \
		"${fs_comments}" >/dev/null 2>&1; then
		fs_log review final_ci_failed_on_head "from=${fs_from} sync=${fs_sync}"
		return 1
	fi
	if ! fs_commit="$(gh_retry gh api "repos/${fs_repo}/commits/${fs_head}" \
		--jq '{parents: [.parents[]?.sha], verified: (.commit.verification.verified // false), committer: (.committer.login // "")}' 2>/dev/null)"; then
		fs_log review commit_read_failed "from=${fs_from} sync=${fs_sync}"
		return 1
	fi
	fs_parent_count="$(printf '%s' "${fs_commit}" | jq -r '.parents | length' 2>/dev/null || echo 0)"
	fs_first="$(printf '%s' "${fs_commit}" | jq -r '.parents[0] // ""' 2>/dev/null || true)"
	fs_second="$(printf '%s' "${fs_commit}" | jq -r '.parents[1] // ""' 2>/dev/null || true)"
	fs_verified="$(printf '%s' "${fs_commit}" | jq -r '.verified' 2>/dev/null || echo false)"
	fs_committer="$(printf '%s' "${fs_commit}" | jq -r '.committer' 2>/dev/null || true)"
	if [ "${fs_parent_count}" != "2" ] || [ "${fs_first}" != "${fs_from}" ]; then
		fs_log review head_is_not_the_update "from=${fs_from} parents=${fs_parent_count} first=${fs_first:-none}"
		return 1
	fi
	if [ "${fs_second}" != "${fs_base_parent}" ]; then
		fs_log review base_parent_mismatch "from=${fs_from} base=${fs_base_parent} base_parent=${fs_second:-none}"
		return 1
	fi
	if [ "${fs_verified}" != "true" ] || [ "${fs_committer}" != "web-flow" ]; then
		fs_log review merge_not_made_by_github "from=${fs_from} verified=${fs_verified} committer=${fs_committer:-none}"
		return 1
	fi
	if ! [[ "${fs_second}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! fs_status="$(gh_retry gh api "repos/${fs_repo}/compare/${fs_second}...${fs_base}" --jq '.status // ""' 2>/dev/null)"; then
		fs_log review base_compare_failed "from=${fs_from} base_parent=${fs_second:-none}"
		return 1
	fi
	case "${fs_status}" in
		ahead|identical) ;;
		*)
			fs_log review base_parent_not_on_base "from=${fs_from} base_parent=${fs_second} status=${fs_status:-none}"
			return 1
			;;
	esac
	REVIEW_HEAD_GATE_FRESHNESS_FROM="${fs_from}"
	REVIEW_HEAD_GATE_FRESHNESS_BASE_PARENT="${fs_second}"
	fs_log skip freshness_sync "from=${fs_from} base_parent=${fs_second} sync=${fs_sync} merge=${fs_head}"
	REVIEW_HEAD_GATE_FRESHNESS_SYNC_FROM="${fs_from}"
	return 0
}

# freshness-sync-inline-run <pr> <head sha> <author login> <comments json file>
# (docs/plans/merge-lease-short-critical-section-plan.md Phase 4). Prints the
# inline_run=<run id> of the newest trusted ai:merge-base-sync:v1 marker for
# <pr> whose `merge` equals <head sha> (same author, heading and capture rules
# as freshness-sync), or nothing when that marker has no inline_run field or
# there is no such marker. The id must match ^[1-9][0-9]{0,19}$. No API call.
# Exit 0 on valid input, 2 on invalid input.
review_head_gate_freshness_inline_run()
{
	local ir_pr="${1:-}" ir_head="${2:-}" ir_login="${3:-}" ir_comments="${4:-}" ir_run=""
	if ! [[ "${ir_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${ir_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| [ -z "${ir_login}" ] || [ ! -f "${ir_comments}" ]; then
		return 2
	fi
	ir_run="$(jq -r --arg login "${ir_login}" --arg pr "${ir_pr}" --arg head "${ir_head}" '
		[ .[]? | select(type == "object" and (.author_login // "") == $login)
		| (.body // "") | select(type == "string" and startswith("**Branch updated from `"))
		| capture("<!-- ai:merge-base-sync:v1 pr=(?<pr>[0-9]+) from=(?<from>[0-9a-f]{40}) sync=(?<sync>[0-9]+)( base=(?<base>[0-9a-f]{40}) merge=(?<merge>[0-9a-f]{40})( inline_run=(?<inline_run>[0-9]+))?)? -->\\s*$")?
		| select(.pr == $pr and .merge == $head) ] | last | (.inline_run // "")' "${ir_comments}" 2>/dev/null || true)"
	if [[ "${ir_run}" =~ ^[1-9][0-9]{0,19}$ ]]; then
		printf '%s\n' "${ir_run}"
	fi
	return 0
}

# final-ci-repair <repo> <pr> <head sha> <author login> <comments json file>
# (docs/plans/final-ci-failure-ci-fix-plan.md Phase 3). Recognises a final-CI
# repair head so its review covers only the repair commit. All must hold:
#   * a marker comment by <author login> (the GH_PAT login) ends in
#     `<!-- ai:final-ci-repair:v1 pr=<pr> from=<sha> repair=<head> attempt=<k> -->`;
#   * the head commit has exactly one parent, and it is `from`;
#   * the head commit's committer is the pipeline identity (`codex-bot` or
#     `claude-bot` at users.noreply.invalid, as review_conflict_resolve.sh
#     commits);
#   * `from`'s newest ai-review/head-gate status is `success` and was posted
#     by <author login>.
# The comments file is the gate's shared marker read (no comments call here).
# API budget (§15): one `commits/<head>` and one `commits/<from>/statuses`
# read, both only when a trusted marker names the head. On success it prints
# `final_ci_repair_from=<from>` and exits 0; any other outcome (including a
# failed read) exits 1, which means a full review. Every outcome logs one
# `REVIEW_HEAD_GATE mode=final_ci_repair` line.
review_head_gate_final_ci_repair()
{
	local fr_repo="${1:-}" fr_pr="${2:-}" fr_head="${3:-}" fr_login="${4:-}" fr_comments="${5:-}"
	local fr_from fr_commit fr_parents fr_name fr_email fr_gate fr_gate_state fr_gate_creator
	fr_log()
	{
		echo "REVIEW_HEAD_GATE mode=final_ci_repair pr=${fr_pr:-?} head=${fr_head:-?} outcome=$1 reason=$2${3:+ $3}"
	}
	if ! review_head_gate_valid_repo "${fr_repo}" || ! [[ "${fr_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${fr_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| [ -z "${fr_login}" ] || [ ! -f "${fr_comments}" ]; then
		fr_log review invalid_input
		return 1
	fi
	fr_from="$(jq -r --arg login "${fr_login}" --arg pr "${fr_pr}" --arg head "${fr_head}" '
		[ .[]? | select(type == "object" and ((.author_login // "") | ascii_downcase) == ($login | ascii_downcase))
		| (.body // "") | select(type == "string")
		| capture("<!-- ai:final-ci-repair:v1 pr=(?<pr>[0-9]+) from=(?<from>[0-9a-f]{40}) repair=(?<repair>[0-9a-f]{40}) attempt=[1-9][0-9]* -->\\s*$")?
		| select(.pr == $pr and .repair == $head) | .from ] | last // ""' "${fr_comments}" 2>/dev/null || true)"
	if ! [[ "${fr_from}" =~ ^[0-9a-f]{40}$ ]]; then
		fr_log review no_repair_marker
		return 1
	fi
	if ! fr_commit="$(gh_retry gh api "repos/${fr_repo}/commits/${fr_head}" \
		--jq '{parents: [.parents[]?.sha], name: (.commit.committer.name // ""), email: (.commit.committer.email // "")}' 2>/dev/null)"; then
		fr_log review commit_read_failed "from=${fr_from}"
		return 1
	fi
	fr_parents="$(printf '%s' "${fr_commit}" | jq -r '.parents | join(",")' 2>/dev/null || true)"
	fr_name="$(printf '%s' "${fr_commit}" | jq -r '.name' 2>/dev/null || true)"
	fr_email="$(printf '%s' "${fr_commit}" | jq -r '.email' 2>/dev/null || true)"
	if [ "${fr_parents}" != "${fr_from}" ]; then
		fr_log review head_is_not_the_repair "from=${fr_from} parents=${fr_parents:-none}"
		return 1
	fi
	case "${fr_name}" in
		codex-bot|claude-bot) ;;
		*) fr_name="" ;;
	esac
	if [ -z "${fr_name}" ] || [ "${fr_email}" != "${fr_name}@users.noreply.invalid" ]; then
		fr_log review committer_not_pipeline "from=${fr_from}"
		return 1
	fi
	if ! fr_gate="$(gh_retry gh api "repos/${fr_repo}/commits/${fr_from}/statuses?per_page=100" \
		--jq '[.[] | select(.context == "ai-review/head-gate")][0] // {} | "\(.state // "none") \(.creator.login // "-")"' 2>/dev/null)"; then
		fr_log review from_gate_read_failed "from=${fr_from}"
		return 1
	fi
	read -r fr_gate_state fr_gate_creator <<< "${fr_gate:-none -}"
	if [ "${fr_gate_state}" != "success" ] \
		|| [ "$(printf '%s' "${fr_gate_creator}" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "${fr_login}" | tr '[:upper:]' '[:lower:]')" ]; then
		fr_log review from_not_approved "from=${fr_from} state=${fr_gate_state:-none}"
		return 1
	fi
	fr_log delta final_ci_repair "from=${fr_from}"
	echo "final_ci_repair_from=${fr_from}"
	return 0
}

# merge-resolve <repo> <pr> <head sha> <base ref> <author login> <comments json file>
# (docs/plans/merge-lease-short-critical-section-plan.md Phase 3, D4).
# Recognises a resolver head ([ai-merge-resolve] merge of an approved head
# with the base) so its review narrows the reviewers' primary diff to the
# resolution (git show --remerge-diff). Every check must hold, otherwise the
# head gets a full review (fail closed):
#   * the newest marker comment by <author login> (the GH_PAT login,
#     case-insensitive) whose final line is
#     `<!-- ai:merge-resolve from=<sha> resolve=<head> base=<sha> -->`;
#   * the head commit has exactly two parents, [from, base];
#   * its committer or author is the pipeline identity (`codex-bot` or
#     `claude-bot` at users.noreply.invalid, the final-ci-repair guard);
#   * `from`'s newest ai-review/head-gate status is `success`, posted by
#     <author login>;
#   * the base parent is contained in <base ref> (`compare/<base>...<base ref>`
#     is `ahead` or `identical`, the freshness_sync check).
# The comments file is the gate's shared marker read (no comments call here).
# API budget (§14): `commits/<head>`, `commits/<from>/statuses` and one
# compare read, only when a trusted marker names the head and the earlier
# checks pass. On success prints `merge_resolve_from=<from>`, sets
# REVIEW_HEAD_GATE_MERGE_RESOLVE_FROM for sourcing callers and returns 0;
# anything else returns 1. Every outcome logs one
# `REVIEW_HEAD_GATE mode=merge_resolve outcome=delta|review reason=<token>` line.
review_head_gate_merge_resolve()
{
	local mr_repo="${1:-}" mr_pr="${2:-}" mr_head="${3:-}" mr_base="${4:-}" mr_login="${5:-}" mr_comments="${6:-}"
	local mr_marker mr_from mr_base_parent mr_commit mr_parents mr_identity mr_gate mr_gate_state mr_gate_creator mr_status
	REVIEW_HEAD_GATE_MERGE_RESOLVE_FROM=""
	mr_log()
	{
		echo "REVIEW_HEAD_GATE mode=merge_resolve pr=${mr_pr:-?} head=${mr_head:-?} outcome=$1 reason=$2${3:+ $3}"
	}
	if ! review_head_gate_valid_repo "${mr_repo}" || ! [[ "${mr_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${mr_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${mr_base}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || [[ "${mr_base}" == *..* ]] \
		|| [ -z "${mr_login}" ] || [ ! -f "${mr_comments}" ]; then
		mr_log review invalid_input
		return 1
	fi
	mr_marker="$(jq -r --arg login "${mr_login}" --arg head "${mr_head}" '
		[ .[]? | select(type == "object" and ((.author_login // "") | ascii_downcase) == ($login | ascii_downcase))
		| (.body // "") | select(type == "string")
		| capture("<!-- ai:merge-resolve from=(?<from>[0-9a-f]{40}) resolve=(?<resolve>[0-9a-f]{40}) base=(?<base>[0-9a-f]{40}) -->\\s*$")?
		| select(.resolve == $head) | "\(.from) \(.base)" ] | last // ""' "${mr_comments}" 2>/dev/null || true)"
	read -r mr_from mr_base_parent _ <<< "${mr_marker}"
	if ! [[ "${mr_from:-}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${mr_base_parent:-}" =~ ^[0-9a-f]{40}$ ]]; then
		mr_log review no_resolve_marker
		return 1
	fi
	if ! mr_commit="$(gh_retry gh api "repos/${mr_repo}/commits/${mr_head}" \
		--jq '{parents: [.parents[]?.sha], cname: (.commit.committer.name // ""), cemail: (.commit.committer.email // ""), aname: (.commit.author.name // ""), aemail: (.commit.author.email // "")}' 2>/dev/null)"; then
		mr_log review commit_read_failed "from=${mr_from}"
		return 1
	fi
	mr_parents="$(printf '%s' "${mr_commit}" | jq -r '.parents | join(",")' 2>/dev/null || true)"
	if [ "${mr_parents}" != "${mr_from},${mr_base_parent}" ]; then
		mr_log review head_is_not_the_resolution "from=${mr_from} parents=${mr_parents:-none}"
		return 1
	fi
	mr_identity="$(printf '%s' "${mr_commit}" | jq -r '
		def pipeline($n; $e): ($n == "codex-bot" or $n == "claude-bot") and $e == ($n + "@users.noreply.invalid");
		if pipeline(.cname; .cemail) or pipeline(.aname; .aemail) then "pipeline" else "other" end' 2>/dev/null || true)"
	if [ "${mr_identity}" != "pipeline" ]; then
		mr_log review identity_not_pipeline "from=${mr_from}"
		return 1
	fi
	if ! mr_gate="$(gh_retry gh api "repos/${mr_repo}/commits/${mr_from}/statuses?per_page=100" \
		--jq '[.[] | select(.context == "ai-review/head-gate")][0] // {} | "\(.state // "none") \(.creator.login // "-")"' 2>/dev/null)"; then
		mr_log review from_gate_read_failed "from=${mr_from}"
		return 1
	fi
	read -r mr_gate_state mr_gate_creator <<< "${mr_gate:-none -}"
	if [ "${mr_gate_state}" != "success" ] \
		|| [ "$(printf '%s' "${mr_gate_creator}" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "${mr_login}" | tr '[:upper:]' '[:lower:]')" ]; then
		mr_log review from_not_approved "from=${mr_from} state=${mr_gate_state:-none}"
		return 1
	fi
	if ! mr_status="$(gh_retry gh api "repos/${mr_repo}/compare/${mr_base_parent}...${mr_base}" --jq '.status // ""' 2>/dev/null)"; then
		mr_log review base_compare_failed "from=${mr_from} base_parent=${mr_base_parent}"
		return 1
	fi
	case "${mr_status}" in
		ahead|identical) ;;
		*)
			mr_log review base_parent_not_on_base "from=${mr_from} base_parent=${mr_base_parent} status=${mr_status:-none}"
			return 1
			;;
	esac
	REVIEW_HEAD_GATE_MERGE_RESOLVE_FROM="${mr_from}"
	mr_log delta merge_resolve "from=${mr_from} base_parent=${mr_base_parent}"
	echo "merge_resolve_from=${mr_from}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	set -euo pipefail
	# shellcheck source=/dev/null
	source "$(dirname -- "${BASH_SOURCE[0]}")/gh_helpers.sh"
	case "${1:-}" in
		gate) review_head_gate_main ;;
		freshness-sync) shift; review_head_gate_freshness_sync "$@" ;;
		final-ci-repair) shift; review_head_gate_final_ci_repair "$@" ;;
		merge-resolve) shift; review_head_gate_merge_resolve "$@" ;;
		freshness-sync-inline-run) shift; review_head_gate_freshness_inline_run "$@" ;;
		*) echo "Usage: review_head_gate.sh gate | freshness-sync <repo> <pr> <head sha> <base ref> <author login> <comments json file> | freshness-sync-inline-run <pr> <head sha> <author login> <comments json file> | final-ci-repair <repo> <pr> <head sha> <author login> <comments json file> | merge-resolve <repo> <pr> <head sha> <base ref> <author login> <comments json file>" >&2; exit 2 ;;
	esac
fi
