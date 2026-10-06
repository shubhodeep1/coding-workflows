#!/usr/bin/env bash
# review_merge_train.sh — serialize ai/issue-* PRs that edit the same files.
#
# Problem this solves: when several ai/issue-* PRs open against one base
# within minutes and edit the same files (an auto-heal burst opened 14
# issues for one incident in tele-funtoken-msg-scoring on 2026-09-07; the
# binance-blessings project-249 wave ran three phases into twap_router.py),
# every merge of one sibling makes every remaining sibling conflict. Each of
# those then spends a reviewer pass, an editor pass and a Codex resolver
# round, and the cycle repeats on the next merge: O(n^2) resolver runs and
# one "Merge conflicts resolved automatically" ping per run.
#
# Policy (Q2 = "lowest PR number wins"): a PR whose changed files overlap an
# OLDER open same-repository ai/issue-* PR on the same base is queued — labelled
# ai:merge-queued and its review run soft-exits — until every older
# overlapping PR is merged or closed. Fork heads are never blockers.
# Issue #6570 (plan P1, docs/plans/remove-unattended-pipeline-stuck-states-plan.md)
# narrows "overlap" with three default-on rules, applied in this order to each
# path-overlapping older PR by _mt_blockers_for_into (so gate and release
# always decide the same way):
#   1. Priority lane (MERGE_TRAIN_PRIORITY_LABELS): a PR whose verified linked
#      issue carries a priority label never waits behind a verified
#      non-priority PR. Two priority PRs keep lowest-number-first.
#   2. Bounded head (MERGE_TRAIN_HEAD_MAX_AGE_HOURS): an older PR that is not
#      itself queued and has been under review longer than the cap stops
#      blocking. Incomplete history or an unverified PR keeps the blocker.
#   3. Real conflicts (MERGE_TRAIN_CONFLICT_CHECK_ENABLED): an older PR whose
#      head `git merge-tree --write-tree` merges cleanly with this PR's head
#      stops blocking. Any git error keeps the blocker (conflict=unknown).
# Every rule can only remove a blocker; any failure keeps today's behaviour.
# The queue
# is released by the `release` subcommand, run from cancel_on_pr_close.yml whenever a PR closes
# (event path, immediate) and from orchestrate_poll.yml on every tick
# (backstop; runs even when the repo has no active orchestrator project).
#
# Usage:
#   review_merge_train.sh gate      # inside review_autofix.yml, before reviewers
#   review_merge_train.sh release   # after a PR closes / on every poll tick
#
# Env (both):
#   GH_TOKEN, GITHUB_REPOSITORY            required
#   MERGE_TRAIN_ENABLED                    default true; any other value = no-op
#   MERGE_TRAIN_MAX_OLDER_PRS              default 20; older PRs examined per PR
#   MERGE_TRAIN_LABEL                      default ai:merge-queued
#   MERGE_TRAIN_HEAD_REF_PREFIX            default ai/issue-
#   MERGE_TRAIN_ALLOW_WORKFLOW_EDITS       default true; forwarded on dispatch
#   MERGE_TRAIN_CONFLICT_CHECK_ENABLED     default true (true/1/yes/on); other = off
#   MERGE_TRAIN_HEAD_MAX_AGE_HOURS         default 24; 0 disables the age bypass;
#                                          a non-integer warns and falls back to 24
#   MERGE_TRAIN_PRIORITY_LABELS            default ai:workflow-heal,ai:security
#                                          (comma-separated); empty, none or off
#                                          disables the priority lane
# Env (gate):
#   PR_NUMBER, BASE_BRANCH, TARGET_BRANCH  required (TARGET_BRANCH = PR head ref)
#   IS_SMOKE_TEST                         only literal true bypasses overlap checks
#   PR_DIFF_FILE                           optional; parsed for this PR's paths
#   GITHUB_ENV                             receives AUTOFIX_MERGE_QUEUED=true and
#                                          AUTOFIX_STALE_BASE_SKIP=true when queued
# Env (release):
#   BASE_BRANCH                            optional; restrict to PRs on this base
#   MERGE_TRAIN_DISPATCH_WORKFLOWS         default "ai-review.yml internal-review.yml review_autofix.yml"
#
# API budget (CLAUDE.md §15), all REST via `gh api`:
#   gate:    1 list call (open PRs on BASE_BRANCH, oldest first) +
#            ceil(files/100) `pulls/N/files` calls per older ai/issue-* PR,
#            at most MERGE_TRAIN_MAX_OLDER_PRS PRs; this PR's own paths come
#            from PR_DIFF_FILE (already fetched by review_collect_pr_metadata.sh)
#            and fall back to one `pulls/PR/files` call when the diff is empty,
#            unparseable, or uses Git C-quoted paths; +1 paginated comments-list
#            call whenever blockers exist, plus conditional label/comment writes
#            when queue state changes. When at least one older PR overlaps
#            and the priority lane is on (or an overlapping blocker is older
#            than MERGE_TRAIN_HEAD_MAX_AGE_HOURS), +1 aliased GraphQL call
#            (_mt_pr_meta_prefetch) covers this PR and every overlapping
#            blocker. The conflict check uses git only (a `git fetch` by SHA of
#            missing heads plus `git merge-tree`), no API calls.
#            Gate-side release adds one comments-list
#            call, up to one marker PATCH, and one label DELETE. A marker lookup
#            adds one cached /user read; a bypass candidate adds a paginated
#            issues/N/events read and one collaborator-permission read.
#   release: 1 list call (open PRs, all bases, 100 per page) + the active-run
#            listing that prevents dispatch beside an active review (one
#            `actions/runs?status=<s>` call per 100 runs for each of
#            requested, pending, queued, waiting, in_progress, then all five
#            again, follow-ups bounded by created_at,
#            at most 10 each — normally 10 calls — read once,
#            only when a queued PR passes the base filter; see
#            _mt_inflight_review_branches) + files calls
#            as above, cached per PR for the run; at most 1 GraphQL call per
#            evaluated queued PR with overlapping blockers, covering only PR
#            numbers not already cached this run; one /user read on the first
#            marker lookup; each unblocked queued PR adds
#            1 comments-list call and up to 1 marker PATCH before its label-
#            removal claim. A release adds 1 workflow dispatch and a best-effort
#            released-comment upsert. A failed dispatch tries to restore the
#            label (best-effort, +1 call) so the next release
#            invocation retries it; if that restore fails too, a ::warning:: is
#            logged and the PR stays unlabelled until its next review-triggering
#            event (push, re-run, or orchestrator stall recovery).
#
# Fail-open contract: general lookup failures, missing input or unexpected shape log
# a ::warning:: and exit 0 WITHOUT queuing (gate) or WITHOUT releasing
# (release). An incomplete active-run listing counts as such a failure:
# release then leaves every queued PR queued (MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE)
# rather than dispatch beside a review it could not see. The train only ever
# delays a review run; it never blocks a
# merge, and a PR that is wrongly left queued is picked up by the next
# release tick once its blockers are gone. After blockers are known, an
# unverifiable or unconsumed *bypass* keeps the PR queued.
set -euo pipefail

_mt_log() { printf '%s\n' "$*"; }
_mt_warn() { printf '::warning::%s\n' "$*"; }

if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
elif [ -f "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

MT_SUBCOMMAND="${1:-}"
MT_ENABLED="${MERGE_TRAIN_ENABLED:-true}"
MT_LABEL="${MERGE_TRAIN_LABEL:-ai:merge-queued}"
MT_PREFIX="${MERGE_TRAIN_HEAD_REF_PREFIX:-ai/issue-}"
MT_MAX_OLDER="${MERGE_TRAIN_MAX_OLDER_PRS:-20}"
MT_REPO="${GITHUB_REPOSITORY:-}"
MT_MARKER="<!-- merge-train:queued -->"
MT_RELEASED_MARKER="<!-- merge-train:released -->"
MT_BYPASSED_MARKER="<!-- merge-train:bypassed -->"
MT_RETIRED_MARKER="<!-- merge-train:queue-retired -->"
MT_AUTOMATION_LOGIN=""
[[ "${MT_MAX_OLDER}" =~ ^[0-9]+$ ]] || MT_MAX_OLDER=20

case "$(printf '%s' "${MT_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
	true|1|yes|on) ;;
	*)
		_mt_log "MERGE_TRAIN_DISABLED subcommand=${MT_SUBCOMMAND:-none} MERGE_TRAIN_ENABLED=${MT_ENABLED}"
		exit 0
		;;
esac

if [ -z "${MT_REPO}" ]; then
	_mt_warn "review_merge_train.sh: GITHUB_REPOSITORY unset; fail-open (no-op)."
	exit 0
fi

# Issue #6570 settings. Every one defaults inside the script (§8) so a caller
# that does not export them gets the default-on behaviour.
case "$(printf '%s' "${MERGE_TRAIN_CONFLICT_CHECK_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	true|1|yes|on) MT_CONFLICT_CHECK="true" ;;
	*) MT_CONFLICT_CHECK="false" ;;
esac
MT_HEAD_MAX_AGE_HOURS="${MERGE_TRAIN_HEAD_MAX_AGE_HOURS:-24}"
if ! [[ "${MT_HEAD_MAX_AGE_HOURS}" =~ ^[0-9]{1,6}$ ]]; then
	_mt_warn "review_merge_train.sh: MERGE_TRAIN_HEAD_MAX_AGE_HOURS='${MT_HEAD_MAX_AGE_HOURS}' is not a whole number of hours; using 24."
	MT_HEAD_MAX_AGE_HOURS=24
fi
MT_HEAD_MAX_AGE_HOURS=$((10#${MT_HEAD_MAX_AGE_HOURS}))
# Unset means the default; an explicitly empty value disables the lane. A repo
# variable cannot pass an empty string through `vars.X || 'default'`, so
# `none` / `off` also disable it.
MT_PRIORITY_LABELS_RAW="${MERGE_TRAIN_PRIORITY_LABELS-ai:workflow-heal,ai:security}"
case "$(printf '%s' "${MT_PRIORITY_LABELS_RAW}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
	''|none|off) MT_PRIORITY_LABELS_JSON='[]' ;;
	*)
		if ! MT_PRIORITY_LABELS_JSON="$(printf '%s' "${MT_PRIORITY_LABELS_RAW}" | jq -Rsc 'split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)) | unique' 2>/dev/null)" \
			|| ! [[ "${MT_PRIORITY_LABELS_JSON}" == \[* ]]; then
			_mt_warn "review_merge_train.sh: could not parse MERGE_TRAIN_PRIORITY_LABELS; priority lane disabled for this run."
			MT_PRIORITY_LABELS_JSON='[]'
		fi
		;;
esac
# Which subcommand is evaluating blockers; appears in the per-blocker log lines.
MT_EVAL_SOURCE=""

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Paths changed by a PR, one per line, sorted. Cached per PR number for the
# lifetime of the process (release examines many PRs against the same older
# set).
#
# Output-variable API: `_mt_pr_files_into <varname> <pr>` assigns the list to
# the caller's variable and returns 1 (leaving it untouched) when the API
# call fails so callers can fail open explicitly. Callers MUST use this form
# rather than `files="$(_mt_pr_files N)"`: a command substitution runs the
# function in a subshell, so the cache write is discarded on return and every
# queued PR re-fetched the same older PRs' file lists (CLAUDE.md §15). The
# printing form `_mt_pr_files` is kept for compatibility and still fills the
# cache when it is called directly (not inside `$(...)`).
#
# API budget: one paginated `GET /pulls/{n}/files` per distinct PR per run.
declare -A _MT_FILES_CACHE=()
_mt_pr_files_into() {
	local __mt_files_dest="$1" __mt_files_pr="$2" __mt_files_out=""
	if [ -n "${_MT_FILES_CACHE[${__mt_files_pr}]+x}" ]; then
		printf -v "${__mt_files_dest}" '%s' "${_MT_FILES_CACHE[${__mt_files_pr}]}"
		return 0
	fi
	if ! __mt_files_out="$(gh_retry gh api --paginate "repos/${MT_REPO}/pulls/${__mt_files_pr}/files?per_page=100" --jq '.[].filename' 2>/dev/null)"; then
		return 1
	fi
	__mt_files_out="$(printf '%s\n' "${__mt_files_out}" | sed '/^$/d' | sort -u)"
	_MT_FILES_CACHE[${__mt_files_pr}]="${__mt_files_out}"
	printf -v "${__mt_files_dest}" '%s' "${__mt_files_out}"
}

_mt_pr_files() {
	local __mt_files_printed=""
	_mt_pr_files_into __mt_files_printed "$1" || return 1
	printf '%s' "${__mt_files_printed}"
}

# This PR's paths from the diff review_collect_pr_metadata.sh already fetched
# (zero API calls); falls back to _mt_pr_files_into. Same output-variable
# contract: `_mt_own_files_into <varname> <pr>`; `_mt_own_files` prints.
_mt_own_files_into() {
	local __mt_own_dest="$1" __mt_own_pr="$2" diff_file="${PR_DIFF_FILE:-}"
	if [ -n "${diff_file}" ] && [ -s "${diff_file}" ]; then
		# Git C-quotes headers containing non-ASCII/control bytes. The REST
		# filenames are canonical, so use them rather than compare quoted text.
		if grep -q '^diff --git "' "${diff_file}"; then
			_mt_pr_files_into "${__mt_own_dest}" "${__mt_own_pr}"
			return $?
		fi
		# `diff --git a/<old> b/<new>` — take the b/ side; renames count on
		# both sides so the old path is included too.
		local parsed_files
		parsed_files="$(sed -n 's#^diff --git a/\(.*\) b/\(.*\)$#\1\n\2#p' "${diff_file}" | sed '/^$/d' | sort -u)"
		if [ -n "${parsed_files}" ]; then
			printf -v "${__mt_own_dest}" '%s' "${parsed_files}"
			return 0
		fi
	fi
	_mt_pr_files_into "${__mt_own_dest}" "${__mt_own_pr}"
}

_mt_own_files() {
	local __mt_own_printed=""
	_mt_own_files_into __mt_own_printed "$1" || return 1
	printf '%s' "${__mt_own_printed}"
}

# Intersection of two newline-separated sorted lists.
_mt_intersect() {
	comm -12 <(printf '%s\n' "$1" | sed '/^$/d' | sort -u) <(printf '%s\n' "$2" | sed '/^$/d' | sort -u)
}

# Open PRs, oldest first, one compact JSON object per line:
# {number, head, head_repo, head_sha, created_at, base, draft, labels[]}.
# head_sha and created_at (issue #6570) feed the conflict check and the head
# age cap; older consumers ignore them. `tojson` matters: `gh api --jq`
# pretty-prints object results across several lines, and the callers read
# this output line by line. $1 = base branch filter (empty = all bases).
_mt_list_open_prs() {
	local base="$1" endpoint
	endpoint="repos/${MT_REPO}/pulls?state=open&sort=created&direction=asc&per_page=100"
	if [ -n "${base}" ]; then
		endpoint="${endpoint}&base=${base}"
	fi
	gh_retry gh api --paginate "${endpoint}" \
		--jq '.[] | {number: .number, head: .head.ref, head_repo: (.head.repo.full_name // ""), head_sha: (.head.sha // ""), created_at: (.created_at // ""), base: .base.ref, draft: .draft, labels: [.labels[].name]} | tojson' 2>/dev/null
}

# Older open same-repository ai/issue-* PRs (same base, lower number, not
# draft, not review-blocked / closed-labelled) whose files overlap $4 (this PR's paths).
# `_mt_blockers_for_into <varname> <pr> <base> <own_files> <prs_json>` assigns
# newline-separated "#N:path,path" lines (empty when unblocked) to the caller's
# variable; returns 1 on API failure. It must run in the caller's shell, not a
# `$(...)` subshell, so the file lists it pulls through _mt_pr_files_into stay
# cached for the next queued PR the release loop evaluates. `_mt_blockers_for`
# is the printing form kept for compatibility.
#
# Two phases (issue #6570). Phase A collects the path-overlapping older PRs
# exactly as before (same filters, same MT_MAX_OLDER cap). Phase B drops a
# candidate only on positive evidence: the priority lane, a stale head, or a
# clean `git merge-tree`; anything unverifiable keeps it.
_mt_blockers_for_into() {
	local __mt_blockers_dest="$1" pr="$2" base="$3" own_files="$4" prs_json="$5"
	local examined=0 line num head head_repo draft labels files common __mt_blockers_acc=""
	local -a __mt_cand_num=() __mt_cand_head=() __mt_cand_sha=() __mt_cand_created=() __mt_cand_labels=() __mt_cand_common=()
	while IFS= read -r line; do
		[ -n "${line}" ] || continue
		num="$(printf '%s' "${line}" | jq -r '.number')"
		head="$(printf '%s' "${line}" | jq -r '.head')"
		head_repo="$(printf '%s' "${line}" | jq -r '.head_repo')"
		draft="$(printf '%s' "${line}" | jq -r '.draft')"
		labels="$(printf '%s' "${line}" | jq -r '.labels | join(",")')"
		[[ "${num}" =~ ^[0-9]+$ ]] || continue
		[ "${num}" -lt "${pr}" ] || continue
		[[ "${head}" == "${MT_PREFIX}"* ]] || continue
		if [ -z "${head_repo}" ] || [ "${head_repo,,}" != "${MT_REPO,,}" ]; then
			_mt_log "MERGE_TRAIN_FOREIGN_HEAD_SKIPPED pr=${pr} older=${num}"
			continue
		fi
		[ "${draft}" != "true" ] || continue
		case ",${labels}," in
			*,ai:review-blocked,*|*,ai:closed,*|*,ai:merged,*) continue ;;
		esac
		examined=$((examined + 1))
		if [ "${examined}" -gt "${MT_MAX_OLDER}" ]; then
			_mt_log "MERGE_TRAIN_OLDER_PR_CAP pr=${pr} cap=${MT_MAX_OLDER} action=stop_examining"
			break
		fi
		if ! _mt_pr_files_into files "${num}"; then
			return 1
		fi
		common="$(_mt_intersect "${own_files}" "${files}")"
		if [ -n "${common}" ]; then
			__mt_cand_num+=("${num}")
			__mt_cand_head+=("${head}")
			__mt_cand_sha+=("$(printf '%s' "${line}" | jq -r '.head_sha // ""')")
			__mt_cand_created+=("$(printf '%s' "${line}" | jq -r '.created_at // ""')")
			__mt_cand_labels+=("${labels}")
			__mt_cand_common+=("$(printf '%s\n' "${common}" | paste -sd, -)")
		fi
	done < <(printf '%s\n' "${prs_json}" | jq -c 'select(.base == $b)' --arg b "${base}")

	if [ "${#__mt_cand_num[@]}" -eq 0 ]; then
		printf -v "${__mt_blockers_dest}" '%s' ""
		return 0
	fi

	# Phase B. This PR's own head (for priority and the conflict probe) comes
	# from its own row in the listing both callers already hold.
	local own_line own_head="" own_sha="" idx age_needed="false" meta_args=() now_epoch created_epoch
	local own_priority="off" own_priority_rc=1 cand_priority_rc cand_priority stale_state conflict_state action shown
	local skipped_stale=0 skipped_priority=0 skipped_clean=0 source_name="${MT_EVAL_SOURCE:-unknown}"
	own_line="$(printf '%s\n' "${prs_json}" | jq -c --argjson n "${pr}" 'select(.number == $n)' 2>/dev/null | head -n 1 || true)"
	if [ -n "${own_line}" ]; then
		own_head="$(printf '%s' "${own_line}" | jq -r '.head // ""')"
		own_sha="$(printf '%s' "${own_line}" | jq -r '.head_sha // ""')"
	fi
	now_epoch="$(date -u +%s)"
	if [ "${MT_HEAD_MAX_AGE_HOURS}" -gt 0 ]; then
		for idx in "${!__mt_cand_num[@]}"; do
			_mt_has_label "${__mt_cand_labels[$idx]}" && continue
			created_epoch="$(_mt_iso_epoch "${__mt_cand_created[$idx]}")" || continue
			if [ $((now_epoch - created_epoch)) -gt $((MT_HEAD_MAX_AGE_HOURS * 3600)) ]; then
				meta_args+=("${__mt_cand_num[$idx]}+timeline")
				age_needed="true"
			fi
		done
	fi
	if [ "${MT_PRIORITY_LABELS_JSON}" != "[]" ]; then
		meta_args+=("${pr}")
		for idx in "${!__mt_cand_num[@]}"; do
			meta_args+=("${__mt_cand_num[$idx]}")
		done
	fi
	if [ "${#meta_args[@]}" -gt 0 ]; then
		_mt_pr_meta_prefetch "${meta_args[@]}"
	fi
	if [ "${MT_PRIORITY_LABELS_JSON}" != "[]" ]; then
		own_priority_rc=0
		_mt_pr_is_priority "${pr}" "${own_head}" || own_priority_rc=$?
		case "${own_priority_rc}" in
			0) own_priority="yes" ;;
			1) own_priority="no" ;;
			*) own_priority="unknown" ;;
		esac
	fi

	for idx in "${!__mt_cand_num[@]}"; do
		num="${__mt_cand_num[$idx]}"
		cand_priority="n/a"
		stale_state="unchecked"
		[ "${MT_HEAD_MAX_AGE_HOURS}" -gt 0 ] || stale_state="off"
		conflict_state="unchecked"
		action="block"
		shown="${__mt_cand_common[$idx]}"
		# 1. Priority lane: only a verified priority PR passes a verified
		# non-priority blocker; an unknown blocker is kept.
		if [ "${own_priority}" = "yes" ]; then
			cand_priority_rc=0
			_mt_pr_is_priority "${num}" "${__mt_cand_head[$idx]}" || cand_priority_rc=$?
			case "${cand_priority_rc}" in
				0) cand_priority="yes" ;;
				1) cand_priority="no" ;;
				*) cand_priority="unknown" ;;
			esac
			if [ "${cand_priority}" = "no" ]; then
				action="skip"
				skipped_priority=$((skipped_priority + 1))
			fi
		fi
		# 2. Bounded head.
		if [ "${action}" = "block" ] && [ "${MT_HEAD_MAX_AGE_HOURS}" -gt 0 ]; then
			stale_state="no"
			if [ "${age_needed}" = "true" ] && _mt_blocker_is_stale "${num}" "${__mt_cand_head[$idx]}" "${__mt_cand_labels[$idx]}" "${now_epoch}"; then
				stale_state="yes"
				action="skip"
				skipped_stale=$((skipped_stale + 1))
			fi
		fi
		# 3. Real conflict.
		if [ "${action}" = "block" ] && [ "${MT_CONFLICT_CHECK}" = "true" ]; then
			_mt_conflict_probe "${own_sha}" "${__mt_cand_sha[$idx]}"
			conflict_state="${_MT_PROBE_RESULT}"
			case "${conflict_state}" in
				none)
					action="skip"
					skipped_clean=$((skipped_clean + 1))
					;;
				conflict)
					if [ -n "${_MT_PROBE_PATHS}" ]; then
						shown="$(printf '%s\n' "${_MT_PROBE_PATHS}" | sed '/^$/d' | paste -sd, -)"
					fi
					;;
			esac
		fi
		_mt_log "MERGE_TRAIN_GATE pr=${pr} source=${source_name} older=${num} overlap=paths conflict=${conflict_state} priority=${own_priority}:${cand_priority} stale=${stale_state} action=${action}"
		if [ "${action}" = "block" ]; then
			__mt_blockers_acc+="#${num}:${shown}"$'\n'
		fi
	done
	_mt_log "MERGE_TRAIN_GATE pr=${pr} source=${source_name} skipped_stale_head=${skipped_stale} skipped_priority=${skipped_priority} skipped_clean=${skipped_clean}"
	# Match what the former `$(...)` capture produced: no trailing newline.
	printf -v "${__mt_blockers_dest}" '%s' "${__mt_blockers_acc%$'\n'}"
	return 0
}

# Epoch seconds for a GitHub UTC timestamp (YYYY-MM-DDTHH:MM:SSZ, optional
# fractional seconds). Returns 1 for anything else, so callers keep blockers.
_mt_iso_epoch()
{
	local ts="$1"
	[[ "${ts}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$ ]] || return 1
	date -u -d "${ts}" +%s 2>/dev/null
}

# Batched PR metadata for the priority lane and the head-age cap (issue #6570).
#
# Input:     PR numbers; "N+timeline" also requests N's queue-label history.
# Output:    _MT_META_CACHE[N] = compact JSON
#            {head_ref, created_at, closing:[{number, labels[]}],
#             timeline: null | {complete, events:[{type, created_at}]}}
#            (events are the MT_LABEL label/unlabel events only). A number the
#            call could not verify gets _MT_META_FAILED[N]=1 and no cache entry.
# API calls: one aliased GraphQL query for every number not already cached
#            (or cached without the history it now needs); 0 when all are
#            cached. Audit (§14): the REST pulls listing carries neither label
#            history nor the linked issues' labels, and the existing
#            issues/N/events read in _mt_bypass_authorized is one REST call
#            per PR. A single aliased query is the only shape that covers N
#            blockers in one call.
# Fail-open: a failed or malformed call caches nothing and marks the numbers
#            failed (no retry this run); callers then keep every blocker and
#            grant no priority.
declare -A _MT_META_CACHE=()
declare -A _MT_META_FAILED=()
_mt_pr_meta_prefetch()
{
	local arg meta_num want_timeline query="" response="" owner name parsed
	local -a fetch_nums=()
	local -A fetch_timeline=()
	owner="${MT_REPO%%/*}"
	name="${MT_REPO#*/}"
	if ! [[ "${owner}" =~ ^[A-Za-z0-9_.-]+$ && "${name}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
		_mt_warn "merge-train: metadata lookup skipped; repository '${MT_REPO}' is not owner/name."
		return 0
	fi
	for arg in "$@"; do
		meta_num="${arg%+timeline}"
		want_timeline="false"
		[ "${arg}" != "${meta_num}" ] && want_timeline="true"
		[[ "${meta_num}" =~ ^[1-9][0-9]*$ ]] || continue
		[ -z "${_MT_META_FAILED[${meta_num}]+x}" ] || continue
		if [ -n "${_MT_META_CACHE[${meta_num}]+x}" ]; then
			if [ "${want_timeline}" != "true" ] || printf '%s' "${_MT_META_CACHE[${meta_num}]}" | jq -e '.timeline != null' >/dev/null 2>&1; then
				continue
			fi
		fi
		if [ -z "${fetch_timeline[${meta_num}]+x}" ]; then
			fetch_nums+=("${meta_num}")
			fetch_timeline[${meta_num}]="${want_timeline}"
		elif [ "${want_timeline}" = "true" ]; then
			fetch_timeline[${meta_num}]="true"
		fi
	done
	[ "${#fetch_nums[@]}" -gt 0 ] || return 0
	query='query($owner:String!,$name:String!){repository(owner:$owner,name:$name){'
	for meta_num in "${fetch_nums[@]}"; do
		query+="p${meta_num}:pullRequest(number:${meta_num}){number headRefName createdAt closingIssuesReferences(first:10){nodes{number labels(first:50){nodes{name}}}}"
		if [ "${fetch_timeline[${meta_num}]}" = "true" ]; then
			query+=' timelineItems(itemTypes:[LABELED_EVENT,UNLABELED_EVENT],last:100){pageInfo{hasPreviousPage} nodes{__typename ... on LabeledEvent{createdAt label{name}} ... on UnlabeledEvent{createdAt label{name}}}}'
		fi
		query+='}'
	done
	query+='}}'
	if ! response="$(gh_retry gh api graphql -f query="${query}" -f owner="${owner}" -f name="${name}" 2>/dev/null)" \
		|| ! printf '%s' "${response}" | jq -e '.data.repository | type == "object"' >/dev/null 2>&1; then
		_mt_warn "merge-train: metadata lookup failed for PR(s) ${fetch_nums[*]}; keeping their blockers and granting no priority."
		for meta_num in "${fetch_nums[@]}"; do
			_MT_META_FAILED[${meta_num}]=1
		done
		return 0
	fi
	for meta_num in "${fetch_nums[@]}"; do
		if parsed="$(printf '%s' "${response}" | jq -ce --arg k "p${meta_num}" --argjson n "${meta_num}" --arg label "${MT_LABEL}" '
			.data.repository[$k] as $p
			| if ($p | type) != "object" or $p.number != $n then error("unverified") else
				{head_ref: ($p.headRefName // ""),
				 created_at: ($p.createdAt // ""),
				 closing: [($p.closingIssuesReferences.nodes // [])[] | select(type == "object") | {number: .number, labels: [(.labels.nodes // [])[] | .name? // empty]}],
				 timeline: (if ($p.timelineItems | type) == "object" then
					{complete: ($p.timelineItems.pageInfo.hasPreviousPage == false),
					 events: [($p.timelineItems.nodes // [])[] | select(type == "object" and (.label.name? // "") == $label) | {type: .__typename, created_at: (.createdAt // "")}]}
					else null end)}
			end' 2>/dev/null)"; then
			_MT_META_CACHE[${meta_num}]="${parsed}"
		else
			_mt_warn "merge-train: metadata for PR #${meta_num} missing or unverified; keeping it as a blocker."
			_MT_META_FAILED[${meta_num}]=1
		fi
	done
	return 0
}

# Priority lane membership (issue #6570). `_mt_pr_is_priority <pr> <head_ref>`
# returns 0 = priority, 1 = not priority, 2 = unknown. Priority needs every
# one of: head ref `${MT_PREFIX}<M>`, GitHub's headRefName equal to it, M among
# GitHub's closingIssuesReferences, and issue M carrying a label from
# MERGE_TRAIN_PRIORITY_LABELS. PR body text never grants priority.
_mt_pr_is_priority()
{
	local pr="$1" head_ref="$2" issue_ref meta
	issue_ref="${head_ref#"${MT_PREFIX}"}"
	if [ -z "${head_ref}" ] || [ "${issue_ref}" = "${head_ref}" ] || ! [[ "${issue_ref}" =~ ^[1-9][0-9]*$ ]]; then
		return 1
	fi
	[ -n "${_MT_META_CACHE[${pr}]+x}" ] || return 2
	meta="${_MT_META_CACHE[${pr}]}"
	if ! printf '%s' "${meta}" | jq -e --arg h "${head_ref}" '.head_ref == $h' >/dev/null 2>&1; then
		return 2
	fi
	if printf '%s' "${meta}" | jq -e --argjson m "${issue_ref}" --argjson wanted "${MT_PRIORITY_LABELS_JSON}" \
		'any(.closing[]; .number == $m and any(.labels[]; . as $l | any($wanted[]; . == $l)))' >/dev/null 2>&1; then
		return 0
	fi
	return 1
}

# Head-age cap (issue #6570). `_mt_blocker_is_stale <num> <head> <labels_csv>
# <now_epoch>` returns 0 only when the blocker is not itself queued, its
# metadata matches the listed head, and its review started more than
# MERGE_TRAIN_HEAD_MAX_AGE_HOURS ago. Review start is the newest queue-label
# removal; a PR never queued (no queue-label event in a complete history)
# starts at its creation. A newest event that is a label add, an incomplete
# history without a removal, a malformed timestamp or a same-second tie keeps
# the blocker (the tie rule of _mt_bypass_authorized).
_mt_blocker_is_stale()
{
	local num="$1" head="$2" labels="$3" now_epoch="$4" meta start start_epoch
	[ "${MT_HEAD_MAX_AGE_HOURS}" -gt 0 ] || return 1
	_mt_has_label "${labels}" && return 1
	[ -n "${_MT_META_CACHE[${num}]+x}" ] || return 1
	meta="${_MT_META_CACHE[${num}]}"
	if ! start="$(printf '%s' "${meta}" | jq -er --arg h "${head}" '
		def ts_ok: type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$");
		if .head_ref != $h or .timeline == null then error("keep")
		elif (.timeline.events | length) == 0 then
			(if .timeline.complete == true and (.created_at | ts_ok) then .created_at else error("keep") end)
		elif any(.timeline.events[]; (.created_at | ts_ok) | not) then error("keep")
		else
			(.timeline.events | max_by(.created_at) | .created_at) as $newest
			| [.timeline.events[] | select(.created_at == $newest)] as $top
			| if ($top | length) != 1 or $top[0].type != "UnlabeledEvent" then error("keep") else $newest end
		end' 2>/dev/null)"; then
		return 1
	fi
	start_epoch="$(_mt_iso_epoch "${start}")" || return 1
	[ $((now_epoch - start_epoch)) -gt $((MT_HEAD_MAX_AGE_HOURS * 3600)) ]
}

# Conflict probe (issue #6570). `_mt_conflict_probe <own_sha> <blocker_sha>`
# sets _MT_PROBE_RESULT to none (the heads merge cleanly), conflict (git
# reports a content conflict; _MT_PROBE_PATHS lists the paths) or unknown (any
# other outcome). Runs in the caller's shell so its caches persist.
# Missing heads are fetched by SHA with --no-write-fetch-head (FETCH_HEAD for
# later steps is untouched) and --depth=200 only in an already-shallow clone,
# so a full clone is never made shallow. merge-tree writes only objects; it
# runs no hooks and no PR-supplied merge driver. No API calls.
declare -A _MT_FETCHED_SHA=()
_MT_GIT_PROBE_OK=""
_MT_PROBE_RESULT="unknown"
_MT_PROBE_PATHS=""
_mt_conflict_probe()
{
	local own_sha="$1" other_sha="$2" probe_sha probe_out="" probe_rc=0
	local -a probe_missing=() probe_depth=()
	_MT_PROBE_RESULT="unknown"
	_MT_PROBE_PATHS=""
	if ! [[ "${own_sha}" =~ ^[0-9a-f]{40}$ && "${other_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		return 0
	fi
	if [ -z "${_MT_GIT_PROBE_OK}" ]; then
		if git merge-tree --write-tree --name-only --no-messages HEAD HEAD >/dev/null 2>&1; then
			_MT_GIT_PROBE_OK="yes"
		else
			_MT_GIT_PROBE_OK="no"
		fi
	fi
	[ "${_MT_GIT_PROBE_OK}" = "yes" ] || return 0
	for probe_sha in "${own_sha}" "${other_sha}"; do
		if ! git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
			[ "${_MT_FETCHED_SHA[${probe_sha}]:-}" = "failed" ] && return 0
			probe_missing+=("${probe_sha}")
		fi
	done
	if [ "${#probe_missing[@]}" -gt 0 ]; then
		if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || true)" = "true" ]; then
			probe_depth=(--depth=200)
		fi
		if ! GIT_TERMINAL_PROMPT=0 timeout 120 git fetch --no-tags --quiet --no-write-fetch-head \
			${probe_depth[@]+"${probe_depth[@]}"} origin "${probe_missing[@]}" >/dev/null 2>&1; then
			for probe_sha in "${probe_missing[@]}"; do
				_MT_FETCHED_SHA[${probe_sha}]="failed"
			done
			return 0
		fi
		for probe_sha in "${probe_missing[@]}"; do
			if git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
				_MT_FETCHED_SHA[${probe_sha}]="ok"
			else
				_MT_FETCHED_SHA[${probe_sha}]="failed"
				return 0
			fi
		done
	fi
	probe_out="$(git merge-tree --write-tree --name-only --no-messages "${own_sha}" "${other_sha}" 2>/dev/null)" || probe_rc=$?
	case "${probe_rc}" in
		0) _MT_PROBE_RESULT="none" ;;
		1)
			_MT_PROBE_RESULT="conflict"
			_MT_PROBE_PATHS="$(printf '%s\n' "${probe_out}" | sed '1d' | sed '/^$/d' | sort -u)"
			;;
		*) _MT_PROBE_RESULT="unknown" ;;
	esac
	return 0
}

_mt_blockers_for() {
	local __mt_blockers_printed=""
	# The per-blocker log lines (issue #6570) go to stderr so stdout keeps
	# carrying only the blocker list, as before.
	_mt_blockers_for_into __mt_blockers_printed "$@" >&2 || return 1
	printf '%s' "${__mt_blockers_printed}"
}

_mt_ensure_label() {
	if type ensure_label_exists >/dev/null 2>&1; then
		ensure_label_exists "${MT_LABEL}" "${MT_REPO}" || true
	elif [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" ]; then
		# shellcheck disable=SC1091
		source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null || true
		type ensure_label_exists >/dev/null 2>&1 && { ensure_label_exists "${MT_LABEL}" "${MT_REPO}" || true; }
	else
		gh_retry gh label create "${MT_LABEL}" --repo "${MT_REPO}" --color "c5def5" \
			--description "Review queued behind an older open PR that edits the same files (merge train)" >/dev/null 2>&1 || true
	fi
}

# The open-PR list contains current labels but neither comment authors nor
# label-event history; the comments list supplies authors but not label events.
# Resolve the authenticated account once, in the caller's shell (not $(...)).
_mt_resolve_automation_login()
{
	local login=""
	[ -z "${MT_AUTOMATION_LOGIN}" ] || return 0
	login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null)" || return 1
	login="$(printf '%s' "${login}" | tr '[:upper:]' '[:lower:]')"
	[[ "${login}" =~ ^[a-z0-9][a-z0-9-]*(\[bot\])?$ ]] || return 1
	MT_AUTOMATION_LOGIN="${login}"
}

# Latest trusted marker as "id created_at"; empty means not found. The old
# id-only helper remains for callers that do not need the creation time.
_mt_find_marker_comment()
{
	local pr="$1" marker="$2"
	[ -n "${MT_AUTOMATION_LOGIN}" ] || return 1
	gh_retry gh api --paginate "repos/${MT_REPO}/issues/${pr}/comments?per_page=100" \
		--jq ".[] | select((.user.login // \"\" | ascii_downcase) == \"${MT_AUTOMATION_LOGIN}\" and ((.body // \"\") | startswith(\"${marker}\"))) | \"\\(.id) \\(.created_at)\"" 2>/dev/null | LC_ALL=C sort -k2,2r -k1,1nr | sed -n '1p'
}

_mt_find_marker_comment_id()
{
	local comment=""
	comment="$(_mt_find_marker_comment "$@")" || return 1
	printf '%s' "${comment%% *}"
}

# The latest label event must be a later, human-authorized removal. A failed
# verification returns a stable reason token; no comment body can satisfy it.
_mt_bypass_authorized()
{
	local pr="$1" marker_created_at="$2" events="" last_event="" action="" event_label="" timestamp="" actor="" role=""
	if ! [[ "${marker_created_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
		echo removal_not_after_marker
		return 1
	fi
	# No existing listing includes label events; read them once, across all pages.
	if ! events="$(gh_retry gh api --paginate "repos/${MT_REPO}/issues/${pr}/events?per_page=100" \
		--jq '.[] | select(.event=="labeled" or .event=="unlabeled") | [.event, (.label.name // ""), .created_at, (.actor.login // "")] | @tsv' 2>/dev/null)"; then
		echo events_unavailable
		return 1
	fi
	# Pagination order is not an authority for which label cycle is current.
	# A same-second tie cannot establish which actor removed the label last.
	if ! last_event="$(printf '%s\n' "${events}" | awk -F '\t' -v l="${MT_LABEL}" '$2==l {if ($3 > newest) {newest=$3; last=$0; tied=0} else if ($3 == newest) tied=1} END {if (tied) exit 1; print last}')"; then
		echo events_unavailable
		return 1
	fi
	IFS=$'\t' read -r action event_label timestamp actor <<< "${last_event}"
	if [ "${action}" != "unlabeled" ] || [ "${event_label}" != "${MT_LABEL}" ]; then
		echo no_label_removal
		return 1
	fi
	if ! [[ "${timestamp}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ && "${timestamp}" > "${marker_created_at}" ]]; then
		echo removal_not_after_marker
		return 1
	fi
	actor="$(printf '%s' "${actor}" | tr '[:upper:]' '[:lower:]')"
	if ! [[ "${actor}" =~ ^[a-z0-9][a-z0-9-]*(\[bot\])?$ ]] || [[ "${actor}" == *'[bot]' || "${actor}" == "ghost" ]]; then
		echo actor_unknown
		return 1
	fi
	if ! role="$(gh_retry gh api "repos/${MT_REPO}/collaborators/${actor}/permission" --jq '.role_name // ""' 2>/dev/null)"; then
		echo permission_unavailable
		return 1
	fi
	case "${role}" in
		admin|maintain|write|triage) return 0 ;;
		*) echo actor_unauthorized; return 1 ;;
	esac
}

_mt_replace_comment()
{
	local comment_id="$1" body="$2"
	[[ "${comment_id}" =~ ^[0-9]+$ ]] || return 1
	gh_retry gh api -X PATCH "repos/${MT_REPO}/issues/comments/${comment_id}" \
		-f body="${body}" >/dev/null 2>&1
}

# Upsert one marker comment per PR so repeated gate runs never spam.
# $1 = pr, $2 = marker, $3 = body, $4 = optional already-looked-up comment id.
# Skips the write when an identical body already carries the marker.
_mt_upsert_comment() {
	local pr="$1" marker="$2" body="$3" existing_id="" existing_body
	if [ "$#" -ge 4 ]; then
		existing_id="$4"
	elif ! existing_id="$(_mt_find_marker_comment_id "${pr}" "${marker}")"; then
		return 1
	fi
	if [[ "${existing_id}" =~ ^[0-9]+$ ]]; then
		existing_body="$(gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body' 2>/dev/null || true)"
		if [ "${existing_body}" = "${body}" ]; then
			return 0
		fi
		gh_retry gh api -X PATCH "repos/${MT_REPO}/issues/comments/${existing_id}" -f body="${body}" >/dev/null 2>&1 || true
		return 0
	fi
	gh_retry gh api -X POST "repos/${MT_REPO}/issues/${pr}/comments" -f body="${body}" >/dev/null 2>&1 || true
}

_mt_has_label() {
	local labels_csv="$1"
	case ",${labels_csv}," in
		*,"${MT_LABEL}",*) return 0 ;;
	esac
	return 1
}

# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------
_mt_gate() {
	local pr="${PR_NUMBER:-}" base="${BASE_BRANCH:-}" head="${TARGET_BRANCH:-}"
	MT_EVAL_SOURCE="gate"
	if ! [[ "${pr}" =~ ^[0-9]+$ ]] || [ -z "${base}" ] || [ -z "${head}" ]; then
		_mt_warn "merge-train gate: invalid inputs PR_NUMBER='${pr}' BASE_BRANCH='${base}' TARGET_BRANCH='${head}'; fail-open (not queued)."
		return 0
	fi
	if [[ "${head}" != "${MT_PREFIX}"* ]]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} head=${head} result=not_ai_issue_branch action=continue"
		return 0
	fi
	local own_files="" prs_json blockers=""
	if [ "${IS_SMOKE_TEST:-}" != "true" ]; then
		if ! _mt_own_files_into own_files "${pr}"; then
			_mt_warn "merge-train gate: could not list changed files for PR #${pr}; fail-open (not queued)."
			return 0
		fi
		if [ -z "${own_files}" ]; then
			_mt_log "MERGE_TRAIN_GATE pr=${pr} result=no_changed_files action=continue"
			return 0
		fi
	fi
	if ! prs_json="$(_mt_list_open_prs "${base}")"; then
		_mt_warn "merge-train gate: could not list open PRs on ${base}; fail-open (not queued)."
		return 0
	fi
	if [ "${IS_SMOKE_TEST:-}" != "true" ]; then
		if ! _mt_blockers_for_into blockers "${pr}" "${base}" "${own_files}" "${prs_json}"; then
			_mt_warn "merge-train gate: could not list files of an older PR; fail-open (not queued)."
			return 0
		fi
	fi
	local own_labels queued_comment_id queued_comment_created queued_comment bypass_reason queue_label_persisted queue_state_verified
	own_labels="$(printf '%s\n' "${prs_json}" | jq -r --argjson n "${pr}" 'select(.number == $n) | .labels | join(",")' 2>/dev/null | head -n 1 || true)"
	if [ -z "${blockers}" ]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=unblocked action=continue"
		if _mt_has_label "${own_labels}"; then
			if ! _mt_resolve_automation_login || ! queued_comment_id="$(_mt_find_marker_comment_id "${pr}" "${MT_MARKER}")"; then
				_mt_warn "merge-train gate: could not inspect queue marker for unblocked PR #${pr}; retaining ${MT_LABEL} for the release backstop."
				return 0
			fi
			if [[ "${queued_comment_id}" =~ ^[0-9]+$ ]] && \
			   ! _mt_replace_comment "${queued_comment_id}" "${MT_RETIRED_MARKER}
**Merge train queue retired.** The train found no older overlapping PRs; the current review run is continuing."; then
				_mt_warn "merge-train gate: could not retire the queue marker for unblocked PR #${pr}; retaining ${MT_LABEL} to avoid arming a false bypass."
				return 0
			fi
			if gh_retry gh api -X DELETE "repos/${MT_REPO}/issues/${pr}/labels/$(printf '%s' "${MT_LABEL}" | jq -sRr @uri)" >/dev/null 2>&1; then
				_mt_log "MERGE_TRAIN_RELEASED pr=${pr} source=gate"
			else
				_mt_warn "merge-train gate: could not remove ${MT_LABEL} from unblocked PR #${pr}; the release backstop will retry."
			fi
		fi
		return 0
	fi
	queue_state_verified="true"
	if ! _mt_resolve_automation_login || ! queued_comment="$(_mt_find_marker_comment "${pr}" "${MT_MARKER}")"; then
		_mt_warn "merge-train gate: could not inspect prior queue state for PR #${pr}; keeping it queued."
		queue_state_verified="false"
		queued_comment=""
	fi
	queued_comment_id="${queued_comment%% *}"
	queued_comment_created="${queued_comment#* }"
	if ! _mt_has_label "${own_labels}" && [[ "${queued_comment_id}" =~ ^[0-9]+$ ]]; then
		if bypass_reason="$(_mt_bypass_authorized "${pr}" "${queued_comment_created}")"; then
			if gh_retry gh api -X PATCH "repos/${MT_REPO}/issues/comments/${queued_comment_id}" \
				-f body="${MT_BYPASSED_MARKER}
**Merge train bypassed once.** The queue label was removed while older overlapping PRs remain open, so this review run is proceeding. A later run will evaluate the train normally." >/dev/null 2>&1; then
				_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=bypassed blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -) action=continue"
				return 0
			fi
			_mt_warn "merge-train gate: could not persist the one-shot bypass marker for PR #${pr}; keeping it queued."
			bypass_reason="marker_update_failed"
		fi
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=bypass_rejected reason=${bypass_reason} action=queue"
	fi
	local blocker_numbers blocker_lines
	blocker_numbers="$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd' ' -)"
	blocker_lines="$(printf '%s\n' "${blockers}" | sed 's/^\(#[0-9]*\):\(.*\)$/- \1 — `\2`/' | sed 's/,/`, `/g')"
	_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=queued blockers=${blocker_numbers// /,} action=soft_exit"
	_mt_ensure_label
	queue_label_persisted="true"
	if ! _mt_has_label "${own_labels}"; then
		queue_label_persisted="false"
		if gh_retry gh api -X POST "repos/${MT_REPO}/issues/${pr}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1; then
			queue_label_persisted="true"
		else
			_mt_warn "merge-train gate: could not add ${MT_LABEL} to PR #${pr}; the run still soft-exits and no bypass marker will be armed."
		fi
	fi
	if [ "${queue_label_persisted}" = "true" ] && [ "${queue_state_verified}" = "true" ]; then
		_mt_upsert_comment "${pr}" "${MT_MARKER}" "${MT_MARKER}
**Review queued (merge train).** This PR edits files that older open PRs on \`${base}\` also change and would conflict with them (or the conflict could not be checked), so its review/autofix run waits until they merge or close. Lowest PR number goes first; the queue is released automatically when a blocker closes (and re-checked on every orchestrator poll tick).

Blocked by:
${blocker_lines}

Set the repository variable \`MERGE_TRAIN_ENABLED=false\` to disable the train, or remove the \`${MT_LABEL}\` label and re-run the review to bypass it once." "${queued_comment_id}"
	fi
	if [ -n "${GITHUB_ENV:-}" ]; then
		{
			echo "AUTOFIX_MERGE_QUEUED=true"
			echo "AUTOFIX_MERGE_QUEUED_BLOCKERS=${blocker_numbers}"
			echo "AUTOFIX_STALE_BASE_SKIP=true"
		} >> "${GITHUB_ENV}"
	fi
	return 0
}

# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------
# The open-PR list cannot report active workflow runs. One cycle-local Actions
# lookup covers every queued PR and avoids a per-PR API call inside the loop.
# Each active review run prints its head branch. A review run dispatched from
# the default branch (issues #4618, #4701) has the default branch as its
# head, so a workflow_dispatch run prints "pr:<N>" from the name the wrappers
# give it ("Internal: AI Review & Autofix [pr:<N>]" / "AI Review [pr:<N>]")
# and never its head branch. Git refs cannot contain ":", so the two kinds of
# key never collide, and _mt_release checks both.
#
# Listing (security, issue #5443): every run in each active status is read,
# page by page. The previous single page of the newest 100 runs of every
# workflow covered well under the 250-minute review budget in coding-workflows
# (153 internal-review.yml dispatches in 5 hours, 2026-09-29), and a failed
# lookup released anyway, so the train could dispatch a second review beside
# a pending one. A run's `path` can also end in an "@<ref>" suffix
# (".github/workflows/ai-review.yml@refs/heads/main" for a workflow defined
# outside the repository's own workflow directory); the suffix is stripped
# before the workflow-file match.
#
# Input:     none (MT_REPO).
# Output:    one key per line on stdout, sorted and unique: the head branch
#            of each active review run that is not a workflow_dispatch run,
#            and "pr:<N>" for a workflow_dispatch run named for PR <N>.
#            Every run an active-status query returned counts as active,
#            whatever its own status field says.
# Returns:   0 = the listing is complete: a PR with no key has no active run.
#            1 = the listing is incomplete. Stdout carries nothing; the caller
#            must not release on it, and the next invocation retries.
# API calls: one `GET actions/runs?status=<s>&per_page=100&page=1` for each
#            non-terminal status, requested, pending, queued, waiting,
#            in_progress (PR #5451 review round 3: `requested` is a new run's
#            state before it is queued; `waiting` is only reached through a
#            deployment environment, which the review workflows do not use,
#            and is read so a wrapper that adds one is still covered), then
#            all five once more. The repo's other
#            active-run guards count the same five statuses
#            (scripts/apply_analysis_on_main.sh, scripts/auto_release_stable.sh).
#            Order (PR #5451 review round 4, and the review of head fd3ad67):
#            the queries run one after another, so a run that leaves a status
#            after that status was read and enters one that was read before
#            it is in neither result. Each pass reads the statuses in
#            lifecycle order, so a run whose status only moves forward during
#            a pass is returned by that pass however often it moves: it never
#            falls behind the status being read. The backward moves,
#            `in_progress` to `waiting` (a later job reaching a deployment
#            environment) and `waiting` to `queued` (an approved job), only
#            happen through a deployment environment, which the review
#            workflows do not use. A run is missed only when it moves
#            backward during both passes, within the seconds the listing
#            takes.
#            A status is complete when one response holds
#            every run its query matched (workflow_runs reaches total_count).
#            Otherwise the next query adds `&created=<=<oldest created_at
#            read>` and asks again, at most 10 queries ("pages") per status.
#            A created_at with fractional seconds is rounded up to the next
#            whole second, so the inclusive bound still covers that run.
#            Normally 10 calls, one per status query. REST only (CLAUDE.md §15).
# Keyset:    the listing is filtered by status and sorted by created_at,
#            newest first (checked live on 2026-09-30), so offset pages
#            (`page=2`, …) skip a run whenever runs above it leave the status
#            and others enter below it between two reads, even when
#            total_count stays the same. Each follow-up query instead starts
#            at the oldest created_at already read (inclusive, so runs
#            created in the same second are read again and deduplicated by
#            id): every run that stays in the status while the listing is
#            read is returned by some query, wherever other runs come and go.
#            A run created after the first query was issued is outside the
#            listing, as it is for any single snapshot.
# Incomplete: a page that failed after gh_retry, a malformed page (including
#            a total_count that is not a whole number, a workflow_runs that
#            is missing, null, or not an array, which the projection passes
#            through for the type check to reject (PR #5451 review round 5),
#            a workflow_runs entry that is not an object, which the projection
#            also passes through (PR #5451, review of head 73e97f5),
#            a run with no numeric id, no non-empty path, no non-empty
#            status, or no non-empty event (the event decides whether a run is
#            keyed by its PR-named title or its head_branch, AD-17), which the
#            listing can neither deduplicate nor classify,
#            and a missing or malformed created_at on a page that needs a
#            next query), a short page
#            before its total_count (the listing shifted while it was read),
#            more runs than 10 queries read or a query that adds no new run
#            (more than 100 runs created in one second), a review run that
#            yields no key (a non-dispatch run with no non-empty head_branch,
#            or a workflow_dispatch run with no PR-named title, whatever its
#            head_branch, so it could belong to any queued PR; PR #5451,
#            review of head fd3ad67 and review round 2), or a failed key
#            filter. Each is logged once on stderr
#            (CLAUDE.md §8):
#            MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<page_failed|malformed_page|listing_shifted|truncated|unattributed_run|filter_failed> status=<s> page=<p> read=<n> total=<n>
_mt_inflight_review_branches()
{
	local __mt_runs_max_pages=10 __mt_runs_status="" __mt_runs_page=0 __mt_runs_reason=""
	local __mt_runs_page_json="" __mt_runs_page_len=0 __mt_runs_total=0 __mt_runs_read=0 __mt_runs_read_before=0
	local __mt_runs_query="" __mt_runs_created_bound=""
	local __mt_runs_status_runs='[]' __mt_runs_all='[]' __mt_runs_keys="" __mt_runs_keyed='[]'
	for __mt_runs_status in requested pending queued waiting in_progress requested pending queued waiting in_progress; do
		__mt_runs_page=1
		__mt_runs_total=0
		__mt_runs_read=0
		__mt_runs_created_bound=""
		__mt_runs_status_runs='[]'
		while :; do
			__mt_runs_query="repos/${MT_REPO}/actions/runs?status=${__mt_runs_status}&per_page=100&page=1"
			if [ -n "${__mt_runs_created_bound}" ]; then
				__mt_runs_query="${__mt_runs_query}&created=%3C%3D${__mt_runs_created_bound}"
			fi
			if ! __mt_runs_page_json="$(gh_retry gh api -X GET "${__mt_runs_query}" \
				--jq '{total_count: .total_count, workflow_runs: (.workflow_runs | if type == "array" then [.[] | if type == "object" then {id: .id, status: .status, event: .event, head_branch: .head_branch, display_title: .display_title, path: .path, created_at: .created_at} else . end] else . end)}' \
				2>/dev/null)"; then
				__mt_runs_reason="page_failed"
				break 2
			fi
			if ! printf '%s' "${__mt_runs_page_json}" \
				| jq -e '(.total_count | type == "number" and . >= 0 and . == floor) and (.workflow_runs | type == "array") and all(.workflow_runs[]; type == "object" and (.id | type == "number") and (.path | type == "string" and length > 0) and (.status | type == "string" and length > 0) and (.event | type == "string" and length > 0))' >/dev/null 2>&1; then
				__mt_runs_reason="malformed_page"
				break 2
			fi
			__mt_runs_read_before="${__mt_runs_read}"
			# Accumulate through stdin, never --argjson: 1,000 runs can exceed
			# the kernel's single-argument limit (MAX_ARG_STRLEN, 128 KiB).
			if ! __mt_runs_total="$(printf '%s' "${__mt_runs_page_json}" | jq -r '.total_count | floor' 2>/dev/null)" \
				|| ! __mt_runs_page_len="$(printf '%s' "${__mt_runs_page_json}" | jq -r '.workflow_runs | length' 2>/dev/null)" \
				|| ! __mt_runs_status_runs="$(printf '%s\n%s\n' "${__mt_runs_status_runs}" "${__mt_runs_page_json}" \
					| jq -cs '(.[0] + .[1].workflow_runs) | unique_by(.id)' 2>/dev/null)" \
				|| ! __mt_runs_read="$(printf '%s' "${__mt_runs_status_runs}" | jq -r 'length' 2>/dev/null)" \
				|| ! [[ "${__mt_runs_total}" =~ ^[0-9]+$ && "${__mt_runs_page_len}" =~ ^[0-9]+$ && "${__mt_runs_read}" =~ ^[0-9]+$ ]]; then
				__mt_runs_reason="malformed_page"
				break 2
			fi
			# One response is a consistent snapshot of its own query, so the
			# status is complete once a response holds everything it matched.
			# total_count belongs to this query alone (each follow-up query has
			# its own created bound), so it is never compared with the runs
			# accumulated across queries.
			if [ "${__mt_runs_page_len}" -ge "${__mt_runs_total}" ]; then
				break
			fi
			if [ "${__mt_runs_page_len}" -lt 100 ]; then
				__mt_runs_reason="listing_shifted"
				break 2
			fi
			if [ "${__mt_runs_read}" -le "${__mt_runs_read_before}" ]; then
				# The bound is inclusive, so more than 100 runs created in one
				# second return the same page forever.
				__mt_runs_reason="truncated"
				break 2
			fi
			if [ "${__mt_runs_page}" -ge "${__mt_runs_max_pages}" ]; then
				__mt_runs_reason="truncated"
				break 2
			fi
			# GitHub reports whole seconds today; a fractional created_at is
			# rounded up so the inclusive bound never drops the oldest second.
			if ! __mt_runs_created_bound="$(printf '%s' "${__mt_runs_page_json}" | jq -r '[.workflow_runs[].created_at | if type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$") then ((sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) + (if test("\\.[0-9]*[1-9][0-9]*Z$") then 1 else 0 end)) else error("malformed created_at") end] | min | todateiso8601' 2>/dev/null)" \
				|| ! [[ "${__mt_runs_created_bound}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
				__mt_runs_reason="malformed_page"
				break 2
			fi
			__mt_runs_page=$((__mt_runs_page + 1))
		done
		if ! __mt_runs_all="$(printf '%s\n%s\n' "${__mt_runs_all}" "${__mt_runs_status_runs}" | jq -cs '.[0] + .[1]' 2>/dev/null)"; then
			__mt_runs_reason="filter_failed"
			break
		fi
	done
	if [ -z "${__mt_runs_reason}" ]; then
		# One array of keys per review run. A review run with no key at all (no
		# non-empty head_branch and no PR-named dispatch title) could belong to
		# any queued PR, so the listing is incomplete (PR #5451, review of head fd3ad67).
		# A workflow_dispatch run is keyed only by its PR-named title: its
		# head_branch is the ref the workflow ran from (the default branch since
		# issue #4701), never the PR it reviews, so a dispatch without a
		# PR-named title (review_autofix.yml has no run-name and re-dispatches
		# itself) is unattributed (PR #5451 review round 2, AD-16).
		if ! __mt_runs_keyed="$(printf '%s' "${__mt_runs_all}" | jq -c '[.[]? | select((.path // "") | sub("@.*$"; "") | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$")) | if (.event // "") == "workflow_dispatch" then [(.display_title // "") | capture("^(Internal: AI Review & Autofix|AI Review) \\[pr:(?<pr>[1-9][0-9]*)\\]$")? | "pr:\(.pr)"] else [.head_branch | select(type == "string" and length > 0)] end]' 2>/dev/null)"; then
			__mt_runs_reason="filter_failed"
		elif ! printf '%s' "${__mt_runs_keyed}" | jq -e 'all(.[]; length > 0)' >/dev/null 2>&1; then
			__mt_runs_reason="unattributed_run"
		elif ! __mt_runs_keys="$(printf '%s' "${__mt_runs_keyed}" | jq -r '.[][]' 2>/dev/null)"; then
			__mt_runs_reason="filter_failed"
		fi
	fi
	if [ -n "${__mt_runs_reason}" ]; then
		echo "MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=${__mt_runs_reason} status=${__mt_runs_status} page=${__mt_runs_page} read=${__mt_runs_read} total=${__mt_runs_total}" >&2
		return 1
	fi
	printf '%s\n' "${__mt_runs_keys}" | sed '/^$/d' | sort -u
}

# Dispatch ref (security, issue #4701): the dispatch always runs the default
# branch's workflow file and passes only a validated PR number. Dispatching
# `--ref <PR head branch>` ran that branch's unmerged copy of the review
# workflow with `secrets: inherit` and write permissions (the finding issue
# #4618 fixed in review_autofix_sweep.yml). review_autofix.yml checks out the
# PR head from the PR's metadata either way. The review wrappers name the
# dispatched run for its PR, which _mt_inflight_review_branches keys as
# "pr:<N>". MERGE_TRAIN_DISPATCHED keeps its ref= field, now "default".
_mt_dispatch_review() {
	local pr="$1" head="$2" wf
	local allow_edits="${MERGE_TRAIN_ALLOW_WORKFLOW_EDITS:-true}"
	if ! [[ "${pr}" =~ ^[1-9][0-9]*$ ]]; then
		_mt_warn "merge-train release: invalid PR number '${pr}' for head ${head}; not dispatching."
		return 1
	fi
	for wf in ${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}; do
		if gh_retry gh workflow run "${wf}" --repo "${MT_REPO}" \
			-f pr_number="${pr}" -f allow_workflow_edits="${allow_edits}" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_DISPATCHED pr=${pr} workflow=${wf} ref=default head=${head}"
			return 0
		fi
	done
	return 1
}

_mt_release() {
	local base_filter="${BASE_BRANCH:-}" prs_json line num head base labels files blockers inflight_review_branches="" released=0 examined=0
	local release_queue_comment_id release_comment_body release_label_restored
	# The active-run listing is read once, lazily, the first time a queued PR
	# passes the base filter (issue #5443): "" = not read yet, "complete", or
	# "incomplete". An incomplete listing proves nothing about active reviews,
	# so every queued PR stays queued this invocation and the next close event
	# or poll tick retries.
	local release_runs_listing_state=""
	MT_EVAL_SOURCE="release"
	if ! prs_json="$(_mt_list_open_prs "")"; then
		_mt_warn "merge-train release: could not list open PRs; fail-open (nothing released)."
		return 0
	fi
	while IFS= read -r line; do
		[ -n "${line}" ] || continue
		num="$(printf '%s' "${line}" | jq -r '.number')"
		head="$(printf '%s' "${line}" | jq -r '.head')"
		base="$(printf '%s' "${line}" | jq -r '.base')"
		labels="$(printf '%s' "${line}" | jq -r '.labels | join(",")')"
		[[ "${num}" =~ ^[1-9][0-9]*$ ]] || continue
		_mt_has_label "${labels}" || continue
		if [ -n "${base_filter}" ] && [ "${base}" != "${base_filter}" ]; then
			continue
		fi
		examined=$((examined + 1))
		if [ -z "${release_runs_listing_state}" ]; then
			if inflight_review_branches="$(_mt_inflight_review_branches)"; then
				release_runs_listing_state="complete"
			else
				release_runs_listing_state="incomplete"
				inflight_review_branches=""
				_mt_warn "merge-train release: could not list every active review run; leaving queued PRs queued for the next close event or poll tick."
			fi
		fi
		if [ "${release_runs_listing_state}" != "complete" ]; then
			_mt_log "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=${num} head=${head} action=leave_queued"
			continue
		fi
		# An empty head is never a pattern: grep -Fx with "" matches a blank
		# line, and a run whose head_branch is "" prints one.
		if [ -n "${inflight_review_branches}" ] && printf '%s\n' "${inflight_review_branches}" | grep -Fxq -e "pr:${num}" ${head:+-e "${head}"}; then
			_mt_log "MERGE_TRAIN_RELEASE_ACTIVE pr=${num} head=${head} action=leave_queued"
			continue
		fi
		files=""
		if ! _mt_pr_files_into files "${num}"; then
			_mt_warn "merge-train release: could not list files for queued PR #${num}; leaving it queued."
			continue
		fi
		blockers=""
		if ! _mt_blockers_for_into blockers "${num}" "${base}" "${files}" "${prs_json}"; then
			_mt_warn "merge-train release: could not evaluate blockers for PR #${num}; leaving it queued."
			continue
		fi
		if [ -n "${blockers}" ]; then
			_mt_log "MERGE_TRAIN_STILL_QUEUED pr=${num} blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -)"
			continue
		fi
		release_queue_comment_id=""
		if ! _mt_resolve_automation_login || ! release_queue_comment_id="$(_mt_find_marker_comment_id "${num}" "${MT_MARKER}")"; then
			_mt_warn "merge-train release: could not inspect the queue marker for PR #${num}; leaving it queued to avoid arming a false bypass."
			continue
		fi
		if [[ "${release_queue_comment_id}" =~ ^[0-9]+$ ]] && \
		   ! _mt_replace_comment "${release_queue_comment_id}" "${MT_RETIRED_MARKER}
**Merge train release claimed.** The train found no older overlapping PRs and is dispatching review."; then
			_mt_warn "merge-train release: could not retire the queue marker for PR #${num}; leaving it queued."
			continue
		fi
		# Claim this release by removing the queue label. Concurrent release
		# invocations cannot both claim it. The queued marker is retired first so
		# an automation-owned label removal can never look like a human bypass.
		if ! gh_retry gh api -X DELETE "repos/${MT_REPO}/issues/${num}/labels/$(printf '%s' "${MT_LABEL}" | jq -sRr @uri)" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_RELEASE_CLAIM_SKIPPED pr=${num} action=leave_to_peer"
			continue
		fi
		if _mt_dispatch_review "${num}" "${head}"; then
			release_comment_body="${MT_RELEASED_MARKER}
**Merge train released.** Every older PR that edited the same files has merged or closed; the review/autofix run was re-dispatched for \`${head}\`."
			_mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" "${release_comment_body}" \
				|| _mt_warn "merge-train release: could not upsert the released comment for PR #${num}; continuing."
			released=$((released + 1))
			_mt_log "MERGE_TRAIN_RELEASED pr=${num} source=release"
		else
			release_label_restored="false"
			if gh_retry gh api -X POST "repos/${MT_REPO}/issues/${num}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1; then
				release_label_restored="true"
			else
				_mt_warn "merge-train release: dispatch and ${MT_LABEL} restoration both failed for PR #${num}; a later PR event must re-evaluate it."
			fi
			if [[ "${release_queue_comment_id}" =~ ^[0-9]+$ ]] && [ "${release_label_restored}" = "true" ]; then
				_mt_replace_comment "${release_queue_comment_id}" "${MT_MARKER}
**Review queued (merge train).** Every older overlapping PR has closed, but the review workflow dispatch failed. Automatic release will retry on the next close event or poll tick." \
					|| _mt_warn "merge-train release: could not restore the queue marker for PR #${num}; the queue label remains authoritative."
			fi
			if [ "${release_label_restored}" = "true" ]; then
				_mt_warn "merge-train release: PR #${num} unblocked but the review workflow could not be dispatched; ${MT_LABEL} was restored for the next close event or poll tick."
			else
				_mt_warn "merge-train release: PR #${num} remains unlabelled after dispatch and label restoration failed; its retired marker cannot be mistaken for a human bypass."
			fi
		fi
	done < <(printf '%s\n' "${prs_json}")
	_mt_log "MERGE_TRAIN_RELEASE_SUMMARY examined=${examined} released=${released} base_filter=${base_filter:-*}"
	return 0
}

case "${MT_SUBCOMMAND}" in
	gate) _mt_gate ;;
	release) _mt_release ;;
	*)
		echo "usage: review_merge_train.sh gate|release" >&2
		exit 2
		;;
esac
