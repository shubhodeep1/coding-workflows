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
#            unparseable, or uses Git C-quoted paths; +1 label call and +1-2
#            comment calls only when the queued state or blocker set changes.
#   release: 1 list call (open PRs, all bases, 100 per page) + 1 recent-runs
#            call that prevents dispatch beside an active review + files calls
#            as above, cached per PR for the run; per released PR 1 label-removal
#            claim, 1 comment, 1 workflow dispatch. A failed dispatch tries to
#            restore the label (best-effort, +1 call) so the next release
#            invocation retries it; if that restore fails too, a ::warning:: is
#            logged and the PR stays unlabelled until its next review-triggering
#            event (push, re-run, or orchestrator stall recovery).
#
# Fail-open contract: any API failure, missing input or unexpected shape logs
# a ::warning:: and exits 0 WITHOUT queuing (gate) or WITHOUT releasing
# (release). The train only ever delays a review run; it never blocks a
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
# set). Prints nothing and returns 1 when the API call fails so callers can
# fail open explicitly.
declare -A _MT_FILES_CACHE=()
_mt_pr_files() {
	local pr="$1"
	if [ -n "${_MT_FILES_CACHE[${pr}]+x}" ]; then
		printf '%s' "${_MT_FILES_CACHE[${pr}]}"
		return 0
	fi
	local out=""
	if ! out="$(gh_retry gh api --paginate "repos/${MT_REPO}/pulls/${pr}/files?per_page=100" --jq '.[].filename' 2>/dev/null)"; then
		return 1
	fi
	out="$(printf '%s\n' "${out}" | sed '/^$/d' | sort -u)"
	_MT_FILES_CACHE[${pr}]="${out}"
	printf '%s' "${out}"
}

# This PR's paths from the diff review_collect_pr_metadata.sh already fetched
# (zero API calls); falls back to _mt_pr_files.
_mt_own_files() {
	local pr="$1" diff_file="${PR_DIFF_FILE:-}"
	if [ -n "${diff_file}" ] && [ -s "${diff_file}" ]; then
		# Git C-quotes headers containing non-ASCII/control bytes. The REST
		# filenames are canonical, so use them rather than compare quoted text.
		if grep -q '^diff --git "' "${diff_file}"; then
			_mt_pr_files "${pr}"
			return $?
		fi
		# `diff --git a/<old> b/<new>` — take the b/ side; renames count on
		# both sides so the old path is included too.
		local parsed_files
		parsed_files="$(sed -n 's#^diff --git a/\(.*\) b/\(.*\)$#\1\n\2#p' "${diff_file}" | sed '/^$/d' | sort -u)"
		if [ -n "${parsed_files}" ]; then
			printf '%s' "${parsed_files}"
			return 0
		fi
	fi
	_mt_pr_files "${pr}"
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
# review-blocked / closed-labelled) whose files overlap $3 (this PR's paths).
# Prints "#N:path,path" lines; empty when unblocked. Returns 1 on API failure.
_mt_blockers_for() {
	local pr="$1" base="$2" own_files="$3" prs_json="$4"
	local examined=0 line num head draft labels files common
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
		if ! files="$(_mt_pr_files "${num}")"; then
			return 1
		fi
		common="$(_mt_intersect "${own_files}" "${files}")"
		if [ -n "${common}" ]; then
			printf '#%s:%s\n' "${num}" "$(printf '%s\n' "${common}" | paste -sd, -)"
		fi
	done < <(printf '%s\n' "${prs_json}" | jq -c 'select(.base == $b)' --arg b "${base}")
	return 0
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
	local own_files prs_json blockers
	if ! own_files="$(_mt_own_files "${pr}")"; then
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
	if ! blockers="$(_mt_blockers_for "${pr}" "${base}" "${own_files}" "${prs_json}")"; then
		_mt_warn "merge-train gate: could not list files of an older PR; fail-open (not queued)."
		return 0
	fi
	local own_labels queued_comment_id
	own_labels="$(printf '%s\n' "${prs_json}" | jq -r --argjson n "${pr}" 'select(.number == $n) | .labels | join(",")' 2>/dev/null | head -n 1 || true)"
	if [ -z "${blockers}" ]; then
		_mt_log "MERGE_TRAIN_GATE pr=${pr} base=${base} result=unblocked action=continue"
		if _mt_has_label "${own_labels}"; then
			gh_retry gh api -X DELETE "repos/${MT_REPO}/issues/${pr}/labels/$(printf '%s' "${MT_LABEL}" | jq -sRr @uri)" >/dev/null 2>&1 || true
			_mt_log "MERGE_TRAIN_RELEASED pr=${pr} source=gate"
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
	if ! _mt_has_label "${own_labels}"; then
		gh_retry gh api -X POST "repos/${MT_REPO}/issues/${pr}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1 \
			|| _mt_warn "merge-train gate: could not add ${MT_LABEL} to PR #${pr}; the run still soft-exits."
	fi
	_mt_upsert_comment "${pr}" "${MT_MARKER}" "${MT_MARKER}
**Review queued (merge train).** This PR edits files that older open PRs on \`${base}\` also change, so its review/autofix run waits until they merge or close. Lowest PR number goes first; the queue is released automatically when a blocker closes (and re-checked on every orchestrator poll tick).

Blocked by:
${blocker_lines}

Set the repository variable \`MERGE_TRAIN_ENABLED=false\` to disable the train, or remove the \`${MT_LABEL}\` label and re-run the review to bypass it once." "${queued_comment_id}"
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
_mt_inflight_review_branches()
{
	gh_retry gh api -X GET "repos/${MT_REPO}/actions/runs?per_page=100" \
		--jq '.workflow_runs[]? | select(.status == "queued" or .status == "pending" or .status == "in_progress") | select((.path // "") | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$")) | .head_branch // empty' 2>/dev/null | sort -u
}

_mt_dispatch_review() {
	local pr="$1" head="$2" wf
	local allow_edits="${MERGE_TRAIN_ALLOW_WORKFLOW_EDITS:-true}"
	for wf in ${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}; do
		if gh_retry gh workflow run "${wf}" --repo "${MT_REPO}" --ref "${head}" \
			-f pr_number="${pr}" -f allow_workflow_edits="${allow_edits}" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_DISPATCHED pr=${pr} workflow=${wf} ref=${head}"
			return 0
		fi
	done
	return 1
}

_mt_release() {
	local base_filter="${BASE_BRANCH:-}" prs_json line num head base labels files blockers inflight_review_branches="" released=0 examined=0
	if ! prs_json="$(_mt_list_open_prs "")"; then
		_mt_warn "merge-train release: could not list open PRs; fail-open (nothing released)."
		return 0
	fi
	if ! inflight_review_branches="$(_mt_inflight_review_branches)"; then
		_mt_warn "merge-train release: could not list active review runs; continuing without the dispatch-dedup guard."
		inflight_review_branches=""
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
		if [ -n "${inflight_review_branches}" ] && printf '%s\n' "${inflight_review_branches}" | grep -Fxq -- "${head}"; then
			_mt_log "MERGE_TRAIN_RELEASE_ACTIVE pr=${num} head=${head} action=leave_queued"
			continue
		fi
		if ! files="$(_mt_pr_files "${num}")"; then
			_mt_warn "merge-train release: could not list files for queued PR #${num}; leaving it queued."
			continue
		fi
		if ! blockers="$(_mt_blockers_for "${num}" "${base}" "${files}" "${prs_json}")"; then
			_mt_warn "merge-train release: could not evaluate blockers for PR #${num}; leaving it queued."
			continue
		fi
		if [ -n "${blockers}" ]; then
			_mt_log "MERGE_TRAIN_STILL_QUEUED pr=${num} blockers=$(printf '%s\n' "${blockers}" | sed 's/:.*//' | paste -sd, -)"
			continue
		fi
		# Claim this release by removing the queue label. Concurrent release
		# invocations cannot both claim it; restore the label if dispatch fails.
		if ! gh_retry gh api -X DELETE "repos/${MT_REPO}/issues/${num}/labels/$(printf '%s' "${MT_LABEL}" | jq -sRr @uri)" >/dev/null 2>&1; then
			_mt_log "MERGE_TRAIN_RELEASE_CLAIM_SKIPPED pr=${num} action=leave_to_peer"
			continue
		fi
		if _mt_dispatch_review "${num}" "${head}"; then
			_mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" "${MT_RELEASED_MARKER}
**Merge train released.** Every older PR that edited the same files has merged or closed; the review/autofix run was re-dispatched on \`${head}\`."
			released=$((released + 1))
			_mt_log "MERGE_TRAIN_RELEASED pr=${num} source=release"
		else
			gh_retry gh api -X POST "repos/${MT_REPO}/issues/${num}/labels" -f "labels[]=${MT_LABEL}" >/dev/null 2>&1 \
				|| _mt_warn "merge-train release: dispatch and ${MT_LABEL} restoration both failed for PR #${num}; a later PR event must re-evaluate it."
			_mt_warn "merge-train release: PR #${num} unblocked but the review workflow could not be dispatched; ${MT_LABEL} was restored for the next close event or poll tick."
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
