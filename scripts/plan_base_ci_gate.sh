#!/usr/bin/env bash
# scripts/plan_base_ci_gate.sh -- red-base plan auto-approval hold (Phase 2 of
# docs/plans/stuck-backlog-prevention-plan.md, Refs #9421).
#
# Sourced by .github/workflows/plan.yml, step "Auto-approve clear plan"
# (id: auto_approve), immediately before the `/approved [auto-approved-by-plan]`
# post. While the default branch's newest decisive push CI run failed and a
# trusted open ai:workflow-heal issue tracks that workflow on that branch, the
# step holds the approval instead of starting an implement run whose PR would
# inherit the red base.
#
# This file is a library: sourcing it only defines functions and changes no
# `set` options. Executed directly, it prints a usage line and exits 2.
#
# Environment (each with a default here, as well as in the workflow env):
#   RED_BASE_APPROVAL_HOLD_ENABLED  kill switch, default `true`. Unset, empty,
#                                   true/1/yes/on keep it on; false/0/no/off turn
#                                   it off; any other value turns it off with a
#                                   ::warning::.
#   RED_BASE_HOLD_WORKFLOWS         comma-separated workflow paths whose push runs
#                                   decide "red", default `.github/workflows/ci.yml`
#                                   (at most 5 valid entries).
#
# Fail mode: every read fails open (no hold, legacy behaviour). Only a failed
# newest decisive push run that a trusted heal issue tracks holds.
#
# GitHub API cost (§14), only on an approval that is about to be posted: one
# runs read per configured workflow; plus one heal-issues read only when a
# workflow failed; plus one comments read and one post only when holding. No
# retry loop beyond gh_retry.
#
# Log line (stable prefix RED_BASE_APPROVAL_HOLD):
#   RED_BASE_APPROVAL_HOLD outcome=<held|pass|skipped> issue=<n> heal=<N|none>
#     workflow=<path|none> run=<id|none> status=<red|green|pending|unknown> reason=<token>

RED_BASE_HOLD_DEFAULT_WORKFLOWS=".github/workflows/ci.yml"
RED_BASE_HOLD_MAX_WORKFLOWS=5

# Calls `gh api` through the step's gh_retry/_safe_gh_jq when they exist.
_red_base_hold_gh_api()
{
	if type gh_retry >/dev/null 2>&1 && type _safe_gh_jq >/dev/null 2>&1; then
		gh_retry _safe_gh_jq "$@"
	else
		gh api "$@"
	fi
}

# Calls `gh <args>` through the step's gh_retry when it exists (the comments
# read with --paginate and the comment post, which _safe_gh_jq does not cover).
_red_base_hold_gh()
{
	if type gh_retry >/dev/null 2>&1; then
		gh_retry gh "$@"
	else
		gh "$@"
	fi
}

# _red_base_hold_log <outcome> <issue>: prints the one RED_BASE_APPROVAL_HOLD
# line from the RED_BASE_HOLD_* globals.
_red_base_hold_log()
{
	local log_outcome="$1" log_issue="$2"
	[[ "${log_issue}" =~ ^[1-9][0-9]{0,9}$ ]] || log_issue="none"
	echo "RED_BASE_APPROVAL_HOLD outcome=${log_outcome} issue=${log_issue} heal=${RED_BASE_HOLD_HEAL:-none} workflow=${RED_BASE_HOLD_WORKFLOW:-none} run=${RED_BASE_HOLD_RUN_ID:-none} status=${RED_BASE_HOLD_STATUS:-unknown} reason=${RED_BASE_HOLD_REASON:-unknown}"
}

# _red_base_hold_flag_on: returns 0 when the kill switch is on.
_red_base_hold_flag_on()
{
	local flag_value="${RED_BASE_APPROVAL_HOLD_ENABLED-true}"
	flag_value="${flag_value#"${flag_value%%[![:space:]]*}"}"
	flag_value="${flag_value%"${flag_value##*[![:space:]]}"}"
	flag_value="$(printf '%s' "${flag_value}" | tr '[:upper:]' '[:lower:]')"
	case "${flag_value}" in
		''|true|1|yes|on) return 0 ;;
		false|0|no|off) return 1 ;;
		*)
			echo "::warning::RED_BASE_APPROVAL_HOLD_ENABLED has an invalid value; treating the red-base approval hold as off."
			return 1
			;;
	esac
}

# _red_base_hold_workflows: prints the valid, de-duplicated workflow paths from
# RED_BASE_HOLD_WORKFLOWS, one per line, at most RED_BASE_HOLD_MAX_WORKFLOWS.
_red_base_hold_workflows()
{
	local raw_list="${RED_BASE_HOLD_WORKFLOWS-}" entry="" kept_count=0 kept_seen=" "
	local -a raw_entries=()
	[ -n "${raw_list//[[:space:],]/}" ] || raw_list="${RED_BASE_HOLD_DEFAULT_WORKFLOWS}"
	IFS=',' read -r -a raw_entries <<< "${raw_list}"
	for entry in "${raw_entries[@]}"; do
		entry="${entry#"${entry%%[![:space:]]*}"}"
		entry="${entry%"${entry##*[![:space:]]}"}"
		[ -n "${entry}" ] || continue
		if ! [[ "${entry}" =~ ^\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml$ ]] || [[ "${entry}" == *..* ]]; then
			echo "::warning::RED_BASE_HOLD_WORKFLOWS: ignoring an entry that is not a .github/workflows/<file>.yml path." >&2
			continue
		fi
		case "${kept_seen}" in
			*" ${entry} "*)
				echo "::warning::RED_BASE_HOLD_WORKFLOWS: ignoring duplicate entry ${entry}." >&2
				continue
				;;
		esac
		if [ "${kept_count}" -ge "${RED_BASE_HOLD_MAX_WORKFLOWS}" ]; then
			echo "::warning::RED_BASE_HOLD_WORKFLOWS: ignoring ${entry}; at most ${RED_BASE_HOLD_MAX_WORKFLOWS} workflows are read." >&2
			continue
		fi
		kept_seen="${kept_seen}${entry} "
		kept_count=$((kept_count + 1))
		printf '%s\n' "${entry}"
	done
}

# red_base_approval_hold_check <repo> <default_branch> <issue_number> <labels_json> [heal_route]
#
# Sets RED_BASE_HOLD_STATUS, RED_BASE_HOLD_REASON, RED_BASE_HOLD_RED,
# RED_BASE_HOLD_HEAL, RED_BASE_HOLD_WORKFLOW and RED_BASE_HOLD_RUN_ID, logs one
# RED_BASE_APPROVAL_HOLD line, and returns 0 to hold or 1 not to hold. It never
# exits, and no error makes it return 0.
# shellcheck disable=SC2034 # the RED_BASE_HOLD_* globals are read by the caller.
red_base_approval_hold_check()
{
	local hold_repo="${1-}" hold_branch="${2-}" hold_issue="${3-}" hold_labels="${4-[]}" hold_heal_route="${5-false}"
	local workflow_path="" workflow_file="" branch_encoded="" runs_json="" run_state="" run_conclusion="" run_id=""
	local any_unknown="false" any_pending="false" heal_listing="" heal_number="" heal_failed="false"
	local -a hold_workflows=() failed_workflows=() failed_run_ids=()
	local failed_index=0

	RED_BASE_HOLD_STATUS="unknown"
	RED_BASE_HOLD_REASON="unknown"
	RED_BASE_HOLD_RED="false"
	RED_BASE_HOLD_HEAL="none"
	RED_BASE_HOLD_WORKFLOW="none"
	RED_BASE_HOLD_RUN_ID="none"

	if ! _red_base_hold_flag_on; then
		RED_BASE_HOLD_REASON="disabled"
		_red_base_hold_log skipped "${hold_issue}"
		return 1
	fi
	if [ "${hold_heal_route}" = "true" ] \
		|| printf '%s' "${hold_labels}" | jq -e 'type == "array" and any(.[]?; . == "ai:workflow-heal")' >/dev/null 2>&1; then
		RED_BASE_HOLD_REASON="heal_issue_exempt"
		_red_base_hold_log skipped "${hold_issue}"
		return 1
	fi
	if ! [[ "${hold_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| ! [[ "${hold_branch}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || [[ "${hold_branch}" == *..* ]] \
		|| ! [[ "${hold_issue}" =~ ^[1-9][0-9]{0,9}$ ]]; then
		RED_BASE_HOLD_REASON="input_invalid"
		_red_base_hold_log pass "${hold_issue}"
		return 1
	fi
	mapfile -t hold_workflows < <(_red_base_hold_workflows)
	if [ "${#hold_workflows[@]}" -eq 0 ]; then
		RED_BASE_HOLD_REASON="no_workflows"
		_red_base_hold_log pass "${hold_issue}"
		return 1
	fi

	branch_encoded="${hold_branch//\//%2F}"
	for workflow_path in "${hold_workflows[@]}"; do
		workflow_file="${workflow_path##*/}"
		run_state=""
		if runs_json="$(_red_base_hold_gh_api "repos/${hold_repo}/actions/workflows/${workflow_file}/runs?branch=${branch_encoded}&event=push&status=completed&per_page=10" 2>/dev/null)"; then
			run_state="$(printf '%s' "${runs_json}" | jq -r '
				if type != "object" or (.workflow_runs | type) != "array" then error("bad_listing") else . end
				| [.workflow_runs[] | select(type == "object") | select((.conclusion // "") | IN("success", "failure"))]
				| sort_by(.created_at // "") | last
				| if . == null then "pending" else "\(.conclusion) \(.id // "" | tostring)" end' 2>/dev/null | tail -n1)" || run_state=""
		fi
		run_conclusion="${run_state%% *}"
		case "${run_conclusion}" in
			success) ;;
			pending) any_pending="true" ;;
			failure)
				run_id="${run_state#* }"
				[[ "${run_id}" =~ ^[1-9][0-9]{0,19}$ ]] || run_id="none"
				failed_workflows+=("${workflow_path}")
				failed_run_ids+=("${run_id}")
				;;
			*) any_unknown="true" ;;
		esac
	done

	if [ "${#failed_workflows[@]}" -eq 0 ]; then
		if [ "${any_unknown}" = "true" ]; then
			RED_BASE_HOLD_STATUS="unknown"
			RED_BASE_HOLD_REASON="runs_lookup_failed"
		elif [ "${any_pending}" = "true" ]; then
			RED_BASE_HOLD_STATUS="pending"
			RED_BASE_HOLD_REASON="no_completed_run"
		else
			RED_BASE_HOLD_STATUS="green"
			RED_BASE_HOLD_REASON="base_green"
		fi
		_red_base_hold_log pass "${hold_issue}"
		return 1
	fi

	# The heal-issue match is the one triage_base_failure_heal_issue uses in
	# scripts/check_failure_triage.sh (#9272), which cannot be sourced.
	if ! heal_listing="$(_red_base_hold_gh_api -X GET "repos/${hold_repo}/issues" \
		-f state=open -f labels=ai:workflow-heal -f sort=created -f direction=asc -F per_page=100 2>/dev/null)" \
		|| ! printf '%s' "${heal_listing}" | jq -e 'type == "array"' >/dev/null 2>&1; then
		heal_failed="true"
	fi
	if [ "${heal_failed}" = "true" ]; then
		RED_BASE_HOLD_STATUS="unknown"
		RED_BASE_HOLD_REASON="heal_lookup_failed"
		RED_BASE_HOLD_WORKFLOW="${failed_workflows[0]}"
		RED_BASE_HOLD_RUN_ID="${failed_run_ids[0]}"
		_red_base_hold_log pass "${hold_issue}"
		return 1
	fi
	for failed_index in "${!failed_workflows[@]}"; do
		heal_number="$(printf '%s' "${heal_listing}" | jq -r \
			--arg source "<!-- workflow-failure-heal:source=${hold_repo}#run -->" \
			--arg workflow "<!-- workflow-failure-heal:workflow=${failed_workflows[${failed_index}]} -->" \
			--arg target "- **Target branch:** \`${hold_branch}\`" '
			[ .[]
				| select(type == "object")
				| select(.pull_request == null)
				| select((.author_association // "") as $a | ["OWNER", "MEMBER", "COLLABORATOR"] | index($a))
				| select(
					([(.body // "" | tostring) | split("\n")[] | rtrimstr("\r")]) as $l
					| ($l[3] // "") == $source
					and ($l[0:7] | index($workflow)) != null
					and ($l | index($target)) != null)
				| .number ]
			| first // empty' 2>/dev/null | tail -n1)" || heal_number=""
		if [[ "${heal_number}" =~ ^[1-9][0-9]{0,9}$ ]]; then
			RED_BASE_HOLD_STATUS="red"
			RED_BASE_HOLD_REASON="base_ci_red"
			RED_BASE_HOLD_RED="true"
			RED_BASE_HOLD_HEAL="${heal_number}"
			RED_BASE_HOLD_WORKFLOW="${failed_workflows[${failed_index}]}"
			RED_BASE_HOLD_RUN_ID="${failed_run_ids[${failed_index}]}"
			_red_base_hold_log held "${hold_issue}"
			return 0
		fi
	done
	RED_BASE_HOLD_STATUS="pending"
	RED_BASE_HOLD_REASON="failure_untracked"
	RED_BASE_HOLD_WORKFLOW="${failed_workflows[0]}"
	RED_BASE_HOLD_RUN_ID="${failed_run_ids[0]}"
	_red_base_hold_log pass "${hold_issue}"
	return 1
}

# red_base_approval_hold_comment <repo> <issue_number> <default_branch>
#
# Posts one hold comment ending with `<!-- ai:red-base-approval-hold:v1 heal=<N> -->`
# after a held red_base_approval_hold_check. Skips the post when a trusted
# comment newer than the newest plan comment already carries the same marker;
# a failed comments read posts anyway. A failed post only warns; a hold never
# turns into an approval.
red_base_approval_hold_comment()
{
	local comment_repo="${1-}" comment_issue="${2-}" comment_branch="${3-}"
	local comment_heal="${RED_BASE_HOLD_HEAL:-none}" comment_workflow="${RED_BASE_HOLD_WORKFLOW:-none}" comment_run="${RED_BASE_HOLD_RUN_ID:-none}"
	local comment_marker="" comments_json="" duplicate="false" hold_body="" run_line=""
	if ! [[ "${comment_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| ! [[ "${comment_issue}" =~ ^[1-9][0-9]{0,9}$ ]] \
		|| ! [[ "${comment_branch}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || [[ "${comment_branch}" == *..* ]] \
		|| ! [[ "${comment_heal}" =~ ^[1-9][0-9]{0,9}$ ]] \
		|| ! [[ "${comment_workflow}" =~ ^\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml$ ]] || [[ "${comment_workflow}" == *..* ]]; then
		echo "::warning::Red-base approval hold comment not posted for issue #${comment_issue//[^0-9]/}: invalid input."
		return 1
	fi
	[[ "${comment_run}" =~ ^[1-9][0-9]{0,19}$ ]] || comment_run="none"
	comment_marker="<!-- ai:red-base-approval-hold:v1 heal=${comment_heal} -->"
	if comments_json="$(_red_base_hold_gh api --paginate "repos/${comment_repo}/issues/${comment_issue}/comments?per_page=100" 2>/dev/null | jq -s -c 'add // []' 2>/dev/null)"; then
		duplicate="$(printf '%s' "${comments_json}" | jq -r --arg marker "${comment_marker}" '
			def trusted: ((.user.login // "" | test("\\[bot\\]$")) or ((.author_association // "") | IN("OWNER", "MEMBER", "COLLABORATOR")));
			def event_key: [(.created_at // ""), ((.id // .databaseId // 0) | tonumber? // 0)];
			def is_plan: ((.body // "") | test("(^|\n)\\s*implementation\\s+plan\\b"; "i"));
			(map(select(trusted and is_plan)) | max_by(event_key)) as $plan
			| any(.[]; trusted and ((.body // "") | contains($marker))
				and (if $plan == null then true else (event_key > ($plan | event_key)) end))' 2>/dev/null)" || duplicate="false"
	else
		echo "::warning::Could not read comments on issue #${comment_issue} to de-duplicate the red-base approval hold comment; posting it."
	fi
	if [ "${duplicate}" = "true" ]; then
		echo "Red-base approval hold already reported for issue #${comment_issue}; not posting another comment."
		return 0
	fi
	if [ "${comment_run}" != "none" ]; then
		run_line="failed (run ${GITHUB_SERVER_URL:-https://github.com}/${comment_repo}/actions/runs/${comment_run})"
	else
		run_line="failed"
	fi
	hold_body="**Automatic approval held: CI on \`${comment_branch}\` is red.**"$'\n\n'
	hold_body+="The plan is ready. The newest \`${comment_workflow}\` push run on \`${comment_branch}\` ${run_line}, and workflow-heal issue #${comment_heal} tracks it, so no automatic \`/approved\` was posted. A pull request started now would inherit the red base and fail CI."$'\n\n'
	hold_body+="Stall recovery may approve the plan later without re-checking base CI, and a maintainer can reply \`/approved\` to start now. \`RED_BASE_APPROVAL_HOLD_ENABLED=false\` turns the hold off."$'\n\n'
	hold_body+="${comment_marker}"
	if ! _red_base_hold_gh api "repos/${comment_repo}/issues/${comment_issue}/comments" -f body="${hold_body}" >/dev/null 2>&1; then
		echo "::warning::Could not post the red-base approval hold comment for issue #${comment_issue}."
	fi
	return 0
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
	echo "usage: source scripts/plan_base_ci_gate.sh, then call red_base_approval_hold_check <repo> <default_branch> <issue_number> <labels_json> [heal_route]" >&2
	exit 2
fi
