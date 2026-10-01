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
# OLDER open ai/issue-* PR on the same base is queued — labelled
# ai:merge-queued and its review run soft-exits — until every older
# overlapping PR is merged or closed. The queue is released by the `release`
# subcommand, run from cancel_on_pr_close.yml whenever a PR closes
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
# Env (gate):
#   PR_NUMBER, BASE_BRANCH, TARGET_BRANCH  required (TARGET_BRANCH = PR head ref)
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
#            when queue state changes. Gate-side release adds one comments-list
#            call, up to one marker PATCH, and one label DELETE.
#   release: 1 list call (open PRs, all bases, 100 per page) + the active-run
#            listing that prevents dispatch beside an active review (one
#            `actions/runs?status=<s>` call per 100 runs for each of
#            requested, pending, queued, waiting, in_progress, then requested,
#            pending, queued, waiting again, follow-ups bounded by created_at,
#            at most 10 each — normally 9 calls — read once,
#            only when a queued PR passes the base filter; see
#            _mt_inflight_review_branches) + files calls
#            as above, cached per PR for the run; each unblocked queued PR adds
#            1 comments-list call and up to 1 marker PATCH before its label-
#            removal claim. A release adds 1 workflow dispatch and a best-effort
#            released-comment upsert. A failed dispatch tries to restore the
#            label (best-effort, +1 call) so the next release
#            invocation retries it; if that restore fails too, a ::warning:: is
#            logged and the PR stays unlabelled until its next review-triggering
#            event (push, re-run, or orchestrator stall recovery).
#
# Fail-open contract: any API failure, missing input or unexpected shape logs
# a ::warning:: and exits 0 WITHOUT queuing (gate) or WITHOUT releasing
# (release). An incomplete active-run listing counts as such a failure:
# release then leaves every queued PR queued (MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE)
# rather than dispatch beside a review it could not see. The train only ever
# delays a review run; it never blocks a
# merge, and a PR that is wrongly left queued is picked up by the next
# release tick once its blockers are gone.
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
# {number, head, base, draft, labels[]}. `tojson` matters: `gh api --jq`
# pretty-prints object results across several lines, and the callers read
# this output line by line. $1 = base branch filter (empty = all bases).
_mt_list_open_prs() {
	local base="$1" endpoint
	endpoint="repos/${MT_REPO}/pulls?state=open&sort=created&direction=asc&per_page=100"
	if [ -n "${base}" ]; then
		endpoint="${endpoint}&base=${base}"
	fi
	gh_retry gh api --paginate "${endpoint}" \
		--jq '.[] | {number: .number, head: .head.ref, base: .base.ref, draft: .draft, labels: [.labels[].name]} | tojson' 2>/dev/null
}

# Older open ai/issue-* PRs (same base, lower number, not draft, not
# review-blocked / closed-labelled) whose files overlap $4 (this PR's paths).
# `_mt_blockers_for_into <varname> <pr> <base> <own_files> <prs_json>` assigns
# newline-separated "#N:path,path" lines (empty when unblocked) to the caller's
# variable; returns 1 on API failure. It must run in the caller's shell, not a
# `$(...)` subshell, so the file lists it pulls through _mt_pr_files_into stay
# cached for the next queued PR the release loop evaluates. `_mt_blockers_for`
# is the printing form kept for compatibility.
_mt_blockers_for_into() {
	local __mt_blockers_dest="$1" pr="$2" base="$3" own_files="$4" prs_json="$5"
	local examined=0 line num head draft labels files common __mt_blockers_acc=""
	while IFS= read -r line; do
		[ -n "${line}" ] || continue
		num="$(printf '%s' "${line}" | jq -r '.number')"
		head="$(printf '%s' "${line}" | jq -r '.head')"
		draft="$(printf '%s' "${line}" | jq -r '.draft')"
		labels="$(printf '%s' "${line}" | jq -r '.labels | join(",")')"
		[[ "${num}" =~ ^[0-9]+$ ]] || continue
		[ "${num}" -lt "${pr}" ] || continue
		[[ "${head}" == "${MT_PREFIX}"* ]] || continue
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
			__mt_blockers_acc+="$(printf '#%s:%s' "${num}" "$(printf '%s\n' "${common}" | paste -sd, -)")"$'\n'
		fi
	done < <(printf '%s\n' "${prs_json}" | jq -c 'select(.base == $b)' --arg b "${base}")
	# Match what the former `$(...)` capture produced: no trailing newline.
	printf -v "${__mt_blockers_dest}" '%s' "${__mt_blockers_acc%$'\n'}"
	return 0
}

_mt_blockers_for() {
	local __mt_blockers_printed=""
	_mt_blockers_for_into __mt_blockers_printed "$@" || return 1
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

# Find the latest comment carrying a marker. An empty result is a successful
# "not found"; API failure returns non-zero so the gate can fail open.
_mt_find_marker_comment_id()
{
	local pr="$1" marker="$2"
	gh_retry gh api --paginate "repos/${MT_REPO}/issues/${pr}/comments?per_page=100" \
		--jq ".[] | select(.body | startswith(\"${marker}\")) | .id" 2>/dev/null | tail -n 1
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
	if ! [[ "${pr}" =~ ^[0-9]+$ ]] || [ -z "${base}" ] || [ -z "${head}" ]; then
		_mt_warn "merge-train gate: invalid inputs PR_NUMBER='${pr}' BASE_BRANCH='${base}' TARGET_BRANCH='${head}'; fail-open (not queued)."
		return 0
	fi
	if [[ "${head}" != "${MT_PREFIX}"* ]]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} head=${head} result=not_ai_issue_branch action=continue"
		return 0
	fi
	local own_files="" prs_json blockers=""
	if ! _mt_own_files_into own_files "${pr}"; then
		_mt_warn "merge-train gate: could not list changed files for PR #${pr}; fail-open (not queued)."
		return 0
	fi
	if [ -z "${own_files}" ]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} result=no_changed_files action=continue"
		return 0
	fi
	if ! prs_json="$(_mt_list_open_prs "${base}")"; then
		_mt_warn "merge-train gate: could not list open PRs on ${base}; fail-open (not queued)."
		return 0
	fi
	if ! _mt_blockers_for_into blockers "${pr}" "${base}" "${own_files}" "${prs_json}"; then
		_mt_warn "merge-train gate: could not list files of an older PR; fail-open (not queued)."
		return 0
	fi
	local own_labels queued_comment_id queue_label_persisted
	own_labels="$(printf '%s\n' "${prs_json}" | jq -r --argjson n "${pr}" 'select(.number == $n) | .labels | join(",")' 2>/dev/null | head -n 1 || true)"
	if [ -z "${blockers}" ]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=unblocked action=continue"
		if _mt_has_label "${own_labels}"; then
			if ! queued_comment_id="$(_mt_find_marker_comment_id "${pr}" "${MT_MARKER}")"; then
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
	if ! queued_comment_id="$(_mt_find_marker_comment_id "${pr}" "${MT_MARKER}")"; then
		_mt_warn "merge-train gate: could not inspect prior queue state for PR #${pr}; fail-open (not queued)."
		return 0
	fi
	if ! _mt_has_label "${own_labels}" && [[ "${queued_comment_id}" =~ ^[0-9]+$ ]]; then
		gh_retry gh api -X PATCH "repos/${MT_REPO}/issues/comments/${queued_comment_id}" \
			-f body="${MT_BYPASSED_MARKER}
**Merge train bypassed once.** The queue label was removed while older overlapping PRs remain open, so this review run is proceeding. A later run will evaluate the train normally." >/dev/null 2>&1 \
			|| _mt_warn "merge-train gate: could not persist the one-shot bypass marker for PR #${pr}; this run still proceeds."
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=bypassed blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -) action=continue"
		return 0
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
	if [ "${queue_label_persisted}" = "true" ]; then
		_mt_upsert_comment "${pr}" "${MT_MARKER}" "${MT_MARKER}
**Review queued (merge train).** This PR edits files that older open PRs on \`${base}\` also change, so its review/autofix run waits until they merge or close. Lowest PR number goes first; the queue is released automatically when a blocker closes (and re-checked on every orchestrator poll tick).

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
# head, so a workflow_dispatch run named for its PR
# ("Internal: AI Review & Autofix [pr:<N>]" / "AI Review [pr:<N>]") also
# prints "pr:<N>". Git refs cannot contain ":", so the two kinds of key never
# collide, and _mt_release checks both.
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
#            of each active review run, plus "pr:<N>" for a workflow_dispatch
#            run named for PR <N>. Every run an active-status query returned
#            counts as active, whatever its own status field says.
# Returns:   0 = the listing is complete: a PR with no key has no active run.
#            1 = the listing is incomplete. Stdout carries nothing; the caller
#            must not release on it, and the next invocation retries.
# API calls: one `GET actions/runs?status=<s>&per_page=100&page=1` for each
#            non-terminal status, requested, pending, queued, waiting,
#            in_progress (PR #5451 review round 3: `requested` is a new run's
#            state before it is queued; `waiting` is only reached through a
#            deployment environment, which the review workflows do not use,
#            and is read so a wrapper that adds one is still covered), then
#            requested, pending, queued, waiting once more. The repo's other
#            active-run guards count the same five statuses
#            (scripts/apply_analysis_on_main.sh, scripts/auto_release_stable.sh).
#            Order (PR #5451 review round 4): the queries run one after
#            another, so a run that leaves a status after that status was
#            read and enters one that was read before it is in neither
#            result. `waiting` can follow `in_progress` (a later job reaching
#            a deployment environment) and precede `queued` or `in_progress`,
#            so no single pass covers every move. The second pass reads every
#            status again after the first read of every other status, so a
#            run that changes status at most once while the listing is read
#            is returned by some query, whichever way it moves.
#            A status is complete when one response holds
#            every run its query matched (workflow_runs reaches total_count).
#            Otherwise the next query adds `&created=<=<oldest created_at
#            read>` and asks again, at most 10 queries ("pages") per status.
#            A created_at with fractional seconds is rounded up to the next
#            whole second, so the inclusive bound still covers that run.
#            Normally 9 calls, one per status query. REST only (CLAUDE.md §15).
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
#            a run with no numeric id, no non-empty path, or no non-empty
#            status, which the listing can neither deduplicate nor classify,
#            and a missing or malformed created_at on a page that needs a
#            next query), a short page
#            before its total_count (the listing shifted while it was read),
#            more runs than 10 queries read or a query that adds no new run
#            (more than 100 runs created in one second), or a failed key
#            filter. Each is logged once on stderr (CLAUDE.md §8):
#            MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<page_failed|malformed_page|listing_shifted|truncated|filter_failed> status=<s> page=<p> read=<n> total=<n>
_mt_inflight_review_branches()
{
	local __mt_runs_max_pages=10 __mt_runs_status="" __mt_runs_page=0 __mt_runs_reason=""
	local __mt_runs_page_json="" __mt_runs_page_len=0 __mt_runs_total=0 __mt_runs_read=0 __mt_runs_read_before=0
	local __mt_runs_query="" __mt_runs_created_bound=""
	local __mt_runs_status_runs='[]' __mt_runs_all='[]' __mt_runs_keys=""
	for __mt_runs_status in requested pending queued waiting in_progress requested pending queued waiting; do
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
				--jq '{total_count: .total_count, workflow_runs: (.workflow_runs | if type == "array" then [.[] | select(type == "object") | {id: .id, status: .status, event: .event, head_branch: .head_branch, display_title: .display_title, path: .path, created_at: .created_at}] else . end)}' \
				2>/dev/null)"; then
				__mt_runs_reason="page_failed"
				break 2
			fi
			if ! printf '%s' "${__mt_runs_page_json}" \
				| jq -e '(.total_count | type == "number" and . >= 0 and . == floor) and (.workflow_runs | type == "array") and all(.workflow_runs[]; (.id | type == "number") and (.path | type == "string" and length > 0) and (.status | type == "string" and length > 0))' >/dev/null 2>&1; then
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
		if ! __mt_runs_keys="$(printf '%s' "${__mt_runs_all}" | jq -r '.[]? | select((.path // "") | sub("@.*$"; "") | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$")) | ((.head_branch // empty), (if (.event // "") == "workflow_dispatch" then ((.display_title // "") | capture("^(Internal: AI Review & Autofix|AI Review) \\[pr:(?<pr>[1-9][0-9]*)\\]$")? | "pr:\(.pr)") else empty end))' 2>/dev/null)"; then
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
		[[ "${num}" =~ ^[0-9]+$ ]] || continue
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
		if ! release_queue_comment_id="$(_mt_find_marker_comment_id "${num}" "${MT_MARKER}")"; then
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
