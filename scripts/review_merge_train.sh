#!/usr/bin/env bash
# Signed job-token comments (run-volume plan item 15): route this script's
# gh comment calls through the shim even when no earlier step loaded it.
[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true
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
#   MERGE_TRAIN_IGNORE_PATHS                default .ai/.workspace_source_manifest.txt;
#                                           exact paths separated by comma/newline, none disables
#   MERGE_TRAIN_LABEL                      default ai:merge-queued
#   MERGE_TRAIN_HEAD_REF_PREFIX            default ai/issue-
#   MERGE_TRAIN_ALLOW_WORKFLOW_EDITS       default true; forwarded on dispatch
#   MERGE_TRAIN_CONFLICT_CHECK_ENABLED     default true (true/1/yes/on); other = off
#   MERGE_TRAIN_HEAD_MAX_AGE_HOURS         default 6; 0 disables the age bypass;
#                                          a non-integer warns and falls back to 6
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
#   MERGE_TRAIN_STALE_UNATTRIBUTED_RUN_HOURS default 6; 0 = off. Age after which an
#                                          untitled, not-started review dispatch stops
#                                          making the active-run listing incomplete
#                                          (_mt_inflight_review_branches)
#   MERGE_TRAIN_STALL_ALERT_HOURS          default 24; 0 = off. Age of the oldest
#                                          still-queued PR that triggers the daily
#                                          queue-age note (_mt_release_stall_alert)
#   MERGE_TRAIN_STALL_ALERT_HOUR_UTC       default 1 (09:00 SGT); first UTC hour of the
#                                          3-hour slot in which the note may be sent
#   MERGE_TRAIN_TG_HELPERS                 path of scripts/tg_helpers.sh from the trusted
#                                          support checkout; unset = no queue-age note
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID, ALERT_MSG_LEVEL   scripts/tg_helpers.sh
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
# Merge lease and final-CI merge resume (scripts/pr_checks_lib.sh), used by
# the release step only. Best-effort: an older support checkout without the
# library skips both, and the release behaves as before.
MT_PR_CHECKS_LIB_LOADED=false
if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/pr_checks_lib.sh" ]; then
	# shellcheck disable=SC1091
	source "${SUPPORT_SCRIPTS_DIR}/pr_checks_lib.sh" 2>/dev/null && MT_PR_CHECKS_LIB_LOADED=true
elif [ -f "$(dirname "${BASH_SOURCE[0]}")/pr_checks_lib.sh" ]; then
	# shellcheck disable=SC1091
	source "$(dirname "${BASH_SOURCE[0]}")/pr_checks_lib.sh" 2>/dev/null && MT_PR_CHECKS_LIB_LOADED=true
fi
type _merge_lease_gate >/dev/null 2>&1 || MT_PR_CHECKS_LIB_LOADED=false

MT_SUBCOMMAND="${1:-}"
MT_ENABLED="${MERGE_TRAIN_ENABLED:-true}"
MT_LABEL="${MERGE_TRAIN_LABEL:-ai:merge-queued}"
MT_PREFIX="${MERGE_TRAIN_HEAD_REF_PREFIX:-ai/issue-}"
MT_MAX_OLDER="${MERGE_TRAIN_MAX_OLDER_PRS:-20}"
MT_IGNORE_RAW="${MERGE_TRAIN_IGNORE_PATHS-.ai/.workspace_source_manifest.txt}"
MT_IGNORE_PATHS=""
MT_REPO="${GITHUB_REPOSITORY:-}"
MT_MARKER="<!-- merge-train:queued -->"
MT_RELEASED_MARKER="<!-- merge-train:released -->"
MT_BYPASSED_MARKER="<!-- merge-train:bypassed -->"
MT_RETIRED_MARKER="<!-- merge-train:queue-retired -->"
# Queue-age note (release, _mt_release_stall_alert): the full first line is
# "<!-- merge-train:stalled alert_date=YYYY-MM-DD -->".
MT_STALLED_MARKER_PREFIX="<!-- merge-train:stalled"
MT_AUTOMATION_LOGIN=""
[[ "${MT_MAX_OLDER}" =~ ^[0-9]+$ ]] || MT_MAX_OLDER=20

# Exact paths only. The empty string and the repo-variable sentinel 'none'
# deliberately retain legacy overlap behavior.
if [ "$(printf '%s' "${MT_IGNORE_RAW}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" != "none" ]; then
	while IFS= read -r mt_ignored_path; do
		mt_ignored_path="$(printf '%s' "${mt_ignored_path}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
		[ -n "${mt_ignored_path}" ] || continue
		case "${mt_ignored_path}" in
			*'*'*|*'?'*|*'['*) _mt_warn "merge-train: ignoring glob entry in MERGE_TRAIN_IGNORE_PATHS: ${mt_ignored_path}"; continue ;;
		esac
		MT_IGNORE_PATHS+="${mt_ignored_path}"$'\n'
	done < <(printf '%s\n' "${MT_IGNORE_RAW}" | tr ',' '\n')
fi
MT_IGNORE_PATHS="${MT_IGNORE_PATHS%$'\n'}"

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
MT_HEAD_MAX_AGE_HOURS="${MERGE_TRAIN_HEAD_MAX_AGE_HOURS:-6}"
if ! [[ "${MT_HEAD_MAX_AGE_HOURS}" =~ ^[0-9]{1,6}$ ]]; then
	_mt_warn "review_merge_train.sh: MERGE_TRAIN_HEAD_MAX_AGE_HOURS='${MT_HEAD_MAX_AGE_HOURS}' is not a whole number of hours; using 6."
	MT_HEAD_MAX_AGE_HOURS=6
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
# A renamed file contributes both its new path (`filename`) and its old path
# (`previous_filename`, when a non-empty string), matching the both-sides
# parse in _mt_own_files_into. Otherwise a queued PR editing the old path of
# an older PR's rename would not be seen as overlapping (fail-open).
# An entry that is not an object, or whose filename is missing, empty or not
# a string, contributes no path (it no longer fails the whole lookup or
# yields a literal "null" path).
#
# API budget: one paginated `GET /pulls/{n}/files` per distinct PR per run.
declare -A _MT_FILES_CACHE=()
# API-cost counters for MERGE_TRAIN_RELEASE_SUMMARY only (file_list_fetches=,
# file_list_cache_hits=); _mt_release resets them at the start of each pass.
# A fetch counts once per logical lookup (gh_retry retries are not counted
# separately) and is counted even when the call fails. They never feed back
# into any decision.
_MT_FILE_LIST_FETCHES=0
_MT_FILE_LIST_CACHE_HITS=0
_mt_pr_files_into() {
	local __mt_files_dest="$1" __mt_files_pr="$2" __mt_files_out=""
	if [ -n "${_MT_FILES_CACHE[${__mt_files_pr}]+x}" ]; then
		_MT_FILE_LIST_CACHE_HITS=$((_MT_FILE_LIST_CACHE_HITS + 1))
		printf -v "${__mt_files_dest}" '%s' "${_MT_FILES_CACHE[${__mt_files_pr}]}"
		return 0
	fi
	_MT_FILE_LIST_FETCHES=$((_MT_FILE_LIST_FETCHES + 1))
	if ! __mt_files_out="$(gh_retry gh api --paginate "repos/${MT_REPO}/pulls/${__mt_files_pr}/files?per_page=100" --jq '.[] | select(type == "object") | (.filename | select(type == "string" and length > 0)), (.previous_filename | select(type == "string" and length > 0))' 2>/dev/null)"; then
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
_mt_drop_ignored() {
	if [ -z "${MT_IGNORE_PATHS}" ]; then
		printf '%s\n' "$1"
	else
		printf '%s\n' "$1" | grep -Fxv -f <(printf '%s\n' "${MT_IGNORE_PATHS}") || true
	fi
}

_mt_ignored_in() {
	[ -n "${MT_IGNORE_PATHS}" ] || return 0
	printf '%s\n' "$1" | grep -Fx -f <(printf '%s\n' "${MT_IGNORE_PATHS}") | sort -u || true
}

_mt_intersect() {
	comm -12 <(_mt_drop_ignored "$1" | sed '/^$/d' | sort -u) <(_mt_drop_ignored "$2" | sed '/^$/d' | sort -u)
}

# Open PRs, oldest first, one compact JSON object per line:
# {number, head, head_repo, head_sha, created_at, base, draft, labels[], title}.
# title (merge-lease Phase 2) is author text: read only through jq, never
# logged; the lease tick matches `[skip ai]` in it as a fixed string.
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
		--jq '.[] | {number: .number, head: .head.ref, head_repo: (.head.repo.full_name // ""), head_sha: (.head.sha // ""), created_at: (.created_at // ""), base: .base.ref, draft: .draft, labels: [.labels[].name], title: (.title // "")} | tojson' 2>/dev/null
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
				none|ignored)
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
# reports a content conflict; _MT_PROBE_PATHS lists the paths), ignored (every
# conflicted path is in MERGE_TRAIN_IGNORE_PATHS; does not block) or unknown
# (any other outcome). Runs in the caller's shell so its caches persist.
# Missing heads are fetched by SHA with --no-write-fetch-head (FETCH_HEAD for
# later steps is untouched) and --depth=200 only in an already-shallow clone,
# so a full clone is never made shallow. merge-tree writes only objects; it
# runs no hooks and no PR-supplied merge driver. No API calls.
#
# Outside a git checkout (the `release` callers in orchestrate_poll.yml and
# cancel_on_pr_close.yml run before or without one), the probe uses a private
# bare repository under RUNNER_TEMP (or TMPDIR) whose origin is
# ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}.git, fetches the two heads by SHA
# with --depth=200, and removes the repository when the script exits. GH_TOKEN
# (or GITHUB_TOKEN) authenticates the fetch through GIT_CONFIG_* environment
# entries, never argv or a config file. Without that fallback every probe in
# those callers returned `unknown` and kept the blocker. Logs
# MERGE_TRAIN_PROBE_REPO outcome=created|unavailable once per run.
declare -A _MT_FETCHED_SHA=()
_MT_GIT_PROBE_OK=""
_MT_PROBE_RESULT="unknown"
_MT_PROBE_PATHS=""
_MT_PROBE_GIT_DIR=""

# git in the probe repository when one was created, else in the cwd checkout.
_mt_git()
{
	if [ -n "${_MT_PROBE_GIT_DIR}" ]; then
		git --git-dir="${_MT_PROBE_GIT_DIR}" "$@"
	else
		git "$@"
	fi
}

# `_mt_git_fetch <seconds> <fetch args...>`: git fetch under `timeout`, in the
# probe repository when one exists. For an https origin the token rides as an
# HTTP extraheader through GIT_CONFIG_* variables exported in a subshell, so it
# never reaches argv (ps) or a config file.
_mt_git_fetch()
{
	local fetch_timeout="$1"
	shift
	(
		local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}" server="${GITHUB_SERVER_URL:-https://github.com}" basic
		local -a git_dir_args=()
		if [ -n "${_MT_PROBE_GIT_DIR}" ]; then
			git_dir_args=(--git-dir="${_MT_PROBE_GIT_DIR}")
			if [ -n "${token}" ] && [[ "${server}" =~ ^https:// ]]; then
				basic="$(printf 'x-access-token:%s' "${token}" | base64 | tr -d '\n')"
				export GIT_CONFIG_COUNT=1
				export GIT_CONFIG_KEY_0="http.${server%/}/.extraheader"
				export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic}"
			fi
		fi
		GIT_TERMINAL_PROMPT=0 timeout "${fetch_timeout}" git ${git_dir_args[@]+"${git_dir_args[@]}"} fetch "$@"
	)
}

_mt_probe_repo_cleanup()
{
	if [ -n "${_MT_PROBE_GIT_DIR}" ] && [[ "${_MT_PROBE_GIT_DIR}" == */merge-train-probe.* ]]; then
		rm -rf -- "${_MT_PROBE_GIT_DIR}"
	fi
}

# Create the private probe repository; returns 1 (probe stays unknown) when
# the repository slug is unusable or git cannot create it.
_mt_probe_repo_init()
{
	local server="${GITHUB_SERVER_URL:-https://github.com}" probe_dir
	if ! [[ "${MT_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		_mt_log "MERGE_TRAIN_PROBE_REPO outcome=unavailable reason=invalid_repository"
		return 1
	fi
	if ! probe_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/merge-train-probe.XXXXXX" 2>/dev/null)" \
		|| ! git init -q --bare "${probe_dir}" >/dev/null 2>&1 \
		|| ! git --git-dir="${probe_dir}" remote add origin "${server%/}/${MT_REPO}.git" >/dev/null 2>&1; then
		[ -n "${probe_dir:-}" ] && rm -rf -- "${probe_dir}"
		_mt_log "MERGE_TRAIN_PROBE_REPO outcome=unavailable reason=init_failed"
		return 1
	fi
	_MT_PROBE_GIT_DIR="${probe_dir}"
	trap _mt_probe_repo_cleanup EXIT
	_mt_log "MERGE_TRAIN_PROBE_REPO outcome=created origin=${server%/}/${MT_REPO}.git"
	return 0
}
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
		elif ! git rev-parse --git-dir >/dev/null 2>&1 && _mt_probe_repo_init; then
			# No checkout here: probe in the private repository instead.
			_MT_GIT_PROBE_OK="yes"
		else
			_MT_GIT_PROBE_OK="no"
		fi
	fi
	[ "${_MT_GIT_PROBE_OK}" = "yes" ] || return 0
	for probe_sha in "${own_sha}" "${other_sha}"; do
		if ! _mt_git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
			[ "${_MT_FETCHED_SHA[${probe_sha}]:-}" = "failed" ] && return 0
			probe_missing+=("${probe_sha}")
		fi
	done
	if [ "${#probe_missing[@]}" -gt 0 ]; then
		# The private probe repository starts empty: fetch shallow there too.
		if [ -n "${_MT_PROBE_GIT_DIR}" ] || [ "$(git rev-parse --is-shallow-repository 2>/dev/null || true)" = "true" ]; then
			probe_depth=(--depth=200)
		fi
		if ! _mt_git_fetch 120 --no-tags --quiet --no-write-fetch-head \
			${probe_depth[@]+"${probe_depth[@]}"} origin "${probe_missing[@]}" >/dev/null 2>&1; then
			for probe_sha in "${probe_missing[@]}"; do
				_MT_FETCHED_SHA[${probe_sha}]="failed"
			done
			return 0
		fi
		for probe_sha in "${probe_missing[@]}"; do
			if _mt_git cat-file -e "${probe_sha}^{commit}" 2>/dev/null; then
				_MT_FETCHED_SHA[${probe_sha}]="ok"
			else
				_MT_FETCHED_SHA[${probe_sha}]="failed"
				return 0
			fi
		done
	fi
	probe_out="$(_mt_git merge-tree --write-tree --name-only --no-messages "${own_sha}" "${other_sha}" 2>/dev/null)" || probe_rc=$?
	case "${probe_rc}" in
		0) _MT_PROBE_RESULT="none" ;;
		1)
			_MT_PROBE_RESULT="conflict"
			_MT_PROBE_PATHS="$(printf '%s\n' "${probe_out}" | sed '1d' | sed '/^$/d' | sort -u)"
			# Conflicts only in MERGE_TRAIN_IGNORE_PATHS (the generated
			# workspace manifest by default) do not block: the path-overlap
			# rule already ignores those files, and the later merge
			# regenerates them.
			if [ -n "${_MT_PROBE_PATHS}" ] && [ -z "$(_mt_drop_ignored "${_MT_PROBE_PATHS}" | sed '/^$/d')" ]; then
				_MT_PROBE_RESULT="ignored"
			fi
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
# Returns the status of the write it made: 0 when no write was needed or the
# PATCH/POST succeeded, the failed gh status otherwise, and 1 when the marker
# lookup failed. Callers handle a non-zero status (warn and continue).
_mt_upsert_comment() {
	local pr="$1" marker="$2" body="$3" existing_id="" existing_body write_rc=0
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
		gh_retry gh api -X PATCH "repos/${MT_REPO}/issues/comments/${existing_id}" -f body="${body}" >/dev/null 2>&1 || write_rc=$?
		return "${write_rc}"
	fi
	gh_retry gh api -X POST "repos/${MT_REPO}/issues/${pr}/comments" -f body="${body}" >/dev/null 2>&1 || write_rc=$?
	return "${write_rc}"
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
	local own_files="" prs_json blockers="" ignored_csv="none"
	if [ "${IS_SMOKE_TEST:-}" != "true" ]; then
		if ! _mt_own_files_into own_files "${pr}"; then
			_mt_warn "merge-train gate: could not list changed files for PR #${pr}; fail-open (not queued)."
			return 0
		fi
		if [ -z "${own_files}" ]; then
			_mt_log "MERGE_TRAIN_GATE pr=${pr} result=no_changed_files action=continue"
			return 0
		fi
		ignored_csv="$( _mt_ignored_in "${own_files}" | paste -sd, - )"
		ignored_csv="${ignored_csv:-none}"
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
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=unblocked action=continue ignored=${ignored_csv}"
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
				_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=bypassed blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -) action=continue ignored=${ignored_csv}"
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
	_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=queued blockers=${blocker_numbers// /,} action=soft_exit ignored=${ignored_csv}"
	_mt_ensure_label
	queue_label_persisted="true"
	if ! _mt_has_label "${own_labels}"; then
		queue_label_persisted="false"
		if GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${MT_REPO}/issues/${pr}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1; then
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

Set the repository variable \`MERGE_TRAIN_ENABLED=false\` to disable the train, or remove the \`${MT_LABEL}\` label and re-run the review to bypass it once." "${queued_comment_id}" \
			|| _mt_warn "merge-train gate: could not upsert the queued comment for PR #${pr}; continuing."
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
# Provenance (issue #6629, re-issue of #5152): a workflow_dispatch run's
# title comes from the dispatched ref's workflow file, so anyone who can push
# a branch copy of a wrapper can dispatch a same-named run from that branch.
# A workflow_dispatch review run is therefore keyed "pr:<N>" only when it is
# trusted: head_branch is the default branch (read once by
# _mt_resolve_default_branch, no fallback) or null, and the title is paired
# with its own wrapper path (".github/workflows/internal-review.yml" with
# "Internal: AI Review & Autofix [pr:<N>]", ".github/workflows/ai-review.yml"
# with "AI Review [pr:<N>]", after stripping "@<ref>" and a leading
# "<this repo>/" prefix). A dispatch run on any other non-empty branch, or a
# PR-named one whose title and path do not pair, is dropped: it is not a
# trusted review run, and counting it as unattributed would let a spoof hold
# every release (Q3). An untitled dispatch on the default branch (or null)
# is still unattributed (review_autofix.yml self-dispatches).
#
# Stale unattributed runs: GitHub sometimes accepts a dispatch and never
# starts it. The run then stays in a not-started status with no jobs and a
# title that was never rendered (coding-workflows run 41814678900, queued
# from 2026-11-04: every release pass ended reason=unattributed_run for six
# days and 16 PRs stayed queued). Such a run is not an in-flight review, so
# an untitled default-branch dispatch whose own status is queued, requested,
# pending or waiting and whose created_at is more than
# MERGE_TRAIN_STALE_UNATTRIBUTED_RUN_HOURS old (default 6, the longest a
# GitHub-hosted job can run; 0 turns the rule off; anything that is not a
# whole number of at most 4 digits warns and uses 6) is dropped instead of
# making the listing incomplete, and logged once on stderr:
#   MERGE_TRAIN_RUNS_LISTING outcome=ignored reason=stale_unattributed_run runs=<n> ids=<id,...> max_age_hours=<h>
# An in_progress run, a younger run, or one with a missing or malformed
# created_at is still unattributed. If a dropped run does start later, the
# release may have dispatched a second review of the same PR; the review
# workflows' per-PR concurrency groups keep the two from running together.
#
# Input:     none (MT_REPO).
# Output:    one key per line on stdout, sorted and unique: the head branch
#            of each active review run that is not a workflow_dispatch run,
#            and "pr:<N>" for a trusted workflow_dispatch run named for PR <N>.
#            Every run an active-status query returned counts as active,
#            whatever its own status field says.
# Returns:   0 = the listing is complete: a PR with no key has no active run.
#            1 = the listing is incomplete. Stdout carries nothing; the caller
#            must not release on it, and the next invocation retries.
# API calls: one `GET repos/<repo>` for the default branch, before any
#            listing call (_mt_resolve_default_branch), then
#            one `GET actions/runs?status=<s>&per_page=100&page=1` for each
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
#            An unresolvable default branch is incomplete too
#            (reason=default_branch_unavailable status=none), reported before
#            any actions/runs call.
#            MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<default_branch_unavailable|page_failed|malformed_page|listing_shifted|truncated|unattributed_run|filter_failed> status=<s> page=<p> read=<n> total=<n>
#            A shifted page is first re-read up to
#            MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRIES times per listing (default 2,
#            0-5; MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRY_SLEEP seconds apart,
#            default 2), each logged as
#            MERGE_TRAIN_RUNS_LISTING outcome=retry reason=listing_shifted ... attempt=<n>/<max>
#            A first-page re-read whose union of runs read so far covers the
#            latest total_count is complete, logged as
#            MERGE_TRAIN_RUNS_LISTING outcome=accepted reason=union_covers_total status=<s> page=1 read=<n> total=<n>
# The repository's default branch for review-run provenance (issue #6629).
# One `GET repos/<repo>` per process; no fallback, so a failed read is never
# mistaken for "main". Output-variable API: `_mt_resolve_default_branch
# <varname>` assigns the branch and returns 0, or returns 1. Runs in the
# caller's shell (not $(...)) so MT_DEFAULT_BRANCH memoizes the result.
MT_DEFAULT_BRANCH=""
_mt_resolve_default_branch()
{
	local __mt_db_dest="$1" __mt_db_value=""
	if [ -z "${MT_DEFAULT_BRANCH}" ]; then
		__mt_db_value="$(gh_retry gh api -X GET "repos/${MT_REPO}" --jq '.default_branch' 2>/dev/null)" || return 1
		if ! [[ "${__mt_db_value}" =~ ^[A-Za-z0-9._/-]+$ ]] || [ "${__mt_db_value}" = "null" ]; then
			return 1
		fi
		MT_DEFAULT_BRANCH="${__mt_db_value}"
	fi
	printf -v "${__mt_db_dest}" '%s' "${MT_DEFAULT_BRANCH}"
}

_mt_inflight_review_branches()
{
	local __mt_runs_max_pages=10 __mt_runs_status="" __mt_runs_page=0 __mt_runs_reason=""
	local __mt_runs_default_branch=""
	if ! _mt_resolve_default_branch __mt_runs_default_branch; then
		_mt_warn "merge-train release: could not resolve the default branch; review dispatch runs cannot be verified." >&2
		echo "MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=default_branch_unavailable status=none page=0 read=0 total=0" >&2
		return 1
	fi
	local __mt_runs_page_json="" __mt_runs_page_len=0 __mt_runs_total=0 __mt_runs_read=0 __mt_runs_read_before=0
	local __mt_runs_query="" __mt_runs_created_bound=""
	local __mt_runs_status_runs='[]' __mt_runs_all='[]' __mt_runs_keys="" __mt_runs_keyed='[]'
	local __mt_runs_classified='{}' __mt_runs_stale_ids="" __mt_runs_stale_count=0 __mt_runs_now_epoch=0
	local __mt_runs_stale_hours="${MERGE_TRAIN_STALE_UNATTRIBUTED_RUN_HOURS:-6}"
	if ! [[ "${__mt_runs_stale_hours}" =~ ^[0-9]{1,4}$ ]]; then
		_mt_warn "merge-train release: MERGE_TRAIN_STALE_UNATTRIBUTED_RUN_HOURS is not a whole number of hours; using 6." >&2
		__mt_runs_stale_hours=6
	fi
	__mt_runs_stale_hours=$((10#${__mt_runs_stale_hours}))
	__mt_runs_now_epoch="$(date -u +%s 2>/dev/null)" || __mt_runs_now_epoch=0
	[[ "${__mt_runs_now_epoch}" =~ ^[0-9]+$ ]] || __mt_runs_now_epoch=0
	local __mt_runs_shift_retries=0 __mt_runs_shift_retry_max="${MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRIES:-2}" __mt_runs_shift_sleep="${MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRY_SLEEP:-2}"
	[[ "${__mt_runs_shift_retry_max}" =~ ^[0-5]$ ]] || __mt_runs_shift_retry_max=2
	[[ "${__mt_runs_shift_sleep}" =~ ^[0-9]{1,2}$ ]] || __mt_runs_shift_sleep=2
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
				# Re-reads of the same first-page query accumulate in the union;
				# once it holds at least as many runs as the latest total_count
				# the listing is accepted as a best-effort cover. This is a
				# count, not proof: a run that left the status since stays in
				# the union and can mask a live run no read returned, so the
				# release may re-dispatch a review that is already in flight
				# (bounded by the second status pass and the pr-autofix
				# concurrency group; accepted trade-off, Q59). GitHub's count and its listing
				# disagree for several seconds while runs churn, so three reads
				# 2 s apart all came back short (run 38043169762, Q59). Later
				# pages carry their own created bound, so this is page 1 only.
				if [ "${__mt_runs_page}" -eq 1 ] && [ "${__mt_runs_read}" -ge "${__mt_runs_total}" ]; then
					echo "MERGE_TRAIN_RUNS_LISTING outcome=accepted reason=union_covers_total status=${__mt_runs_status} page=${__mt_runs_page} read=${__mt_runs_read} total=${__mt_runs_total}" >&2
					break
				fi
				# Runs changed status between GitHub's count and its listing. One
				# run starting mid-read used to abort the whole release tick
				# (run 38003551535); re-read the same query a bounded number of
				# times (shared by every status of this listing) first. Runs
				# already read stay in the union, so a re-read only adds runs.
				if [ "${__mt_runs_shift_retries}" -lt "${__mt_runs_shift_retry_max}" ]; then
					__mt_runs_shift_retries=$((__mt_runs_shift_retries + 1))
					echo "MERGE_TRAIN_RUNS_LISTING outcome=retry reason=listing_shifted status=${__mt_runs_status} page=${__mt_runs_page} read=${__mt_runs_read} total=${__mt_runs_total} attempt=${__mt_runs_shift_retries}/${__mt_runs_shift_retry_max}" >&2
					sleep "${__mt_runs_shift_sleep}"
					continue
				fi
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
		# Issue #6629: only a trusted dispatch run (see the header) gets its
		# "pr:<N>" key; a dispatch on another non-empty branch, or a PR-named
		# one whose title and path do not pair, is dropped (no key, not counted).
		# The filter classifies each review run once: {k: [keys]} for a run that
		# is counted (an empty list is an unattributed run) or {s: id} for a
		# stale unattributed run that is dropped (see the header). A clock that
		# could not be read ($now 0) turns the stale rule off.
		if ! __mt_runs_classified="$(printf '%s' "${__mt_runs_all}" | jq -c --arg db "${__mt_runs_default_branch}" --arg repo "${MT_REPO}" \
			--argjson now "${__mt_runs_now_epoch}" --argjson stale_secs "$((__mt_runs_stale_hours * 3600))" '
			def mt_review_path:
				((.path // "") | if type == "string" then . else "" end | sub("@.*$"; "")) as $p
				| if $repo != "" and ($p | ascii_downcase | startswith(($repo | ascii_downcase) + "/"))
					then $p[(($repo | length) + 1):]
					else $p
					end;
			def mt_stale_unattributed:
				$stale_secs > 0 and $now > 0
				and ((.status // "") as $s | $s == "queued" or $s == "requested" or $s == "pending" or $s == "waiting")
				and (.created_at
					| if type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$")
						then ((sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) <= ($now - $stale_secs))
						else false
						end);
			[.[]?
			| select((.path // "") | sub("@.*$"; "") | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$"))
			| if (.event // "") == "workflow_dispatch" then
				((.display_title // "") | if type == "string" then . else "" end) as $t
				| [$t | capture("^(Internal: AI Review & Autofix|AI Review) \\[pr:(?<pr>[1-9][0-9]*)\\]$")? | .pr] as $prs
				| if ((.head_branch | type) == "string" and .head_branch != "" and .head_branch != $db) then empty
					elif ($prs | length) == 0 then (if mt_stale_unattributed then {s: .id} else {k: []} end)
					elif (mt_review_path == ".github/workflows/internal-review.yml" and $t == ("Internal: AI Review & Autofix [pr:" + $prs[0] + "]"))
						or (mt_review_path == ".github/workflows/ai-review.yml" and $t == ("AI Review [pr:" + $prs[0] + "]")) then {k: ["pr:" + $prs[0]]}
					else empty
					end
				else {k: [.head_branch | select(type == "string" and length > 0)]}
				end] as $m
			| {keyed: [$m[] | select(has("k")) | .k], stale: ([$m[] | select(has("s")) | .s] | unique)}' 2>/dev/null)" \
			|| ! __mt_runs_keyed="$(printf '%s' "${__mt_runs_classified}" | jq -c '.keyed' 2>/dev/null)" \
			|| ! __mt_runs_stale_ids="$(printf '%s' "${__mt_runs_classified}" | jq -r '.stale | map(tostring) | join(",")' 2>/dev/null)" \
			|| ! __mt_runs_stale_count="$(printf '%s' "${__mt_runs_classified}" | jq -r '.stale | length' 2>/dev/null)"; then
			__mt_runs_reason="filter_failed"
		elif ! printf '%s' "${__mt_runs_keyed}" | jq -e 'all(.[]; length > 0)' >/dev/null 2>&1; then
			__mt_runs_reason="unattributed_run"
		elif ! __mt_runs_keys="$(printf '%s' "${__mt_runs_keyed}" | jq -r '.[][]' 2>/dev/null)"; then
			__mt_runs_reason="filter_failed"
		fi
	fi
	if [ -n "${__mt_runs_stale_ids}" ] && [ "${__mt_runs_reason}" != "filter_failed" ]; then
		echo "MERGE_TRAIN_RUNS_LISTING outcome=ignored reason=stale_unattributed_run runs=${__mt_runs_stale_count} ids=${__mt_runs_stale_ids} max_age_hours=${__mt_runs_stale_hours}" >&2
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
	# SHELL-001: an unquoted expansion of the list also glob-expanded items
	# such as `*.yml` against the working directory. `read -a` splits on the
	# default IFS (spaces, tabs, newlines; -d '' keeps every line) and never
	# does pathname expansion, so global `set -f` is not needed. `|| true`:
	# read -d '' returns 1 at end of input under set -e.
	local -a mt_dispatch_workflow_list=()
	IFS=$' \t\n' read -r -d '' -a mt_dispatch_workflow_list \
		<<< "${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}" || true
	for wf in "${mt_dispatch_workflow_list[@]}"; do
		if gh_retry gh workflow run "${wf}" --repo "${MT_REPO}" \
			-f pr_number="${pr}" -f allow_workflow_edits="${allow_edits}" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_DISPATCHED pr=${pr} workflow=${wf} ref=default head=${head}"
			return 0
		fi
	done
	return 1
}

# Queue-age note: one Telegram WARNING a day while the oldest still-queued PR
# is older than MERGE_TRAIN_STALL_ALERT_HOURS (default 24, 0 = off).
#
# Why: from 2026-11-04 the coding-workflows train released nothing for six
# days (see "Stale unattributed runs" above) and nothing reported it. The note
# states a fact, how long the oldest queued PR has been open and what the last
# pass counted, not a diagnosis: one incomplete listing is routine, so a tick's
# own result cannot tell a stall from churn.
#
# Input:     $1 PR number of the oldest still-queued PR, $2 its age in
#            seconds, $3 PRs still queued after this pass, $4 its counters as text.
# Called by: _mt_release on poll ticks only (no BASE_BRANCH filter), so a PR
#            close event never sends it.
# Once a day: sent only in the 3-hour UTC slot that starts at
#            MERGE_TRAIN_STALL_ALERT_HOUR_UTC (default 1), and only when the
#            PR has no automation-authored note for today. The note is one
#            PR comment whose first line is
#            "<!-- merge-train:stalled alert_date=YYYY-MM-DD -->", updated in
#            place each day. It is written before Telegram is called, so a
#            failed write sends nothing and a failed send is not repeated
#            that day.
# API calls: none unless the age, the slot, the Telegram helpers and the alert
#            level all allow a send. Then one `GET user` (cached per process,
#            _mt_resolve_automation_login), one paginated comments list of
#            that PR and one comment PATCH or POST. The open-PR list and the
#            active-run listing this pass already read carry no comments, so
#            neither can answer "was today's note sent".
# Fail-open: every failure logs its outcome and returns 0; no release, label
#            or dispatch decision depends on it.
# Log:       MERGE_TRAIN_STALL_ALERT pr=<n> age_secs=<n> queued=<n> outcome=<sent|not_delivered|already_alerted|outside_slot|telegram_unavailable|telegram_unconfigured|below_alert_level|marker_unavailable|marker_write_failed>
_mt_release_stall_alert()
{
	local stall_pr="$1" stall_age_secs="$2" stall_examined="$3" stall_detail="$4"
	local stall_hours="${MERGE_TRAIN_STALL_ALERT_HOURS:-24}" stall_slot_hour="${MERGE_TRAIN_STALL_ALERT_HOUR_UTC:-1}"
	local stall_now_epoch="" stall_now_hour="" stall_today="" stall_rows="" stall_row="" stall_comment_id="" stall_comment_created="" stall_last_date=""
	local stall_body="" stall_msg="" stall_msg_id="" stall_age_hours=0 stall_filter="" stall_outcome=""
	if ! [[ "${stall_hours}" =~ ^[0-9]{1,4}$ ]]; then
		_mt_warn "merge-train release: MERGE_TRAIN_STALL_ALERT_HOURS is not a whole number of hours; using 24."
		stall_hours=24
	fi
	stall_hours=$((10#${stall_hours}))
	[ "${stall_hours}" -gt 0 ] || return 0
	[[ "${stall_pr}" =~ ^[1-9][0-9]*$ && "${stall_age_secs}" =~ ^[0-9]+$ ]] || return 0
	[ "${stall_age_secs}" -ge "$((stall_hours * 3600))" ] || return 0
	stall_age_hours=$((stall_age_secs / 3600))
	if ! [[ "${stall_slot_hour}" =~ ^[0-9]{1,2}$ ]] || [ "$((10#${stall_slot_hour}))" -gt 23 ]; then
		stall_slot_hour=1
	fi
	stall_slot_hour=$((10#${stall_slot_hour}))
	stall_now_epoch="$(date -u +%s 2>/dev/null)" || stall_now_epoch=""
	# One clock read: the hour comes from the same epoch, so an hour or
	# midnight boundary between two date calls cannot skew the slot offset.
	if [[ "${stall_now_epoch}" =~ ^[0-9]+$ ]]; then
		stall_now_hour="$(date -u -d "@${stall_now_epoch}" +%H 2>/dev/null)" || stall_now_hour=""
	fi
	# The note is keyed by the UTC date the slot started, so a slot that
	# crosses midnight (start hour 22 or 23) still sends only once.
	if [[ "${stall_now_epoch}" =~ ^[0-9]+$ && "${stall_now_hour}" =~ ^[0-9]{2}$ ]]; then
		stall_today="$(date -u -d "@$((stall_now_epoch - ((10#${stall_now_hour} - stall_slot_hour + 24) % 24) * 3600))" +%Y-%m-%d 2>/dev/null)" || stall_today=""
	fi
	if ! [[ "${stall_now_hour}" =~ ^[0-9]{2}$ && "${stall_today}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
		|| [ "$(( (10#${stall_now_hour} - stall_slot_hour + 24) % 24 ))" -ge 3 ]; then
		stall_outcome="outside_slot"
	elif [ -z "${MERGE_TRAIN_TG_HELPERS:-}" ] || [ -L "${MERGE_TRAIN_TG_HELPERS}" ] || [ ! -f "${MERGE_TRAIN_TG_HELPERS}" ]; then
		stall_outcome="telegram_unavailable"
	fi
	if [ -z "${stall_outcome}" ]; then
		# shellcheck disable=SC1090
		source "${MERGE_TRAIN_TG_HELPERS}" 2>/dev/null || true
		if ! type tg_send_msg >/dev/null 2>&1 || ! type _tg_should_send >/dev/null 2>&1; then
			stall_outcome="telegram_unavailable"
		elif [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ]; then
			stall_outcome="telegram_unconfigured"
		elif ! _tg_should_send WARNING; then
			stall_outcome="below_alert_level"
		fi
	fi
	if [ -z "${stall_outcome}" ]; then
		# Same trust rule as _mt_find_marker_comment (only the automation
		# account's comment counts), plus the note's date in the same call.
		stall_filter='.[] | select((.user.login // "" | ascii_downcase) == "MT_LOGIN" and ((.body // "") | startswith("MT_PREFIX"))) | "\(.id) \(.created_at) \((.body | capture("^MT_PREFIX alert_date=(?<d>[0-9]{4}-[0-9]{2}-[0-9]{2}) -->").d) // "none")"'
		stall_filter="${stall_filter//MT_PREFIX/${MT_STALLED_MARKER_PREFIX}}"
		if ! _mt_resolve_automation_login; then
			stall_outcome="marker_unavailable"
		else
			stall_filter="${stall_filter//MT_LOGIN/${MT_AUTOMATION_LOGIN}}"
			if ! stall_rows="$(gh_retry gh api --paginate "repos/${MT_REPO}/issues/${stall_pr}/comments?per_page=100" --jq "${stall_filter}" 2>/dev/null)"; then
				stall_outcome="marker_unavailable"
			fi
		fi
	fi
	if [ -z "${stall_outcome}" ]; then
		stall_row="$(printf '%s\n' "${stall_rows}" | sed '/^$/d' | LC_ALL=C sort -k2,2r -k1,1nr | sed -n '1p')"
		read -r stall_comment_id stall_comment_created stall_last_date <<< "${stall_row}" || true
		if [ "${stall_last_date}" = "${stall_today}" ]; then
			stall_outcome="already_alerted"
		fi
	fi
	if [ -z "${stall_outcome}" ]; then
		stall_body="${MT_STALLED_MARKER_PREFIX} alert_date=${stall_today} -->
**Merge train: still queued.** This PR has been open for ${stall_age_hours} hours and is the oldest of ${stall_examined} pull request(s) still labelled \`${MT_LABEL}\`. Last release pass: ${stall_detail}. When \`runs_incomplete\` is not 0, look for \`MERGE_TRAIN_RUNS_LISTING outcome=incomplete\` in the latest orchestrator poll run. This note is updated at most once a day while the queue stays this old."
		if [[ "${stall_comment_id}" =~ ^[0-9]+$ ]]; then
			_mt_replace_comment "${stall_comment_id}" "${stall_body}" || stall_outcome="marker_write_failed"
		else
			gh_retry gh api -X POST "repos/${MT_REPO}/issues/${stall_pr}/comments" -f body="${stall_body}" >/dev/null 2>&1 || stall_outcome="marker_write_failed"
		fi
	fi
	if [ -z "${stall_outcome}" ]; then
		stall_msg="Merge train: ${stall_examined} queued PR(s) in ${MT_REPO}; the oldest, #${stall_pr}, has been open ${stall_age_hours}h. Last release pass: ${stall_detail}. https://github.com/${MT_REPO}/pull/${stall_pr}"
		stall_msg_id="$(tg_send_msg "${stall_msg}" WARNING 2>/dev/null || true)"
		if [ -n "${stall_msg_id}" ]; then
			stall_outcome="sent"
		else
			stall_outcome="not_delivered"
		fi
	fi
	_mt_log "MERGE_TRAIN_STALL_ALERT pr=${stall_pr} age_secs=${stall_age_secs} queued=${stall_examined} outcome=${stall_outcome}"
	return 0
}

# Epoch milliseconds for elapsed_ms= in MERGE_TRAIN_RELEASE_SUMMARY. Bash's
# own EPOCHREALTIME is read first: on ubuntu-26.04 runners (Rust uutils
# coreutils) `date +%s%3N` sometimes returned a reading one digit short,
# which printed negative elapsed_ms (#9240). On bash < 5 `date +%s%3N` is
# used when it has 13+ digits (BSD/busybox date may print a literal "3N" or
# nothing); anything else falls back to whole seconds * 1000. No API calls.
_mt_now_ms()
{
	local __mt_now_ms_val __mt_now_ms_real="${EPOCHREALTIME:-}"
	if [[ "${__mt_now_ms_real}" =~ ^([0-9]+)[.,]([0-9]{6})$ ]]; then
		printf '%s\n' "$(( 10#${BASH_REMATCH[1]} * 1000 + 10#${BASH_REMATCH[2]} / 1000 ))"
		return 0
	fi
	__mt_now_ms_val="$(date +%s%3N 2>/dev/null || true)"
	if [[ "${__mt_now_ms_val}" =~ ^[0-9]{13,}$ ]]; then
		printf '%s\n' "${__mt_now_ms_val}"
	else
		__mt_now_ms_val="$(date -u +%s 2>/dev/null || true)"
		[[ "${__mt_now_ms_val}" =~ ^[0-9]+$ ]] || __mt_now_ms_val=0
		printf '%s\n' "$(( 10#${__mt_now_ms_val} * 1000 ))"
	fi
}

# Merge-lease tick (scripts/pr_checks_lib.sh "Merge lease"). For each lease
# ref (one `git/matching-refs` call), frees a held lease whose holder is no
# longer open (from the open-PR list this pass already read) or whose expiry
# passed, then reserves a free lease for the first waiter in queue order
# (priority label, then lowest PR number), removes that waiter's
# MERGE_LEASE_WAIT_LABEL and re-dispatches its review. The reservation makes
# the waiter the holder, so its merge path updates the branch at once.
#
# Input: $1 the _mt_list_open_prs output (one JSON object per line), $2 the
# base filter ("" = every base).
# API calls: 1 matching-refs read; per lease ref one commit read; a release
# write is 1 commit POST + 1 ref PATCH; a grant adds 1 paginated files read,
# the write, 1 label DELETE and 1 workflow dispatch. No call when the library
# is unavailable or MERGE_LEASE_ENABLED is off.
# Fail-open: every failure logs and returns 0; a lease left held expires
# after MERGE_LEASE_TTL_MINUTES.
# Log: MERGE_LEASE pr=<n> base=<ref> mode=tick outcome=held|released|granted|
# no_waiter|write_failed|lost_race reason=<token>
# Metrics: one MERGE_LEASE_METRIC mode=tick line after each of those (and
# event=unavailable after the two read warnings), with hold_s from the record
# already read and waiters counted from $1. No extra API call.
# Phase 2 (merge-lease-parallel, Refs #9781; MERGE_LEASE_REVOKE_ENABLED,
# default true, and only when the staged library has _merge_lease_pr_eligible):
# - Revocation: a held, unexpired holder found in $1 is released when
#   _merge_lease_pr_eligible says it can no longer merge
#   (reason=revoked_draft|revoked_skip_ai|revoked_no_head_gate|
#   revoked_final_ci_failed), or when MERGE_LEASE_IDLE_MINUTES (default 60)
#   have passed since the record's updated=/acquired= and no CI run or
#   required check is running on its head (reason=revoked_idle). A holder
#   missing from $1 keeps today's closed-check only.
# - Skip list: an expired or revoked holder is added to the record as
#   `skip=<pr>:<head>` (the release write); entries whose PR is no longer open
#   on the base with that head are pruned from $1 (no API call). A labelled
#   waiter held back by an entry logs outcome=skipped
#   reason=skip_head_unchanged and keeps its label. The tick never grants it
#   on that head (D4); when the queue holds no other waiter, the skip-held
#   waiters are unlabelled instead, so the sweep dispatches them again and the
#   update-mode gate decides (its lone-caller rule), and no labelled waiter is
#   stranded.
# - Grant-time eligibility: up to 10 candidates per base per tick, in queue
#   order; an ineligible one is unlabelled and logs outcome=skipped
#   reason=draft|skip_ai|no_head_gate|final_ci_failed (event=skipped). When
#   none is left after a skip: outcome=no_waiter reason=no_eligible_waiter.
# API budget (§14): draft, title and head come from the open-PR listing $1
# already read (no call). Per evaluated PR (the holder, and grant candidates
# up to the first eligible one; draft and `[skip ai]` cost nothing): 1
# commit-statuses read plus the required-checks reads of
# _pr_wait_for_required_checks (0-minute budget, no dispatch), and for the
# idle test at most one `actions/runs` read when those did not read the runs.
# Plus one cached `GET user` per tick. No dedicated endpoint exists for
# "can this PR merge"; these are the reads _mt_final_ci_merge_resume already
# uses for the same question. Every read failure is eligible (fail-open).
# Phase 5 (merge-lease-parallel, Refs #9781): when the staged library has the
# multi-holder helpers (tick_p5_lib) and MERGE_LEASE_MAX_HOLDERS > 1 or the
# record lists 2+ live holders, the base is handled by
# _mt_merge_lease_tick_multi (below); otherwise the block here runs unchanged.
_mt_merge_lease_tick()
{
	local tick_prs_json="$1" tick_base_filter="$2"
	local tick_refs_json tick_prefix tick_ref tick_base tick_rc tick_reason tick_base_prs tick_first tick_first_head tick_paths
	# Metrics (MERGE_LEASE_METRIC, observability only): skipped when the staged
	# library predates _merge_lease_metric; never change a decision below.
	local tick_metric_ok=0 tick_waiters="" tick_grant_ts=""
	declare -F _merge_lease_metric >/dev/null 2>&1 && tick_metric_ok=1
	# Phase 2 state (see the header). tick_p2_lib: the staged library has the
	# Phase 2 helpers; tick_p2: on for this base.
	local tick_p2_lib=0 tick_p2=0 tick_login="" tick_login_tried=0 tick_open_heads tick_holder_row tick_holder_head tick_holder_title
	local tick_holder_draft tick_since tick_idle_s tick_ci_active tick_skip_head tick_excluded tick_skipped_any tick_cand_n tick_skip_pr
	local tick_first_row tick_skip_why tick_skip_held tick_queue_skips tick_skip_held_prs tick_queue_empty
	local tick_max_candidates=10
	# Phase 5: tick_p5_lib, the staged library has the multi-holder helpers.
	local tick_p5_lib=0 tick_max_holders=1
	if declare -F _merge_lease_max_holders >/dev/null 2>&1 && declare -F _merge_lease_write_holders >/dev/null 2>&1 \
		&& declare -F _merge_lease_rows_without >/dev/null 2>&1; then
		tick_p5_lib=1
	fi
	if declare -F _merge_lease_pr_eligible >/dev/null 2>&1 && declare -F _merge_lease_revoke_enabled >/dev/null 2>&1 \
		&& declare -F _merge_lease_idle_seconds >/dev/null 2>&1; then
		tick_p2_lib=1
	fi
	[ "${MT_PR_CHECKS_LIB_LOADED}" = "true" ] || return 0
	_merge_lease_enabled || return 0
	[ "${tick_p5_lib}" = 1 ] && tick_max_holders="$(_merge_lease_max_holders)"
	tick_prefix="$(_merge_lease_ref_name "")"
	if ! tick_refs_json="$(gh_retry _safe_gh_jq "repos/${MT_REPO}/git/matching-refs/${tick_prefix%/}" 2>/dev/null)" \
		|| ! jq -e 'type == "array"' >/dev/null 2>&1 <<< "${tick_refs_json}"; then
		_mt_warn "MERGE_LEASE mode=tick outcome=unavailable reason=refs_list_failed (no lease freed or granted this tick)"
		[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric unavailable "" "" tick refs_list_failed "" "" "" || true
		return 0
	fi
	while IFS= read -r tick_ref; do
		[ -n "${tick_ref}" ] || continue
		tick_base="${tick_ref#refs/"${tick_prefix}"}"
		_merge_lease_valid_base "${tick_base}" || continue
		if [ -n "${tick_base_filter}" ] && [ "${tick_base}" != "${tick_base_filter}" ]; then
			continue
		fi
		tick_rc=0
		_merge_lease_read "${tick_base}" || tick_rc=$?
		if [ "${tick_rc}" -ne 0 ]; then
			_mt_warn "MERGE_LEASE base=${tick_base} mode=tick outcome=unavailable reason=read_failed"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric unavailable "${tick_base}" "" tick read_failed "" "" "" || true
			continue
		fi
		# Waiters for this base, from the open-PR list already read (no API call).
		tick_waiters=""
		if [ "${tick_metric_ok}" = 1 ]; then
			tick_waiters="$(printf '%s\n' "${tick_prs_json}" | jq -s --arg b "${tick_base}" --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" \
				'[ .[] | select(type == "object" and .base == $b and (.draft // false) == false)
				| select([ (.labels // [])[] | if type == "object" then .name else . end ] | index($l) != null) ] | length' 2>/dev/null)" || tick_waiters=""
		fi
		tick_p2=0
		if [ "${tick_p2_lib}" = 1 ] && _merge_lease_revoke_enabled; then
			tick_p2=1
			if [ "${tick_login_tried}" = 0 ]; then
				# Once per tick; a failure leaves the login empty (fail-open:
				# only the draft and `[skip ai]` checks apply).
				tick_login_tried=1
				if _mt_resolve_automation_login; then
					tick_login="${MT_AUTOMATION_LOGIN}"
				else
					_mt_warn "MERGE_LEASE mode=tick outcome=unavailable reason=login_unavailable (eligibility fails open this tick)"
				fi
			fi
			# Prune skip entries whose PR is not open on this base with the
			# same head (from $1; no API call).
			if [ -n "${MERGE_LEASE_SKIPS:-}" ]; then
				tick_open_heads="$(printf '%s\n' "${tick_prs_json}" | jq -r --arg b "${tick_base}" 'select(type == "object" and .base == $b) | "\(.number):\(.head_sha // "")"' 2>/dev/null)" || tick_open_heads=""
				MERGE_LEASE_SKIPS="$(printf '%s\n' "${MERGE_LEASE_SKIPS}" | grep -Fx -f <(printf '%s\n' "${tick_open_heads}" | sed '/^$/d') 2>/dev/null || true)"
			fi
		else
			# Phase 2 off: no skip entry is honoured or carried forward.
			MERGE_LEASE_SKIPS=""
		fi
		if [ "${tick_p5_lib}" = 1 ] && { [ "${tick_max_holders}" -gt 1 ] || [ "${MERGE_LEASE_HOLDER_COUNT:-0}" -gt 1 ]; }; then
			_mt_merge_lease_tick_multi "${tick_base}" "${tick_prs_json}" "${tick_max_holders}" "${tick_p2}" "${tick_login}" "${tick_metric_ok}" "${tick_waiters}" || true
			continue
		fi
		if [ "${MERGE_LEASE_STATE}" = "held" ]; then
			tick_reason=""
			tick_holder_head=""
			tick_holder_row=""
			if [ "${tick_p2}" = 1 ]; then
				# Same base only: a holder retargeted elsewhere keeps the closed-check path.
				tick_holder_row="$(printf '%s\n' "${tick_prs_json}" | jq -c --argjson n "${MERGE_LEASE_PR}" --arg b "${tick_base}" 'select(type == "object" and .number == $n and .base == $b)' 2>/dev/null | head -n1)" || tick_holder_row=""
				tick_holder_head="$(jq -r '.head_sha // ""' <<< "${tick_holder_row:-null}" 2>/dev/null)" || tick_holder_head=""
			fi
			if [ "${MERGE_LEASE_EXPIRES}" -le "$(date -u +%s)" ]; then
				tick_reason="expired"
			elif [ "${tick_p2}" = 1 ] && [ -n "${tick_holder_row}" ]; then
				# Phase 2 revocation: the holder is open and listed (its row,
				# read above, is the evidence; no closed-check is needed).
				tick_holder_title="$(jq -r '.title // ""' <<< "${tick_holder_row}" 2>/dev/null)" || tick_holder_title=""
				tick_holder_draft="$(jq -r '.draft // false' <<< "${tick_holder_row}" 2>/dev/null)" || tick_holder_draft="false"
				tick_since="$((10#${MERGE_LEASE_UPDATED:-0}))"
				[ "$((10#${MERGE_LEASE_ACQUIRED:-0}))" -le "${tick_since}" ] || tick_since="$((10#${MERGE_LEASE_ACQUIRED:-0}))"
				if ! _merge_lease_pr_eligible "${MERGE_LEASE_PR}" "${tick_holder_head}" "${tick_holder_title}" "${tick_holder_draft}" \
					"${tick_base}" "${tick_login}" "${tick_since}"; then
					tick_reason="revoked_${MERGE_LEASE_INELIGIBLE_REASON:-no_head_gate}"
				elif [ -n "${tick_login}" ]; then
					tick_idle_s="$(_merge_lease_idle_seconds)"
					if [ "${tick_idle_s}" -gt 0 ] && [ "${tick_since}" -gt 0 ] && [ $(( $(date -u +%s) - tick_since )) -ge "${tick_idle_s}" ]; then
						tick_ci_active="${MERGE_LEASE_CI_ACTIVE:-unknown}"
						if [ "${tick_ci_active}" = "unknown" ] && [[ "${tick_holder_head}" =~ ^[0-9a-f]{40}$ ]]; then
							PR_CHECKS_REPOSITORY="${MT_REPO}" _pr_head_ci_runs_pending "${tick_holder_head}" || true
							if [ "${PR_CHECKS_RUNS_READ:-}" = "true" ]; then
								tick_ci_active="false"
								[ "${PR_CHECKS_RUNS_PENDING:-0}" -eq 0 ] || tick_ci_active="true"
							fi
						fi
						[ "${tick_ci_active}" != "false" ] || tick_reason="revoked_idle"
					fi
				fi
			elif ! printf '%s\n' "${tick_prs_json}" | jq -e --argjson n "${MERGE_LEASE_PR}" 'select(type == "object" and .number == $n)' >/dev/null 2>&1; then
				# The open-PR list was read at the start of this pass; confirm
				# with a live read so a holder opened since is never freed.
				if [ "$(gh_retry _safe_gh_jq "repos/${MT_REPO}/pulls/${MERGE_LEASE_PR}" --jq '.state // ""' 2>/dev/null | tail -n1)" = "closed" ]; then
					tick_reason="holder_closed"
				fi
			fi
			if [ -z "${tick_reason}" ]; then
				_mt_log "MERGE_LEASE pr=${MERGE_LEASE_PR} base=${tick_base} mode=tick outcome=held reason=holder_open"
				[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric held "${tick_base}" "${MERGE_LEASE_PR}" tick holder_open "$(_merge_lease_metric_hold_seconds "${MERGE_LEASE_ACQUIRED:-0}" 2>/dev/null)" "${tick_waiters}" || true
				continue
			fi
			if [ "${tick_p2}" = 1 ] && [ "${tick_reason}" != "holder_closed" ]; then
				# Skip the released holder until its head changes.
				tick_skip_head="${tick_holder_head:-${MERGE_LEASE_HEAD}}"
				if [[ "${tick_skip_head}" =~ ^[0-9a-f]{40}$ ]]; then
					MERGE_LEASE_SKIPS="$(printf '%s\n%s\n' "${MERGE_LEASE_SKIPS:-}" "${MERGE_LEASE_PR}:${tick_skip_head}" | sed '/^$/d')"
				fi
			fi
			tick_rc=0
			_merge_lease_write "${tick_base}" released "${MERGE_LEASE_PR}" "${MERGE_LEASE_HEAD}" "" || tick_rc=$?
			if [ "${tick_rc}" -ne 0 ]; then
				_mt_log "MERGE_LEASE pr=${MERGE_LEASE_PR} base=${tick_base} mode=tick outcome=$([ "${tick_rc}" -eq 3 ] && echo lost_race || echo write_failed) reason=${tick_reason}"
				[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric "$([ "${tick_rc}" -eq 3 ] && echo lost_race || echo write_failed)" "${tick_base}" "${MERGE_LEASE_PR}" tick "${tick_reason}" "" "${tick_waiters}" || true
				continue
			fi
			_mt_log "MERGE_LEASE pr=${MERGE_LEASE_PR} base=${tick_base} mode=tick outcome=released reason=${tick_reason}"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric released "${tick_base}" "${MERGE_LEASE_PR}" tick "${tick_reason}" "$(_merge_lease_metric_hold_seconds "${MERGE_LEASE_ACQUIRED:-0}" 2>/dev/null)" "${tick_waiters}" || true
			MERGE_LEASE_STATE="released"
		fi
		# The rows keep _mt_list_open_prs's `head` and `head_repo` fields: they
		# feed the lease queue's forward-merge priority (merge-lease-parallel
		# Phase 3, Refs #9781). Do not trim them from the projection.
		tick_base_prs="$(printf '%s\n' "${tick_prs_json}" | jq -sc --arg b "${tick_base}" '[ .[] | select(type == "object" and .base == $b) ]' 2>/dev/null || echo '[]')"
		tick_excluded=""
		tick_skipped_any=0
		tick_cand_n=0
		tick_skip_held=0
		tick_skip_held_prs=""
		tick_queue_empty=0
		tick_queue_skips="${MERGE_LEASE_SKIPS:-}"
		if [ "${tick_p2}" = 1 ] && [ -n "${MERGE_LEASE_SKIPS:-}" ]; then
			# Labelled waiters held back by a skip entry keep their label.
			while IFS= read -r tick_skip_pr; do
				[[ "${tick_skip_pr}" =~ ^[1-9][0-9]*$ ]] || continue
				tick_skipped_any=1
				tick_skip_held=1
				tick_skip_held_prs="${tick_skip_held_prs:+${tick_skip_held_prs} }${tick_skip_pr}"
				_mt_log "MERGE_LEASE pr=${tick_skip_pr} base=${tick_base} mode=tick outcome=skipped reason=skip_head_unchanged"
				[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric skipped "${tick_base}" "${tick_skip_pr}" tick skip_head_unchanged "" "${tick_waiters}" - || true
			done < <(jq -r --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" --arg s "${MERGE_LEASE_SKIPS}" \
				'($s | split("\n")) as $sk | .[] | select(type == "object" and (.draft // false) == false)
				| select([ (.labels // [])[] | if type == "object" then .name else . end ] | index($l) != null)
				| .number as $n | (.head_sha // "") as $hs | select(($sk | index("\($n):\($hs)")) != null) | $n' <<< "${tick_base_prs}" 2>/dev/null)
		fi
		while :; do
			if [ "${tick_p2}" = 1 ]; then
				tick_first="$(_merge_lease_queue_first "${tick_base_prs}" "" "${tick_queue_skips}" "${tick_excluded}")"
				# Empty: every waiter in the queue was checked or skip-held (not
				# the 10-candidate cap).
				[ -n "${tick_first}" ] || tick_queue_empty=1
			else
				tick_first="$(_merge_lease_queue_first "${tick_base_prs}" "")"
			fi
			[ -n "${tick_first}" ] && [ "${tick_p2}" = 1 ] || break
			# Grant-time eligibility (Phase 2): waiters need a pipeline head-gate success.
			tick_cand_n=$((tick_cand_n + 1))
			tick_first_row="$(jq -c --argjson n "${tick_first}" '[ .[] | select(.number == $n) ][0] // {}' <<< "${tick_base_prs}" 2>/dev/null)" || tick_first_row="{}"
			if _merge_lease_pr_eligible "${tick_first}" "$(jq -r '.head_sha // ""' <<< "${tick_first_row}" 2>/dev/null)" \
				"$(jq -r '.title // ""' <<< "${tick_first_row}" 2>/dev/null)" "$(jq -r '.draft // false' <<< "${tick_first_row}" 2>/dev/null)" \
				"${tick_base}" "${tick_login}"; then
				break
			fi
			tick_skip_why="${MERGE_LEASE_INELIGIBLE_REASON:-no_head_gate}"
			tick_skipped_any=1
			_merge_lease_unlabel_wait "${tick_first}"
			_mt_log "MERGE_LEASE pr=${tick_first} base=${tick_base} mode=tick outcome=skipped reason=${tick_skip_why}"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric skipped "${tick_base}" "${tick_first}" tick "${tick_skip_why}" "" "${tick_waiters}" - || true
			tick_excluded="${tick_excluded:+${tick_excluded},}${tick_first}"
			tick_first=""
			[ "${tick_cand_n}" -lt "${tick_max_candidates}" ] || break
		done
		if [ -z "${tick_first}" ]; then
			if [ "${tick_p2}" = 1 ] && [ "${tick_queue_empty}" = 1 ] && [ "${tick_skip_held}" = 1 ]; then
				# D4: a skip entry blocks a re-grant on the same head, so the tick
				# never grants a skip-held waiter. With no other waiter left it
				# would keep its label, and the sweep skips labelled PRs, so it
				# is unlabelled: the sweep dispatches it again and the update-mode
				# gate (lone-caller rule) decides. Its skip entry stays.
				for tick_skip_pr in ${tick_skip_held_prs}; do
					_merge_lease_unlabel_wait "${tick_skip_pr}"
				done
			fi
			if [ "${tick_skipped_any}" = 0 ]; then
				_mt_log "MERGE_LEASE pr=- base=${tick_base} mode=tick outcome=no_waiter reason=lease_free"
				[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric no_waiter "${tick_base}" "" tick lease_free "" 0 - || true
			else
				# Phase 2: every waiter left was skipped (ineligible or on the skip list).
				_mt_log "MERGE_LEASE pr=- base=${tick_base} mode=tick outcome=no_waiter reason=no_eligible_waiter"
				[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric no_waiter "${tick_base}" "" tick no_eligible_waiter "" 0 - || true
			fi
			continue
		fi
		tick_first_head="$(jq -r --argjson n "${tick_first}" '[ .[] | select(.number == $n) | .head_sha // "" ][0] // ""' <<< "${tick_base_prs}" 2>/dev/null)"
		tick_paths="$(_merge_lease_pr_paths "${tick_first}" 2>/dev/null || true)"
		tick_rc=0
		_merge_lease_write "${tick_base}" held "${tick_first}" "${tick_first_head}" "${tick_paths:-*}" || tick_rc=$?
		if [ "${tick_rc}" -ne 0 ]; then
			_mt_log "MERGE_LEASE pr=${tick_first} base=${tick_base} mode=tick outcome=$([ "${tick_rc}" -eq 3 ] && echo lost_race || echo write_failed) reason=grant"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric "$([ "${tick_rc}" -eq 3 ] && echo lost_race || echo write_failed)" "${tick_base}" "${tick_first}" tick grant "" "${tick_waiters}" - || true
			continue
		fi
		# Grant time for the metric: the write above, not after unlabel/dispatch.
		tick_grant_ts="$(date -u +%s 2>/dev/null)" || tick_grant_ts=""
		# Unlabel before the dispatch: if the dispatch fails, the sweep (which
		# skips labelled waiters) picks the PR up again within 30 minutes.
		_merge_lease_unlabel_wait "${tick_first}"
		if _mt_dispatch_review "${tick_first}" "$(jq -r --argjson n "${tick_first}" '[ .[] | select(.number == $n) | .head // "" ][0] // ""' <<< "${tick_base_prs}" 2>/dev/null)"; then
			_mt_log "MERGE_LEASE pr=${tick_first} base=${tick_base} mode=tick outcome=granted reason=first_in_queue"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric granted "${tick_base}" "${tick_first}" tick first_in_queue 0 "${tick_waiters}" "${tick_first}" "${tick_grant_ts}" || true
		else
			_mt_log "MERGE_LEASE pr=${tick_first} base=${tick_base} mode=tick outcome=granted reason=dispatch_failed_sweep_retries"
			[ "${tick_metric_ok}" = 1 ] && _merge_lease_metric granted "${tick_base}" "${tick_first}" tick dispatch_failed_sweep_retries 0 "${tick_waiters}" "${tick_first}" "${tick_grant_ts}" || true
		fi
	done < <(jq -r --arg p "refs/${tick_prefix}" '.[] | select(type == "object" and ((.ref // "") | startswith($p))) | .ref' <<< "${tick_refs_json}" 2>/dev/null)
	return 0
}

# Multi-holder lease tick for one base (merge-lease-parallel Phase 5, Refs
# #9781). Called by _mt_merge_lease_tick after the record is read and the
# Phase 2 skip list pruned, when the staged library has the Phase 5 helpers and
# MERGE_LEASE_MAX_HOLDERS > 1 or the record lists 2+ live holders.
# Args: <base> <open-PR rows ($1 of the tick)> <max holders> <tick_p2>
# <login> <metric_ok> <waiters>.
# Release pass: every holder row of a held record (expired rows included)
# gets the single-holder checks with its own head and times: expired; Phase 2
# revocation and idle test (tick_p2); closed (live read when missing from the
# listing). Each release is its own compare-and-swap write that keeps the
# other holders (_merge_lease_rows_without: a released primary promotes the
# oldest-acquired holder; one holder left is written in the v1 shape with its
# own expiry) or frees the lease (_merge_lease_write released) when none is
# left; an expired or revoked holder gets a skip entry. A failed or lost write
# ends this base for this tick. Kept holders log outcome=held
# reason=holder_open holders=<n>.
# Grant pass: while fewer than <max holders> hold the lease, the Phase 2
# candidate loop (queue order, skip list, eligibility with unlabel, 10
# candidates shared) also requires the waiter's paths to be disjoint from
# every live holder (_merge_lease_paths_intersect with both heads, so the
# Phase 4 soft class applies; unreadable files count as `*`). An overlapping
# waiter keeps its label and logs outcome=skipped reason=holder_shares_paths
# blocker=<holder>. A grant onto a free lease writes the single-holder record
# (_merge_lease_write); otherwise the waiter is added as a holder. Then
# unlabel and dispatch as the single-holder tick; the line ends holders=<n>.
# API (§14): per holder and per grant as the single-holder tick, plus one
# files read per waiter tested for overlap (within the 10-candidate cap).
# Fail-open: every failure logs and returns 0.
_mt_merge_lease_tick_multi()
{
	local tm_base="$1" tm_prs_json="$2" tm_max="$3" tm_p2="$4" tm_login="$5" tm_metric_ok="$6" tm_waiters="$7"
	local tm_rows="" tm_line tm_h tm_h_head tm_h_exp tm_h_acq tm_h_upd tm_now tm_reason tm_listed_row tm_listed_head tm_title tm_draft
	local tm_since tm_idle_s tm_ci_active tm_skip_head tm_new_rows tm_rc tm_count=0 tm_outcome tm_base_prs tm_excluded=""
	local tm_queue_skips tm_skip_pr tm_skip_held=0 tm_skip_held_prs="" tm_skipped_any=0 tm_cand_n=0 tm_granted=0 tm_queue_empty=0
	local tm_first tm_first_row tm_first_head tm_first_paths tm_blocker tm_skip_why tm_grant_ts tm_new_row
	local tm_max_candidates=10
	local -a tm_row_arr=()
	[ "${MERGE_LEASE_STATE}" = "held" ] && tm_rows="${MERGE_LEASE_HOLDER_ALL_ROWS:-}"
	mapfile -t tm_row_arr < <(printf '%s\n' "${tm_rows}" | sed '/^$/d')
	for tm_line in ${tm_row_arr[@]+"${tm_row_arr[@]}"}; do
		read -r tm_h tm_h_head tm_h_exp tm_h_acq tm_h_upd <<< "${tm_line}"
		[[ "${tm_h}" =~ ^[1-9][0-9]*$ ]] || continue
		[ "${tm_h_head}" != "-" ] || tm_h_head=""
		tm_reason=""
		tm_listed_row=""
		tm_listed_head=""
		if [ "${tm_p2}" = 1 ]; then
			tm_listed_row="$(printf '%s\n' "${tm_prs_json}" | jq -c --argjson n "${tm_h}" --arg b "${tm_base}" 'select(type == "object" and .number == $n and .base == $b)' 2>/dev/null | head -n1)" || tm_listed_row=""
			tm_listed_head="$(jq -r '.head_sha // ""' <<< "${tm_listed_row:-null}" 2>/dev/null)" || tm_listed_head=""
		fi
		tm_now="$(date -u +%s)"
		if [ "${tm_h_exp}" -le "${tm_now}" ]; then
			tm_reason="expired"
		elif [ "${tm_p2}" = 1 ] && [ -n "${tm_listed_row}" ]; then
			tm_title="$(jq -r '.title // ""' <<< "${tm_listed_row}" 2>/dev/null)" || tm_title=""
			tm_draft="$(jq -r '.draft // false' <<< "${tm_listed_row}" 2>/dev/null)" || tm_draft="false"
			tm_since="$((10#${tm_h_upd:-0}))"
			[ "$((10#${tm_h_acq:-0}))" -le "${tm_since}" ] || tm_since="$((10#${tm_h_acq:-0}))"
			if ! _merge_lease_pr_eligible "${tm_h}" "${tm_listed_head}" "${tm_title}" "${tm_draft}" "${tm_base}" "${tm_login}" "${tm_since}"; then
				tm_reason="revoked_${MERGE_LEASE_INELIGIBLE_REASON:-no_head_gate}"
			elif [ -n "${tm_login}" ]; then
				tm_idle_s="$(_merge_lease_idle_seconds)"
				if [ "${tm_idle_s}" -gt 0 ] && [ "${tm_since}" -gt 0 ] && [ $(( $(date -u +%s) - tm_since )) -ge "${tm_idle_s}" ]; then
					tm_ci_active="${MERGE_LEASE_CI_ACTIVE:-unknown}"
					if [ "${tm_ci_active}" = "unknown" ] && [[ "${tm_listed_head}" =~ ^[0-9a-f]{40}$ ]]; then
						PR_CHECKS_REPOSITORY="${MT_REPO}" _pr_head_ci_runs_pending "${tm_listed_head}" || true
						if [ "${PR_CHECKS_RUNS_READ:-}" = "true" ]; then
							tm_ci_active="false"
							[ "${PR_CHECKS_RUNS_PENDING:-0}" -eq 0 ] || tm_ci_active="true"
						fi
					fi
					[ "${tm_ci_active}" != "false" ] || tm_reason="revoked_idle"
				fi
			fi
		elif ! printf '%s\n' "${tm_prs_json}" | jq -e --argjson n "${tm_h}" 'select(type == "object" and .number == $n)' >/dev/null 2>&1; then
			if [ "$(gh_retry _safe_gh_jq "repos/${MT_REPO}/pulls/${tm_h}" --jq '.state // ""' 2>/dev/null | tail -n1)" = "closed" ]; then
				tm_reason="holder_closed"
			fi
		fi
		[ -n "${tm_reason}" ] || continue
		if [ "${tm_p2}" = 1 ] && [ "${tm_reason}" != "holder_closed" ]; then
			tm_skip_head="${tm_listed_head:-${tm_h_head}}"
			if [[ "${tm_skip_head}" =~ ^[0-9a-f]{40}$ ]]; then
				MERGE_LEASE_SKIPS="$(printf '%s\n%s\n' "${MERGE_LEASE_SKIPS:-}" "${tm_h}:${tm_skip_head}" | sed '/^$/d')"
			fi
		fi
		tm_new_rows="$(_merge_lease_rows_without "${tm_h}" "${tm_rows}")"
		tm_rc=0
		if [ -z "${tm_new_rows}" ]; then
			_merge_lease_write "${tm_base}" released "${tm_h}" "${tm_h_head}" "" || tm_rc=$?
		else
			_merge_lease_write_holders "${tm_base}" "${tm_new_rows}" || tm_rc=$?
		fi
		if [ "${tm_rc}" -ne 0 ]; then
			tm_outcome="write_failed"
			[ "${tm_rc}" -eq 3 ] && tm_outcome="lost_race"
			_mt_log "MERGE_LEASE pr=${tm_h} base=${tm_base} mode=tick outcome=${tm_outcome} reason=${tm_reason}"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric "${tm_outcome}" "${tm_base}" "${tm_h}" tick "${tm_reason}" "" "${tm_waiters}" "${tm_h}" || true
			return 0
		fi
		_mt_log "MERGE_LEASE pr=${tm_h} base=${tm_base} mode=tick outcome=released reason=${tm_reason}"
		[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric released "${tm_base}" "${tm_h}" tick "${tm_reason}" "$(_merge_lease_metric_hold_seconds "${tm_h_acq:-0}" 2>/dev/null)" "${tm_waiters}" "${tm_h}" || true
		tm_rows="${tm_new_rows}"
	done
	# Every expired row was released above, so the rows left are live.
	mapfile -t tm_row_arr < <(printf '%s\n' "${tm_rows}" | sed '/^$/d')
	tm_count="${#tm_row_arr[@]}"
	for tm_line in ${tm_row_arr[@]+"${tm_row_arr[@]}"}; do
		read -r tm_h _ _ tm_h_acq _ <<< "${tm_line}"
		tm_excluded="${tm_excluded:+${tm_excluded},}${tm_h}"
		_mt_log "MERGE_LEASE pr=${tm_h} base=${tm_base} mode=tick outcome=held reason=holder_open holders=${tm_count}"
		[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric held "${tm_base}" "${tm_h}" tick holder_open "$(_merge_lease_metric_hold_seconds "${tm_h_acq:-0}" 2>/dev/null)" "${tm_waiters}" "${tm_h}" || true
	done
	[ "${tm_count}" -lt "${tm_max}" ] || return 0
	tm_base_prs="$(printf '%s\n' "${tm_prs_json}" | jq -sc --arg b "${tm_base}" '[ .[] | select(type == "object" and .base == $b) ]' 2>/dev/null || echo '[]')"
	tm_queue_skips=""
	if [ "${tm_p2}" = 1 ] && [ -n "${MERGE_LEASE_SKIPS:-}" ]; then
		tm_queue_skips="${MERGE_LEASE_SKIPS}"
		while IFS= read -r tm_skip_pr; do
			[[ "${tm_skip_pr}" =~ ^[1-9][0-9]*$ ]] || continue
			tm_skipped_any=1
			tm_skip_held=1
			tm_skip_held_prs="${tm_skip_held_prs:+${tm_skip_held_prs} }${tm_skip_pr}"
			_mt_log "MERGE_LEASE pr=${tm_skip_pr} base=${tm_base} mode=tick outcome=skipped reason=skip_head_unchanged"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric skipped "${tm_base}" "${tm_skip_pr}" tick skip_head_unchanged "" "${tm_waiters}" - || true
		done < <(jq -r --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}" --arg s "${MERGE_LEASE_SKIPS}" \
			'($s | split("\n")) as $sk | .[] | select(type == "object" and (.draft // false) == false)
			| select([ (.labels // [])[] | if type == "object" then .name else . end ] | index($l) != null)
			| .number as $n | (.head_sha // "") as $hs | select(($sk | index("\($n):\($hs)")) != null) | $n' <<< "${tm_base_prs}" 2>/dev/null)
	fi
	while [ "${tm_count}" -lt "${tm_max}" ] && [ "${tm_cand_n}" -lt "${tm_max_candidates}" ]; do
		tm_first="$(_merge_lease_queue_first "${tm_base_prs}" "" "${tm_queue_skips}" "${tm_excluded}")"
		if [ -z "${tm_first}" ]; then
			tm_queue_empty=1
			break
		fi
		tm_cand_n=$((tm_cand_n + 1))
		tm_excluded="${tm_excluded:+${tm_excluded},}${tm_first}"
		tm_first_row="$(jq -c --argjson n "${tm_first}" '[ .[] | select(.number == $n) ][0] // {}' <<< "${tm_base_prs}" 2>/dev/null)" || tm_first_row="{}"
		tm_first_head="$(jq -r '.head_sha // ""' <<< "${tm_first_row}" 2>/dev/null)" || tm_first_head=""
		if [ "${tm_p2}" = 1 ] && ! _merge_lease_pr_eligible "${tm_first}" "${tm_first_head}" \
			"$(jq -r '.title // ""' <<< "${tm_first_row}" 2>/dev/null)" "$(jq -r '.draft // false' <<< "${tm_first_row}" 2>/dev/null)" \
			"${tm_base}" "${tm_login}"; then
			tm_skip_why="${MERGE_LEASE_INELIGIBLE_REASON:-no_head_gate}"
			tm_skipped_any=1
			_merge_lease_unlabel_wait "${tm_first}"
			_mt_log "MERGE_LEASE pr=${tm_first} base=${tm_base} mode=tick outcome=skipped reason=${tm_skip_why}"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric skipped "${tm_base}" "${tm_first}" tick "${tm_skip_why}" "" "${tm_waiters}" - || true
			continue
		fi
		tm_first_paths="$(_merge_lease_pr_paths "${tm_first}" 2>/dev/null)" || tm_first_paths="*"
		[ -n "${tm_first_paths}" ] || tm_first_paths="*"
		tm_blocker=""
		for tm_line in ${tm_row_arr[@]+"${tm_row_arr[@]}"}; do
			read -r tm_h tm_h_head _ <<< "${tm_line}"
			[ "${tm_h_head}" != "-" ] || tm_h_head=""
			if _merge_lease_paths_intersect "$(_merge_lease_holder_paths "${tm_h}")" "${tm_first_paths}" "${tm_first_head}" "${tm_h_head}"; then
				tm_blocker="${tm_h}"
				break
			fi
		done
		MERGE_LEASE_SOFT_SHARED=""
		if [ -n "${tm_blocker}" ]; then
			# Keeps its label: the next tick tries again once that holder leaves.
			tm_skipped_any=1
			_mt_log "MERGE_LEASE pr=${tm_first} base=${tm_base} mode=tick outcome=skipped reason=holder_shares_paths blocker=${tm_blocker}"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric skipped "${tm_base}" "${tm_first}" tick holder_shares_paths "" "${tm_waiters}" "${tm_blocker}" || true
			continue
		fi
		tm_now="$(date -u +%s)"
		[[ "${tm_first_head}" =~ ^[0-9a-f]{40}$ ]] && tm_new_row="${tm_first} ${tm_first_head}" || tm_new_row="${tm_first} -"
		tm_new_row="${tm_new_row} $((tm_now + $(_merge_lease_ttl_seconds))) ${tm_now} ${tm_now}"
		tm_rc=0
		if [ "${tm_count}" -eq 0 ]; then
			_merge_lease_write "${tm_base}" held "${tm_first}" "${tm_first_head}" "${tm_first_paths}" || tm_rc=$?
		else
			_merge_lease_holder_paths_set "${tm_first}" "${tm_first_paths}"
			_merge_lease_write_holders "${tm_base}" "$(printf '%s\n%s\n' "${tm_rows}" "${tm_new_row}" | sed '/^$/d')" || tm_rc=$?
		fi
		if [ "${tm_rc}" -ne 0 ]; then
			tm_outcome="write_failed"
			[ "${tm_rc}" -eq 3 ] && tm_outcome="lost_race"
			_mt_log "MERGE_LEASE pr=${tm_first} base=${tm_base} mode=tick outcome=${tm_outcome} reason=grant"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric "${tm_outcome}" "${tm_base}" "${tm_first}" tick grant "" "${tm_waiters}" - || true
			return 0
		fi
		tm_grant_ts="$(date -u +%s 2>/dev/null)" || tm_grant_ts=""
		_merge_lease_holder_paths_set "${tm_first}" "${tm_first_paths}"
		tm_rows="$(printf '%s\n%s\n' "${tm_rows}" "${tm_new_row}" | sed '/^$/d')"
		tm_row_arr+=("${tm_new_row}")
		tm_count=$((tm_count + 1))
		tm_granted=$((tm_granted + 1))
		_merge_lease_unlabel_wait "${tm_first}"
		if _mt_dispatch_review "${tm_first}" "$(jq -r '.head // ""' <<< "${tm_first_row}" 2>/dev/null)"; then
			_mt_log "MERGE_LEASE pr=${tm_first} base=${tm_base} mode=tick outcome=granted reason=first_in_queue holders=${tm_count}"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric granted "${tm_base}" "${tm_first}" tick first_in_queue 0 "${tm_waiters}" "${tm_first}" "${tm_grant_ts}" || true
		else
			_mt_log "MERGE_LEASE pr=${tm_first} base=${tm_base} mode=tick outcome=granted reason=dispatch_failed_sweep_retries holders=${tm_count}"
			[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric granted "${tm_base}" "${tm_first}" tick dispatch_failed_sweep_retries 0 "${tm_waiters}" "${tm_first}" "${tm_grant_ts}" || true
		fi
	done
	[ "${tm_granted}" -eq 0 ] || return 0
	if [ "${tm_p2}" = 1 ] && [ "${tm_queue_empty}" = 1 ] && [ "${tm_skip_held}" = 1 ]; then
		# D4, as the single-holder tick: hand skip-held waiters back to the sweep.
		for tm_skip_pr in ${tm_skip_held_prs}; do
			_merge_lease_unlabel_wait "${tm_skip_pr}"
		done
	fi
	[ "${tm_count}" -eq 0 ] || return 0
	if [ "${tm_skipped_any}" = 0 ]; then
		_mt_log "MERGE_LEASE pr=- base=${tm_base} mode=tick outcome=no_waiter reason=lease_free"
		[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric no_waiter "${tm_base}" "" tick lease_free "" 0 - || true
	else
		_mt_log "MERGE_LEASE pr=- base=${tm_base} mode=tick outcome=no_waiter reason=no_eligible_waiter"
		[ "${tm_metric_ok}" = 1 ] && _merge_lease_metric no_waiter "${tm_base}" "" tick no_eligible_waiter "" 0 - || true
	fi
	return 0
}

# Final-CI merge resume (scripts/pr_checks_lib.sh "Final-CI merge resume").
# For each open PR labelled FINAL_CI_MERGE_PENDING_LABEL: when its head still
# carries the pipeline's `pending` FINAL_CI_MERGE_PENDING_CONTEXT status and a
# pipeline-posted `ai-review/head-gate` success, its required checks are
# green now (_pr_wait_for_required_checks with a 0-minute budget: one read,
# no final-CI dispatch, no new marker), and the base is fresh and the merge
# lease allows it (_pr_base_fresh_for_merge), this enables head-bound
# auto-merge (squash), marks the status success, labels the linked issues
# ai:ready-to-merge and removes the PR label. A head without the marker (the
# branch moved), a fork or draft, an integration or forward-merge head, or a
# failed required check removes the label: the normal review path owns it.
#
# Input: as _mt_merge_lease_tick.
# API calls per labelled PR: 1 statuses read, the checks read(s) of
# _pr_wait_for_required_checks, the freshness reads, and on success 1 merge
# call, 1 status POST, 1 GraphQL closing-issues read, 1 label POST per issue
# and 1 label DELETE. Plus one cached `GET user`.
# Fail-open: never merges on an unreadable state; logs and leaves the label.
# Log: FINAL_CI_MERGE_RESUME pr=<n> head=<sha> outcome=auto_merge_enabled|
# waiting|deferred|dropped|failed reason=<token>
_mt_final_ci_merge_resume()
{
	local resume_prs_json="$1" resume_base_filter="$2"
	local resume_label="${FINAL_CI_MERGE_PENDING_LABEL:-ai:final-ci-merge-pending}"
	local resume_context="${FINAL_CI_MERGE_PENDING_CONTEXT:-ai-review/final-ci-merge-pending}"
	local resume_line resume_num resume_head_ref resume_sha resume_head_repo resume_base resume_draft resume_statuses
	local resume_marker resume_gate resume_issue resume_issues resume_block_candidate
	local resume_owner="${MT_REPO%%/*}" resume_name="${MT_REPO#*/}"
	local resume_auto_merge_on="true" resume_labels resume_block_label
	[ "${MT_PR_CHECKS_LIB_LOADED}" = "true" ] || return 0
	case "$(printf '%s' "${FINAL_CI_MERGE_RESUME_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		true|1|yes|on) ;;
		*) return 0 ;;
	esac
	# ENABLE_AUTO_MERGE is the review path's kill switch; an operator who
	# turns it off after a head was marked must not get a merge from here.
	[ "${ENABLE_AUTO_MERGE:-true}" = "true" ] || resume_auto_merge_on="false"
	while IFS= read -r resume_line; do
		[ -n "${resume_line}" ] || continue
		resume_num="$(jq -r '.number' <<< "${resume_line}")"
		resume_head_ref="$(jq -r '.head // ""' <<< "${resume_line}")"
		resume_sha="$(jq -r '.head_sha // ""' <<< "${resume_line}")"
		resume_head_repo="$(jq -r '.head_repo // ""' <<< "${resume_line}")"
		resume_base="$(jq -r '.base // ""' <<< "${resume_line}")"
		resume_draft="$(jq -r '.draft // false' <<< "${resume_line}")"
		[[ "${resume_num}" =~ ^[1-9][0-9]*$ ]] || continue
		if [ -n "${resume_base_filter}" ] && [ "${resume_base}" != "${resume_base_filter}" ]; then
			continue
		fi
		if ! _mt_resolve_automation_login; then
			_mt_warn "FINAL_CI_MERGE_RESUME outcome=failed reason=login_unavailable (nothing resumed this tick)"
			return 0
		fi
		if [ "${resume_head_repo}" != "${MT_REPO}" ] || [ "${resume_draft}" = "true" ] || ! [[ "${resume_sha}" =~ ^[0-9a-f]{40}$ ]] \
			|| [[ "${resume_head_ref}" =~ ^auto/forward-merge-stable- ]] \
			|| printf '%s\n' "${resume_head_ref}" | grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-}"; then
			_mt_final_ci_pending_unlabel "${resume_num}" "${resume_label}"
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha:-?} outcome=dropped reason=not_eligible"
			continue
		fi
		# A hold placed after the head was marked wins: drop the label and
		# leave the PR to the review path (auto-merge off, a human hold, a
		# review block, the merge train queue, a failed security pass).
		resume_labels="$(jq -r '(.labels // []) | join(",")' <<< "${resume_line}" 2>/dev/null || true)"
		resume_block_label=""
		for resume_block_candidate in ai:needs-human ai:review-blocked ai:merge-queued ai:security-pass-failed "${MT_LABEL}"; do
			if printf ',%s,' "${resume_labels}" | grep -Fq ",${resume_block_candidate},"; then
				resume_block_label="${resume_block_candidate}"
				break
			fi
		done
		if [ "${resume_auto_merge_on}" != "true" ] || [ -n "${resume_block_label}" ]; then
			_mt_final_ci_pending_unlabel "${resume_num}" "${resume_label}"
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=dropped reason=$([ "${resume_auto_merge_on}" != "true" ] && echo auto_merge_disabled || echo "blocked_by_${resume_block_label}")"
			continue
		fi
		if ! resume_statuses="$(gh_retry _safe_gh_jq "repos/${MT_REPO}/commits/${resume_sha}/statuses?per_page=100" 2>/dev/null)" \
			|| ! jq -e 'type == "array"' >/dev/null 2>&1 <<< "${resume_statuses}"; then
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=waiting reason=statuses_unavailable"
			continue
		fi
		# Newest first: the latest status per context decides.
		resume_marker="$(jq -r --arg c "${resume_context}" '[ .[] | select(.context == $c) ][0] | if . == null then "" else "\(.state) \((.creator.login // "") | ascii_downcase)" end' <<< "${resume_statuses}" 2>/dev/null)"
		resume_gate="$(jq -r '[ .[] | select(.context == "ai-review/head-gate") ][0] | if . == null then "" else "\(.state) \((.creator.login // "") | ascii_downcase)" end' <<< "${resume_statuses}" 2>/dev/null)"
		if [ "${resume_marker}" != "pending ${MT_AUTOMATION_LOGIN}" ]; then
			_mt_final_ci_pending_unlabel "${resume_num}" "${resume_label}"
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=dropped reason=no_marker_on_head"
			continue
		fi
		if [ "${resume_gate}" != "success ${MT_AUTOMATION_LOGIN}" ]; then
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=waiting reason=head_gate_not_success"
			continue
		fi
		if ! PR_CHECKS_REPOSITORY="${MT_REPO}" REVIEW_FINAL_CI_WORKFLOW="" _pr_wait_for_required_checks "${resume_num}" "${resume_sha}" "${resume_base}" 0; then
			if [ "${PR_CHECKS_WAIT_OUTCOME:-}" = "failed" ]; then
				_mt_final_ci_pending_unlabel "${resume_num}" "${resume_label}"
				_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=dropped reason=required_checks_failed"
			else
				_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=waiting reason=required_checks_${PR_CHECKS_WAIT_OUTCOME:-unknown}"
			fi
			continue
		fi
		# Resume only on a named required CI check that exists and passed: a
		# base whose wait would read "no check-runs" as green is left to the
		# review path, which polls a grace period first.
		if [ "${PR_CHECKS_LAST_CI_STATE:-}" != "success" ]; then
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=waiting reason=no_named_ci_success ci_check=${PR_CHECKS_LAST_CI_NAME:-none}"
			continue
		fi
		if ! PR_CHECKS_REPOSITORY="${MT_REPO}" REVIEW_FINAL_CI_WORKFLOW="" _pr_base_fresh_for_merge "${resume_num}" "${resume_sha}" "${resume_base}"; then
			_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=deferred reason=${PR_BASE_FRESHNESS_OUTCOME:-unknown}"
			continue
		fi
		if ! gh_retry gh pr merge "${resume_num}" --repo "${MT_REPO}" --squash --auto --match-head-commit "${resume_sha}" >/dev/null 2>&1; then
			_mt_warn "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=failed reason=auto_merge_refused"
			continue
		fi
		gh_retry _safe_gh_jq -X POST "repos/${MT_REPO}/statuses/${resume_sha}" -f state=success -f "context=${resume_context}" \
			-f "description=Final CI passed; head-bound auto-merge enabled by the poll tick" >/dev/null 2>&1 || true
		_mt_final_ci_pending_unlabel "${resume_num}" "${resume_label}"
		resume_issues="$(gh_retry gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){closingIssuesReferences(first:10){nodes{number}}}}}' \
			-f "o=${resume_owner}" -f "r=${resume_name}" -F "n=${resume_num}" --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' 2>/dev/null || true)"
		while IFS= read -r resume_issue; do
			[[ "${resume_issue}" =~ ^[1-9][0-9]*$ ]] || continue
			GH_RETRY_IDEMPOTENT=true gh_retry _safe_gh_jq -X POST "repos/${MT_REPO}/issues/${resume_issue}/labels" -f "labels[]=ai:ready-to-merge" >/dev/null 2>&1 \
				|| _mt_warn "FINAL_CI_MERGE_RESUME pr=${resume_num} issue=${resume_issue} outcome=label_failed"
		done <<< "${resume_issues}"
		_mt_log "FINAL_CI_MERGE_RESUME pr=${resume_num} head=${resume_sha} outcome=auto_merge_enabled reason=final_ci_green"
	done < <(printf '%s\n' "${resume_prs_json}" | jq -c --arg l "${resume_label}" 'select(type == "object" and ((.labels // []) | index($l)) != null)' 2>/dev/null)
	return 0
}

_mt_final_ci_pending_unlabel()
{
	gh_retry _safe_gh_jq -X DELETE "repos/${MT_REPO}/issues/$1/labels/$(printf '%s' "$2" | jq -sRr @uri)" >/dev/null 2>&1 || true
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
	# Per-reason leave-queued counts for MERGE_TRAIN_RELEASE_SUMMARY, so a tick
	# that examines queued PRs and releases none can be attributed. Each counted
	# branch is terminal for its PR, so released + the counts == examined.
	local release_count_overlap=0 release_count_active_review=0 release_count_runs_incomplete=0
	local release_count_files_unavailable=0 release_count_blockers_unavailable=0 release_count_marker_unavailable=0
	local release_count_claim_skipped=0 release_count_dispatch_failed=0
	local release_created_at="" release_created_epoch="" release_oldest_created_epoch="" release_oldest_age="none"
	# Queue-age note (_mt_release_stall_alert): the oldest queued PR and the
	# PRs this pass released, so a PR released just now is never reported.
	local release_oldest_pr="" release_released_prs=" "
	local release_now_epoch
	# API-cost counters (analysis/workflow-optimization-2026-10-08.md, GH API
	# Call Audit #1): reset per pass; they add no calls and change no decision.
	local release_start_ms release_elapsed_ms=0 release_active_listing_calls=0
	release_start_ms="$(_mt_now_ms)"
	_MT_FILE_LIST_FETCHES=0
	_MT_FILE_LIST_CACHE_HITS=0
	release_now_epoch="$(date -u +%s)"
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
		# Oldest queued age: created_at is already in the _mt_list_open_prs
		# output, so this adds no API call (CLAUDE.md §14). A missing or
		# malformed timestamp is skipped (fail-open).
		release_created_at="$(printf '%s' "${line}" | jq -r '.created_at // ""' 2>/dev/null || true)"
		if release_created_epoch="$(_mt_iso_epoch "${release_created_at}")" && [[ "${release_created_epoch}" =~ ^[0-9]+$ ]]; then
			if [ -z "${release_oldest_created_epoch}" ] || [ "${release_created_epoch}" -lt "${release_oldest_created_epoch}" ]; then
				release_oldest_created_epoch="${release_created_epoch}"
				release_oldest_pr="${num}"
			fi
		fi
		if [ -z "${release_runs_listing_state}" ]; then
			# Counted here, not inside the listing: it runs in $(...).
			release_active_listing_calls=$((release_active_listing_calls + 1))
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
			release_count_runs_incomplete=$((release_count_runs_incomplete + 1))
			continue
		fi
		# An empty head is never a pattern: grep -Fx with "" matches a blank
		# line, and a run whose head_branch is "" prints one.
		if [ -n "${inflight_review_branches}" ] && printf '%s\n' "${inflight_review_branches}" | grep -Fxq -e "pr:${num}" ${head:+-e "${head}"}; then
			_mt_log "MERGE_TRAIN_RELEASE_ACTIVE pr=${num} head=${head} action=leave_queued"
			release_count_active_review=$((release_count_active_review + 1))
			continue
		fi
		files=""
		if ! _mt_pr_files_into files "${num}"; then
			_mt_warn "merge-train release: could not list files for queued PR #${num}; leaving it queued."
			release_count_files_unavailable=$((release_count_files_unavailable + 1))
			continue
		fi
		blockers=""
		if ! _mt_blockers_for_into blockers "${num}" "${base}" "${files}" "${prs_json}"; then
			_mt_warn "merge-train release: could not evaluate blockers for PR #${num}; leaving it queued."
			release_count_blockers_unavailable=$((release_count_blockers_unavailable + 1))
			continue
		fi
		if [ -n "${blockers}" ]; then
			_mt_log "MERGE_TRAIN_STILL_QUEUED pr=${num} blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -)"
			release_count_overlap=$((release_count_overlap + 1))
			continue
		fi
		release_queue_comment_id=""
		if ! _mt_resolve_automation_login || ! release_queue_comment_id="$(_mt_find_marker_comment_id "${num}" "${MT_MARKER}")"; then
			_mt_warn "merge-train release: could not inspect the queue marker for PR #${num}; leaving it queued to avoid arming a false bypass."
			release_count_marker_unavailable=$((release_count_marker_unavailable + 1))
			continue
		fi
		if [[ "${release_queue_comment_id}" =~ ^[0-9]+$ ]] && \
		   ! _mt_replace_comment "${release_queue_comment_id}" "${MT_RETIRED_MARKER}
**Merge train release claimed.** The train found no older overlapping PRs and is dispatching review."; then
			_mt_warn "merge-train release: could not retire the queue marker for PR #${num}; leaving it queued."
			release_count_marker_unavailable=$((release_count_marker_unavailable + 1))
			continue
		fi
		# Claim this release by removing the queue label. Concurrent release
		# invocations cannot both claim it. The queued marker is retired first so
		# an automation-owned label removal can never look like a human bypass.
		if ! gh_retry gh api -X DELETE "repos/${MT_REPO}/issues/${num}/labels/$(printf '%s' "${MT_LABEL}" | jq -sRr @uri)" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_RELEASE_CLAIM_SKIPPED pr=${num} action=leave_to_peer"
			release_count_claim_skipped=$((release_count_claim_skipped + 1))
			continue
		fi
		if _mt_dispatch_review "${num}" "${head}"; then
			release_comment_body="${MT_RELEASED_MARKER}
**Merge train released.** Every older PR that edited the same files has merged or closed; the review/autofix run was re-dispatched for \`${head}\`."
			_mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" "${release_comment_body}" \
				|| _mt_warn "merge-train release: could not upsert the released comment for PR #${num}; continuing."
			released=$((released + 1))
			release_released_prs="${release_released_prs}${num} "
			_mt_log "MERGE_TRAIN_RELEASED pr=${num} source=release"
		else
			release_count_dispatch_failed=$((release_count_dispatch_failed + 1))
			release_label_restored="false"
			if GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${MT_REPO}/issues/${num}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1; then
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
	# Lease hand-off first (a closed holder frees its lease), then the
	# final-CI resume, whose merge-mode lease check then sees the freed lease.
	_mt_merge_lease_tick "${prs_json}" "${base_filter}" || true
	_mt_final_ci_merge_resume "${prs_json}" "${base_filter}" || true
	if [ -n "${release_oldest_created_epoch}" ]; then
		release_oldest_age=$((release_now_epoch - release_oldest_created_epoch))
		# Clock skew: a future-dated PR reports 0, never a negative age.
		[ "${release_oldest_age}" -ge 0 ] || release_oldest_age=0
	fi
	# Fields are only ever appended, never reordered or renamed: the per-reason
	# fields (overlap= through oldest_queued_age_secs=) were appended after
	# base_filter= so the prefix and first three fields stay byte-identical.
	# The API-cost fields (file_list_fetches= through elapsed_ms=) are appended
	# last, after oldest_queued_age_secs=; they add no calls.
	release_elapsed_ms=$(( $(_mt_now_ms) - release_start_ms ))
	[ "${release_elapsed_ms}" -ge 0 ] || release_elapsed_ms=0
	_mt_log "MERGE_TRAIN_RELEASE_SUMMARY examined=${examined} released=${released} base_filter=${base_filter:-*} overlap=${release_count_overlap} active_review=${release_count_active_review} runs_incomplete=${release_count_runs_incomplete} files_unavailable=${release_count_files_unavailable} blockers_unavailable=${release_count_blockers_unavailable} marker_unavailable=${release_count_marker_unavailable} claim_skipped=${release_count_claim_skipped} dispatch_failed=${release_count_dispatch_failed} oldest_queued_age_secs=${release_oldest_age} file_list_fetches=${_MT_FILE_LIST_FETCHES} file_list_cache_hits=${_MT_FILE_LIST_CACHE_HITS} active_listing_calls=${release_active_listing_calls} elapsed_ms=${release_elapsed_ms}"
	if [ -z "${base_filter}" ] && [ -n "${release_oldest_pr}" ] && [[ "${release_oldest_age}" =~ ^[0-9]+$ ]] \
		&& [[ "${release_released_prs}" != *" ${release_oldest_pr} "* ]]; then
		_mt_release_stall_alert "${release_oldest_pr}" "${release_oldest_age}" "$((examined - released))" \
			"released=${released} overlap=${release_count_overlap} active_review=${release_count_active_review} runs_incomplete=${release_count_runs_incomplete}" || true
	fi
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
