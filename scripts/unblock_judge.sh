#!/usr/bin/env bash
# Unblock judge (docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 7, Q10-Q13).
#
# One run judges one blocked item: an issue, a pull request or an orchestrator
# project (its tracking issue). The poller's unblock scan dispatches it through
# unblock_judge_dispatch.yml. The run:
#   1. reads the item, its comments and, for a project item, the tracking
#      issue's comments, and works out the stop id (scripts/unblock_ledger.py);
#   2. if an earlier `descope` / `operator_step` verdict is waiting on its
#      fix-up issue, checks the fix-up instead: still open, it refreshes the
#      wait marker and stops; merged, it posts the stop's resume command
#      (scripts/unblock_actions.py followup) and stops, unless the protected
#      handoff applies and the verdict followed an automation-path rejection
#      that is still the newest guard result (#7287): then it records the
#      operator handoff (unblock_actions.py followup-handoff) instead;
#   2a. for a scope-blocked issue whose newest rejection is from the
#      automation-path guard, re-runs today's guard on the rejected paths and,
#      when it grants them all, resumes the issue without a verdict (once per
#      rejection run); a blocked fix-up the judge filed itself is listed once
#      in the `ai:operator-step` issue instead of getting another fix-up;
#   2a'. for an ai:check-triage issue, runs scripts/check_triage_resolution.py
#      gate (Q129): a verified fix closes the issue with evidence, no round;
#   2b. if a `wait` verdict (or an operator step, Q126) holds the item, checks
#      its condition instead (unblock_hold_condition): met, it posts the
#      stop's resume command (unblock_actions.py hold-followup); unmet, it
#      refreshes the hold and stops; after UNBLOCK_JUDGE_WAIT_MAX_DAYS, or
#      when the condition can no longer be met, the hold expires and the
#      item is judged again. A hold is not a round;
#   3. fingerprints the failure, asks the ledger what is still allowed
#      (never repeat a verdict per fingerprint, 2 rounds per item, 6 per
#      project, the terminal close), and, unless only the close is left, asks
#      the UNBLOCK_JUDGE role (prompts/mode-judge-unblock.txt) for a verdict;
#   4. validates the verdict against the ledger and the hard limits, posts it
#      with its <!-- ai:unblock:v1 ... --> marker on the item (and on the
#      tracking issue for a project's item), and runs the operations
#      scripts/unblock_actions.py plans for it.
#
# Usage: unblock_judge.sh            Env: REPOSITORY, ITEM.
# Optional env: SUPPORT_DIR (checkout with scripts/ and prompts/, default this
# script's repository root), TARGET_DIR (read-only checkout of the default
# branch for the model; default SUPPORT_DIR), RUNTIME_DIR,
# UNBLOCK_JUDGE_ENABLED (default true), NEEDS_HUMAN_DIGEST_ENABLED (default
# true: a ledger-forced terminal parks the item in the needs-human digest,
# scripts/operator_step_issue.py, instead of closing it; false restores the
# terminal close), UNBLOCK_JUDGE_MODEL,
# UNBLOCK_JUDGE_REASONING, UNBLOCK_JUDGE_TIMEOUT_SECS (default 1500),
# UNBLOCK_JUDGE_FIXUP_WAIT_HOURS (default 72),
# UNBLOCK_PROTECTED_HANDOFF_ENABLED (default false; #7060: on an issue whose
# newest trusted guard comment is an automation-path grant rejection,
# `descope` / `operator_step` record an operator handoff instead of a fix-up,
# and a trusted handoff hold marker keeps later runs from judging it again),
# UNBLOCK_JUDGE_WAIT_ENABLED (default true; `false` takes `wait` off the menu,
# ends open holds and lets an operator_step fix-up resume at once again; any
# other value is treated as false), UNBLOCK_JUDGE_WAIT_MAX_DAYS (default 14),
# UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED (default true; any value other than
# `false` keeps it on: verdict instructions that need network or remote access
# are refused and the model is asked once more, op=instruction_sandbox_check),
# MOCK_UNBLOCK_JUDGE_JSON (tests only: used instead of the model),
# MOCK_UNBLOCK_JUDGE_JSON_REASK (tests only, default empty: the model's answer
# to the sandbox re-ask when set),
# MOCK_UNBLOCK_JUDGE_NOW (tests only).
# UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED (default true; read by
# scripts/unblock_actions.py: a harness-owned verdict files a heal report
# instead of a fix-up; `false` restores the old behaviour),
# WORKFLOW_HEAL_ENABLED (default true; `false` skips the heal report and the
# project hold request).
# UNBLOCK_CREATED_ISSUES_FILE (optional, default unset): unblock_run_ops
# appends each issue number it creates to this file.
#
# Both engines run in a network-isolated container with a host-side provider relay;
# its output is data, and verdicts containing literal or encoded credentials
# are rejected before only ledger-approved operations are acted on.
# A PR binds to a project only with a same-repository head, an ai/issue-<n>
# head branch, and <n> in pipeline-authored project state.
#
# Never fails its caller: every problem is logged and the exit code is 0.
# API budget (CLAUDE.md §15), per run: one `user` read, the item, its
# comments (paginated; fresh reads before recording a `project-failed`
# verdict and before its terminal label), the tracking issue's comments for a project's item,
# at most one linked-issue read, one fix-up read while waiting, the PR diff,
# at most three run-metadata reads to bind a run cited in a pipeline-authored
# comment to this item, then that run's log, one verdict comment (two for a
# project's item) and
# the planned operations (at most about six writes). For a standalone `reissue`
# of a CI-failure issue (#7032, unblock_resolve_ci_owner): one owning-PR read,
# then either one `actions/runs?head_sha=` read plus the shared check gate
# (one protection read plus paginated check-runs) or one `git/ref` read. A scope re-grant also
# reads the source issues a pipeline-authored item names (files_touched_scope_guard.py
# --origin-repo: at most 8 issues, two attempts each). A failed project marker
# is reconciled if the item stays blocked; actuation used the item ledger.
# A hold check reads at most two items (the pull request and its compare
# against the branch, or the issue) or runs a local `git grep` in TARGET_DIR.
# For an ai:workflow-heal issue (Q148, unblock_heal_already_fixed): the
# repository's heal issues (paginated), one re-issue search, and at most three
# closed-PR reads for the fix branches of matching siblings.
# A `heal_report` op (harness routing): one actions/runs listing and one
# repository_dispatch to shubhodeep1/coding-workflows; a `hold_request` op:
# one comment on the tracking issue.
# Log: UNBLOCK_JUDGE item= kind= stop= fingerprint= verdict= round= outcome= reason=
#      UNBLOCK_JUDGE item= op=hold hold= ref= outcome=held|holding|met|expired|skip reason=
#      UNBLOCK_JUDGE item= op=reissue outcome=superseded pr= head=
#      UNBLOCK_JUDGE item= kind= stop= fingerprint= op=instruction_sandbox_check outcome=reasked|refused_after_reask reason=
#      UNBLOCK_JUDGE item= op=ci_owner outcome= reason=|pr=
#      UNBLOCK_JUDGE item= op=heal_already_fixed outcome=closed|judge|skip sibling= pr= status=|reason=
#      UNBLOCK_JUDGE item= op=heal_report issue= label= outcome=sent|failed|skip reason=
#      UNBLOCK_JUDGE item= op=hold_request issue= outcome=posted|failed|skip reason=
#      UNBLOCK_TARGET_REPO_GUARD item= verdict= outcome=consumer|harness|mixed|disabled paths=
# Project-failed fallback skips: project_state_unverified, project_not_failed,
# project_state_recheck_unavailable, project_resumed.
set -uo pipefail

UNBLOCK_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPPORT_DIR="${SUPPORT_DIR:-$(cd "${UNBLOCK_SELF_DIR}/.." && pwd)}"
TARGET_DIR="${TARGET_DIR:-${SUPPORT_DIR}}"
RUNTIME_DIR="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}/unblock-judge}"
mkdir -p "${RUNTIME_DIR}"
UNBLOCK_SOURCE_REPO="shubhodeep1/coding-workflows"
NEEDS_HUMAN_DIGEST_ENABLED="${NEEDS_HUMAN_DIGEST_ENABLED:-true}"
UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED="${UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED:-true}"
# Spliced into the re-ask prompt, so it is never taken from the environment.
UNBLOCK_JUDGE_REASK_NOTE=""
# Set by unblock_ask_model when the model gave no answer; never from the environment.
UNBLOCK_JUDGE_MODEL_NO_ANSWER="false"
if [ -f "${SUPPORT_DIR}/scripts/label_helpers.sh" ]; then
	# shellcheck source=/dev/null
	source "${SUPPORT_DIR}/scripts/label_helpers.sh"
fi

unblock_log()
{
	echo "UNBLOCK_JUDGE $*"
}

unblock_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "$@"
}

unblock_now()
{
	if [ -n "${MOCK_UNBLOCK_JUDGE_NOW:-}" ]; then
		printf '%s\n' "${MOCK_UNBLOCK_JUDGE_NOW}"
	else
		date -u +%Y-%m-%dT%H:%M:%SZ
	fi
}

# All comments of an issue or PR, oldest first, as one JSON array.
unblock_fetch_comments()
{
	local number="$1" out="$2"
	if ! gh api --paginate "repos/${REPOSITORY}/issues/${number}/comments?per_page=100" 2>/dev/null | jq -s 'add // []' > "${out}" 2>/dev/null; then
		echo '[]' > "${out}"
		return 1
	fi
	jq -e 'type == "array"' "${out}" >/dev/null 2>&1 || { echo '[]' > "${out}"; return 1; }
}

# Only the pipeline login's V2 state can authorize the unlabeled project-failed
# fallback. No V1 or untrusted-comment fallback is safe for this decision.
unblock_trusted_project_status()
{
	local comments_file="$1" out_state_file="$2"
	jq -e --arg login "${UNBLOCK_LOGIN}" '
		if type == "array" then [.[] | select(.user.login == $login)] else empty end
	' "${comments_file}" > "${RUNTIME_DIR}/project_state_comments.json" 2>/dev/null || return 1
	unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract \
		--require-latest --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${out_state_file}" 2>/dev/null || return 1
	jq -er 'if type == "object" and (.status | type == "string") then .status else empty end' \
		"${out_state_file}" 2>/dev/null
}

unblock_tg()
{
	local level="$1" text="$2"
	if [ -f "${SUPPORT_DIR}/scripts/tg_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${SUPPORT_DIR}/scripts/tg_helpers.sh" 2>/dev/null || true
		if declare -F tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "${text}" "${level}" >/dev/null 2>&1 || true
		fi
	fi
}

# The newest trusted wait marker: `<comment id> <fixup> <done|open> <iso time>`.
unblock_latest_wait()
{
	jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" '
		[.[] | select((.user.login // "") == $login)
			| . as $c
			| ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | rtrimstr("\r")) as $last
			| ($last | capture("^<!-- ai:unblock-wait:v1 item=(?<item>[0-9]+) fixup=(?<fixup>[0-9]+)(?<done> done)? -->$")?) as $m
			| select($m.item == $item)
			| {id: $c.id, fixup: $m.fixup, done: (if ($m.done // "") != "" then "done" else "open" end), at: ($c.created_at // ""), updated: ($c.updated_at // $c.created_at // "")}]
		| sort_by(.at) | last // empty
		| "\(.id) \(.fixup) \(.done) \(.at) \(.updated)"
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null || true
}

# Default on: unset, empty or `true`; `false` turns waits off; any other
# value turns them off and says so.
unblock_wait_enabled()
{
	case "${UNBLOCK_JUDGE_WAIT_ENABLED:-true}" in
		""|true|TRUE|True) return 0 ;;
		false|FALSE|False) return 1 ;;
	esac
	unblock_log "item=${ITEM:-} op=wait_config outcome=disabled reason=invalid_value"
	return 1
}

unblock_wait_max_hours()
{
	local days="${UNBLOCK_JUDGE_WAIT_MAX_DAYS:-14}"
	if ! [[ "${days}" =~ ^[0-9]{1,3}$ ]] || [ "$((10#${days}))" -lt 1 ]; then
		# stderr: callers read the hours from stdout.
		unblock_log "item=${ITEM:-} op=wait_config outcome=default reason=invalid_max_days" >&2
		days=14
	fi
	echo "$((10#${days} * 24))"
}

# The newest trusted hold marker on the item: "<id> <kind> <ref> <open|done|expired> <created> <updated>".
unblock_latest_hold()
{
	jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" '
		[.[] | select((.user.login // "") == $login)
			| . as $c
			| ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | rtrimstr("\r")) as $last
			| ($last | capture("^<!-- ai:unblock-wait:v1 item=(?<item>[0-9]+) hold=(?<kind>pr_on_branch|item_closed|placeholder_retired) ref=(?<ref>[A-Za-z0-9_./@-]{1,120})(?: (?<state>done|expired))? -->$")?) as $m
			| select($m.item == $item)
			| {id: $c.id, kind: $m.kind, ref: $m.ref, state: ($m.state // "open"), at: ($c.created_at // ""), updated: ($c.updated_at // $c.created_at // "")}]
		| sort_by(.at) | last // empty
		| "\(.id) \(.kind) \(.ref) \(.state) \(.at) \(.updated)"
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null || true
}

unblock_hold_description()
{
	case "$1" in
		pr_on_branch) printf 'pull request #%s merged and on `%s`' "${2%%@*}" "${2#*@}" ;;
		item_closed) printf '#%s closed' "$2" ;;
		placeholder_retired) printf 'operator step `%s` done' "$2" ;;
	esac
}

# Whether a hold's condition holds now: prints met, unmet, impossible (it can
# never be met, so the hold ends) or unknown (a read failed; try later).
unblock_hold_condition()
{
	local kind="$1" ref="$2" pr branch pr_json state merged sha status dir rc=0
	case "${kind}" in
		item_closed)
			[[ "${ref}" =~ ^[1-9][0-9]*$ ]] || { echo unknown; return 0; }
			state="$(gh api "repos/${REPOSITORY}/issues/${ref}" --jq '.state // ""' 2>/dev/null)" || { echo unknown; return 0; }
			case "${state}" in
				closed) echo met ;;
				open) echo unmet ;;
				*) echo unknown ;;
			esac
			;;
		pr_on_branch)
			pr="${ref%%@*}"
			branch="${ref#*@}"
			if ! [[ "${pr}" =~ ^[1-9][0-9]*$ ]] || [ -z "${branch}" ] || [ "${branch}" = "${ref}" ]; then
				echo unknown
				return 0
			fi
			pr_json="$(gh api "repos/${REPOSITORY}/pulls/${pr}" --jq '{state, merged, merge_commit_sha}' 2>/dev/null)" || { echo unknown; return 0; }
			state="$(jq -r '.state // ""' <<< "${pr_json}" 2>/dev/null || true)"
			merged="$(jq -r '.merged // false' <<< "${pr_json}" 2>/dev/null || true)"
			sha="$(jq -r '.merge_commit_sha // ""' <<< "${pr_json}" 2>/dev/null || true)"
			if [ "${merged}" != "true" ]; then
				case "${state}" in
					closed) echo impossible ;;
					open) echo unmet ;;
					*) echo unknown ;;
				esac
				return 0
			fi
			[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { echo unknown; return 0; }
			# `behind` or `identical`: the merge commit is already on the branch.
			status="$(gh api "repos/${REPOSITORY}/compare/${branch}...${sha}" --jq '.status // ""' 2>/dev/null)" || { echo unknown; return 0; }
			case "${status}" in
				identical|behind) echo met ;;
				ahead|diverged) echo unmet ;;
				*) echo unknown ;;
			esac
			;;
		placeholder_retired)
			[[ "${ref}" =~ ^[A-Z][A-Z0-9_]{2,63}$ ]] && [[ "${ref}" == *_UNSET_OPERATOR_STEP ]] || { echo unknown; return 0; }
			dir="${TARGET_DIR:-${SUPPORT_DIR}}"
			if ! git -C "${dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
				echo unknown
				return 0
			fi
			# The changelog keeps the history of a retired step, so it does not count.
			git -C "${dir}" grep -q -F -e "${ref}" -- . ':(exclude)CHANGELOG.md' ':(exclude)changelog.d' >/dev/null 2>&1 || rc=$?
			case "${rc}" in
				1) echo met; return 0 ;;
				0) ;;
				*) echo unknown; return 0 ;;
			esac
			# Still named in code: an env-var step is done once a person set it as a
			# repository Actions variable or secret (Q130). A read error, a missing
			# permission included, keeps the hold waiting.
			if [ "$(gh api "repos/${REPOSITORY}/actions/variables/${ref}" --jq '.name // ""' 2>/dev/null || true)" = "${ref}" ] \
				|| [ "$(gh api "repos/${REPOSITORY}/actions/secrets/${ref}" --jq '.name // ""' 2>/dev/null || true)" = "${ref}" ]; then
				echo met
			else
				echo unmet
			fi
			;;
		*) echo unknown ;;
	esac
}

# The placeholder of the trusted operator_step verdict that filed the fix-up
# whose wait marker was posted at $1, when it waits on a person
# (`_UNSET_OPERATOR_STEP`); empty otherwise. The verdict comment, not the
# fix-up, is the record: "Stays off behind `NAME` until the operator step is done."
# Only when the newest verdict before that fix-up wait (the one that filed the
# fix-up) is the operator_step; a later descope fix-up resumes as usual.
unblock_operator_step_placeholder()
{
	jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" --arg at "$1" '
		[.[] | select((.user.login // "") == $login and (.created_at // "") <= $at)
			| ((.body // "") | split("\n") | map(select(test("\\S")) | rtrimstr("\r"))) as $lines
			| select(($lines | last // "") | test("^<!-- ai:unblock:v1 item=" + $item + " stop=[a-z-]+ fingerprint=[0-9a-f]{12} verdict=[a-z_]+ round=[1-9][0-9]*( override=[a-z_]+)? -->$"))
			| {at: (.created_at // ""), operator_step: (($lines | last) | test(" verdict=operator_step ")), name: ([$lines[] | (capture("^Stays off behind `(?<n>[A-Z][A-Z0-9_]{2,63})` until the operator step is done\\.$")? | .n)] | first // "")}]
		| sort_by(.at) | last // empty | select(.operator_step) | .name
		| select(test("^[A-Z][A-Z0-9_]{2,63}$") and endswith("_UNSET_OPERATOR_STEP"))
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null || true
}

# Q129: run the check-triage resolution gate (#7095) that clarify runs. It
# closes an ai:check-triage issue with evidence once its pull request merged
# and the failure is verified fixed on the default branch. Returns 0 when this
# run must stop (closed, or verified but a write failed: retried next run);
# 1 to judge the item as usual.
unblock_check_triage_resolved()
{
	local default_branch verdict status rc=0
	# The gate's own off switch; checked here too so it costs no reads.
	if [ "$(printf '%s' "${CHECK_TRIAGE_RESOLVED_GATE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" = "false" ]; then
		unblock_log "item=${ITEM} op=triage_resolution outcome=judge status=disabled"
		return 1
	fi
	default_branch="$(git -C "${TARGET_DIR:-${SUPPORT_DIR}}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
	if [ -z "${default_branch}" ]; then
		default_branch="$(gh api "repos/${REPOSITORY}" --jq '.default_branch // ""' 2>/dev/null || true)"
	fi
	if ! [[ "${default_branch}" =~ ^[A-Za-z0-9._/-]{1,100}$ ]]; then
		unblock_log "item=${ITEM} op=triage_resolution outcome=skip reason=default_branch_unknown"
		return 1
	fi
	verdict="$(unblock_py "${SUPPORT_DIR}/scripts/check_triage_resolution.py" gate --issue-json "${RUNTIME_DIR}/item.json" \
		--repo "${REPOSITORY}" --issue-number "${ITEM}" --default-branch "${default_branch}" 2>/dev/null)" || rc=$?
	status="$(jq -r '.status // ""' <<< "${verdict}" 2>/dev/null || true)"
	status="$(printf '%s' "${status}" | tr -cd 'a-z_' | cut -c1-40)"
	if [ "${rc}" -eq 3 ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=triage_resolution outcome=skip reason=write_failed"
		return 0
	fi
	if [ "${rc}" -eq 0 ] && { [ "${status}" = "resolved" ] || [ "${status}" = "already_resolved" ]; }; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=triage_resolution outcome=closed status=${status}"
		return 0
	fi
	unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=triage_resolution outcome=judge status=${status:-error} rc=${rc}"
	return 1
}

# Q148: a blocked heal issue whose failure was already fixed through a
# sibling (another heal issue for the same source and failure reason, or an
# unblock-judge re-issue of one, closed as completed with ai:merged and a
# merged ai/issue-<N> pull request) is closed as completed with that link
# instead of using a round (#7082 after #7059 / #7065). Returns 0 when this
# run must stop (closed, or a write failed: retried next run); 1 to judge the
# item as usual. Every read failure judges as usual (fail open).
unblock_heal_already_fixed()
{
	local heal_file="${RUNTIME_DIR}/heal_issues.json" reissues_file="${RUNTIME_DIR}/heal_reissues.json"
	local result_file="${RUNTIME_DIR}/heal_already_fixed.json" candidate pr_json pr_number pr_base pr_sha owner item_created
	if [ "$(printf '%s' "${UNBLOCK_HEAL_ALREADY_FIXED_ENABLED:-true}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" = "false" ]; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=disabled"
		return 1
	fi
	if [ ! -f "${SUPPORT_DIR}/scripts/workflow_failure_heal.py" ]; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=support_missing"
		return 1
	fi
	if ! gh api -X GET --paginate "repos/${REPOSITORY}/issues" -F state=all -F labels=ai:workflow-heal -F per_page=100 \
		--jq '.[] | {number, state, state_reason, body: (.body // ""), author: (.user.login // ""), labels: [.labels[]?.name], pull_request: (.pull_request != null)}' 2>/dev/null \
		| jq -s --arg repository "${REPOSITORY}" 'map(. + {repository: $repository})' > "${heal_file}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${heal_file}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=heal_list_unavailable"
		return 1
	fi
	if ! gh api -X GET search/issues -f q="repo:${REPOSITORY} is:issue in:body \"Re-issued by the unblock judge from\"" \
		-f sort=created -f order=desc -f per_page=100 \
		--jq '[.items[]? | {number, state, state_reason, body: (.body // ""), author: (.user.login // ""), labels: [.labels[]?.name], pull_request: (.pull_request != null)}]' 2>/dev/null \
		| jq --arg repository "${REPOSITORY}" 'map(. + {repository: $repository})' > "${reissues_file}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${reissues_file}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=reissue_search_unavailable"
		return 1
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/workflow_failure_heal.py" heal-already-fixed --item-json "${RUNTIME_DIR}/item.json" \
		--heal-issues-json "${heal_file}" --reissues-json "${reissues_file}" --pipeline-login "${UNBLOCK_LOGIN}" > "${result_file}" 2>/dev/null \
		|| ! jq -e '.status | type == "string"' "${result_file}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=check_failed"
		return 1
	fi
	if [ "$(jq -r '.status' "${result_file}")" != "candidates" ]; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=$(jq -r '.reason // "none"' "${result_file}" | tr -cd 'a-z_' | cut -c1-40)"
		return 1
	fi
	# A fix that merged before this item was filed did not hold (a later
	# generation of the same failure): only a fix merged after it counts.
	item_created="$(jq -r '.created_at // ""' "${RUNTIME_DIR}/item.json" 2>/dev/null | tr -cd '0-9TZ:.+-' | cut -c1-40)"
	if [ -z "${item_created}" ]; then
		unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=item_created_unknown"
		return 1
	fi
	owner="${REPOSITORY%%/*}"
	while read -r candidate; do
		[[ "${candidate}" =~ ^[1-9][0-9]*$ ]] || continue
		# One read per candidate (at most three): the sibling's own fix branch.
		pr_json="$(gh api -X GET "repos/${REPOSITORY}/pulls" -f state=closed -f head="${owner}:ai/issue-${candidate}" -f per_page=10 \
			--jq '[.[] | select(.merged_at != null and .head.ref == "ai/issue-'"${candidate}"'")] | sort_by(.merged_at) | last // empty' 2>/dev/null || true)"
		[ -n "${pr_json}" ] || continue
		pr_number="$(jq -r '.number // ""' <<< "${pr_json}" 2>/dev/null || true)"
		[[ "${pr_number}" =~ ^[1-9][0-9]*$ ]] || continue
		jq -e --arg created "${item_created}" '(.merged_at // "") > $created' <<< "${pr_json}" >/dev/null 2>&1 || continue
		pr_base="$(jq -r '.base.ref // ""' <<< "${pr_json}" | tr -cd 'A-Za-z0-9._/-' | cut -c1-100)"
		pr_sha="$(jq -r '.merge_commit_sha // ""' <<< "${pr_json}" | tr -cd '0-9a-f' | cut -c1-40)"
		# A trusted marker from an earlier run whose close failed: post it once only.
		if jq -e --arg login "${UNBLOCK_LOGIN}" --arg marker "<!-- ai:unblock-heal-already-fixed:v1 item=${ITEM} sibling=${candidate} pr=${pr_number} -->" \
			'any(.[]; (.user.login // "") == $login and ((.body // "") | sub("\\s+$"; "") | endswith($marker)))' "${RUNTIME_DIR}/item_comments.json" >/dev/null 2>&1; then
			:
		elif ! gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="Closing as completed: the same failure was already fixed through #${candidate}, whose pull request #${pr_number} merged into \`${pr_base}\`${pr_sha:+ (\`${pr_sha}\`)}. The unblock judge used no round on this item.

<!-- ai:unblock-heal-already-fixed:v1 item=${ITEM} sibling=${candidate} pr=${pr_number} -->" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=heal_already_fixed outcome=skip reason=comment_failed sibling=${candidate} pr=${pr_number}"
			return 0
		fi
		if ! gh api -X PATCH "repos/${REPOSITORY}/issues/${ITEM}" -f state=closed -f state_reason=completed >/dev/null 2>&1; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=heal_already_fixed outcome=skip reason=close_failed sibling=${candidate} pr=${pr_number}"
			return 0
		fi
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=heal_already_fixed outcome=closed sibling=${candidate} pr=${pr_number}"
		return 0
	done < <(jq -r '.candidates[]?' "${result_file}")
	unblock_log "item=${ITEM} op=heal_already_fixed outcome=judge status=no_merged_pr"
	return 1
}

unblock_patch_comment()
{
	local comment_id="$1" body="$2"
	gh api -X PATCH "repos/${REPOSITORY}/issues/comments/${comment_id}" -f body="${body}" >/dev/null 2>&1
}

unblock_hours_since()
{
	python3 - "$1" "$(unblock_now)" <<'PY'
import datetime as dt, sys
def parse(v):
	t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
	return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
try:
	print(int((parse(sys.argv[2]) - parse(sys.argv[1])).total_seconds() // 3600))
except ValueError:
	print(0)
PY
}

# Re-reads a project-failed fallback's trusted state directly before a
# terminal label (ai:unblock-closed or the needs-human park) is applied.
# Returns 1, after logging, unless the project is still failed.
unblock_project_terminal_recheck()
{
	local recheck_op="$1" terminal_project_status
	if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments_terminal.json"; then
		unblock_log "item=${ITEM} kind=project op=${recheck_op} outcome=skip reason=project_state_terminal_recheck_unavailable"
		return 1
	fi
	if ! terminal_project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments_terminal.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
		unblock_log "item=${ITEM} kind=project op=${recheck_op} outcome=skip reason=project_state_terminal_unverified"
		return 1
	fi
	if [ "${terminal_project_status}" != "failed" ]; then
		unblock_log "item=${ITEM} kind=project op=${recheck_op} outcome=skip reason=project_state_changed_before_terminal_label status=$(printf '%s' "${terminal_project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
		return 1
	fi
	return 0
}

# Validation-harness routing, Phase 3 (docs/plans/validation-failure-harness-routing-plan.md):
# send the `heal_report` op that scripts/unblock_actions.py plans for a
# harness-owned verdict, the reporter sequence of
# scripts/workflow_failure_heal_report.sh on the judge's already-read files.
# Returns 0 when dispatched, 2 when skipped (UNBLOCK_HEAL_SKIP_REASON set),
# 1 when a step failed. The OIDC token and the dispatch body are removed in
# every outcome and never printed.
UNBLOCK_HEAL_SKIP_REASON=""
unblock_send_heal_report()
{
	local ops_file="$1" idx="$2" issue label sendable heal_py issue_json runs_json payload identity dispatch identity_status wrapper_sha="" run_url="" detail
	local -a wrap_args=()
	UNBLOCK_HEAL_SKIP_REASON=""
	issue="$(jq -r ".ops[${idx}].issue // empty" "${ops_file}" 2>/dev/null)"
	label="$(jq -r ".ops[${idx}].label // empty" "${ops_file}" 2>/dev/null)"
	sendable="$(jq -r ".ops[${idx}].sendable // false" "${ops_file}" 2>/dev/null)"
	heal_py="${SUPPORT_DIR}/scripts/workflow_failure_heal.py"
	# no_accepted_label first: the planner already pages for it, so a second
	# skip reason must not add another WARNING after the ops.
	if [ "${sendable}" != "true" ] || [ -z "${label}" ]; then
		UNBLOCK_HEAL_SKIP_REASON="no_accepted_label"
	elif [ "$(printf '%s' "${WORKFLOW_HEAL_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
		UNBLOCK_HEAL_SKIP_REASON="workflow_heal_disabled"
	elif [ "${REPOSITORY}" = "${UNBLOCK_SOURCE_REPO}" ]; then
		UNBLOCK_HEAL_SKIP_REASON="self_repo"
	elif [ ! -f "${heal_py}" ] || [ -L "${heal_py}" ]; then
		UNBLOCK_HEAL_SKIP_REASON="helper_missing"
	elif ! [[ "${issue}" =~ ^[1-9][0-9]*$ ]]; then
		UNBLOCK_HEAL_SKIP_REASON="invalid_issue"
	else
		# The labels the heal intake accepts (workflow_failure_heal.HUMAN_NEEDED_LABELS).
		case "${label}" in
			ai:needs-human|ai:check-triage-escalated|ai:destructive-blocked|ai:scope-blocked|ai:harness-broken|ai:resolver-escalated|ai:security-pass-failed) ;;
			*) UNBLOCK_HEAL_SKIP_REASON="label_not_accepted" ;;
		esac
	fi
	if [ -n "${UNBLOCK_HEAL_SKIP_REASON}" ]; then
		unblock_log "item=${ITEM} op=heal_report outcome=skip reason=${UNBLOCK_HEAL_SKIP_REASON}"
		return 2
	fi
	issue_json="${RUNTIME_DIR}/item.json"
	if [ "${issue}" != "${ITEM}" ]; then
		issue_json="${RUNTIME_DIR}/heal_issue.json"
		if ! gh api "repos/${REPOSITORY}/issues/${issue}" > "${issue_json}" 2>/dev/null \
			|| ! jq -e '.number' "${issue_json}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=failed reason=issue_read_failed"
			return 1
		fi
	fi
	# A pull request: the intake binds an issue-kind report only to an issue
	# (kind_mismatch otherwise) and accepts unblock_judge.yml only as the
	# reporter of issue-kind reports, so the report would be dropped unfiled.
	if jq -e '.pull_request' "${issue_json}" >/dev/null 2>&1; then
		UNBLOCK_HEAL_SKIP_REASON="pull_request_item"
		unblock_log "item=${ITEM} op=heal_report issue=${issue} outcome=skip reason=${UNBLOCK_HEAL_SKIP_REASON}"
		return 2
	fi
	# API hygiene (CLAUDE.md §15): the judge's other run reads
	# (unblock_select_run_log) fetch single runs by id; none lists runs. The
	# heal intake drops a report that names no run (#7424), and
	# build-issue-payload picks the project's failed validation runs from this
	# listing, so one listing per sent heal report. Fail open to no runs.
	runs_json="${RUNTIME_DIR}/heal_runs.json"
	if ! gh api --method GET "repos/${REPOSITORY}/actions/runs" -F per_page=100 > "${runs_json}" 2>/dev/null \
		|| ! jq -e 'type == "object"' "${runs_json}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=heal_report issue=${issue} runs=unavailable"
		printf '{"workflow_runs":[]}' > "${runs_json}"
	fi
	# Release pin: consumer wrappers carry `uses: shubhodeep1/coding-workflows/...@<sha> # stable`.
	if [ -d "${TARGET_DIR}/.github/workflows" ]; then
		wrapper_sha="$(grep -rhoE "uses:[[:space:]]+${UNBLOCK_SOURCE_REPO}/\.github/workflows/[A-Za-z0-9_.-]+\.yml@[0-9a-fA-F]{40}" "${TARGET_DIR}/.github/workflows" 2>/dev/null \
			| sed -E 's/.*@//' | head -1 || true)"
	fi
	wrapper_sha="$(printf '%s' "${wrapper_sha}" | tr '[:upper:]' '[:lower:]')"
	[[ "${wrapper_sha}" =~ ^[0-9a-f]{40}$ ]] || wrapper_sha=""
	if [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]]; then
		run_url="${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
	fi
	payload="${RUNTIME_DIR}/heal_payload.json"
	identity="${RUNTIME_DIR}/heal_identity.jwt"
	dispatch="${RUNTIME_DIR}/heal_dispatch.json"
	if ! unblock_py "${heal_py}" build-issue-payload --repo "${REPOSITORY}" --kind issue --label "${label}" \
		--issue-json "${issue_json}" --comments-json "${RUNTIME_DIR}/item_comments.json" --runs-json "${runs_json}" \
		--wrapper-sha "${wrapper_sha}" --reporter-run-url "${run_url}" --pipeline-login "${UNBLOCK_LOGIN:-}" \
		> "${payload}" 2>/dev/null; then
		unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=failed reason=build_failed"
		return 1
	fi
	# The intake skips a report that names no run without filing anything
	# (#7424), so a run-less report counts as failed: no hold, and a WARNING.
	if ! jq -e '((.run_refs // []) | length) > 0' "${payload}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=failed reason=runs_unavailable"
		return 1
	fi
	rm -f "${identity}" "${dispatch}"
	identity_status="$(unblock_py "${heal_py}" request-report-identity --out "${identity}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
	identity_status="$(printf '%s' "${identity_status}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
	unblock_log "item=${ITEM} op=heal_report issue=${issue} ${identity_status:-identity=absent reason=unknown}"
	wrap_args=(--payload-json "${payload}")
	if [ -s "${identity}" ]; then
		wrap_args+=(--report-identity-file "${identity}")
	fi
	if ! unblock_py "${heal_py}" wrap-dispatch "${wrap_args[@]}" > "${dispatch}" 2>/dev/null; then
		rm -f "${identity}" "${dispatch}"
		unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=failed reason=wrap_failed"
		return 1
	fi
	# One attempt: a repository_dispatch is a create, so it is never retried.
	if ! gh api -X POST "repos/${UNBLOCK_SOURCE_REPO}/dispatches" --input "${dispatch}" >/dev/null 2> "${RUNTIME_DIR}/heal_dispatch_error.txt"; then
		detail="$(head -c 200 "${RUNTIME_DIR}/heal_dispatch_error.txt" 2>/dev/null | tr '\n' ' ' | tr -cd '\040-\176' \
			| sed -E 's/gh[pousr]_[A-Za-z0-9_]{10,}/[redacted]/g; s/github_pat_[A-Za-z0-9_]{10,}/[redacted]/g; s/eyJ[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]*){0,2}/[redacted]/g')"
		rm -f "${identity}" "${dispatch}"
		unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=failed reason=dispatch_failed detail=${detail}"
		return 1
	fi
	rm -f "${identity}" "${dispatch}"
	unblock_log "item=${ITEM} op=heal_report issue=${issue} label=${label} outcome=sent"
	return 0
}

# UNBLOCK_TARGET_REPO_GUARD: where the planned ops sent a guarded verdict's
# work. Derived from the planned heal_report op, never recomputed. Log only.
unblock_log_target_repo_guard()
{
	local ops_file="$1" verdict="$2" switch outcome="consumer" paths="none" reason
	case "${verdict}" in
		retry_budget|descope|reissue|accept_with_followup|operator_step) ;;
		*) return 0 ;;
	esac
	switch="$(printf '%s' "${UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
	if [ -n "${switch}" ] && [ "${switch}" != "true" ]; then
		outcome="disabled"
	else
		reason="$(jq -r 'first(.ops[] | select(.op == "heal_report") | .reason) // ""' "${ops_file}" 2>/dev/null || true)"
		case "${reason}" in
			target_repo|backstop) outcome="harness" ;;
			mixed_harness_part) outcome="mixed" ;;
		esac
		if [ "${outcome}" != "consumer" ]; then
			paths="$(jq -r 'first(.ops[] | select(.op == "heal_report") | (.harness_paths // []) | join(",")) // ""' "${ops_file}" 2>/dev/null || true)"
			paths="$(printf '%s' "${paths}" | tr -cd 'A-Za-z0-9._/,-' | cut -c1-300)"
			[ -n "${paths}" ] || paths="none"
		fi
	fi
	echo "UNBLOCK_TARGET_REPO_GUARD item=${ITEM} verdict=${verdict} outcome=${outcome} paths=${paths}"
}

# Runs the operations of unblock_actions.py output, in order. A failed
# operation is logged and the rest still run, except a failed prerequisite
# must not close an item, and a failed close must not add the terminal label.
unblock_run_ops()
{
	local ops_file="$1" project_failed_guard="${2:-false}" count idx op issue number created body label close_failed="false" close_succeeded="false" ops_failed="false" unblock_removed_guard="" heal_sent="false" heal_skip_reason="" heal_rc=0 heal_planned="false" heal_warned="false"
	count="$(jq '.ops | length' "${ops_file}" 2>/dev/null || echo 0)"
	for ((idx = 0; idx < count; idx++)); do
		op="$(jq -r ".ops[${idx}].op" "${ops_file}")"
		issue="$(jq -r ".ops[${idx}].issue // empty" "${ops_file}")"
		if [ "${ops_failed}" = "true" ] && [ "${op}" != "close" ] && [ "${op}" != "telegram" ] \
			&& ! { [ "${op}" = "add_labels" ] && [ "${close_succeeded}" = "true" ] && [ "${ITEM_KIND}" = "pr" ] \
				&& jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and ((.ops[3].text // "") | startswith("Unblock judge closed untrusted PR #"))' "${ops_file}" >/dev/null 2>&1; }; then
			if [ "${op}" = "heal_report" ]; then
				# A heal report skipped after an earlier failure still needs its page.
				heal_planned="true"
				if [ "$(jq -r ".ops[${idx}].sendable // false" "${ops_file}" 2>/dev/null)" = "true" ]; then
					heal_skip_reason="earlier_op_failed"
				else
					heal_skip_reason="no_accepted_label"
				fi
				unblock_log "item=${ITEM} op=heal_report outcome=skip reason=${heal_skip_reason}"
			fi
			continue
		fi
		case "${op}" in
			comment)
				body="$(jq -r ".ops[${idx}].body" "${ops_file}")"
				if ! gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=comment issue=${issue} outcome=failed"
					if [ "${body}" = "/approved" ] && [ -n "${unblock_removed_guard}" ]; then
						# Keep a failed approval discoverable after its trigger label was cleared.
						if ! gh api -X POST "repos/${REPOSITORY}/issues/${issue}/labels" -f "labels[]=${unblock_removed_guard}" >/dev/null 2>&1; then
							unblock_log "item=${ITEM} op=restore_label issue=${issue} label=${unblock_removed_guard} outcome=failed"
							unblock_tg "CRITICAL" "Unblock judge could not restore ${unblock_removed_guard} on #${issue} after /approved failed (${REPOSITORY})."
						fi
					fi
				fi
				;;
			add_labels)
				while IFS= read -r label; do
					[ -n "${label}" ] || continue
					if [ "${close_failed}" = "true" ] && [ "${label}" = "ai:unblock-closed" ]; then
						continue
					fi
					if declare -F ensure_label_exists >/dev/null 2>&1; then
						ensure_label_exists "${label}" "${REPOSITORY}" >/dev/null 2>&1 || true
					fi
					if [ "${project_failed_guard}" = "true" ] && [ "${label}" = "ai:unblock-closed" ]; then
						unblock_project_terminal_recheck add_labels || return 1
					fi
					gh api -X POST "repos/${REPOSITORY}/issues/${issue}/labels" -f "labels[]=${label}" >/dev/null 2>&1 \
						|| { ops_failed="true"; unblock_log "item=${ITEM} op=add_labels issue=${issue} label=${label} outcome=failed"; }
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				;;
			remove_label)
				label="$(jq -r ".ops[${idx}].label" "${ops_file}")"
				if gh api -X DELETE "repos/${REPOSITORY}/issues/${issue}/labels/$(jq -rn --arg l "${label}" '$l | @uri')" >/dev/null 2> "${RUNTIME_DIR}/remove_label_error.txt"; then
					unblock_removed_guard="${label}"
				else
					if ! grep -q 'HTTP 404' "${RUNTIME_DIR}/remove_label_error.txt"; then
						ops_failed="true"
						unblock_log "item=${ITEM} op=remove_label issue=${issue} label=${label} outcome=failed"
					else
						unblock_removed_guard="${label}"
					fi
				fi
				;;
			create_issue)
				local -a create_args=(-f "title=$(jq -r ".ops[${idx}].title" "${ops_file}")" -f "body=$(jq -r ".ops[${idx}].body" "${ops_file}")")
				while IFS= read -r label; do
					[ -n "${label}" ] && create_args+=(-f "labels[]=${label}")
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				created="$(gh api "repos/${REPOSITORY}/issues" "${create_args[@]}" --jq '[.number, ([.labels[].name] | join(","))] | @tsv' 2>/dev/null || true)"
				local created_labels=""
				created_labels="${created#*$'\t'}"
				created="${created%%$'\t'*}"
				if ! [[ "${created}" =~ ^[0-9]+$ ]]; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=create_issue outcome=failed"
					continue
				fi
				while IFS= read -r label; do
					[ -n "${label}" ] || continue
					if [[ ",${created_labels}," != *",${label},"* ]]; then
						ops_failed="true"
						unblock_log "item=${ITEM} op=create_issue outcome=labels_missing issue=${created}"
						break
					fi
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				[ "${ops_failed}" != "true" ] || continue
				# Opt-in record of created issue numbers (default unset: no-op); the
				# conflict resolver's host-only re-issue names the replacement with it.
				if [ -n "${UNBLOCK_CREATED_ISSUES_FILE:-}" ]; then
					printf '%s\n' "${created}" >> "${UNBLOCK_CREATED_ISSUES_FILE}" 2>/dev/null || true
				fi
				number="$(jq -r ".ops[${idx}].wait_on // empty" "${ops_file}")"
				if [[ "${number}" =~ ^[0-9]+$ ]]; then
					gh api "repos/${REPOSITORY}/issues/${number}/comments" \
						-f body="Waiting for fix-up #${created} to merge; the unblock judge resumes this item afterwards.

<!-- ai:unblock-wait:v1 item=${number} fixup=${created} -->" >/dev/null 2>&1 \
						|| {
							ops_failed="true"
							unblock_log "item=${ITEM} op=wait_marker outcome=failed fixup=${created}"
							# Without the parent marker, a later verdict could create another fix-up.
							gh api -X PATCH "repos/${REPOSITORY}/issues/${created}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
								|| unblock_log "item=${ITEM} op=orphan_fixup_close outcome=failed fixup=${created}"
						}
				fi
				;;
			edit_files_touched)
				jq -r '.body // ""' "${RUNTIME_DIR}/item.json" > "${RUNTIME_DIR}/item_body.txt"
				jq -c ".ops[${idx}].paths" "${ops_file}" > "${RUNTIME_DIR}/override_paths.json"
				if unblock_py - "${RUNTIME_DIR}/item_body.txt" "${RUNTIME_DIR}/override_paths.json" > "${RUNTIME_DIR}/item_body_new.txt" <<'PY'
import json, sys
body = open(sys.argv[1], encoding="utf-8").read().rstrip("\n")
paths = json.load(open(sys.argv[2], encoding="utf-8"))
lines = body.split("\n")
for idx, raw in enumerate(lines):
	if raw.strip() not in ("files_touched:", "- files_touched:"):
		continue
	base = len(raw) - len(raw.lstrip(" "))
	end = idx + 1
	indent = base + 2
	existing = set()
	# A bare key's list may sit flush with the key (files_touched_scope_guard.py
	# extract_files_touched); new entries then join it at that indent.
	flush_allowed = raw.strip() == "files_touched:"
	flush = None
	for pos in range(idx + 1, len(lines)):
		candidate = lines[pos]
		if not candidate.strip():
			if existing:
				break
			continue
		cand_indent = len(candidate) - len(candidate.lstrip(" "))
		if flush is None:
			flush = flush_allowed and cand_indent == base
		if (cand_indent != base if flush else cand_indent <= base) or not candidate.strip().startswith("- "):
			break
		indent = cand_indent
		existing.add(candidate.strip()[2:].strip())
		end = pos + 1
	new = [" " * indent + "- " + path for path in paths if path not in existing]
	lines[end:end] = new
	print("\n".join(lines))
	break
else:
	print("\n".join(lines + ["", "files_touched:"] + ["  - " + path for path in paths]))
PY
				then
					gh api -X PATCH "repos/${REPOSITORY}/issues/${issue}" -f body="$(cat "${RUNTIME_DIR}/item_body_new.txt")" >/dev/null 2>&1 \
						|| { ops_failed="true"; unblock_log "item=${ITEM} op=edit_files_touched outcome=failed"; }
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=edit_files_touched outcome=failed"
				fi
				;;
			operator_step)
				jq -c ".ops[${idx}].steps" "${ops_file}" > "${RUNTIME_DIR}/operator_steps.json"
				unblock_py "${SUPPORT_DIR}/scripts/operator_step_issue.py" upsert --repo "${REPOSITORY}" \
					--key "$(jq -r ".ops[${idx}].key" "${ops_file}")" --source "$(jq -r ".ops[${idx}].source" "${ops_file}")" \
					--steps-file "${RUNTIME_DIR}/operator_steps.json" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=operator_step outcome=failed"; }
				;;
			auto_decision)
				jq -c "{decisions: [.ops[${idx}].decision]}" "${ops_file}" > "${RUNTIME_DIR}/decisions.json"
				if unblock_py "${SUPPORT_DIR}/scripts/auto_decisions.py" render --comments-file "${RUNTIME_DIR}/item_comments.json" \
					--decisions-file "${RUNTIME_DIR}/decisions.json" --source "unblock judge" > "${RUNTIME_DIR}/ad.json" 2>/dev/null; then
					body="$(jq -r '.body' "${RUNTIME_DIR}/ad.json")"
					number="$(jq -r '.comment_id // empty' "${RUNTIME_DIR}/ad.json")"
					if [[ "${number}" =~ ^[0-9]+$ ]]; then
						unblock_patch_comment "${number}" "${body}" || { ops_failed="true"; unblock_log "item=${ITEM} op=auto_decision outcome=failed"; }
					else
						gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1 \
							|| { ops_failed="true"; unblock_log "item=${ITEM} op=auto_decision outcome=failed"; }
					fi
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=auto_decision outcome=failed"
				fi
				;;
			close)
				if [ "${ops_failed}" = "true" ] && ! { [ "${ITEM_KIND}" = "pr" ] \
					&& jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and ((.ops[3].text // "") | startswith("Unblock judge closed untrusted PR #"))' "${ops_file}" >/dev/null 2>&1; }; then
					unblock_log "item=${ITEM} op=close issue=${issue} outcome=skip reason=prerequisite_failed"
					continue
				fi
				if [ "$(jq -r ".ops[${idx}].pr" "${ops_file}")" = "true" ]; then
					gh api -X PATCH "repos/${REPOSITORY}/pulls/${issue}" -f state=closed >/dev/null 2>&1 \
						&& close_succeeded="true" \
						|| { close_failed="true"; ops_failed="true"; unblock_log "item=${ITEM} op=close issue=${issue} outcome=failed"; }
				else
					gh api -X PATCH "repos/${REPOSITORY}/issues/${issue}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
						&& close_succeeded="true" \
						|| { close_failed="true"; ops_failed="true"; unblock_log "item=${ITEM} op=close issue=${issue} outcome=failed"; }
				fi
				;;
			needs_human)
				# Plan item 4a (D6): every round is spent, so park the item in the
				# needs-human digest; it stays open and never gets ai:unblock-closed.
				local -a park_args=(needs-human park --repo "${REPOSITORY}" --item "${issue}"
					--kind "$(jq -r ".ops[${idx}].kind" "${ops_file}")" --stop "$(jq -r ".ops[${idx}].stop" "${ops_file}")"
					--reason "$(jq -r ".ops[${idx}].reason" "${ops_file}")" --link "${UNBLOCK_VERDICT_URL:-}" --trusted-login "${UNBLOCK_LOGIN}")
				if [ "${issue}" = "${ITEM}" ] && jq -e 'index("ai:needs-human") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
					park_args+=(--item-labeled)
				fi
				if [ "${project_failed_guard}" = "true" ] && [ "${issue}" = "${ITEM}" ]; then
					# Same late-resume guard as ai:unblock-closed: the poller may have
					# resumed the project after the earlier recheck, and parking adds
					# ai:needs-human, which would leave the resumed project blocked.
					if declare -F ensure_label_exists >/dev/null 2>&1; then
						ensure_label_exists "ai:needs-human" "${REPOSITORY}" >/dev/null 2>&1 || true
					fi
					unblock_project_terminal_recheck needs_human || return 1
				fi
				if unblock_py "${SUPPORT_DIR}/scripts/operator_step_issue.py" "${park_args[@]}" > "${RUNTIME_DIR}/needs_human_park.json"; then
					if jq -e '.newly_parked == true' "${RUNTIME_DIR}/needs_human_park.json" >/dev/null 2>&1; then
						unblock_tg "CRITICAL" "Unblock judge parked #${issue} ($(jq -r ".ops[${idx}].kind" "${ops_file}"), $(jq -r ".ops[${idx}].stop" "${ops_file}"), $(jq -r ".ops[${idx}].reason" "${ops_file}")) in needs-human digest #$(jq -r '.issue // "?"' "${RUNTIME_DIR}/needs_human_park.json"); a person must decide (${REPOSITORY})."
					fi
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=needs_human issue=${issue} outcome=failed"
				fi
				;;
			dispatch_review)
				number="$(jq -r ".ops[${idx}].pr" "${ops_file}")"
				local review_workflow="ai-review.yml"
				[ "${REPOSITORY}" = "${UNBLOCK_SOURCE_REPO}" ] && review_workflow="internal-review.yml"
				gh workflow run "${review_workflow}" -R "${REPOSITORY}" -f pr_number="${number}" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=dispatch_review pr=${number} outcome=failed"; }
				;;
			heal_report)
				heal_planned="true"
				heal_rc=0
				unblock_send_heal_report "${ops_file}" "${idx}" || heal_rc=$?
				if [ "${heal_rc}" -eq 0 ]; then
					heal_sent="true"
				elif [ "${heal_rc}" -eq 2 ]; then
					heal_skip_reason="${UNBLOCK_HEAL_SKIP_REASON}"
				else
					ops_failed="true"
				fi
				;;
			hold_request)
				# No hold without a sent report: nothing would ship the fix it waits for.
				if [ "${heal_sent}" != "true" ]; then
					unblock_log "item=${ITEM} op=hold_request outcome=skip reason=no_heal_report"
					continue
				fi
				if ! [[ "${issue}" =~ ^[1-9][0-9]*$ ]]; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=hold_request outcome=failed reason=invalid_issue"
					continue
				fi
				body="$(jq -r ".ops[${idx}].body" "${ops_file}")"
				if gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1; then
					unblock_log "item=${ITEM} op=hold_request issue=${issue} outcome=posted"
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=hold_request issue=${issue} outcome=failed"
				fi
				;;
			telegram)
				if [ "${ops_failed}" = "true" ] && [ "${close_succeeded}" != "true" ]; then
					if [ "${ITEM_KIND}" = "project" ] && [ "$(jq -r ".ops[${idx}].level" "${ops_file}")" = "CRITICAL" ]; then
						unblock_tg "CRITICAL" "Unblock judge could not complete closure of project #${ITEM}; inspect the failed operation in the workflow log (${REPOSITORY})."
					elif [ "${ITEM_KIND}" = "issue" ] && [ "$(jq -r ".ops[${idx}].level" "${ops_file}")" = "CRITICAL" ] \
						&& jq -e 'index("ai:security") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
						# A failed terminal-label write cannot hide an unresolved finding.
						unblock_tg "CRITICAL" "$(jq -r ".ops[${idx}].text" "${ops_file}") (${REPOSITORY})"
					elif [ "${ITEM_KIND}" = "pr" ] && jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and ((.ops[3].text // "") | startswith("Unblock judge closed untrusted PR #"))' "${ops_file}" >/dev/null 2>&1; then
						# The untrusted-PR close (unblock_actions.py) reports at DEBUG when it
						# lands; a failed close leaves the PR open, which needs a person.
						unblock_tg "WARNING" "Unblock judge could not close untrusted PR #${ITEM}; a write failed (${REPOSITORY})."
					elif [ "${ITEM_KIND}" = "issue" ] && jq -e '.ops[0].op == "operator_step" and .ops[2].op == "telegram" and ((.ops[2].text // "") | startswith("Operator handoff for #"))' "${ops_file}" >/dev/null 2>&1; then
						# Protected-path handoff (#7060): the verdict is already recorded,
						# so a failed write is not retried; the WARNING is the only page.
						# A handoff after a merged fix-up (#7287) keeps its wait open and
						# is retried by the next run.
						if jq -e '.ops[2].handoff_retry == true' "${ops_file}" >/dev/null 2>&1; then
							unblock_tg "WARNING" "Operator handoff for #${ITEM} after fix-up merge is incomplete: a write failed; the next judge run retries it; the item stays blocked (${REPOSITORY})."
						else
							unblock_tg "WARNING" "Operator handoff for #${ITEM} is incomplete: a write failed and the verdict will not be retried; the item stays blocked (${REPOSITORY})."
						fi
					elif jq -e ".ops[${idx}].text // \"\" | contains(\"has no escalation label for a heal report\")" "${ops_file}" >/dev/null 2>&1; then
						# The planner's own no-label WARNING names the real cause; keep it.
						heal_warned="true"
						unblock_tg "WARNING" "$(jq -r ".ops[${idx}].text" "${ops_file}") (${REPOSITORY})"
					elif jq -e 'any(.ops[]; .op == "heal_report")' "${ops_file}" >/dev/null 2>&1; then
						# Harness routing: the verdict is already recorded, so this is the only page.
						heal_warned="true"
						if [ "${heal_sent}" = "true" ]; then
							unblock_tg "WARNING" "Unblock judge sent the harness heal report for #${ITEM}, but a later write (such as the project hold request) failed; the item stays blocked (${REPOSITORY})."
						else
							unblock_tg "WARNING" "Unblock judge could not send the harness heal report for #${ITEM}; a write failed and the item stays blocked (${REPOSITORY})."
						fi
					fi
					continue
				fi
				if [ "${heal_sent}" != "true" ] && [ -n "${heal_skip_reason}" ] && [ "${heal_skip_reason}" != "no_accepted_label" ] \
					&& jq -e ".ops[${idx}].text // \"\" | startswith(\"Unblock judge routed #\")" "${ops_file}" >/dev/null 2>&1; then
					# The planned DEBUG says a report was sent; it was not, so a person must act.
					heal_warned="true"
					if [ "${heal_skip_reason}" = "workflow_heal_disabled" ]; then
						unblock_tg "WARNING" "Unblock judge found #${ITEM} harness-owned, but WORKFLOW_HEAL_ENABLED=false, so no heal report was sent; a person must file the harness change (${REPOSITORY})."
					else
						unblock_tg "WARNING" "Unblock judge found #${ITEM} harness-owned, but no heal report was sent (${heal_skip_reason}); a person must file the harness change (${REPOSITORY})."
					fi
					continue
				fi
				unblock_tg "$(jq -r ".ops[${idx}].level" "${ops_file}")" "$(jq -r ".ops[${idx}].text" "${ops_file}") (${REPOSITORY})"
				;;
			*)
				unblock_log "item=${ITEM} op=${op} outcome=skip reason=unknown_op"
				;;
		esac
	done
	# A mixed route plans no telegram op of its own, so a report that was not
	# sent (skipped or failed) would otherwise page nobody.
	if [ "${heal_planned}" = "true" ] && [ "${heal_sent}" != "true" ] && [ "${heal_warned}" != "true" ] \
		&& [ "${heal_skip_reason}" != "no_accepted_label" ]; then
		if [ "${heal_skip_reason}" = "workflow_heal_disabled" ]; then
			unblock_tg "WARNING" "Unblock judge found a harness-owned part in #${ITEM}, but WORKFLOW_HEAL_ENABLED=false, so no heal report was sent; a person must file the harness change (${REPOSITORY})."
		elif [ -n "${heal_skip_reason}" ]; then
			unblock_tg "WARNING" "Unblock judge found a harness-owned part in #${ITEM}, but no heal report was sent (${heal_skip_reason}); a person must file the harness change (${REPOSITORY})."
		else
			unblock_tg "WARNING" "Unblock judge could not send the harness heal report for #${ITEM}; a person must file the harness change (${REPOSITORY})."
		fi
	fi
	[ "${ops_failed}" != "true" ]
}

# Normalised evidence for the fingerprint: the block's reason line and, for a
# pull request, its failing checks. Run ids, SHAs and long numbers are dropped
# so the same failure keeps the same fingerprint across runs.
unblock_evidence()
{
	unblock_py - "${RUNTIME_DIR}/item_comments.json" "${UNBLOCK_LOGIN}" "${RUNTIME_DIR}/failing_checks.json" "${ITEM_KIND}" "${ITEM}" <<'PY'
import json, re, sys
comments = json.load(open(sys.argv[1], encoding="utf-8"))
login, kind, item = sys.argv[2], sys.argv[4], sys.argv[5]
try:
	checks = json.load(open(sys.argv[3], encoding="utf-8"))
except (OSError, ValueError):
	checks = []
def norm(text):
	text = re.sub(r"https?://\S+", "", text)
	text = re.sub(r"\b[0-9a-f]{7,40}\b", "", text)
	text = re.sub(r"\b\d{6,}\b", "", text)
	return " ".join(text.split())[:300]
reason = ""
for comment in reversed(comments if isinstance(comments, list) else []):
	body = str(comment.get("body") or "")
	if (comment.get("user") or {}).get("login") != login or "ai:unblock" in body or "ORCHESTRATOR_STATE" in body:
		continue
	first = next((line for line in body.splitlines() if line.strip()), "")
	reason = norm(first.lstrip("#").strip())
	if reason:
		break
evidence = {"reason": reason or "no pipeline comment"}
if kind == "pr":
	evidence["pr"] = item
	if isinstance(checks, list) and checks:
		evidence["checks"] = sorted({str(name) for name in checks if name})[:30]
print(json.dumps(evidence))
PY
}

# Item/comments/check-runs and the optional run-log read do not include Actions
# artifact metadata. Only a destructive override needs these three reads.
unblock_rejection_snapshot()
{
	local artifact_id rejection_run_id rejection_artifact_file="${RUNTIME_DIR}/rejection_artifact.json"
	rm -f "${RUNTIME_DIR}/rejected_deletions.json"
	rejection_run_id="$(jq -r 'if .status == "ok" and (.run | type) == "string" then .run else empty end' "${RUNTIME_DIR}/rejection.json")"
	if ! [[ "${rejection_run_id}" =~ ^[1-9][0-9]*$ ]]; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=rejection_run_unbound"
		return 1
	fi
	if ! gh api "repos/${REPOSITORY}/actions/runs/${rejection_run_id}/artifacts?per_page=100" > "${RUNTIME_DIR}/rejection_artifacts.json" 2>/dev/null; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=artifact_list_unavailable"
		return 1
	fi
	if ! jq -e --arg name "destructive-rejection-issue-${ITEM}" --argjson run "${rejection_run_id}" '
		[.artifacts[]? | select(.name == $name and .expired == false and .workflow_run.id == $run)]
		| sort_by(.created_at) | last // empty
	' "${RUNTIME_DIR}/rejection_artifacts.json" > "${rejection_artifact_file}" 2>/dev/null; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=artifact_missing"
		return 1
	fi
	artifact_id="$(jq -r '.id' "${rejection_artifact_file}")"
	rejection_run_id="$(jq -r '.workflow_run.id' "${rejection_artifact_file}")"
	if ! [[ "${artifact_id}" =~ ^[1-9][0-9]*$ && "${rejection_run_id}" =~ ^[1-9][0-9]*$ ]] \
		|| ! gh api "repos/${REPOSITORY}/actions/runs/${rejection_run_id}" > "${RUNTIME_DIR}/rejection_run.json" 2>/dev/null \
		|| ! gh api "repos/${REPOSITORY}/actions/artifacts/${artifact_id}/zip" > "${RUNTIME_DIR}/rejection.zip" 2>/dev/null \
		|| ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" rejection-snapshot \
			--run-file "${RUNTIME_DIR}/rejection_run.json" --artifact-file "${rejection_artifact_file}" \
			--zip-file "${RUNTIME_DIR}/rejection.zip" --item "${ITEM}" --repo "${REPOSITORY}" \
			> "${RUNTIME_DIR}/rejected_deletions.json"; then
		rm -f "${RUNTIME_DIR}/rejected_deletions.json"
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=unverified"
		return 1
	fi
}

# An automation-path latch set by an older guard (Q54). Re-run today's guard on
# the paths the newest trusted rejection names; when it grants every one, post
# the re-grant marker and the stop's resume command instead of asking the
# judge. The implementation run checks the grant again before it commits.
# Returns 0 when it acted (this run ends), 1 to let the judge decide.
# UNBLOCK_REGRANT_RESULT is `regranted` or `failed` after a 0 return, so a
# caller can tell a completed resume from a failed actuation (#7287).
unblock_try_regrant()
{
	UNBLOCK_REGRANT_RESULT=""
	local rejection_file="${RUNTIME_DIR}/automation_rejection.json" grant_file="${RUNTIME_DIR}/automation_grant.json"
	local staged_file="${RUNTIME_DIR}/automation_staged.txt" ops_file="${RUNTIME_DIR}/regrant_ops.json" run_id reason guard_rc
	if [ "${UNBLOCK_JUDGE_REGRANT_ENABLED:-true}" != "true" ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant outcome=skip reason=disabled"
		return 1
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" automation-rejection --item "${ITEM}" \
		--comments-file "${RUNTIME_DIR}/item_comments.json" --trusted-login "${UNBLOCK_LOGIN}" > "${rejection_file}" 2>/dev/null; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant outcome=skip reason=rejection_unreadable"
		return 1
	fi
	if [ "$(jq -r '.status // ""' "${rejection_file}" 2>/dev/null)" != "ok" ]; then
		reason="$(jq -r '.reason // "unknown"' "${rejection_file}" 2>/dev/null | tr -cd 'a-z_' | cut -c1-40)"
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant outcome=skip reason=${reason:-unknown}"
		return 1
	fi
	run_id="$(jq -r '.run' "${rejection_file}")"
	if ! [[ "${run_id}" =~ ^[0-9]+$ ]] || ! jq -r '.paths[]' "${rejection_file}" > "${staged_file}" 2>/dev/null; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant outcome=skip reason=rejection_unreadable"
		return 1
	fi
	# --origin-repo (Q82): a re-grant re-checks a re-issue's source chain too
	# (one gh api read per source issue, at most 8).
	if ! unblock_py "${SUPPORT_DIR}/scripts/files_touched_scope_guard.py" --emit-automation-grant "${grant_file}" \
		--issue-meta-file "${RUNTIME_DIR}/item.json" --pipeline-login "${UNBLOCK_LOGIN}" \
		--origin-repo "${REPOSITORY}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant run=${run_id} outcome=skip reason=grant_unavailable"
		return 1
	fi
	unblock_py "${SUPPORT_DIR}/scripts/files_touched_scope_guard.py" --check-automation-paths \
		--automation-grant-file "${grant_file}" --staged-file "${staged_file}" \
		--allow-workflow-edits "${ALLOW_WORKFLOW_EDITS:-false}" --issue-number "${ITEM}" >/dev/null 2>&1
	guard_rc=$?
	if [ "${guard_rc}" -ne 0 ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant run=${run_id} outcome=skip reason=still_denied rc=${guard_rc}"
		return 1
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" regrant --context-file "${RUNTIME_DIR}/context.json" \
		--rejection-file "${rejection_file}" > "${ops_file}" || ! unblock_run_ops "${ops_file}"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant run=${run_id} outcome=failed reason=actuation_failed"
		UNBLOCK_REGRANT_RESULT="failed"
		return 0
	fi
	unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=regrant run=${run_id} paths=$(wc -l < "${staged_file}" | tr -d ' ') outcome=regranted"
	UNBLOCK_REGRANT_RESULT="regranted"
	return 0
}

# A blocked fix-up the judge filed for another item (Q56): the trusted fix-up
# marker on the first non-blank line of an issue the pipeline login authored
# (a project fix-up the poller files from a request comment starts with a
# blank line). Prints the original item number.
unblock_fixup_origin()
{
	jq -r --arg login "${UNBLOCK_LOGIN}" '
		select((.user.login // "") == $login)
		| (.body // "") | split("\n") | map(rtrimstr("\r")) | map(select(test("\\S"))) | first // ""
		| capture("^<!-- ai:unblock-fixup:v1 item=(?<item>[1-9][0-9]*) round=[1-9][0-9]* -->$")? | .item
	' "${RUNTIME_DIR}/item.json" 2>/dev/null || true
}

# Escalate a blocked fix-up once: an `ai:operator-step` entry, a marker
# comment, one WARNING. A later run finds the marker and stops quietly.
unblock_escalate_fixup()
{
	local origin="$1" ops_file="${RUNTIME_DIR}/fixup_escalate_ops.json"
	if jq -e --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" '
		any(.[]; (.user.login // "") == $login
			and ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | rtrimstr("\r"))
				== "<!-- ai:unblock-fixup-escalated:v1 item=\($item) -->")
	' "${RUNTIME_DIR}/item_comments.json" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} origin=${origin} outcome=skip reason=fixup_escalated"
		return 0
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" fixup-escalate --context-file "${RUNTIME_DIR}/context.json" \
		--origin "${origin}" > "${ops_file}" || ! unblock_run_ops "${ops_file}"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} origin=${origin} outcome=failed reason=fixup_escalation_failed"
		return 0
	fi
	unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} origin=${origin} outcome=escalated reason=fixup_blocked"
}

# A project may only claim an issue that its authenticated V2 state lists.
unblock_state_lists_issue()
{
	local bound_issue="$1" binding_json="$2"
	jq -e --argjson issue "${bound_issue}" 'any((.issue_number_map // {})[]; . == $issue) or any(.waves[]?.issues[]?; .github_issue == $issue) or any(.security_pass_active_fix_issues[]?; . == $issue) or any(.validation_active_fix_issues[]?; . == $issue)' "${binding_json}" >/dev/null 2>&1
}

# Facts about the PR that owns a CI-failure issue, for a standalone reissue
# (#7032). Merges `ci_owner` into context.json; unblock_actions.py decides.
# Fails open: an unreadable fact leaves today's reissue (no or `unverified`
# ci_owner). §14: no earlier call on this path reads the owning PR, its check
# runs or its head ref (the item read is an issue read).
unblock_resolve_ci_owner()
{
	local owner_json owner_pr owner_reason declared pr_json owner_state="verified" owner_head_sha="" owner_head_ref="" owner_base_ref="" default_branch="" ref_json
	UNBLOCK_CI_OWNER_STATE=""
	UNBLOCK_CI_OWNER_PR=""
	UNBLOCK_CI_OWNER_HEAD=""
	owner_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" ci-owner --item-file "${RUNTIME_DIR}/item.json" --repo "${REPOSITORY}" 2>/dev/null)" || return 0
	owner_pr="$(jq -r '.pr // empty' <<< "${owner_json}" 2>/dev/null || true)"
	if ! [[ "${owner_pr}" =~ ^[1-9][0-9]*$ ]]; then
		owner_reason="$(jq -r '.reason // "unknown"' <<< "${owner_json}" 2>/dev/null | tr -cd 'a-z_' | cut -c1-40)"
		[ "${owner_reason}" = "not_ci_failure" ] || unblock_log "item=${ITEM} op=ci_owner outcome=none reason=${owner_reason:-unknown}"
		return 0
	fi
	# The marker and `Pull request:` line are issue-body text, so only an issue
	# written by the pipeline, the triage bot or a repository member names its
	# owning PR; any other author keeps today's reissue.
	if ! jq -e --arg login "${UNBLOCK_LOGIN:-}" '
		(.user.login // "") as $u
		| ($u != "") and (((.author_association // "") | IN("OWNER", "MEMBER", "COLLABORATOR"))
			or ($login != "" and $u == $login) or $u == "github-actions[bot]")
	' "${RUNTIME_DIR}/item.json" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=ci_owner outcome=none reason=untrusted_author"
		return 0
	fi
	declared="$(jq -r '.declared_branch // ""' <<< "${owner_json}" 2>/dev/null || true)"
	if ! pr_json="$(gh api "repos/${REPOSITORY}/pulls/${owner_pr}" 2>/dev/null)" \
		|| ! jq -e --argjson n "${owner_pr}" --arg repo "${REPOSITORY}" '
			type == "object" and .number == $n
			and (((.head.repo.full_name // "") | ascii_downcase) == ($repo | ascii_downcase))
			and (((.base.repo.full_name // "") | ascii_downcase) == ($repo | ascii_downcase))
			and ((.head.sha // "") | test("^[0-9a-f]{40}([0-9a-f]{24})?$"))
		' <<< "${pr_json}" >/dev/null 2>&1; then
		owner_state="unverified"
		unblock_log "item=${ITEM} op=ci_owner outcome=unverified reason=pr_unverified pr=${owner_pr}"
	else
		owner_head_sha="$(jq -r '.head.sha' <<< "${pr_json}")"
		owner_head_ref="$(jq -r '.head.ref // ""' <<< "${pr_json}")"
		owner_base_ref="$(jq -r '.base.ref // ""' <<< "${pr_json}")"
		default_branch="$(jq -r '.base.repo.default_branch // ""' <<< "${pr_json}")"
		if jq -e '.merged == true' <<< "${pr_json}" >/dev/null 2>&1; then
			# Merged into the default branch or the issue's declared integration
			# branch, with the shared required-checks gate green on its head.
			if [ -n "${owner_base_ref}" ] && { [ "${owner_base_ref}" = "${default_branch}" ] || [ "${owner_base_ref}" = "${declared}" ]; } \
				&& [ -f "${SUPPORT_DIR}/scripts/gh_helpers.sh" ] && [ -f "${SUPPORT_DIR}/scripts/pr_checks_lib.sh" ] \
				&& (
					# shellcheck source=/dev/null
					source "${SUPPORT_DIR}/scripts/gh_helpers.sh" >/dev/null 2>&1 || exit 1
					# shellcheck source=/dev/null
					source "${SUPPORT_DIR}/scripts/pr_checks_lib.sh" >/dev/null 2>&1 || exit 1
					declare -F _pr_checks_completed >/dev/null 2>&1 || exit 1
					export PR_CHECKS_REPOSITORY="${REPOSITORY}"
					# Bind the named CI check to an AUTO_MERGE_WAIT_WORKFLOWS run on
					# this head, as _pr_wait_for_required_checks does: other workflows
					# also have a `lint` job, and their success is not CI's. With no
					# named check, or an unreadable runs listing, nothing proves CI
					# passed (AUTO_MERGE_WAIT_WORKFLOWS=none matches by name only).
					case "$(_pr_auto_merge_required_ci_check_name "${owner_base_ref}" 2>/dev/null)" in
						""|none|__invalid__) exit 1 ;;
					esac
					_pr_head_ci_runs_pending "${owner_head_sha}"
					case "${PR_CHECKS_RUNS_READ}" in
						true) PR_CHECKS_CI_RUN_IDS="${PR_CHECKS_RUNS_IDS}" ;;
						disabled) unset PR_CHECKS_CI_RUN_IDS ;;
						*) exit 1 ;;
					esac
					# Three arguments: the shared required-checks filter applies.
					_pr_checks_completed "${owner_pr}" "${owner_head_sha}" "${owner_base_ref}" >/dev/null 2>&1 || exit 1
					# A head with no check-runs at all is not evidence of green checks,
					# nor is an allow-all required set. The required-set filter only
					# blocks failed runs that exist, so an absent required check plus
					# an unrelated green run still returns ok: the named CI check
					# (AUTO_MERGE_REQUIRED_CI_CHECK, from the same listing) must have
					# run and succeeded. With no named check (`disabled`, the consumer
					# default) nothing proves CI passed, so the reissue stays as today.
					[ "${PR_CHECKS_LAST_TOTAL:-0}" -gt 0 ] 2>/dev/null || exit 1
					[ "${PR_CHECKS_LAST_REASON:-}" = "ok" ] || exit 1
					[ "${PR_CHECKS_LAST_CI_STATE:-unknown}" = "success" ] || exit 1
				); then
				owner_state="superseded"
			fi
		elif [ "$(jq -r '.state // ""' <<< "${pr_json}")" = "open" ] \
			&& [[ "${owner_head_ref}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] \
			&& [[ "${owner_head_ref}" != *..* && "${owner_head_ref}" != *//* && "${owner_head_ref}" != */ && "${owner_head_ref}" != *. && "${owner_head_ref}" != *.lock ]]; then
			if ref_json="$(gh api "repos/${REPOSITORY}/git/ref/heads/${owner_head_ref}" 2>/dev/null)" \
				&& jq -e --arg ref "refs/heads/${owner_head_ref}" '.ref == $ref' <<< "${ref_json}" >/dev/null 2>&1; then
				owner_state="open"
			fi
		fi
		[ "${owner_state}" = "open" ] || owner_head_ref=""
		unblock_log "item=${ITEM} op=ci_owner outcome=${owner_state} pr=${owner_pr}"
	fi
	if jq --argjson n "${owner_pr}" --arg state "${owner_state}" --arg sha "${owner_head_sha}" --arg ref "${owner_head_ref}" --arg base "${owner_base_ref}" \
		'. + {ci_owner: {number: $n, state: $state, head_sha: (if $sha == "" then null else $sha end), head_ref: (if $ref == "" then null else $ref end), base_ref: (if $base == "" then null else $base end)}}' \
		"${RUNTIME_DIR}/context.json" > "${RUNTIME_DIR}/context_owner.json" 2>/dev/null \
		&& mv "${RUNTIME_DIR}/context_owner.json" "${RUNTIME_DIR}/context.json"; then
		UNBLOCK_CI_OWNER_STATE="${owner_state}"
		UNBLOCK_CI_OWNER_PR="${owner_pr}"
		UNBLOCK_CI_OWNER_HEAD="${owner_head_sha}"
	fi
	return 0
}

# Protected-path handoff (#7060), off by default: only a scope-blocked issue
# (the stop the automation-path guard latches) is ever held.
unblock_protected_handoff_applies()
{
	[ "$(printf '%s' "${UNBLOCK_PROTECTED_HANDOFF_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ] \
		&& [ "${ITEM_KIND}" = "issue" ] && [ "${ITEM_STOP}" = "scope-blocked" ]
}

# Parses the newest trusted automation-path rejection from the comments this
# run already fetched into ${RUNTIME_DIR}/protected_rejection.json, once per
# run. An unreadable result is written as status none and logged.
UNBLOCK_PROTECTED_REJECTION_LOADED=""
unblock_protected_rejection_load()
{
	local protected_file="${RUNTIME_DIR}/protected_rejection.json"
	if [ "${UNBLOCK_PROTECTED_REJECTION_LOADED}" = "true" ] && [ -f "${protected_file}" ]; then
		return 0
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" protected-rejection --item "${ITEM}" \
		--comments-file "${RUNTIME_DIR}/item_comments.json" --trusted-login "${UNBLOCK_LOGIN}" > "${protected_file}" \
		|| ! jq -e 'type == "object"' "${protected_file}" >/dev/null 2>&1; then
		echo '{"status":"none","reason":"unreadable"}' > "${protected_file}"
		unblock_log "item=${ITEM} op=protected_rejection outcome=failed reason=unreadable"
	fi
	UNBLOCK_PROTECTED_REJECTION_LOADED="true"
}

# Adds the loaded rejection to context.json for unblock_actions.py.
unblock_protected_context_merge()
{
	if jq --slurpfile protected "${RUNTIME_DIR}/protected_rejection.json" \
		'. + {protected_rejection: ($protected[0] | {guard, run, paths, truncated})}' \
		"${RUNTIME_DIR}/context.json" > "${RUNTIME_DIR}/context.json.tmp" 2>/dev/null; then
		mv "${RUNTIME_DIR}/context.json.tmp" "${RUNTIME_DIR}/context.json"
		return 0
	fi
	rm -f "${RUNTIME_DIR}/context.json.tmp"
	unblock_log "item=${ITEM} op=protected_rejection outcome=failed reason=context_merge"
	return 1
}

unblock_main()
{
	local now stop_json fp verdict_name round marker_line comment_body terminal ops_file wait
	UNBLOCK_PROTECTED_REJECTION_LOADED=""
	local project_failed_substituted="false" project_status=""
	if [ "${UNBLOCK_JUDGE_ENABLED:-true}" = "false" ]; then
		unblock_log "item=${ITEM:-} outcome=skip reason=disabled"
		return 0
	fi
	if [ -z "${REPOSITORY:-}" ] || ! [[ "${ITEM:-}" =~ ^[1-9][0-9]*$ ]]; then
		unblock_log "item=${ITEM:-} outcome=skip reason=missing_input"
		return 0
	fi
	now="$(unblock_now)"
	UNBLOCK_LOGIN="$(gh api user --jq '.login' 2>/dev/null || true)"
	if ! [[ "${UNBLOCK_LOGIN}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
		unblock_log "item=${ITEM} outcome=skip reason=login_unavailable"
		return 0
	fi
	if ! gh api "repos/${REPOSITORY}/issues/${ITEM}" > "${RUNTIME_DIR}/item.json" 2>/dev/null \
		|| ! jq -e '.number' "${RUNTIME_DIR}/item.json" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} outcome=skip reason=item_unreadable"
		return 0
	fi
	if [ "$(jq -r '.state' "${RUNTIME_DIR}/item.json")" != "open" ]; then
		unblock_log "item=${ITEM} outcome=skip reason=closed"
		return 0
	fi
	jq -c '[.labels[]?.name]' "${RUNTIME_DIR}/item.json" > "${RUNTIME_DIR}/labels.json"
	if jq -e 'index("ai:orchestrator-tracking")' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		ITEM_KIND="project"
	elif jq -e '.pull_request' "${RUNTIME_DIR}/item.json" >/dev/null 2>&1; then
		ITEM_KIND="pr"
	else
		ITEM_KIND="issue"
	fi
	if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments.json"; then
		unblock_log "item=${ITEM} outcome=skip reason=comments_unavailable"
		return 0
	fi

	# The project an item belongs to, and a PR's linked issue.
	local tracking="" linked="" finding="" body_text pr_json="" head_sha="" head_ref="" head_repo="" binding_issue=""
	local pr_trusted="false" pr_author="" pr_assoc="" pr_head_repo="" untrusted_project_pr="false"
	body_text="$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json")"
	if [ "${ITEM_KIND}" = "issue" ]; then
		finding="$(jq -r '(.body // "") | [match("<!-- ai:security-finding:([^>]+) -->").captures[0].string] | first // "" | gsub("^\\s+|\\s+$"; "")' "${RUNTIME_DIR}/item.json")"
	fi
	if [ "${ITEM_KIND}" = "project" ]; then
		tracking="${ITEM}"
	elif [ "${ITEM_KIND}" = "issue" ] && jq -e 'index("ai:orchestrator-managed")' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		tracking="$(printf '%s\n' "${body_text}" | sed -n 's/^[[:space:]]*-\{0,1\}[[:space:]]*\(\*\*\)\{0,1\}Tracking issue:\(\*\*\)\{0,1\}[[:space:]]*#\([0-9][0-9]*\)[[:space:]]*$/\3/p' | head -n1)"
		binding_issue="${ITEM}"
	fi
	if [ "${ITEM_KIND}" = "pr" ]; then
		# PR body references are untrusted; project binding also requires a
		# same-repository head and trusted project-state membership below.
		pr_json="$(gh api "repos/${REPOSITORY}/pulls/${ITEM}" 2>/dev/null || true)"
		if ! jq -e --argjson item "${ITEM}" '.number == $item and (.base.ref | type == "string")' <<< "${pr_json}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} outcome=skip reason=pr_unreadable"
			return 0
		fi
		head_sha="$(jq -r '.head.sha // ""' <<< "${pr_json}")"
		head_ref="$(jq -r '.head.ref // ""' <<< "${pr_json}")"
		pr_author="$(jq -r '.user.login // ""' <<< "${pr_json}")"
		pr_assoc="$(jq -r '.author_association // ""' <<< "${pr_json}")"
		pr_head_repo="$(jq -r '.head.repo.full_name // ""' <<< "${pr_json}")"
		if jq -e --arg repo "${REPOSITORY}" --arg login "${UNBLOCK_LOGIN}" '
			(.head.repo.full_name | type == "string")
			and ((.head.repo.full_name | ascii_downcase) == ($repo | ascii_downcase))
			and (.user.login | strings | test("^[A-Za-z0-9][A-Za-z0-9-]*(\\[bot\\])?$"))
			and (.head.sha | strings | test("^[0-9a-f]{40}([0-9a-f]{24})?$"))
			and ((.author_association | IN("OWNER", "MEMBER", "COLLABORATOR"))
				or .user.login == $login or .user.login == "github-actions[bot]")
		' <<< "${pr_json}" >/dev/null 2>&1; then
			pr_trusted="true"
		fi
		unblock_log "item=${ITEM} kind=pr op=pr_provenance trusted=${pr_trusted} head_repo=$(printf '%s' "${pr_head_repo}" | tr '\r\n' '  ') assoc=$(printf '%s' "${pr_assoc}" | tr '\r\n' '  ')"
		if [[ "$(jq -r '.base.ref' <<< "${pr_json}")" =~ ^orchestrator/project-([1-9][0-9]*)$ ]]; then
			if [ "${pr_trusted}" != "true" ]; then
				# Never bind an untrusted PR to project state; only its rejection may proceed.
				untrusted_project_pr="true"
			else
				tracking="${BASH_REMATCH[1]}"
				head_repo="$(jq -r '.head.repo.full_name // ""' <<< "${pr_json}")"
				if [ -z "${head_repo}" ] || [ "${head_repo,,}" != "${REPOSITORY,,}" ]; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=fork_head"
					return 0
				fi
				if [[ ! "${head_ref}" =~ ^ai/issue-([1-9][0-9]*)$ ]]; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=head_ref"
					return 0
				fi
				binding_issue="${BASH_REMATCH[1]}"
			fi
		fi
		linked="$(printf '%s\n' "${body_text}" | grep -oiE '\b(refs|closes|close|closed|fixes|fix|fixed|resolves|resolve|resolved)[[:space:]]+#[0-9]+' | head -n1 | grep -oE '[0-9]+' || true)"
	fi

	if ! stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop --labels-json "$(cat "${RUNTIME_DIR}/labels.json")")"; then
		if [ "${ITEM_KIND}" = "project" ]; then
			if ! project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
				unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_unverified"
				return 0
			fi
			if [ "${project_status}" != "failed" ]; then
				unblock_log "item=${ITEM} kind=project outcome=skip reason=project_not_failed status=$(printf '%s' "${project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
				return 0
			fi
			project_failed_substituted="true"
			stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop --project-failed)"
		else
			unblock_log "item=${ITEM} kind=${ITEM_KIND} outcome=skip reason=not_blocked"
			return 0
		fi
	fi
	ITEM_STOP="$(jq -r '.stop' <<< "${stop_json}")"

	local has_plan="false"
	if jq -e --arg login "${UNBLOCK_LOGIN}" 'any(.[]; ((.body // "") | test("(?im)^\\s*implementation\\s+plan\\b")) and (((.user.type // "") == "Bot") or ((.author_association // "") as $a | ["OWNER", "MEMBER", "COLLABORATOR"] | index($a)) or ((.user.login // "") == $login)))' \
		"${RUNTIME_DIR}/item_comments.json" >/dev/null 2>&1; then
		has_plan="true"
	fi
	jq -n --arg repo "${REPOSITORY}" --arg kind "${ITEM_KIND}" --argjson item "${ITEM}" --arg stop "${ITEM_STOP}" \
		--slurpfile labels "${RUNTIME_DIR}/labels.json" --arg tracking "${tracking}" --arg linked "${linked}" --arg finding "${finding}" \
		--argjson has_plan "${has_plan}" --arg title "$(jq -r '.title // ""' "${RUNTIME_DIR}/item.json")" \
		--arg security_body "${body_text}" \
		--argjson pr_trusted "${pr_trusted}" --arg pr_author "${pr_author}" --arg pr_head_repo "${pr_head_repo}" --arg pr_head_sha "${head_sha}" \
		--arg needs_human_digest "${NEEDS_HUMAN_DIGEST_ENABLED}" \
		'{needs_human_digest: (($needs_human_digest | ascii_downcase) != "false"), repo: $repo, kind: $kind, item: $item, stop: $stop, labels: $labels[0], tracking: (if $tracking == "" then null else ($tracking | tonumber) end), linked_issue: (if $linked == "" then null else ($linked | tonumber) end), has_plan: $has_plan, title: $title, security_finding_id: (if $finding == "" then null else $finding end), security_source_body: (if $kind == "issue" and ($labels[0] | index("ai:security")) != null then $security_body else null end), pr_trusted: $pr_trusted, pr_author: $pr_author, pr_head_repo: $pr_head_repo, pr_head_sha: $pr_head_sha}' \
		> "${RUNTIME_DIR}/context.json"

	# A pending fix-up comes first (Q11): wait for it, or run its follow-up.
	local last_activity="" wait_id wait_fixup wait_state wait_at wait_updated fixup_json
	wait=""
	[ "${untrusted_project_pr}" = "true" ] || wait="$(unblock_latest_wait)"
	if [ -n "${wait}" ]; then
		read -r wait_id wait_fixup wait_state wait_at wait_updated <<< "${wait}"
		if [ "${wait_state}" = "done" ]; then
			last_activity="${wait_updated}"
		else
			fixup_json="$(gh api "repos/${REPOSITORY}/issues/${wait_fixup}" --jq '{state, state_reason, labels: [.labels[]?.name]}' 2>/dev/null || echo '{}')"
			if ! jq -e '.state == "open" or .state == "closed"' <<< "${fixup_json}" >/dev/null 2>&1; then
				unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=fixup_unavailable"
				return 0
			fi
			if jq -e '.state == "closed" and .state_reason == "completed" and (.labels | index("ai:merged") != null)' <<< "${fixup_json}" >/dev/null 2>&1; then
				# #7287: a fix-up filed for a descope / operator_step verdict that
				# followed an automation-path rejection must not resume the item
				# into the same guard; hand it to an operator instead.
				if unblock_protected_handoff_applies && unblock_protected_rejection_load \
					&& jq -e '.status == "ok"' "${RUNTIME_DIR}/protected_rejection.json" >/dev/null 2>&1; then
					local followup_run followup_verdict
					followup_run="$(jq -r '.run' "${RUNTIME_DIR}/protected_rejection.json")"
					followup_verdict="$(jq -r '.prior_handoff_verdict // ""' "${RUNTIME_DIR}/protected_rejection.json")"
					# A grant the current guard already accepts resumes the item
					# (as the deterministic exit below would) instead of a handoff.
					rm -f "${RUNTIME_DIR}/automation_rejection.json"
					if unblock_try_regrant; then
						if [ "${UNBLOCK_REGRANT_RESULT:-}" != "regranted" ]; then
							# A failed resume keeps the fix-up wait open, so the next
							# run checks the item again instead of a done wait hiding it.
							unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=followup_regrant_failed run=${followup_run}"
							return 0
						fi
						unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged; the unblock judge re-checked the automation-path grant instead of posting a resume command.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
						unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=followup_regrant run=${followup_run}"
						return 0
					fi
					if jq -e '.handoff_pending == true' "${RUNTIME_DIR}/protected_rejection.json" >/dev/null 2>&1; then
						unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged; this item is waiting for an operator (protected-path handoff), so no resume command was posted.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
						unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=skip reason=operator_handoff_pending run=${followup_run}"
						return 0
					fi
					if jq -e --arg run "${followup_run}" '.status == "none" and .reason == "already_regranted" and .run == $run' \
						"${RUNTIME_DIR}/automation_rejection.json" >/dev/null 2>&1; then
						# An earlier re-grant of this run posted its marker but its resume
						# failed: the guard already granted these paths, so retry the
						# resume below instead of asking an operator to grant them.
						unblock_log "item=${ITEM} fixup=${wait_fixup} op=regrant run=${followup_run} outcome=resume_retry reason=already_regranted"
					elif [[ "${followup_verdict}" =~ ^(descope|operator_step)$ ]]; then
						if ! unblock_protected_context_merge; then
							unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=followup_handoff_failed"
							return 0
						fi
						ops_file="${RUNTIME_DIR}/followup_handoff_ops.json"
						if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" followup-handoff --context-file "${RUNTIME_DIR}/context.json" \
							--fixup "${wait_fixup}" --verdict "${followup_verdict}" > "${ops_file}" \
							|| ! unblock_run_ops "${ops_file}"; then
							# The wait stays open, so the next run retries; the keyed
							# operator step and the hold marker make that idempotent.
							unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=followup_handoff_failed"
							return 0
						fi
						unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged, but the automation-path guard's rejection of run ${followup_run} is still the newest guard result; the unblock judge handed this item to an operator instead of posting a resume command.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
						unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=followup_handoff fixup=${wait_fixup} run=${followup_run}"
						return 0
					fi
				fi
				# Q126 (#6831): a fix-up whose operator step waits on a person does
				# not resume the item; it holds it until that step is done.
				local step_placeholder=""
				if unblock_wait_enabled; then
					step_placeholder="$(unblock_operator_step_placeholder "${wait_at}")"
				fi
				# A hold already posted for this fix-up (its wait marker update failed
				# afterwards) is not posted again: holds count toward MAX_HOLDS_PER_ITEM.
				local prior_hold prior_hold_kind prior_hold_ref prior_hold_at
				prior_hold=""
				[ -n "${step_placeholder}" ] && prior_hold="$(unblock_latest_hold)"
				if [ -n "${prior_hold}" ]; then
					read -r _ prior_hold_kind prior_hold_ref _ prior_hold_at _ <<< "${prior_hold}"
					if [ "${prior_hold_kind}" != "placeholder_retired" ] || [ "${prior_hold_ref}" != "${step_placeholder}" ] \
						|| [[ "${prior_hold_at}" < "${wait_at}" ]]; then
						prior_hold=""
					fi
				fi
				if [ -n "${step_placeholder}" ]; then
					if [ -z "${prior_hold}" ] && ! gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="Fix-up #${wait_fixup} merged. Its operator step \`${step_placeholder}\` still needs a person, so the unblock judge holds this item until that step is done instead of resuming it now. This hold uses no round.

<!-- ai:unblock-wait:v1 item=${ITEM} hold=placeholder_retired ref=${step_placeholder} -->" >/dev/null 2>&1; then
						# The fix-up wait stays open, so the next run tries again.
						unblock_log "item=${ITEM} fixup=${wait_fixup} op=hold hold=placeholder_retired ref=${step_placeholder} outcome=skip reason=hold_record_failed"
						return 0
					fi
					unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged; the unblock judge holds this item until operator step \`${step_placeholder}\` is done.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
					unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} op=hold hold=placeholder_retired ref=${step_placeholder} outcome=held"
					return 0
				fi
				ops_file="${RUNTIME_DIR}/followup_ops.json"
				if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" followup --context-file "${RUNTIME_DIR}/context.json" --fixup "${wait_fixup}" > "${ops_file}" \
					|| ! unblock_run_ops "${ops_file}"; then
					unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=followup_failed"
					return 0
				fi
				unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged; the unblock judge posted this item's resume command.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=followup"
				return 0
			fi
			if [ "$(jq -r '.state // ""' <<< "${fixup_json}")" = "open" ] \
				&& [ "$(unblock_hours_since "${wait_at}")" -lt "${UNBLOCK_JUDGE_FIXUP_WAIT_HOURS:-72}" ]; then
				unblock_patch_comment "${wait_id}" "Waiting for fix-up #${wait_fixup} to merge; the unblock judge resumes this item afterwards. Last checked ${now}.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} -->" || true
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=waiting"
				return 0
			fi
			# Closed without merging, or waited too long: judge again.
			unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} did not merge in time; the unblock judge decides again.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
			last_activity="${now}"
		fi
	fi

	# Q129: a resolved check-triage issue is closed with evidence, no round used.
	if [ "${ITEM_KIND}" = "issue" ] && [ -f "${SUPPORT_DIR}/scripts/check_triage_resolution.py" ] \
		&& jq -e 'index("ai:check-triage") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1 \
		&& unblock_check_triage_resolved; then
		return 0
	fi

	# Q148: a heal issue whose failure a sibling already fixed is closed, no round used.
	if [ "${ITEM_KIND}" = "issue" ] && [ -z "${tracking}" ] \
		&& jq -e 'index("ai:workflow-heal") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1 \
		&& unblock_heal_already_fixed; then
		return 0
	fi

	# A hold from a `wait` verdict or an operator step (Q116, Q126): no round
	# and no resume until its condition holds; then the stop's resume command.
	local hold hold_id hold_kind hold_ref hold_state hold_at hold_updated hold_status hold_desc hold_reason
	hold=""
	[ "${untrusted_project_pr}" = "true" ] || hold="$(unblock_latest_hold)"
	if [ -n "${hold}" ]; then
		read -r hold_id hold_kind hold_ref hold_state hold_at hold_updated <<< "${hold}"
		if [ "${hold_state}" != "open" ]; then
			# Like a fix-up's follow-up, the end of a hold restarts the 24-hour clock.
			if [ -z "${last_activity}" ] || [[ "${hold_updated}" > "${last_activity}" ]]; then
				last_activity="${hold_updated}"
			fi
		else
			hold_desc="$(unblock_hold_description "${hold_kind}" "${hold_ref}")"
			if unblock_wait_enabled; then
				hold_status="$(unblock_hold_condition "${hold_kind}" "${hold_ref}")"
			else
				hold_status="disabled"
			fi
			case "${hold_status}" in
				met)
					ops_file="${RUNTIME_DIR}/hold_followup_ops.json"
					if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" hold-followup --context-file "${RUNTIME_DIR}/context.json" \
						--kind "${hold_kind}" --ref "${hold_ref}" > "${ops_file}" \
						|| ! unblock_run_ops "${ops_file}"; then
						# The hold stays open, so the next run retries the resume.
						unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=followup_failed"
						return 0
					fi
					unblock_patch_comment "${hold_id}" "Waited for ${hold_desc}; the unblock judge posted this item's resume command.

<!-- ai:unblock-wait:v1 item=${ITEM} hold=${hold_kind} ref=${hold_ref} done -->" \
						|| unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=hold_refresh_failed"
					unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=hold hold=${hold_kind} ref=${hold_ref} outcome=met"
					return 0
					;;
				unknown)
					# A condition that stays unreadable still ends at the hold's lifetime.
					if [ "$(unblock_hours_since "${hold_at}")" -lt "$(unblock_wait_max_hours)" ]; then
						# Refresh the open hold so the scan (keyed on the marker's
						# updatedAt) waits UNBLOCK_JUDGE_RETRY_HOURS before re-dispatching.
						unblock_patch_comment "${hold_id}" "Waiting for ${hold_desc}; the condition could not be read this time, so the unblock judge checks again later. This hold uses no round. Last checked ${now}.

<!-- ai:unblock-wait:v1 item=${ITEM} hold=${hold_kind} ref=${hold_ref} -->" \
							|| unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=hold_refresh_failed"
						unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=hold_check_unavailable"
						return 0
					fi
					hold_reason="max_days"
					;;
				unmet)
					if [ "$(unblock_hours_since "${hold_at}")" -lt "$(unblock_wait_max_hours)" ]; then
						unblock_patch_comment "${hold_id}" "Waiting for ${hold_desc}; the unblock judge resumes this item afterwards. This hold uses no round. Last checked ${now}.

<!-- ai:unblock-wait:v1 item=${ITEM} hold=${hold_kind} ref=${hold_ref} -->" \
							|| unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=hold_refresh_failed"
						unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=hold hold=${hold_kind} ref=${hold_ref} outcome=holding"
						return 0
					fi
					hold_reason="max_days"
					;;
				impossible) hold_reason="cannot_be_met" ;;
				*) hold_reason="waits_disabled" ;;
			esac
			unblock_patch_comment "${hold_id}" "Stopped waiting for ${hold_desc} (${hold_reason}); the unblock judge decides again.

<!-- ai:unblock-wait:v1 item=${ITEM} hold=${hold_kind} ref=${hold_ref} expired -->" \
				|| unblock_log "item=${ITEM} op=hold hold=${hold_kind} ref=${hold_ref} outcome=skip reason=hold_refresh_failed"
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} op=hold hold=${hold_kind} ref=${hold_ref} outcome=expired reason=${hold_reason}"
			last_activity="${now}"
		fi
	fi

	# Deterministic exits before the judge: a latch the current guard would not
	# set (Q54), then a blocked fix-up, which never gets a fix-up of its own (Q56).
	if [ "${ITEM_KIND}" = "issue" ]; then
		if [ "${ITEM_STOP}" = "scope-blocked" ] && unblock_try_regrant; then
			return 0
		fi
		local fixup_origin
		fixup_origin="$(unblock_fixup_origin)"
		if [[ "${fixup_origin}" =~ ^[1-9][0-9]*$ ]]; then
			unblock_escalate_fixup "${fixup_origin}"
			return 0
		fi
	fi

	# Evidence and fingerprint.
	echo '[]' > "${RUNTIME_DIR}/failing_checks.json"
	if [ "${ITEM_KIND}" = "pr" ]; then
		if [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
			gh api "repos/${REPOSITORY}/commits/${head_sha}/check-runs?per_page=100" \
				--jq '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out") | .name]' \
				> "${RUNTIME_DIR}/failing_checks.json" 2>/dev/null || echo '[]' > "${RUNTIME_DIR}/failing_checks.json"
		fi
	fi
	unblock_evidence > "${RUNTIME_DIR}/evidence.json" 2>/dev/null || echo '{"reason": "unreadable"}' > "${RUNTIME_DIR}/evidence.json"
	fp="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" fingerprint --stop "${ITEM_STOP}" --evidence-file "${RUNTIME_DIR}/evidence.json" | jq -r '.fingerprint // empty')"
	if ! [[ "${fp}" =~ ^[0-9a-f]{12}$ ]]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=fingerprint_failed"
		return 0
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" rejection --item "${ITEM}" --stop "${ITEM_STOP}" \
		--comments-file "${RUNTIME_DIR}/item_comments.json" --trusted-login "${UNBLOCK_LOGIN}" > "${RUNTIME_DIR}/rejection.json"; then
		echo '{"status":"none","reason":"unreadable"}' > "${RUNTIME_DIR}/rejection.json"
	fi
	# Protected-path handoff (#7060), off by default. Reads only the comments
	# already fetched; it never changes the guard or override eligibility.
	# The automation-path guard always latches ai:scope-blocked, so only that
	# stop is held: an old rejection cannot hold a later, unrelated block.
	if unblock_protected_handoff_applies; then
		local protected_file="${RUNTIME_DIR}/protected_rejection.json"
		unblock_protected_rejection_load
		if jq -e '.status == "ok" and .handoff_pending == true' "${protected_file}" >/dev/null 2>&1; then
			# Refresh the hold comment, as the fix-up wait does, so the poller's
			# scan (UNBLOCK_JUDGE_RETRY_HOURS on the marker's updatedAt) does
			# not re-dispatch a judge run that would only skip again.
			local handoff_id handoff_run
			handoff_id="$(jq -r '.handoff_comment_id // empty' "${protected_file}")"
			handoff_run="$(jq -r '.run' "${protected_file}")"
			if [[ "${handoff_id}" =~ ^[0-9]+$ ]] && [[ "${handoff_run}" =~ ^[0-9]+$ ]]; then
				jq -r --argjson id "${handoff_id}" '[.[] | select(.id == $id) | (.body // "")] | first // ""' \
					"${RUNTIME_DIR}/item_comments.json" 2>/dev/null \
					| grep -v -e '^<!-- ai:unblock-wait:v1 ' -e '^Still waiting for an operator; last checked ' \
					> "${RUNTIME_DIR}/handoff_body.txt" || true
				# Trailing blank lines are dropped so repeated refreshes do not grow the body.
				unblock_patch_comment "${handoff_id}" "$(cat "${RUNTIME_DIR}/handoff_body.txt")
Still waiting for an operator; last checked ${now}.

<!-- ai:unblock-wait:v1 item=${ITEM} reason=operator_handoff run=${handoff_run} -->" \
					|| unblock_log "item=${ITEM} op=handoff_refresh outcome=failed"
			fi
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=operator_handoff_pending run=${handoff_run}"
			return 0
		fi
		if jq -e '.status == "ok"' "${protected_file}" >/dev/null 2>&1; then
			unblock_protected_context_merge || true
		fi
	fi

	local -a decide_args=(decide --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --comments-file "${RUNTIME_DIR}/item_comments.json"
		--trusted-login "${UNBLOCK_LOGIN}" --now "${now}" --kind "${ITEM_KIND}" --rejection-file "${RUNTIME_DIR}/rejection.json")
	[ -n "${last_activity}" ] && decide_args+=(--last-activity "${last_activity}")
	unblock_wait_enabled || decide_args+=(--wait-disabled)
	# An ai:security issue never gets accept_with_followup on its menu (#6541);
	# unblock_actions.py refuses it again if this flag is ever missing.
	if [ "${ITEM_KIND}" = "issue" ] && jq -e 'index("ai:security") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		decide_args+=(--security-issue)
	fi
	if [[ "${tracking}" =~ ^[0-9]+$ ]]; then
		if [ "${tracking}" = "${ITEM}" ]; then
			cp "${RUNTIME_DIR}/item_comments.json" "${RUNTIME_DIR}/project_comments.json"
		else
			if ! unblock_fetch_comments "${tracking}" "${RUNTIME_DIR}/project_comments.json"; then
				unblock_log "item=${ITEM} outcome=skip reason=project_comments_unavailable"
				return 0
			fi
		fi
		if ! jq -e --arg login "${UNBLOCK_LOGIN}" '[.[] | select((.user.login // "") == $login)]' \
			"${RUNTIME_DIR}/project_comments.json" > "${RUNTIME_DIR}/project_state_comments.json"; then
			unblock_log "item=${ITEM} outcome=skip reason=project_ledger_unreadable detail=state_filter"
			return 0
		fi
		if [ "${tracking}" != "${ITEM}" ]; then
			if [ "${ITEM_KIND}" = "issue" ] || [ "${ITEM_KIND}" = "pr" ]; then
				if ! unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${RUNTIME_DIR}/project_binding.json" 2>/dev/null \
					|| ! unblock_state_lists_issue "${binding_issue}" "${RUNTIME_DIR}/project_binding.json"; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified"
					return 0
				fi
			fi
			# Restore any item verdict whose project copy failed to post. Do not
			# decide from an incomplete project-wide round ledger this run.
			local missing_project_markers unblock_marker_entry
			if ! missing_project_markers="$(jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" \
				--slurpfile project "${RUNTIME_DIR}/project_comments.json" '
				[$project[0][]? | select((.user.login // "") == $login) | ((.body // "") | split("\n") | map(select(length > 0)) | last // "" | rtrimstr("\r"))] as $known
				| [.[] | select((.user.login // "") == $login)
				   | ((.body // "") | split("\n") | map(select(length > 0)) | last // "" | rtrimstr("\r"))
				   | select(startswith("<!-- ai:unblock:v1 item=" + $item + " ") and endswith(" -->"))
				   | select(. as $entry | ($known | index($entry)) == null)] | unique | .[]
			' "${RUNTIME_DIR}/item_comments.json")"; then
				unblock_log "item=${ITEM} outcome=skip reason=project_ledger_unreadable"
				return 0
			fi
			if [ -n "${missing_project_markers}" ]; then
				while IFS= read -r unblock_marker_entry; do
					if ! gh api "repos/${REPOSITORY}/issues/${tracking}/comments" -f body="Reconciled unblock judge verdict on #${ITEM}.

${unblock_marker_entry}" >/dev/null 2>&1; then
						unblock_log "item=${ITEM} outcome=skip reason=project_ledger_repair_failed"
						return 0
					fi
				done <<< "${missing_project_markers}"
				unblock_log "item=${ITEM} outcome=skip reason=project_ledger_repaired"
				return 0
			fi
		fi
		decide_args+=(--project-comments-file "${RUNTIME_DIR}/project_comments.json")
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${decide_args[@]}" > "${RUNTIME_DIR}/decision.json"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} outcome=skip reason=decide_failed"
		return 0
	fi
	terminal="$(jq -r '.terminal' "${RUNTIME_DIR}/decision.json")"

	if [ "${terminal}" = "true" ]; then
		jq -n --arg reason "$(jq -r '"Terminal: " + .terminal_reason + " (" + (.item_rounds | tostring) + " round(s) on this item)."' "${RUNTIME_DIR}/decision.json")" \
			'{verdict: "close", reason: ($reason + " The unblock judge has no verdict left that could clear this block.")}' > "${RUNTIME_DIR}/verdict_raw.json"
	else
		unblock_ask_model || return 0
	fi
	local -a validate_args=()
	local validate_ok="false" sandbox_reasked="false" validate_err sandbox_reason
	# At most two passes: a verdict whose instructions need network or remote
	# access (sandbox_incompatible:<id>) gets one re-ask; anything else, or a
	# second refusal, falls through to the invalid_verdict handling below.
	while :; do
		validate_args=(validate --verdict-file "${RUNTIME_DIR}/verdict_raw.json" --decision-file "${RUNTIME_DIR}/decision.json" --repo "${REPOSITORY}" --rejection-file "${RUNTIME_DIR}/rejection.json")
		if [ "${ITEM_STOP}" = "destructive-blocked" ] && [ "${ITEM_KIND}" = "issue" ] \
			&& jq -e '.verdict == "override_guard"' "${RUNTIME_DIR}/verdict_raw.json" >/dev/null 2>&1; then
			if unblock_rejection_snapshot; then
				validate_args+=(--rejected-deletions-file "${RUNTIME_DIR}/rejected_deletions.json")
			fi
		fi
		if [ "${UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED}" != "false" ]; then
			validate_args+=(--sandbox-check)
		fi
		if unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${validate_args[@]}" > "${RUNTIME_DIR}/verdict.json"; then
			validate_ok="true"
			break
		fi
		validate_err="$(jq -r '.error // ""' "${RUNTIME_DIR}/verdict.json" 2>/dev/null || true)"
		if [[ "${validate_err}" != sandbox_incompatible:* ]]; then
			# A re-asked answer that fails for another reason still closes the re-ask in the log.
			[ "${sandbox_reasked}" = "true" ] && unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=instruction_sandbox_check outcome=refused_after_reask reason=other"
			break
		fi
		sandbox_reason="$(printf '%s' "${validate_err#sandbox_incompatible:}" | tr -cd 'a-z_' | cut -c1-40)"
		if [ "${sandbox_reasked}" = "true" ] || [ "${terminal}" = "true" ]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=instruction_sandbox_check outcome=refused_after_reask reason=${sandbox_reason}"
			break
		fi
		sandbox_reasked="true"
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=instruction_sandbox_check outcome=reasked reason=${sandbox_reason}"
		UNBLOCK_JUDGE_REASK_NOTE="Your previous verdict was refused: its instructions need network or remote access (${sandbox_reason}). The implementer has none. Rewrite them using only the Implementer environment above, and do not name a git fetch/pull/push/clone, an origin/ ref, a package install, curl/wget or gh at all, not even to forbid it."
		if ! unblock_ask_model; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=instruction_sandbox_check outcome=refused_after_reask reason=ask_failed"
			return 0
		fi
	done
	if [ "${validate_ok}" != "true" ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} outcome=skip reason=invalid_verdict detail=$(jq -r '.error // ""' "${RUNTIME_DIR}/verdict.json" | tr ' ' '_' | cut -c1-120)"
		# A refused answer is not a round, so no other terminal condition can
		# ever hold for it. Once INVALID_VERDICT_PARK_AFTER refusals in a row
		# (this one included) hit the same block, take the ledger's terminal
		# close, which parks the item for a person with one CRITICAL page
		# (tele-funtoken-msg-scoring#4770 was retried silently every 6 hours).
		local invalid_park="false" invalid_text invalid_marker
		# A model or isolation failure (no answer at all) is not a refused
		# answer: post the older, uncounted marker so an outage cannot park items.
		invalid_marker="<!-- ai:unblock-wait:v1 item=${ITEM} reason=invalid_verdict stop=${ITEM_STOP} fingerprint=${fp} -->"
		if [ "${UNBLOCK_JUDGE_MODEL_NO_ANSWER}" = "true" ]; then
			invalid_marker="<!-- ai:unblock-wait:v1 item=${ITEM} reason=invalid_verdict -->"
		elif [ "${terminal}" != "true" ] \
			&& jq -e '((.invalid_verdict_streak // 0) + 1) >= (.invalid_verdict_park_after // 2)' "${RUNTIME_DIR}/decision.json" >/dev/null 2>&1; then
			invalid_park="true"
		fi
		invalid_text="The unblock judge could not reach a valid verdict this time and will try again later."
		[ "${invalid_park}" = "true" ] && invalid_text="The unblock judge could not reach a valid verdict on this block again."
		# One attempt only: the streak counts each posted marker, so retrying a
		# create whose response was lost could post two and park after one refusal.
		# A failed write is logged and paged instead.
		local invalid_record_rc=0
		gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="${invalid_text}

${invalid_marker}" >/dev/null 2>&1 || invalid_record_rc=$?
		if [ "${invalid_record_rc}" -ne 0 ]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=invalid_verdict_record outcome=failed reason=record_failed rc=${invalid_record_rc}"
			[ "${invalid_park}" != "true" ] && [ "${UNBLOCK_JUDGE_MODEL_NO_ANSWER}" != "true" ] && unblock_tg WARNING "Unblock judge could not record a refused answer on #${ITEM} (${ITEM_STOP}); refused answers are not counted until this write succeeds, so the item cannot be parked for a person yet."
		fi
		if [ "${invalid_park}" != "true" ]; then
			return 0
		fi
		if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${decide_args[@]}" --pending-invalid-verdicts 1 > "${RUNTIME_DIR}/decision.json" \
			|| ! jq -e '.terminal == true and .terminal_reason == "invalid_verdicts"' "${RUNTIME_DIR}/decision.json" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=invalid_verdict_park outcome=skip reason=decide_failed"
			[ "${invalid_record_rc}" -ne 0 ] && unblock_tg WARNING "Unblock judge could not record a refused answer on #${ITEM} (${ITEM_STOP}) and could not park it for a person (decide_failed); it will be retried, but refused answers are not counted until the write succeeds."
			return 0
		fi
		terminal="true"
		jq -n --arg reason "$(jq -r '"Terminal: invalid_verdicts (" + (.invalid_verdict_streak | tostring) + " refused answers in a row on this block, " + (.item_rounds | tostring) + " round(s) on this item)."' "${RUNTIME_DIR}/decision.json")" \
			'{verdict: "close", reason: ($reason + " The unblock judge could not produce a usable verdict, so a person must decide.")}' > "${RUNTIME_DIR}/verdict_raw.json"
		if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" validate --verdict-file "${RUNTIME_DIR}/verdict_raw.json" \
			--decision-file "${RUNTIME_DIR}/decision.json" --repo "${REPOSITORY}" --rejection-file "${RUNTIME_DIR}/rejection.json" > "${RUNTIME_DIR}/verdict.json"; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=invalid_verdict_park outcome=skip reason=validate_failed"
			[ "${invalid_record_rc}" -ne 0 ] && unblock_tg WARNING "Unblock judge could not record a refused answer on #${ITEM} (${ITEM_STOP}) and could not park it for a person (validate_failed); it will be retried, but refused answers are not counted until the write succeeds."
			return 0
		fi
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} op=invalid_verdict_park outcome=terminal streak=$(jq -r '.invalid_verdict_streak' "${RUNTIME_DIR}/decision.json")"
	fi
	verdict_name="$(jq -r '.verdict' "${RUNTIME_DIR}/verdict.json")"
	if [ "${untrusted_project_pr}" = "true" ] && [[ ! "${verdict_name}" =~ ^(reissue|descope|operator_step|accept_with_followup|close)$ ]]; then
		unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=untrusted_pr_verdict"
		return 0
	fi
	if [ "${verdict_name}" = "wait" ]; then
		# A hold, not a round: no ai:unblock:v1 marker and no project copy, so
		# it never counts toward the caps. The item keeps its block.
		local hold_line
		hold_kind="$(jq -r '.hold_kind // ""' "${RUNTIME_DIR}/verdict.json")"
		hold_ref="$(jq -r '.hold_ref // ""' "${RUNTIME_DIR}/verdict.json")"
		hold_line="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" hold-marker --item "${ITEM}" --kind "${hold_kind}" --ref "${hold_ref}" | jq -r '.marker // empty')"
		if [ -z "${hold_line}" ]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=wait outcome=skip reason=marker_failed"
			return 0
		fi
		hold_desc="$(unblock_hold_description "${hold_kind}" "${hold_ref}")"
		comment_body="$(jq -r --arg desc "${hold_desc}" --arg days "$(( $(unblock_wait_max_hours) / 24 ))" --arg marker "${hold_line}" '
			"## Unblock judge: `wait`\n\nReason: " + .reason + "\n\n"
			+ "Waiting for " + $desc + ". This item keeps its block and the hold uses no round: the unblock judge checks again on its next runs, posts the resume command once the condition holds, and decides again after " + $days + " days.\n\n"
			+ $marker
		' "${RUNTIME_DIR}/verdict.json")"
		if ! gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="${comment_body}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=wait outcome=skip reason=record_failed"
			return 0
		fi
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=wait op=hold hold=${hold_kind} ref=${hold_ref} outcome=held"
		return 0
	fi
	round="$(jq -r '.round' "${RUNTIME_DIR}/verdict.json")"
	local -a marker_args=(marker --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --verdict "${verdict_name}" --round "${round}")
	[ "$(jq -r '.override // ""' "${RUNTIME_DIR}/verdict.json")" = "bulk_delete" ] && marker_args+=(--override bulk_delete)
	marker_line="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${marker_args[@]}" | jq -r '.marker // empty')"
	if [ -z "${marker_line}" ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=marker_failed"
		return 0
	fi
	if [ "${verdict_name}" = "close" ]; then
		# Issue #6557: the decision and the model run can be minutes old, so a
		# close is recorded and acted on only against a fresh, verified block
		# state: the item must still be open and still carry the same stop.
		# Comment text never decides this. Any read failure fails closed.
		local recheck_stop_json recheck_stop
		if ! gh api "repos/${REPOSITORY}/issues/${ITEM}" > "${RUNTIME_DIR}/item_recheck.json" 2>/dev/null \
			|| ! jq -e '.number' "${RUNTIME_DIR}/item_recheck.json" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=block_state_recheck_unavailable"
			return 0
		fi
		if [ "$(jq -r '.state' "${RUNTIME_DIR}/item_recheck.json")" != "open" ]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=block_state_changed detail=closed"
			return 0
		fi
		if [ "${project_failed_substituted}" != "true" ]; then
			# A failed project without a block label is re-checked against its
			# trusted state just below instead.
			if ! recheck_stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop \
				--labels-json "$(jq -c '[.labels[]?.name]' "${RUNTIME_DIR}/item_recheck.json")" 2>/dev/null)"; then
				# Only the ledger's own "no block label" error means the item was
				# unblocked; any other failure (crash, bad input) is a failed recheck.
				if [ "$(jq -r '.error // ""' <<< "${recheck_stop_json}" 2>/dev/null || true)" = "no block label on this item" ]; then
					unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=block_state_changed detail=unblocked"
				else
					unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=block_state_recheck_unavailable"
				fi
				return 0
			fi
			recheck_stop="$(jq -r '.stop // ""' <<< "${recheck_stop_json}" 2>/dev/null || true)"
			if [ "${recheck_stop}" != "${ITEM_STOP}" ]; then
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=block_state_changed detail=stop_changed"
				return 0
			fi
		fi
	fi
	if [ "${project_failed_substituted}" = "true" ]; then
		# The model can take minutes; never record or act on a stale verdict.
		if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments_recheck.json"; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_recheck_unavailable"
			return 0
		fi
		if ! project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments_recheck.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_unverified stage=recheck"
			return 0
		fi
		if [ "${project_status}" != "failed" ]; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_resumed status=$(printf '%s' "${project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
			return 0
		fi
	fi
	comment_body="$(jq -r --arg marker "${marker_line}" '
		"## Unblock judge: `" + .verdict + "` (round " + (.round | tostring) + ")\n\nReason: " + .reason + "\n\n"
		+ (if (.instructions // "") != "" then "Instructions: " + .instructions + "\n\n" else "" end)
		+ (if (.answer // "") != "" then "Answer: " + .answer + "\n\n" else "" end)
		+ (if (.paths // []) | length > 0 then "Paths: " + ((.paths // []) | map("`" + . + "`") | join(", ")) + "\n\n" else "" end)
		+ (if (.override // "") == "bulk_delete" then "Approved deletions: " + (.paths | tojson) + "\n\n" else "" end)
		+ (if (.override // "") == "bulk_delete" then "Rejected run: " + (.rejected_run | tostring) + "\n\n" else "" end)
		+ (if (.rejection_run // "") != "" then "Bound to guard rejection from run " + .rejection_run + ".\n\n" else "" end)
		+ (if (.placeholder // "") == "" then ""
			elif (.placeholder | endswith("_UNSET_OPERATOR_STEP")) then "Stays off behind `" + .placeholder + "` until the operator step is done.\n\n"
			else "Ships on by default; `" + .placeholder + "=false` turns it off.\n\n" end)
		+ $marker
	' "${RUNTIME_DIR}/verdict.json")"
	# The ledger record goes first, so a failed operation below can never
	# make the same verdict available again.
	if ! UNBLOCK_VERDICT_URL="$(gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="${comment_body}" --jq '.html_url // ""' 2>/dev/null)"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=record_failed"
		return 0
	fi
	ops_file="${RUNTIME_DIR}/ops.json"
	UNBLOCK_CI_OWNER_STATE=""
	if [ "${verdict_name}" = "reissue" ] && [ "${ITEM_KIND}" = "issue" ] && [ -z "${tracking}" ] \
		&& ! jq -e 'index("ai:security") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		unblock_resolve_ci_owner
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" plan --verdict-file "${RUNTIME_DIR}/verdict.json" \
		--context-file "${RUNTIME_DIR}/context.json" > "${ops_file}"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=plan_failed"
		return 0
	fi
	unblock_log_target_repo_guard "${ops_file}" "${verdict_name}"
	local actuation_failed="false"
	if ! unblock_run_ops "${ops_file}" "${project_failed_substituted}"; then
		actuation_failed="true"
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=actuation_failed"
	fi
	if [[ "${tracking}" =~ ^[0-9]+$ ]] && [ "${tracking}" != "${ITEM}" ]; then
		if ! gh api "repos/${REPOSITORY}/issues/${tracking}/comments" -f body="Unblock judge verdict on #${ITEM}: \`${verdict_name}\`.

${marker_line}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} op=project_record outcome=failed"
			return 0
		fi
	fi
	if [ "${actuation_failed}" = "true" ]; then
		return 0
	fi
	if [ "${UNBLOCK_CI_OWNER_STATE}" = "superseded" ] && jq -e '[.ops[].op] == ["comment", "close"]' "${ops_file}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} op=reissue outcome=superseded pr=${UNBLOCK_CI_OWNER_PR} head=${UNBLOCK_CI_OWNER_HEAD}"
	fi
	unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=acted"
}

# Attach failed logs only when a pipeline-authored citation belongs to this
# item. Missing or unverifiable metadata leaves the prompt's log section empty.
unblock_select_run_log()
{
	local candidate_id run_json checked=0 unreadable="false" reason="run_unbound"
	local -a candidates=()
	: > "${RUNTIME_DIR}/run_log_tail.txt"
	mapfile -t candidates < <(jq -r --arg login "${UNBLOCK_LOGIN}" '
		[.[] | select((.user.login // "") == $login and (((.body // "") | test("ai:unblock|ORCHESTRATOR_STATE")) | not))
		 | (.body // "") | scan("/actions/runs/([0-9]+)") | .[0]]
		| reverse | reduce .[] as $id ([]; if index($id) == null then . + [$id] else . end)
		| .[:3][]
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null)
	if [ "${#candidates[@]}" -eq 0 ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=omitted reason=no_trusted_run candidates=0"
		return 0
	fi
	for candidate_id in "${candidates[@]}"; do
		[[ "${candidate_id}" =~ ^[0-9]+$ ]] || continue
		checked=$((checked + 1))
		# The existing item, comment, PR and check-runs reads contain no run
		# repository/branch metadata; this bounded read is needed to bind the ID.
		if ! run_json="$(gh api "repos/${REPOSITORY}/actions/runs/${candidate_id}" 2>/dev/null)"; then
			unreadable="true"
			continue
		fi
		if ! jq -e 'type == "object"' <<< "${run_json}" >/dev/null 2>&1; then
			unreadable="true"
			continue
		fi
		if ! jq -e --arg id "${candidate_id}" --arg repo "${REPOSITORY}" --arg kind "${ITEM_KIND}" \
				--arg item "${ITEM}" \
				--arg sha "${head_sha:-}" --arg ref "${head_ref:-}" '
				(.id == ($id | tonumber) and .repository.full_name == $repo and .head_repository.full_name == $repo)
				and (if $kind == "pr" then
					((.display_title // "") | endswith("[pr:" + $item + "]"))
					or (($sha | test("^[0-9a-f]{40}$")) and .head_sha == $sha)
					or ($ref != "" and .head_branch == $ref)
					or any(.pull_requests[]?; .number == ($item | tonumber))
				elif $kind == "issue" then
					.head_branch == ("ai/issue-" + $item)
				else
					.head_branch == ("orchestrator/project-" + $item)
					or ((.display_title // "") | endswith("[tracking:" + $item + "]"))
				end)
			' <<< "${run_json}" >/dev/null 2>&1; then
			continue
		fi
		gh run view "${candidate_id}" -R "${REPOSITORY}" --log-failed 2>/dev/null | tail -n 400 | tail -c 120000 > "${RUNTIME_DIR}/run_log_tail.txt" || true
		unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=attached run=${candidate_id}"
		return 0
	done
	[ "${unreadable}" = "false" ] || reason="run_unreadable"
	unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=omitted reason=${reason} candidates=${checked}"
}

# Builds the evidence prompt, runs the UNBLOCK_JUDGE role and writes the raw
# verdict JSON to verdict_raw.json. Returns 1 (after logging) when there is
# no usable answer.
unblock_ask_model()
{
	local prompt_file="${RUNTIME_DIR}/prompt.txt" output_file="${RUNTIME_DIR}/model_output.txt" model reasoning engine rc iso_rc parse_rc
	# A sandbox re-ask reuses the first ask's run log and PR diff: no new API reads.
	if [ -z "${UNBLOCK_JUDGE_REASK_NOTE}" ]; then
		unblock_select_run_log
		: > "${RUNTIME_DIR}/pr_diff.txt"
		if [ "${ITEM_KIND}" = "pr" ]; then
			gh api "repos/${REPOSITORY}/pulls/${ITEM}" -H "Accept: application/vnd.github.diff" 2>/dev/null | head -c 120000 > "${RUNTIME_DIR}/pr_diff.txt" || true
		fi
	fi
	echo '{}' > "${RUNTIME_DIR}/state_slice.json"
	local spec=""
	if [ -n "${tracking:-}" ] && [ -s "${RUNTIME_DIR}/project_state_comments.json" ]; then
		if unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${RUNTIME_DIR}/state_full.json" 2>/dev/null; then
			jq '{status, current_wave, integration_branch, judge_cycle, recovery_count, validation_last_raw_status, validation_failure_reason, security_pass_status, final_merge_status, waves: [(.waves // [])[] | {issues: [(.issues // [])[] | {id, github_issue, status}]}]}' \
				"${RUNTIME_DIR}/state_full.json" > "${RUNTIME_DIR}/state_slice.json" 2>/dev/null || echo '{}' > "${RUNTIME_DIR}/state_slice.json"
		fi
		if [ "${ITEM_KIND}" = "project" ]; then
			spec="$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json" | head -c 8000)"
		fi
	fi
	jq -n \
		--slurpfile context "${RUNTIME_DIR}/context.json" \
		--slurpfile decision "${RUNTIME_DIR}/decision.json" \
		--slurpfile evidence "${RUNTIME_DIR}/evidence.json" \
		--slurpfile rejection "${RUNTIME_DIR}/rejection.json" \
		--slurpfile comments "${RUNTIME_DIR}/item_comments.json" \
		--slurpfile state "${RUNTIME_DIR}/state_slice.json" \
		--arg login "${UNBLOCK_LOGIN}" \
		--arg item_body "$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json" | head -c 6000)" \
		--arg spec "${spec}" \
		'{
			block: {item: $context[0].item, kind: $context[0].kind, stop: $context[0].stop, labels: $context[0].labels, tracking_issue: $context[0].tracking, title: $context[0].title},
			fingerprint: $decision[0].fingerprint,
			evidence: $evidence[0],
			allowed: $decision[0].allowed,
			used_for_this_fingerprint: $decision[0].used,
			guard_rejection: (if $rejection[0].status == "ok" then ($rejection[0] | {guard, paths, run}) else null end),
			rounds: {item: $decision[0].item_rounds, project: $decision[0].project_rounds},
			prior_unblock_verdicts: [$comments[0][] | select((.user.login // "") == $login and ((.body // "") | test("<!-- ai:unblock:v1 "))) | {created_at, body: ((.body // "")[0:1500])}] | .[-5:],
			last_comments: [$comments[0][-20:][] | {author: (.user.login // ""), created_at, body: ((.body // "")[0:2000])}],
			item_body_untrusted: $item_body,
			project_state: $state[0],
			project_spec_untrusted: $spec
		}' > "${RUNTIME_DIR}/judge_context.json" 2>/dev/null || {
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=context_failed"
		return 1
	}
	{
		bash "${SUPPORT_DIR}/scripts/render_prompt.sh" "${SUPPORT_DIR}/prompts/mode-judge-unblock.txt" 2>/dev/null \
			|| cat "${SUPPORT_DIR}/prompts/mode-judge-unblock.txt"
		echo
		if [ -n "${UNBLOCK_JUDGE_REASK_NOTE}" ]; then
			echo "=== PREVIOUS VERDICT REFUSED ==="
			printf '%s\n' "${UNBLOCK_JUDGE_REASK_NOTE}"
			echo
		fi
		echo "=== UNBLOCK CONTEXT JSON (untrusted text inside) ==="
		cat "${RUNTIME_DIR}/judge_context.json"
		echo
		echo "=== FAILING RUN LOG TAIL (untrusted) ==="
		cat "${RUNTIME_DIR}/run_log_tail.txt"
		echo
		echo "=== PULL REQUEST DIFF (untrusted, at most 120 KB) ==="
		cat "${RUNTIME_DIR}/pr_diff.txt"
	} > "${prompt_file}"
	: > "${output_file}"
	# Set when the model gave no answer (bad config, isolation failure); never
	# read from the environment.
	UNBLOCK_JUDGE_MODEL_NO_ANSWER="false"
	if [ -n "${UNBLOCK_JUDGE_REASK_NOTE}" ] && [ -n "${MOCK_UNBLOCK_JUDGE_JSON_REASK:-}" ]; then
		printf '%s\n' "${MOCK_UNBLOCK_JUDGE_JSON_REASK}" > "${output_file}"
	elif [ -n "${MOCK_UNBLOCK_JUDGE_JSON:-}" ]; then
		printf '%s\n' "${MOCK_UNBLOCK_JUDGE_JSON}" > "${output_file}"
	else
		model="${UNBLOCK_JUDGE_MODEL:-${WORKFLOW_EDITOR_MODEL:-openai/gpt-6-sol}}"
		reasoning="${UNBLOCK_JUDGE_REASONING:-high}"
		if [[ ! "${model}" =~ ^[a-zA-Z0-9/_.-]+$ ]] || [[ ! "${reasoning}" =~ ^(xhigh|high|medium|low|none)$ ]]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=invalid_model_config"
			UNBLOCK_JUDGE_MODEL_NO_ANSWER="true"
			echo '{}' > "${RUNTIME_DIR}/verdict_raw.json"
			return 0
		fi
		UNBLOCK_JUDGE_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}"
		if [[ ! "${UNBLOCK_JUDGE_TIMEOUT_SECS}" =~ ^[1-9][0-9]*$ ]]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=timeout_fallback reason=invalid_timeout default=1500"
			UNBLOCK_JUDGE_TIMEOUT_SECS="1500"
		fi
		engine="codex"
		if [ -f "${SUPPORT_DIR}/scripts/ai_engine.sh" ]; then
			# shellcheck source=/dev/null
			source "${SUPPORT_DIR}/scripts/ai_engine.sh"
			engine="$(AI_ENGINE_LABELS="$(cat "${RUNTIME_DIR}/labels.json")" AI_ENGINE_MODEL_HINT="${model}" AI_ENGINE_EFFORT_HINT="${reasoning}" \
				ai_engine_for_role UNBLOCK_JUDGE 2>/dev/null || echo codex)"
		fi
		rc=75
		if [ "${engine}" = "claude" ]; then
			rc=0
			# The OAuth credential stays in the host relay; the model runs only in the container.
			(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u OPENROUTER_API_KEY \
				MODEL_EDITOR="${model}" MODEL_REASONING_EFFORT="${reasoning}" \
				CLARIFY_ISOLATION_SUPPORT_DIR="${SUPPORT_DIR}/scripts" \
				CLARIFY_ISOLATION_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}" \
				bash "${SUPPORT_DIR}/scripts/clarify_isolated_run.sh" "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/claude.err" claude UNBLOCK_JUDGE) || rc=$?
			if [ "${rc}" -eq 80 ]; then
				# Claude capacity wait (issue #7370): no verdict, no
				# invalid_verdict marker; the unblock scan retries later.
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=claude_capacity_wait"
				return 1
			fi
			if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 75 ]; then
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=isolation_failed rc=${rc}"
				UNBLOCK_JUDGE_MODEL_NO_ANSWER="true"
				: > "${output_file}"
			fi
		fi
		if [ "${rc}" -eq 75 ]; then
			iso_rc=0
			(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET \
				MODEL_EDITOR="${model}" MODEL_REASONING_EFFORT="${reasoning}" \
				CLARIFY_ISOLATION_SUPPORT_DIR="${SUPPORT_DIR}/scripts" \
				CLARIFY_ISOLATION_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}" \
				bash "${SUPPORT_DIR}/scripts/clarify_isolated_run.sh" "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/codex.err" codex UNBLOCK_JUDGE) || iso_rc=$?
			if [ "${iso_rc}" -ne 0 ]; then
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=isolation_failed rc=${iso_rc}"
				UNBLOCK_JUDGE_MODEL_NO_ANSWER="true"
				: > "${output_file}"
			fi
		fi
	fi
	parse_rc=0
	unblock_py - "${output_file}" > "${RUNTIME_DIR}/verdict_raw.json" <<'PY' || parse_rc=$?
import base64, json, os, re, sys, urllib.parse
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
separators = re.compile(r'''[\s\-_.:/,"']''')
def strings(value):
	if isinstance(value, str):
		yield value
	elif isinstance(value, list):
		for item in value:
			yield from strings(item)
	elif isinstance(value, dict):
		for key, item in value.items():
			yield from strings(key)
			yield from strings(item)
def contains_secret(value):
	text = "".join(strings(value))
	compact = separators.sub("", text).lower()
	if re.search(r"sk-or-v1-[0-9a-fA-F]{32,}|sk-or-[A-Za-z0-9_-]{20,}", text):
		return True
	if len(api_key) < 16:
		return False
	key_bytes = api_key.encode("utf-8")
	literal_needles = {api_key, api_key[::-1], urllib.parse.quote(api_key, safe="")}
	compact_needles = {separators.sub("", api_key).lower(), key_bytes.hex(), base64.b32encode(key_bytes).decode("ascii").rstrip("=").lower()}
	for shift in range(3):
		shifted = b"\0" * shift + key_bytes
		for encode in (base64.b64encode, base64.urlsafe_b64encode):
			encoded = encode(shifted).decode("ascii")
			if shift == 0:
				literal_needles.add(encoded)
				literal_needles.add(encoded.rstrip("="))
			else:
				# Ignore the prefix group and incomplete tail; the middle depends
				# entirely on credential bytes, regardless of adjacent data.
				middle = encoded[4:(len(encoded.rstrip("=")) // 4) * 4]
				if len(middle) >= 16:
					literal_needles.add(middle)
	return any(needle and needle in text for needle in literal_needles) or any(
		len(needle) >= 16 and needle in compact for needle in compact_needles
	)
def redact_verdict_value(value):
	if isinstance(value, str):
		return value.replace(api_key, "[redacted]") if api_key else value
	if isinstance(value, list):
		return [redact_verdict_value(item) for item in value]
	if isinstance(value, dict):
		return {key: redact_verdict_value(item) for key, item in value.items()}
	return value
for start in [m.start() for m in re.finditer(r"\{", raw)]:
	try:
		value = json.JSONDecoder().raw_decode(raw[start:])[0]
	except ValueError:
		continue
	if isinstance(value, dict) and "verdict" in value:
		if contains_secret(value):
			sys.exit(3)
		print(json.dumps(redact_verdict_value(value)))
		sys.exit(0)
sys.exit(1)
PY
	if [ "${parse_rc}" -ne 0 ]; then
		[ "${parse_rc}" -ne 3 ] || unblock_log "item=${ITEM} outcome=skip reason=verdict_secret_rejected"
		# An empty reply is no answer (an outage, not a refusal): post the
		# uncounted marker so it cannot park the item. Non-empty output with no
		# verdict is a refused answer and still counts.
		if [ "${parse_rc}" -ne 3 ] && [ "${UNBLOCK_JUDGE_MODEL_NO_ANSWER}" != "true" ] && ! grep -q '[^[:space:]]' "${output_file}" 2>/dev/null; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=empty_output"
			UNBLOCK_JUDGE_MODEL_NO_ANSWER="true"
		fi
		echo '{}' > "${RUNTIME_DIR}/verdict_raw.json"
	fi
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	unblock_main "$@"
	exit 0
fi
