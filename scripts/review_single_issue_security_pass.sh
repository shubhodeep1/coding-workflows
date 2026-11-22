#!/usr/bin/env bash
# Signed job-token comments (run-volume plan item 15): route this script's
# gh comment calls through the shim even when no earlier step loaded it.
[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true
# Single-issue security pass (port P1, docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 8a).
#
# A standalone PR (base = the default branch) gets the same security audit an
# orchestrator project gets before it merges. Two modes:
#
#   gate    Run by review_autofix.yml right before "Enable auto-merge on PR",
#           after a clean review. Writes `hold=true|false` to GITHUB_OUTPUT.
#           hold=false lets the merge steps run; hold=true skips them for this
#           run. The PR is eligible when its base is the default branch, its
#           head is in this repository and is not an orchestrator integration
#           branch, it carries no `e2e-smoke-test` label, and its linked issue
#           is not a verified automation follow-up (scripts/security_pass_skip.py,
#           the #4623 rules, so follow-ups of follow-ups never recurse). For an
#           eligible PR the latest trusted marker decides:
#             - `status=clean` for the current head: hold=false.
#             - no marker for the current head, and the head is a verified
#               merge-base freshness update (scripts/review_head_gate.sh
#               freshness-sync: GitHub's own signed two-parent merge of the
#               approved head with a base tip already on the default branch,
#               bound by the pipeline's ai:merge-base-sync:v1 marker) whose
#               approved head has a clean marker: hold=false without a new
#               audit. The update adds only code already on the default
#               branch, which merges unaudited whenever it touches no PR
#               file. Chains of updates are walked back at most
#               SINGLE_PASS_INHERIT_MAX_HOPS (10) verified updates; any
#               failed check audits the head as before. The stricter
#               base-sync carry (issue #9533, see single_pass_base_sync_carry)
#               runs first; a merge it proves impure is audited, not inherited.
#             - `status=pending` for the current head, younger than
#               SECURITY_PASS_PENDING_STALE_HOURS: hold=true, nothing else,
#               unless the marker is at least
#               SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES (20; 0 = off) old
#               and every audit run named for this PR since the marker
#               completed 10+ minutes ago without a result (cancelled in
#               the queue, or lost): then it is retried like a stale one.
#             - pending-only cycles (no result marker ever posted) do not
#               use the MAX_SECURITY_PASS_CYCLES budget. Every dispatch is
#               still an attempt; SECURITY_PASS_MAX_AUDIT_ATTEMPTS (default
#               twice the cycle cap) bounds them and is exhausted like the
#               cycle cap.
#             - before a dispatch, with SECURITY_PASS_MAX_CONCURRENT_AUDITS
#               (3; 0 = off) audit runs already queued or running: hold
#               with hold_reason=audit_throttled, no marker and no cycle
#               used; the next review run (sweep tick) tries again.
#             - `status=findings` for the current head: hold=true until the
#               follow-up fixes change the head.
#             - otherwise, with fewer than MAX_SECURITY_PASS_CYCLES cycles
#               used: dispatch the security audit for the PR head branch with
#               `pr_number`, post a `status=pending` marker, hold=true.
#             - cycles used up: label the PR `ai:security-pass-failed` once,
#               hold=true and exhausted=true only if this head has completed
#               with findings. Otherwise retry a bounded number of audits of
#               this head, then hold without entering security-exhaustion mode.
#               A pending head attempt whose audit run is provably lost
#               (single_pass_marker_audit_lost) does not count, up to
#               SECURITY_PASS_LOST_AUDIT_RETRIES (default 2; 0 restores the
#               old counting) per head; lost ones beyond that count again.
#               The review-blocked judge (scripts/review_rb_judge.sh) can
#               decide to merge only an exhausted, audited head.
#             Cycles available = MAX_SECURITY_PASS_CYCLES plus one per
#             trusted extension marker: the judge posts one each time it
#             pushes a [judge-fix] after the pass ran out, so that fix is
#             audited too.
#           A dispatch that fails (for example a consumer wrapper without the
#           `pr_number` input) holds the merge for a later retry. Below the
#           cycle cap it also records a failed cycle, so later review runs
#           retry until the cycle budget is exhausted. Past the cap a failed
#           retry dispatch records a failed head attempt, so repeated failures
#           still reach the ai:security-pass-failed label. If that failed
#           marker cannot be posted, the step still holds but exits 1 so the
#           unrecorded failure surfaces through the review failure path.
#           Every hold=true also writes `hold_reason=<reason>`: audit_dispatched,
#           audit_pending, audit_throttled or awaiting_followups when matching open follow-ups
#           are younger than the limit; followups_missing,
#           followups_unverifiable, followups_stalled, markers_unverifiable,
#           extensions_unverifiable, label_write_failed, pending_marker_failed,
#           head_unpushed (an extension marker needs the checkout at the PR
#           head, but it holds a commit on top of it that this run has not
#           pushed yet; the push re-runs the review),
#           cycles_exhausted,
#           exhausted_without_completed_audit or dispatch_failed otherwise.
#           review_rb_judge.sh passes it on so a judge merge held here alerts
#           only when a human is needed.
#   status  Runs the gate's checks with no label, comment, dispatch or
#           GITHUB_OUTPUT write. May fetch missing Git history to verify
#           extension ancestry; prints
#           SINGLE_ISSUE_SECURITY_PASS_STATE=<skip|unverifiable|clean|pending|
#           findings|exhausted|exhausted_unaudited|needs_audit>. An exhausted
#           head also emits SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD=<sha>.
#           review_rb_judge.sh uses it to enter security-exhaustion mode.
#   report  Run by security-audit.yml after an audit dispatched with
#           `pr_number`. Posts the `status=clean|findings|failed` marker for
#           the audited commit. On clean, failed or final-cycle findings it
#           re-dispatches review (also for findings with missing ID metadata),
#           so the next review merges or escalates.
#   since   Run by security-audit.yml's scope step: prints the last
#           clean-audited ancestor of the audited head for a delta audit
#           (see single_pass_since).
#           Findings become follow-up issues that target
#           the PR branch; their merges push to the PR and start a new review.
#           After a clean marker it retires the branch's open follow-ups
#           (#9479): their PRs into the head ref are closed and the issues are
#           closed as not_planned. Failures only log
#           outcome=followup_retire_failed; the clean result still stands.
#           Delta carry-forward (stuck-states P5a,
#           SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED, default true): after a
#           delta audit, a prior finding that was not re-reported, whose file is
#           untouched since the delta base and whose follow-up is still open is
#           carried into the result (a follow-up listing or git failure carries
#           every such candidate), so a delta audit that reports 0 cannot publish
#           `clean` while it stays unfixed. A findings result also stores the
#           audited findings above the marker (`Audited findings (base64 JSON):`
#           and `Audited findings sha256:`, omitted over 40,000 encoded bytes)
#           so the next delta audit can verify them.
#   delta   Run by security-audit.yml before the audit when the gate requested
#           a delta re-audit. The dispatch inputs `audit_diff_since` and
#           `prior_findings` are requests: this mode re-derives them from the
#           pipeline account's markers, checks ancestry in the audit-data
#           checkout and the stored findings' sha256, and only then writes
#           SECURITY_AUDIT_DIFF_SINCE / SECURITY_AUDIT_PRIOR_FINDINGS (plus
#           SECURITY_PASS_DELTA_SINCE / SECURITY_PASS_PRIOR_FINDINGS_FILE for the
#           report) to GITHUB_ENV. Anything else writes nothing, so the audit
#           runs in full (`mode=delta outcome=full reason=<r>`).
#   followup-guard
#           Run by review_enable_auto_merge.sh for the reviewed PR
#           (PR_PAYLOAD_FILE = its /pulls/<n> JSON). Prints
#           SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow|refuse|unverifiable.
#           Refuses (and comments once) when the PR's base is the head branch
#           of an open standalone PR whose current head has a status=clean
#           marker as its latest completed result.
#
# Delta dispatch (gate): when SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED is not
# `false`, the dispatch is not an exhausted-head retry and the latest trusted
# completed (clean/findings) marker is for an older head, the gate adds
# `-f audit_diff_since=<that head>` and, for findings,
# `-f prior_findings=<that comment id>`. A rejected delta dispatch (a consumer
# wrapper without the inputs) is retried once with today's arguments
# (`outcome=delta_fallback reason=dispatch_rejected`). The first cycle, the
# exhausted-head retry and the switch off keep today's exact arguments, so the
# final exhausted-head audit stays a full merge-base..head audit.
#
# Marker (last non-empty line of a comment by the pipeline account; any other
# author is ignored):
#   <!-- ai:single-issue-security-pass:v1 status=<s> head=<40 hex> cycle=<n> -->
# The findings comment carries a base64-encoded JSON array of current finding
# IDs above that unchanged final marker; old comments without IDs still hold
# the merge but cannot silence a missing-follow-up alert.
# Extension marker, same trust rule, posted by review_rb_judge.sh:
#   <!-- ai:single-issue-security-pass-extension:v1 head=<40 hex> -->
#
# API budget (CLAUDE.md §15). gate: none for an ineligible PR; for an eligible
# one, reuses the PR payload and comments the review job already fetched (and,
# for a head with no marker, one `commits/<sha>` and one compare read per
# verified freshness update walked, at most SINGLE_PASS_INHERIT_MAX_HOPS), plus
# at most 3 GETs for the skip check and one /user identity read (the gate
# job's /user result is not exported), plus one paginated open ai:security
# issue listing only for current-head findings in gate mode, plus at most one
# audit-runs listing (shared by the cancelled-audit check of a pending marker,
# the lost-attempt classification past a cap, also in status mode, and the
# concurrency throttle before a dispatch), then one dispatch
# and up to SECURITY_PASS_PENDING_MARKER_ATTEMPTS pending-marker POSTs (default 3,
# valid range 1-10; backoff base defaults to 5s, range 0-30s), or one label
# write. A rejected delta dispatch adds one dispatch (the legacy retry). A head
# with no clean marker may add the base-sync carry's one commits/<head> and one
# compare read (issue #9533; only when a trusted merge-base-sync marker names
# the head).
# report: one PR read to bind the audit inputs, one /user
# identity read, one paginated comments read, one comment and at most one dispatch,
# plus one paginated open ai:security listing when a delta audit left
# carry-forward candidates. A clean report adds one paginated open ai:security
# issue listing, one paginated pulls?base=<head ref> listing when a follow-up
# matches, and one comment plus one close per retired issue or PR.
# delta: one /user read and one paginated comments read. followup-guard: none for a PR
# into the default or an integration branch; otherwise one paginated
# head-filtered pulls listing, one /user read, one paginated parent comments read, at most one
# paginated read of this PR's comments (skipped when PR_ISSUE_COMMENTS_FILE is
# usable) and at most one comment. Those counts are per invocation: the
# "Enable auto-merge on PR" step (review_enable_auto_merge.sh) invokes it up to
# twice, before the success status and again after the required-checks wait,
# so one step can double them; a refusal on the first call skips the second.
#
# Log: SINGLE_ISSUE_SECURITY_PASS mode= pr= head= outcome= reason= cycle=
#      (followup_guard adds parent=; retirement adds item=, issues=, prs=)
set -uo pipefail

SECURITY_PASS_MARKER_RE='^<!-- ai:single-issue-security-pass:v1 status=(pending|clean|findings|failed) head=([0-9a-f]{40}) cycle=([1-9][0-9]*) -->$'
SECURITY_PASS_EXTENSION_MARKER_RE='^<!-- ai:single-issue-security-pass-extension:v1 head=([0-9a-f]{40}) -->$'
# `status` mode: evaluate the gate without side effects (see the header).
# Set only by the `status` subcommand, never from the environment: a gate
# run that skipped its hold write would let auto-merge through.
SINGLE_PASS_STATUS_ONLY="false"

single_pass_log()
{
	echo "SINGLE_ISSUE_SECURITY_PASS $*"
}

single_pass_output()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		return 0
	fi
	if [ -z "${GITHUB_OUTPUT:-}" ]; then
		echo "::error::GITHUB_OUTPUT is unset; cannot record the security-pass decision." >&2
		exit 1
	fi
	local decision="hold=$1"
	# hold_reason=<reason> (see the header) accompanies every hold=true.
	if [ "$1" = "true" ] && [ -n "${2:-}" ]; then
		decision+=$'\n'"hold_reason=$2"
	fi
	if ! printf '%s\n' "${decision}" >> "${GITHUB_OUTPUT}"; then
		echo "::error::Could not record the security-pass decision; failing closed." >&2
		exit 1
	fi
}

# Records exhausted=true for the review-blocked judge step. A failed write
# only logs: hold=true was already recorded, so the merge stays held.
single_pass_exhausted_output()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ] || [ -z "${GITHUB_OUTPUT:-}" ]; then
		return 0
	fi
	printf 'exhausted=true\n' >> "${GITHUB_OUTPUT}" \
		|| echo "::warning::Could not record exhausted=true; the review-blocked judge will not run for this exhausted security pass." >&2
}

# In `status` mode, prints the gate's state for review_rb_judge.sh.
single_pass_state()
{
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		echo "SINGLE_ISSUE_SECURITY_PASS_STATE=$1"
	fi
}

single_pass_marker()
{
	printf '<!-- ai:single-issue-security-pass:v1 status=%s head=%s cycle=%s -->' "$1" "$2" "$3"
}

# Confirm the created comment carries the pending marker before claiming the
# dispatched audit can publish its result. Keep retries local: sourcing a
# helper from the PR checkout would execute untrusted code in the review job.
single_pass_post_pending_marker()
{
	local body="$1" marker="${1##*$'\n'}" attempts="${SECURITY_PASS_PENDING_MARKER_ATTEMPTS:-3}"
	local delay="${SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS:-5}" attempt response
	[[ "${attempts}" =~ ^[1-9][0-9]*$ ]] || attempts=3
	[[ "${delay}" =~ ^[0-9]+$ ]] || delay=5
	# Invalid or out-of-range attempts (1-10) and delay (0-30s) use defaults.
	if [ "${#attempts}" -gt 2 ]; then attempts=3; else attempts=$((10#${attempts})); fi
	if [ "${#delay}" -gt 2 ]; then delay=5; else delay=$((10#${delay})); fi
	if [ "${attempts}" -gt 10 ]; then attempts=3; fi
	if [ "${delay}" -gt 30 ]; then delay=5; fi
	SINGLE_PASS_PENDING_MARKER_ATTEMPTS_USED="${attempts}"
	for ((attempt = 1; attempt <= attempts; attempt++)); do
		if [ "${attempt}" -gt 1 ] && [ "${delay}" -gt 0 ]; then
			sleep "$((delay * (attempt - 1)))"
		fi
		if response="$(gh api "repos/${REPOSITORY}/issues/${PR_NUMBER:-}/comments" -f body="${body}" 2>/dev/null)" \
			&& jq -e --arg m "${marker}" '(.id | type == "number") and ((.body // "") | type == "string") and (.body | contains($m))' <<< "${response}" >/dev/null 2>&1; then
			return 0
		fi
		single_pass_log "mode=gate pr=${PR_NUMBER} outcome=marker_retry attempt=${attempt}"
	done
	return 1
}

# Prints the pipeline's markers as TSV: created_at, id, status, head, cycle.
single_pass_markers()
{
	local comments_file="$1"
	[ -n "${SECURITY_PASS_AUTHOR_LOGIN:-}" ] || return 1
	[ -s "${comments_file}" ] || return 0
	jq -r --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" '
		.[]?
		| select(type == "object")
		| select(.user.login == $author)
		| [(.created_at // ""), (.id // 0), ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | gsub("^\\s+|\\s+$"; ""))]
		| @tsv
	' "${comments_file}" 2>/dev/null | while IFS=$'\t' read -r created id line; do
		if [[ "${line}" =~ ${SECURITY_PASS_MARKER_RE} ]]; then
			printf '%s\t%s\t%s\t%s\t%s\n' "${created}" "${id}" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
		fi
	done | sort -t $'\t' -k1,1 -k2,2n
}

# Prints how many extra audit cycles the security-exhaustion judge granted:
# one per trusted extension whose fix commit is reachable from the audited
# branch head. A marker posted before a failed push must not extend the budget.
single_pass_extensions()
{
	local comments_file="$1" audited_head="$2" checkout="$3" branch_ref="$4" count=0 extension_line extension_sha extension_lines extension_ancestor_rc
	local -A single_pass_extension_seen=()
	local single_pass_extension_head_checked=false
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN:-}" ] || [ ! -s "${comments_file}" ]; then
		return 1
	fi
	if ! extension_lines="$(jq -r --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" '
			.[]?
			| select(type == "object")
			| select(.user.login == $author)
			| ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | gsub("^\\s+|\\s+$"; ""))
		' "${comments_file}" 2>/dev/null)"; then
		return 1
	fi
	while IFS= read -r extension_line; do
		if [[ "${extension_line}" =~ ${SECURITY_PASS_EXTENSION_MARKER_RE} ]]; then
			extension_sha="${BASH_REMATCH[1]}"
			if [ -n "${single_pass_extension_seen[${extension_sha}]:-}" ]; then
				continue
			fi
			single_pass_extension_seen["${extension_sha}"]=1
			# The checkout must sit at the audited head before a marker can be
			# checked against it. With no marker there is nothing to verify and
			# no cycle to grant, so an unpushed local commit (e.g. the review
			# job's deferred [ai-merge-resolve] merge) must not hold the gate.
			if [ "${single_pass_extension_head_checked}" != "true" ]; then
				if [ "$(git -C "${checkout}" rev-parse HEAD 2>/dev/null || true)" != "${audited_head}" ]; then
					return 1
				fi
				single_pass_extension_head_checked=true
			fi
			if ! git -C "${checkout}" cat-file -e "${extension_sha}^{commit}" 2>/dev/null; then
				if [ "$(git -C "${checkout}" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
					# An absent object in a shallow clone may be a real fix outside
					# the fetched history, not an orphan from a failed push.
					if ! git check-ref-format --branch "${branch_ref}" >/dev/null 2>&1 \
						|| ! git -C "${checkout}" fetch --no-tags --unshallow origin "+refs/heads/${branch_ref}:refs/remotes/origin/${branch_ref}" 2>/dev/null; then
						return 1
					fi
				fi
				if ! git -C "${checkout}" cat-file -e "${extension_sha}^{commit}" 2>/dev/null; then
					continue
				fi
			fi
			extension_ancestor_rc=0
			git -C "${checkout}" merge-base --is-ancestor "${extension_sha}" "${audited_head}" 2>/dev/null || extension_ancestor_rc=$?
			if [ "${extension_ancestor_rc}" -eq 1 ] && [ "$(git -C "${checkout}" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
				# An existing commit can still have an incomplete path to HEAD.
				if ! git check-ref-format --branch "${branch_ref}" >/dev/null 2>&1 \
					|| ! git -C "${checkout}" fetch --no-tags --unshallow origin "+refs/heads/${branch_ref}:refs/remotes/origin/${branch_ref}" 2>/dev/null; then
					return 1
				fi
				extension_ancestor_rc=0
				git -C "${checkout}" merge-base --is-ancestor "${extension_sha}" "${audited_head}" 2>/dev/null || extension_ancestor_rc=$?
			fi
			case "${extension_ancestor_rc}" in
				0) count=$((count + 1)) ;;
				1) ;;
				*) return 1 ;;
			esac
		fi
	done <<< "${extension_lines}"
	echo "${count}"
}

single_pass_audit_workflow()
{
	if [ "${REPOSITORY}" = "shubhodeep1/coding-workflows" ]; then
		echo "security-audit.yml"
	else
		echo "ai-security-audit.yml"
	fi
}

# Cycle accounting (issue: single-issue pass convergence). Input: the marker
# TSV from single_pass_markers, the effective cycle cap and, in report mode,
# the cycle whose result is being posted (0 otherwise). Output, one line:
#   <attempts> <used> <cap_cycle>
# attempts  highest `cycle=` on any marker: every dispatch, the numbering
#           source for the next marker and the input to the attempts cap.
# used      cycles that count toward MAX_SECURITY_PASS_CYCLES: every cycle
#           number up to <attempts> except one whose only markers are
#           `pending` (an audit that never posted a result: cancelled in the
#           concurrency queue, lost, or superseded by a new head). A gap with
#           no marker at all still counts, so trimmed history never frees
#           budget.
# cap_cycle cycle number at which <used> reached the cap (0 = not reached);
#           post-cap head retries are the attempts numbered above it.
# Pure text processing; no API calls.
single_pass_cycle_accounting()
{
	local markers="$1" cap="$2" reported_cycle="${3:-0}"
	printf '%s\n' "${markers}" | awk -F'\t' -v cap="${cap}" -v rc="${reported_cycle}" '
		NF >= 5 {
			c = $5 + 0
			if (c > max) max = c
			seen[c] = 1
			if ($3 != "pending") reported[c] = 1
		}
		END {
			used = 0; cap_cycle = 0
			for (c = 1; c <= max; c++) {
				if (seen[c] && !reported[c] && c != rc + 0) continue
				used++
				if (used == cap && cap_cycle == 0) cap_cycle = c
			}
			print max + 0, used, cap_cycle
		}'
}

# One Actions read shared by the cancelled-audit check, the lost-attempt
# classification and the concurrency throttle (CLAUDE.md §15): the audit
# workflow's runs created in the last 24 hours (an audit times out after 60
# minutes, so no live run is older), one page of 100. Issue #9942: when the
# gate is past a cap, SINGLE_PASS_AUDIT_RUNS_FROM holds the created_at of the
# oldest pending head marker above it, and the listing starts 120 seconds
# before that marker if that is earlier (still one request; it only widens).
# SINGLE_PASS_AUDIT_RUNS_SINCE_EPOCH records the start actually used. The
# listing is incomplete when total_count is missing or exceeds the returned
# runs; single_pass_marker_audit_lost then classifies only markers whose
# window starts at or after the oldest returned run. Cached in
# SINGLE_PASS_AUDIT_RUNS for the rest of the gate run. Returns 1 when the
# listing is unavailable; callers fail open (the throttle) or count the
# marker (lost-attempt classification).
SINGLE_PASS_AUDIT_RUNS=""
SINGLE_PASS_AUDIT_RUNS_STATE="unfetched"
SINGLE_PASS_AUDIT_RUNS_FROM=""
SINGLE_PASS_AUDIT_RUNS_SINCE_EPOCH=""
single_pass_audit_runs()
{
	local workflow since since_epoch audit_runs_from_epoch
	if [ "${SINGLE_PASS_AUDIT_RUNS_STATE}" = "fetched" ]; then
		return 0
	fi
	if [ "${SINGLE_PASS_AUDIT_RUNS_STATE}" = "failed" ]; then
		return 1
	fi
	workflow="$(single_pass_audit_workflow)"
	since_epoch="$(date -u -d '24 hours ago' +%s 2>/dev/null || true)"
	if [[ "${since_epoch}" =~ ^[0-9]+$ ]] \
		&& [[ "${SINGLE_PASS_AUDIT_RUNS_FROM}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$ ]] \
		&& audit_runs_from_epoch="$(date -u -d "${SINGLE_PASS_AUDIT_RUNS_FROM}" +%s 2>/dev/null)" \
		&& [[ "${audit_runs_from_epoch}" =~ ^[0-9]+$ ]] \
		&& [ "$((audit_runs_from_epoch - 120))" -lt "${since_epoch}" ]; then
		since_epoch=$((audit_runs_from_epoch - 120))
	fi
	since=""
	if [[ "${since_epoch}" =~ ^[0-9]+$ ]]; then
		since="$(date -u -d "@${since_epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
	fi
	if [ -z "${since}" ] \
		|| ! SINGLE_PASS_AUDIT_RUNS="$(gh api -X GET "repos/${REPOSITORY}/actions/workflows/${workflow}/runs" -f per_page=100 -f "created=>=${since}" 2>/dev/null)" \
		|| ! jq -e '.workflow_runs | type == "array"' <<< "${SINGLE_PASS_AUDIT_RUNS}" >/dev/null 2>&1; then
		SINGLE_PASS_AUDIT_RUNS_STATE="failed"
		SINGLE_PASS_AUDIT_RUNS=""
		return 1
	fi
	SINGLE_PASS_AUDIT_RUNS_SINCE_EPOCH="${since_epoch}"
	SINGLE_PASS_AUDIT_RUNS_STATE="fetched"
	return 0
}

# Prints lost|live|unknown for the audit a pending marker stands for. The
# audit run is named "... (PR #<n>)" (run-name in security-audit.yml and the
# consumer wrapper). Only positive evidence counts as lost: at least one run
# for this PR created from two minutes before the marker on, every such run
# completed at least 10 minutes ago (time for its report step to post), and
# the marker is still the latest for the head. A missing run (listing lag, or
# a consumer wrapper without run-name) prints unknown and the 6-hour stale
# window stays the backstop.
single_pass_pending_audit_state()
{
	local marker_created="$1"
	if ! single_pass_audit_runs; then
		echo unknown
		return 0
	fi
	jq -r --arg created "${marker_created}" --arg pr "${PR_NUMBER}" '
		def ts: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
		($created | ts) as $since
		| [.workflow_runs[]
			| select(((.display_title // "") | endswith(" (PR #" + $pr + ")")) and ((.created_at // "") | ts) >= ($since - 120))]
		| if length == 0 then "unknown"
		  elif any(.[]; .status != "completed") then "live"
		  elif all(.[]; ((.updated_at // "") | ts) <= (now - 600)) then "lost"
		  else "live" end
	' <<< "${SINGLE_PASS_AUDIT_RUNS}" 2>/dev/null || echo unknown
}

# Issue #9942. Prints lost|live|unknown for a post-cap pending head attempt,
# with the same "lost" rule as single_pass_pending_audit_state but over the
# window [created - 120s, next_created - 120s) (unbounded without a next
# marker on this head). The audit is dispatched before its marker is posted,
# so ending the window 120s before the next marker keeps that attempt's run
# out of this one. Never fetches: the caller fills the cache in its own shell.
# unknown (counted as an attempt) when the listing is not fetched, the window
# starts before the listing, the listing is incomplete (total_count missing or
# above the returned runs) and the window starts before its oldest run, no run
# for this PR falls in the window, or jq fails.
single_pass_marker_audit_lost()
{
	local lost_marker_created="$1" lost_marker_next="${2:-}"
	if [ "${SINGLE_PASS_AUDIT_RUNS_STATE}" != "fetched" ] || ! [[ "${SINGLE_PASS_AUDIT_RUNS_SINCE_EPOCH}" =~ ^[0-9]+$ ]]; then
		echo unknown
		return 0
	fi
	jq -r --arg created "${lost_marker_created}" --arg next "${lost_marker_next}" --arg pr "${PR_NUMBER}" \
		--argjson since "${SINGLE_PASS_AUDIT_RUNS_SINCE_EPOCH}" '
		def ts: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
		(($created | ts) - 120) as $window_start
		| (if $next == "" then null else (($next | ts) - 120) end) as $window_end
		| .workflow_runs as $all
		| if $window_start < $since then "unknown"
		  elif ((.total_count | type) != "number" or .total_count > ($all | length))
			and (($all | length) == 0 or $window_start < ([$all[] | (.created_at // "") | ts] | min)) then "unknown"
		  else
			[$all[]
			| select(((.display_title // "") | endswith(" (PR #" + $pr + ")"))
				and (((.created_at // "") | ts) as $c | $c >= $window_start and ($window_end == null or $c < $window_end)))]
			| if length == 0 then "unknown"
			  elif any(.[]; .status != "completed") then "live"
			  elif all(.[]; ((.updated_at // "") | ts) <= (now - 600)) then "lost"
			  else "live" end
		  end
	' <<< "${SINGLE_PASS_AUDIT_RUNS}" 2>/dev/null || echo unknown
}

# Prints how many audit runs are queued or running right now, or nothing when
# the listing is unavailable (the caller then dispatches as before).
single_pass_active_audits()
{
	single_pass_audit_runs || return 0
	jq -r '[.workflow_runs[] | select(.status != "completed")] | length' <<< "${SINGLE_PASS_AUDIT_RUNS}" 2>/dev/null || true
}

single_pass_review_workflow()
{
	if [ "${REPOSITORY}" = "shubhodeep1/coding-workflows" ]; then
		echo "internal-review.yml"
	else
		echo "ai-review.yml"
	fi
}

# Delta re-audit source (stuck-states P5a). Input: the TSV printed by
# single_pass_markers and the current head. Output:
# `<head>\t<comment_id>\t<status>\t<cycle>` of the latest trusted completed
# (clean/findings) marker, or nothing when there is none, when it is for the
# current head (conservative: a completed result for this head must not be
# superseded by an older one) or when a findings marker has no valid comment id.
# No API calls.
single_pass_delta_source()
{
	local delta_markers_tsv="$1" delta_current_head="$2"
	printf '%s\n' "${delta_markers_tsv}" | awk -F'\t' -v h="${delta_current_head}" '
		NF >= 5 && ($3 == "clean" || $3 == "findings") { line = $4 "\t" $2 "\t" $3 "\t" $5; last_head = $4; last_status = $3; last_id = $2 }
		END {
			if (line == "" || last_head == h) exit 0
			if (last_status == "findings" && last_id !~ /^[1-9][0-9]*$/) exit 0
			print line
		}'
}

# Input: PR head ref. Output: JSON array of {number, marker, body} for the
# open pipeline-authored ai:security issues whose `Integration branch:` line is
# that ref; `marker` is the body's first line when it is an
# `<!-- ai:security-finding:<id> -->` marker, otherwise "". `body` is the issue
# body; single_pass_open_followup_ids reads its leading marker block. The PR
# payload and cached PR comments lack issue state, and LINKED_ISSUES_JSON lists issues the
# PR closes, not audit follow-ups. One paginated issues listing (one API call
# per page), shared by the gate's follow-up count and the report's clean-audit
# retirement. An incomplete or malformed listing returns 1.
single_pass_followup_issues()
{
	local followup_ref="$1" followup_file followup_list
	followup_file="$(mktemp)" || return 1
	if ! { if type gh_retry >/dev/null 2>&1; then
		gh_retry gh api --paginate --slurp "repos/${REPOSITORY}/issues?labels=ai:security&state=open&per_page=100"
	else
		gh api --paginate --slurp "repos/${REPOSITORY}/issues?labels=ai:security&state=open&per_page=100"
	fi; } > "${followup_file}" 2>/dev/null \
		|| [ ! -s "${followup_file}" ] \
		|| ! jq -e 'type == "array" and all(.[]; type == "array")' "${followup_file}" >/dev/null 2>&1; then
		rm -f "${followup_file}"
		return 1
	fi
	if ! followup_list="$(jq -c --arg ref "${followup_ref}" --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" '
		[.[][] | select(type == "object" and .pull_request == null and .user.login == $author and .state == "open")
		| select((.body | type) == "string")
		| select(.body | split("\n") | any(.[];
			gsub("^\\s+|\\s+$"; "") | sub("^-\\s*"; "")
			| if startswith("Integration branch:") then ltrimstr("Integration branch:")
			  elif startswith("**Integration branch:**") then ltrimstr("**Integration branch:**")
			  else "" end
			| gsub("^\\s+|\\s+$"; "") | sub("^`"; "") | sub("`$"; "")
			| gsub("^\\s+|\\s+$"; "") | . == $ref))
		| {number: (.number // null),
		   marker: (.body | split("\n")[0] | if test("^<!-- ai:security-finding:[^<>\\r\\n]+ -->$") then . else "" end),
		   body: .body}]
	' "${followup_file}" 2>/dev/null)" || [ -z "${followup_list}" ]; then
		rm -f "${followup_file}"
		return 1
	fi
	rm -f "${followup_file}"
	printf '%s\n' "${followup_list}"
}

# Input: PR head ref and JSON array of finding IDs. Output: JSON array of the
# IDs covered by matching open follow-ups. An issue covers every ID in its
# leading marker block: the run of `<!-- ai:security-finding:<id> -->` lines
# from line 1 (one line in a legacy per-finding body, one per finding in a
# stuck-states P5b consolidated body). A marker further down never counts. The PR
# state comes from the shared issues listing (single_pass_followup_issues).
# Gate-mode current-head findings (through single_pass_open_followups) and
# report-mode delta carry-forward call this. A failed or malformed listing
# returns 1 so the caller fails closed.
single_pass_open_followup_ids()
{
	local followup_ref="$1" expected_ids="$2" followup_issues followup_covered
	followup_issues="$(single_pass_followup_issues "${followup_ref}")" || return 1
	if ! followup_covered="$(jq -c --argjson ids "${expected_ids}" '
		[.[]
		| .body | split("\n")
		| (map(gsub("\\s+$"; "") | test("^<!-- ai:security-finding:[^<>]+ -->$")) | index(false)) as $block_end
		| .[0:($block_end // length)] | map(gsub("\\s+$"; ""))] | flatten as $issue_markers
		| [$ids[] | select(. as $id | any($issue_markers[]; . == ("<!-- ai:security-finding:" + $id + " -->")))]
	' <<< "${followup_issues}" 2>/dev/null)"; then
		return 1
	fi
	printf '%s\n' "${followup_covered}"
}

# Input: PR head ref and JSON array of current finding IDs. Output: integer
# count of IDs covered by matching open follow-ups (see
# single_pass_open_followup_ids). Only gate-mode current-head findings call
# this. A failed listing returns 1 so the gate holds and pages.
single_pass_open_followups()
{
	local followup_covered_ids
	followup_covered_ids="$(single_pass_open_followup_ids "$1" "$2")" || return 1
	jq -er 'length' <<< "${followup_covered_ids}" 2>/dev/null || return 1
}

# Called by single_pass_report after a status=clean marker for <head_sha> is
# posted. The clean audit supersedes every earlier finding on this branch, so
# its open follow-up issues (pipeline-authored, first-line finding marker,
# `Integration branch:` == <head_ref>) and their open PRs into <head_ref> are
# closed; otherwise their merges would move the clean head (#9479, PR #9356).
# Matches PRs by the implement branch convention ai/issue-<n>, same repository.
# API budget: the shared issues listing, one paginated pulls?base= listing only
# when an issue matches, then a comment and a close per retired item (a failed
# comment still closes the item; closes use gh_retry when sourced). Fails
# open: every failure logs outcome=followup_retire_failed and it returns 0.
single_pass_retire_followups()
{
	local retire_pr="$1" retire_ref="$2" retire_sha="$3" retire_issues retire_issue_json retire_listing retire_prs_file retire_prs="" retire_ref_uri
	local retire_item retire_body retire_issue_count=0 retire_pr_count=0
	local retire_marker="<!-- ai:single-issue-security-pass-followup-retired:v1 head=${retire_sha} -->"
	if ! retire_listing="$(single_pass_followup_issues "${retire_ref}")" \
		|| ! retire_issue_json="$(jq -c '[.[] | select((.number | type) == "number" and .number >= 1 and (.number | floor) == .number and (.marker | type) == "string" and (.marker | length) > 0) | .number] | unique' <<< "${retire_listing}" 2>/dev/null)" \
		|| ! retire_issues="$(jq -r '.[]' <<< "${retire_issue_json}" 2>/dev/null)"; then
		echo "::warning::Could not list the security follow-ups of PR #${retire_pr}; superseded follow-ups stay open, and the merge-side guard refuses their PRs."
		single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=listing_failed"
		return 0
	fi
	if [ -z "${retire_issues}" ]; then
		return 0
	fi
	# The issues listing carries no PR state or base, and the report's
	# pulls/<n> read covers only the audited PR, so one batched listing of the
	# open PRs into this branch finds the follow-up PRs (no per-issue reads).
	retire_ref_uri="$(jq -rn --arg r "${retire_ref}" '$r | @uri' 2>/dev/null)" || retire_ref_uri=""
	retire_prs_file="$(mktemp 2>/dev/null)" || retire_prs_file=""
	if [ -n "${retire_ref_uri}" ] && [ -n "${retire_prs_file}" ] \
		&& { if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api --paginate --slurp "repos/${REPOSITORY}/pulls?state=open&base=${retire_ref_uri}&per_page=100"
		else
			gh api --paginate --slurp "repos/${REPOSITORY}/pulls?state=open&base=${retire_ref_uri}&per_page=100"
		fi; } > "${retire_prs_file}" 2>/dev/null \
		&& retire_prs="$(jq -r --arg ref "${retire_ref}" --arg repo "${REPOSITORY}" --argjson issues "${retire_issue_json}" '
			if type == "array" and all(.[]; type == "array") then . else error("shape") end
			| [.[][] | select(type == "object" and .state == "open" and .base.ref == $ref and .head.repo.full_name == $repo)
			| select((.number | type) == "number" and (.head.ref | type) == "string")
			| . as $p
			| select(any($issues[]; . as $n | $p | .head.ref | test("^ai/issue-" + ($n | tostring) + "([-/].*)?$")))
			| .number] | unique | .[]
		' "${retire_prs_file}" 2>/dev/null)"; then
		:
	else
		retire_prs=""
		echo "::warning::Could not list the open PRs into ${retire_ref}; superseded follow-up PRs stay open, and the merge-side guard refuses them."
		single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=pr_listing_failed"
	fi
	[ -n "${retire_prs_file}" ] && rm -f "${retire_prs_file}"
	while IFS= read -r retire_item; do
		[[ "${retire_item}" =~ ^[1-9][0-9]*$ ]] || continue
		retire_body="## Superseded by a clean security audit

The single-issue security audit of \`${retire_sha}\` on PR #${retire_pr} came back clean, so this follow-up PR is superseded and closed.

${retire_marker}"
		# The comment POST is not retried (not idempotent). A failed comment
		# still closes the PR, so it cannot merge into the clean head.
		if ! gh api "repos/${REPOSITORY}/issues/${retire_item}/comments" -f body="${retire_body}" >/dev/null 2>&1; then
			single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=comment_failed item=${retire_item} kind=pr"
		fi
		# The close PATCH is idempotent, so gh_retry retries it when sourced.
		if ! { if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api -X PATCH "repos/${REPOSITORY}/pulls/${retire_item}" -f state=closed
		else
			gh api -X PATCH "repos/${REPOSITORY}/pulls/${retire_item}" -f state=closed
		fi; } >/dev/null 2>&1; then
			single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=pr_close_failed item=${retire_item}"
			continue
		fi
		retire_pr_count=$((retire_pr_count + 1))
	done <<< "${retire_prs}"
	# Issues close even when their PR could not be closed: no later clean
	# report retries this head, and the merge-side guard refuses that PR.
	while IFS= read -r retire_item; do
		[[ "${retire_item}" =~ ^[1-9][0-9]*$ ]] || continue
		retire_body="## Superseded by a clean security audit

The clean single-issue security audit of \`${retire_sha}\` (PR #${retire_pr}) superseded this finding, so it is closed as not planned.

${retire_marker}"
		if ! gh api "repos/${REPOSITORY}/issues/${retire_item}/comments" -f body="${retire_body}" >/dev/null 2>&1; then
			single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=comment_failed item=${retire_item} kind=issue"
		fi
		if ! { if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api -X PATCH "repos/${REPOSITORY}/issues/${retire_item}" -f state=closed -f state_reason=not_planned
		else
			gh api -X PATCH "repos/${REPOSITORY}/issues/${retire_item}" -f state=closed -f state_reason=not_planned
		fi; } >/dev/null 2>&1; then
			single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followup_retire_failed reason=issue_close_failed item=${retire_item}"
			continue
		fi
		retire_issue_count=$((retire_issue_count + 1))
	done <<< "${retire_issues}"
	single_pass_log "mode=report pr=${retire_pr} head=${retire_sha} outcome=followups_retired issues=${retire_issue_count} prs=${retire_pr_count}"
	return 0
}

# Minutes since an ISO-8601 timestamp; 0 for a missing or invalid one, so a
# bad timestamp never triggers the early cancelled-audit check.
single_pass_marker_age_minutes()
{
	local marker_minutes
	marker_minutes="$(python3 -c 'import datetime,sys; t=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); assert t.tzinfo is not None; print(max(0, int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()//60)))' "$1" 2>/dev/null)" || marker_minutes=""
	printf '%s\n' "${marker_minutes:-0}"
}

single_pass_marker_age_hours()
{
	local created_at="$1" marker_hours
	marker_hours="$(python3 -c 'import datetime,sys; t=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); assert t.tzinfo is not None; print(int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()//3600))' "${created_at}" 2>/dev/null)" || marker_hours=""
	# A missing or invalid date cannot justify silencing a security hold.
	printf '%s\n' "${marker_hours:-999999}"
}

# Base-sync carry (issue #9533). The pipeline's freshness update merges the
# base into an already-audited head and records <!-- ai:merge-base-sync:v1
# pr= from=<approved head> sync= base=<base parent> merge=<update commit> -->
# (scripts/pr_checks_lib.sh). The update commit carries the clean audit of
# from= only when all of these hold; anything unverifiable returns 1 and the
# head is audited as before:
#   - review_head_gate_freshness_sync (scripts/review_head_gate.sh, the one
#     marker parser) accepts the head: newest trusted marker with merge=<head>,
#     parents [from, base parent], a verified web-flow merge, base parent on
#     the base branch;
#   - the newest trusted security-pass marker for from= is status=clean
#     (single_pass_markers, the same author filter as the gate);
#   - locally, head^1 == from and head^2 == base parent, from and the base
#     parent have exactly one merge base, and the PR's changed files
#     (base parent..head) equal the audited ones (merge base..from);
#   - over those files, the -U0 diffs merge base..from and base parent..head
#     have the same `git patch-id --verbatim` (stable ordering, whitespace
#     kept: an indentation-only change is a mismatch; a git without
#     --verbatim yields no id and fails closed), so the merge changed none of the
#     PR's own lines and added nothing the base side did not already contain.
# Sets single_pass_carry_from on success. Logs outcome=carry_skipped
# reason=<token> otherwise and leaves the token in
# single_pass_carry_skip_reason, so single_pass_gate can tell a merge proven
# impure (no freshness-sync inheritance either) from one it could not check.
# API budget: the helper's one commits/<head> and one compare read, only when
# a trusted marker is bound to this head.
single_pass_carry_skip()
{
	single_pass_carry_skip_reason="$2"
	single_pass_log "mode=gate pr=${PR_NUMBER:-} head=$1 outcome=carry_skipped reason=$2"
	return 1
}

single_pass_base_sync_carry()
{
	local comments_file="$1" head_sha="$2" base_ref="$3" head_ref="$4" markers_tsv="$5"
	local carry_dir carry_tmp carry_result carry_rc carry_from carry_base_parent carry_sha carry_mb carry_from_status
	local carry_pr_files carry_audited_files carry_patch_audited carry_patch_head
	local -a carry_paths=()
	single_pass_carry_from=""
	single_pass_carry_skip_reason=""
	if [ "${SECURITY_PASS_BASE_SYNC_CARRY_ENABLED:-true}" = "false" ]; then
		single_pass_carry_skip "${head_sha}" disabled
		return 1
	fi
	# Same trusted support copy as single_pass_inherited_clean when staged;
	# otherwise the helpers next to this script.
	if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/review_head_gate.sh" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" ]; then
		carry_dir="${SUPPORT_SCRIPTS_DIR}"
	else
		carry_dir="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || carry_dir=""
	fi
	if [ -z "${carry_dir}" ] || [ ! -f "${carry_dir}/review_head_gate.sh" ]; then
		single_pass_carry_skip "${head_sha}" helper_missing
		return 1
	fi
	carry_tmp="$(mktemp -d)" || { single_pass_carry_skip "${head_sha}" tmp_unavailable; return 1; }
	if ! jq '[.[]? | select(type == "object") | {author_login: (.user.login // ""), body: (.body // "")}]' "${comments_file}" > "${carry_tmp}/comments.json" 2>/dev/null; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" comments_unreadable
		return 1
	fi
	# The shared verifier runs in a subshell so sourcing its helpers cannot
	# change this gate's own functions (gh_retry) or shell options.
	carry_result="$(
		# shellcheck source=/dev/null
		source "${carry_dir}/gh_helpers.sh" >/dev/null 2>&1 || true
		type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
		# shellcheck source=/dev/null
		source "${carry_dir}/review_head_gate.sh" >/dev/null 2>&1 || exit 3
		set +e
		review_head_gate_freshness_sync "${REPOSITORY}" "${PR_NUMBER}" "${head_sha}" "${base_ref}" "${SECURITY_PASS_AUTHOR_LOGIN}" "${carry_tmp}/comments.json" > "${carry_tmp}/helper.log" 2>/dev/null
		printf '%s %s %s\n' "$?" "${REVIEW_HEAD_GATE_FRESHNESS_FROM:-}" "${REVIEW_HEAD_GATE_FRESHNESS_BASE_PARENT:-}"
	)" || carry_result=""
	[ -s "${carry_tmp}/helper.log" ] && cat "${carry_tmp}/helper.log"
	read -r carry_rc carry_from carry_base_parent <<< "${carry_result}"
	if [ "${carry_rc:-}" != "0" ] || ! [[ "${carry_from:-}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${carry_base_parent:-}" =~ ^[0-9a-f]{40}$ ]]; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" "$([ -n "${carry_result}" ] && echo freshness_unverified || echo helper_missing)"
		return 1
	fi
	carry_from_status="$(printf '%s\n' "${markers_tsv}" | awk -F'\t' -v h="${carry_from}" '$4 == h' | tail -n 1 | cut -f3)"
	if [ "${carry_from_status}" != "clean" ]; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" from_not_clean
		return 1
	fi
	for carry_sha in "${head_sha}" "${carry_from}" "${carry_base_parent}"; do
		if ! git cat-file -e "${carry_sha}^{commit}" 2>/dev/null; then
			if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ] \
				&& git check-ref-format --branch "${head_ref}" >/dev/null 2>&1; then
				git fetch --no-tags --unshallow origin "+refs/heads/${head_ref}:refs/remotes/origin/${head_ref}" >/dev/null 2>&1 || true
			fi
			if ! git cat-file -e "${carry_sha}^{commit}" 2>/dev/null; then
				rm -rf "${carry_tmp}"
				single_pass_carry_skip "${head_sha}" git_objects_unavailable
				return 1
			fi
		fi
	done
	if [ "$(git rev-parse --verify -q "${head_sha}^1" 2>/dev/null)" != "${carry_from}" ] \
		|| [ "$(git rev-parse --verify -q "${head_sha}^2" 2>/dev/null)" != "${carry_base_parent}" ] \
		|| git rev-parse --verify -q "${head_sha}^3" >/dev/null 2>&1; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" parents_mismatch
		return 1
	fi
	carry_mb="$(git merge-base --all "${carry_from}" "${carry_base_parent}" 2>/dev/null)" || carry_mb=""
	if ! [[ "${carry_mb}" =~ ^[0-9a-f]{40}$ ]]; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" merge_base_ambiguous
		return 1
	fi
	carry_pr_files="${carry_tmp}/pr_files"
	carry_audited_files="${carry_tmp}/audited_files"
	if ! git diff --name-only -z --no-renames "${carry_base_parent}" "${head_sha}" 2>/dev/null | sort -z > "${carry_pr_files}" \
		|| ! git diff --name-only -z --no-renames "${carry_mb}" "${carry_from}" 2>/dev/null | sort -z > "${carry_audited_files}" \
		|| [ ! -s "${carry_pr_files}" ] || ! cmp -s "${carry_pr_files}" "${carry_audited_files}"; then
		rm -rf "${carry_tmp}"
		single_pass_carry_skip "${head_sha}" changed_files_differ
		return 1
	fi
	mapfile -d '' -t carry_paths < "${carry_pr_files}"
	carry_patch_audited="$(GIT_LITERAL_PATHSPECS=1 git diff --no-ext-diff --no-textconv --no-renames --binary -U0 "${carry_mb}" "${carry_from}" -- "${carry_paths[@]}" 2>/dev/null | git patch-id --verbatim 2>/dev/null | awk '{ print $1 }')" || carry_patch_audited=""
	carry_patch_head="$(GIT_LITERAL_PATHSPECS=1 git diff --no-ext-diff --no-textconv --no-renames --binary -U0 "${carry_base_parent}" "${head_sha}" -- "${carry_paths[@]}" 2>/dev/null | git patch-id --verbatim 2>/dev/null | awk '{ print $1 }')" || carry_patch_head=""
	rm -rf "${carry_tmp}"
	if ! [[ "${carry_patch_audited}" =~ ^[0-9a-f]{40}$ ]] || [ "${carry_patch_audited}" != "${carry_patch_head}" ]; then
		single_pass_carry_skip "${head_sha}" patch_mismatch
		return 1
	fi
	single_pass_carry_from="${carry_from}"
	return 0
}

# Walk verified merge-base freshness updates back from <head> to an approved
# head with a trusted clean marker (see the header). Prints the clean head and
# the number of updates walked as "<sha> <hops>" and returns 0; returns 1 when
# any update cannot be verified, the walk meets a head with a non-clean marker,
# or the support helpers are unavailable. Verification is
# review_head_gate_freshness_sync, the check that already lets the same head
# skip the reviewer panel; it runs in a subshell from SUPPORT_SCRIPTS_DIR (the
# trusted support checkout, never the PR head).
SINGLE_PASS_INHERIT_MAX_HOPS=10
single_pass_inherited_clean()
{
	local comments_file="$1" markers="$2" head_sha="$3" base_ref="$4"
	local support_dir="${SUPPORT_SCRIPTS_DIR:-}" inherit_work inherit_comments inherit_markers inherit_result
	[ -n "${support_dir}" ] && [ -f "${support_dir}/review_head_gate.sh" ] && [ -f "${support_dir}/gh_helpers.sh" ] || return 1
	[ -n "${SECURITY_PASS_AUTHOR_LOGIN:-}" ] || return 1
	inherit_work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/single_pass_inherit.XXXXXX" 2>/dev/null)" || return 1
	inherit_comments="${inherit_work}/comments.json"
	inherit_markers="${inherit_work}/markers.tsv"
	if ! jq '[.[]? | select(type == "object") | {author_login: (.user.login // ""), body: (.body // "")}]' "${comments_file}" > "${inherit_comments}" 2>/dev/null \
		|| ! printf '%s\n' "${markers}" > "${inherit_markers}"; then
		rm -rf "${inherit_work}"
		return 1
	fi
	# One subshell sources the helpers once and walks the whole chain; only
	# its last stdout line ("<clean head> <hops>") is the result.
	inherit_result="$(
		cd "${support_dir}" || exit 1
		# shellcheck source=/dev/null
		source "${support_dir}/gh_helpers.sh" >/dev/null 2>&1 || exit 1
		# shellcheck source=/dev/null
		source "${support_dir}/review_head_gate.sh" >/dev/null 2>&1 || exit 1
		inherit_cur="${head_sha}"
		inherit_hops=0
		while [ "${inherit_hops}" -lt "${SINGLE_PASS_INHERIT_MAX_HOPS}" ]; do
			review_head_gate_freshness_sync "${REPOSITORY}" "${PR_NUMBER}" "${inherit_cur}" "${base_ref}" \
				"${SECURITY_PASS_AUTHOR_LOGIN}" "${inherit_comments}" >&2 || exit 1
			inherit_from="${REVIEW_HEAD_GATE_FRESHNESS_SYNC_FROM:-}"
			[[ "${inherit_from}" =~ ^[0-9a-f]{40}$ ]] || exit 1
			inherit_hops=$((inherit_hops + 1))
			inherit_status="$(awk -F'\t' -v h="${inherit_from}" '$4 == h { s = $3 } END { print s }' "${inherit_markers}")"
			case "${inherit_status}" in
				clean) printf '%s %s\n' "${inherit_from}" "${inherit_hops}"; exit 0 ;;
				"") inherit_cur="${inherit_from}" ;;
				*) exit 1 ;;
			esac
		done
		exit 1
	)" || inherit_result=""
	rm -rf "${inherit_work}"
	[[ "${inherit_result}" =~ ^[0-9a-f]{40}\ [0-9]+$ ]] || return 1
	printf '%s\n' "${inherit_result}"
	return 0
}

single_pass_gate()
{
	local pr_json="${PR_PAYLOAD_FILE:-}" comments="${PR_ISSUE_COMMENTS_FILE:-}" default_branch="${DEFAULT_BRANCH:-}"
	local max_cycles="${MAX_SECURITY_PASS_CYCLES:-5}" stale_hours="${SECURITY_PASS_PENDING_STALE_HOURS:-6}"
	local followup_stale_hours="${SECURITY_PASS_FOLLOWUP_STALE_HOURS:-24}" followup_count followup_age followup_ids followup_expected
	local exhausted_head_limit="${SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS:-2}" exhausted_retry="false" head_attempts=0
	# Issue #9942: lost (cancelled, never reported) head attempts forgiven per
	# head; 0 restores the old counting.
	local lost_audit_retries="${SECURITY_PASS_LOST_AUDIT_RETRIES:-2}" lost_window_cap=0 lost_attempts=0 lost_forgiven=0
	local lost_row_created lost_row_cycle lost_row_result lost_row_next
	local pattern="${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-}"
	local state base head_ref head_sha head_repo labels linked skip_json markers latest latest_status latest_head latest_created
	local cycles_used next_cycle age_hours workflow body extensions effective_max completed_findings failed_marker_unrecorded="false"
	local gate_checkout_head
	local delta_source="" delta_since="" delta_comment_id="" delta_status="" delta_note="" dispatch_ok="false"
	local -a dispatch_args=() delta_dispatch_args=()
	local attempts_used cap_cycle effective_attempts pending_state="" active_audits
	local max_attempts="${SECURITY_PASS_MAX_AUDIT_ATTEMPTS:-}" max_concurrent="${SECURITY_PASS_MAX_CONCURRENT_AUDITS:-3}"
	local cancel_check_minutes="${SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES:-20}"
	[[ "${max_cycles}" =~ ^[1-9][0-9]*$ ]] || max_cycles=5
	# Attempts cap: every dispatch, reported or not. Default twice the cycle
	# cap; never below it, so the cycle cap still decides first.
	[[ "${max_attempts}" =~ ^[1-9][0-9]{0,3}$ ]] || max_attempts=$((max_cycles * 2))
	[ "${max_attempts}" -ge "${max_cycles}" ] || max_attempts="${max_cycles}"
	# 0 disables the throttle; 0 disables the cancelled-audit check.
	[[ "${max_concurrent}" =~ ^[0-9]{1,3}$ ]] || max_concurrent=3
	[[ "${cancel_check_minutes}" =~ ^[0-9]{1,4}$ ]] || cancel_check_minutes=20
	[[ "${stale_hours}" =~ ^[1-9][0-9]*$ ]] || stale_hours=6
	[[ "${followup_stale_hours}" =~ ^[1-9][0-9]*$ ]] || followup_stale_hours=24
	[[ "${exhausted_head_limit}" =~ ^[1-9][0-9]*$ ]] || exhausted_head_limit=2
	[[ "${lost_audit_retries}" =~ ^[0-9]{1,3}$ ]] || lost_audit_retries=2
	lost_audit_retries=$((10#${lost_audit_retries}))
	if [ "${SINGLE_ISSUE_SECURITY_PASS_ENABLED:-false}" = "false" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER:-} outcome=skip reason=disabled"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || [ ! -s "${pr_json}" ] || [ -z "${default_branch}" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER:-} outcome=skip reason=missing_input"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	state="$(jq -r '.state // ""' "${pr_json}")"
	base="$(jq -r '.base.ref // ""' "${pr_json}")"
	head_ref="$(jq -r '.head.ref // ""' "${pr_json}")"
	head_sha="$(jq -r '.head.sha // ""' "${pr_json}")"
	head_repo="$(jq -r '.head.repo.full_name // ""' "${pr_json}")"
	labels="$(jq -r '[.labels[]?.name] | join(",")' "${pr_json}")"
	if [ "${state}" != "open" ] || [ "${base}" != "${default_branch}" ] || [ "${head_repo}" != "${REPOSITORY}" ] \
		|| ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || printf '%s\n' "${head_ref}" | grep -Eq -- "${pattern}" \
		|| printf ',%s,' "${labels}" | grep -q ',e2e-smoke-test,'; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=skip reason=not_standalone"
		single_pass_state skip
		single_pass_output false
		return 0
	fi
	# Skip only when the sole linked issue is verified; mixed-issue PRs
	# must not inherit one follow-up's exemption.
	linked="$(printf '%s' "${LINKED_ISSUES_JSON:-[]}" | jq -r 'if type == "array" and length == 1 then .[0].number // empty else empty end' 2>/dev/null || true)"
	if [[ "${linked}" =~ ^[0-9]+$ ]] && [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/security_pass_skip.py" ]; then
		skip_json="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR:-scripts}/security_pass_skip.py" --repo "${REPOSITORY}" --issue "${linked}" 2>/dev/null || true)"
		if [ "$(printf '%s' "${skip_json}" | jq -r '.skip // false' 2>/dev/null)" = "true" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=skip reason=verified_followup issue=${linked}"
			single_pass_state skip
			single_pass_output false
			return 0
		fi
	fi
	# The gate job also probes /user, but its identity is not exported to this job.
	SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	fi
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ] || [ ! -s "${comments}" ] \
		|| ! jq -e 'type == "array"' "${comments}" >/dev/null 2>&1; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=markers_unverifiable"
		single_pass_state unverifiable
		single_pass_output true markers_unverifiable
		return 0
	fi
	if ! markers="$(single_pass_markers "${comments}")"; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=markers_unverifiable"
		single_pass_state unverifiable
		single_pass_output true markers_unverifiable
		return 0
	fi
	if ! extensions="$(single_pass_extensions "${comments}" "${head_sha}" . "${head_ref}")"; then
		# The checkout can be ahead of the PR head when this run made a commit
		# it has not pushed yet: the conflict resolver's [ai-merge-resolve]
		# commit is pushed by the job's final push step, after the review-blocked
		# judge. An extension marker can only be checked with the checkout at the
		# PR head, so the merge holds, but the push starts a new review whose
		# gate audits the new head: no human is needed (PRs #9338 and #9283
		# paged extensions_unverifiable for this). Checked only here, after an
		# extension marker failed: with no marker an unpushed local commit must
		# not hold the gate (#9011 / #9134). A checkout that is not a descendant
		# of the PR head still takes the extensions_unverifiable hold.
		gate_checkout_head="$(git rev-parse HEAD 2>/dev/null || true)"
		if [[ "${gate_checkout_head}" =~ ^[0-9a-f]{40}$ ]] && [ "${gate_checkout_head}" != "${head_sha}" ] \
			&& git merge-base --is-ancestor "${head_sha}" "${gate_checkout_head}" 2>/dev/null; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} checkout=${gate_checkout_head} outcome=hold reason=head_unpushed"
			single_pass_state unverifiable
			single_pass_output true head_unpushed
			return 0
		fi
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=extensions_unverifiable"
		single_pass_state unverifiable
		single_pass_output true extensions_unverifiable
		return 0
	fi
	effective_max=$((max_cycles + extensions))
	effective_attempts=$((max_attempts + extensions))
	# Pending-only cycles (audits that never reported) do not use the cycle
	# budget; the attempts cap bounds them instead.
	read -r attempts_used cycles_used cap_cycle <<< "$(single_pass_cycle_accounting "${markers}" "${effective_max}")"
	[[ "${attempts_used}" =~ ^[0-9]+$ ]] || attempts_used=0
	[[ "${cycles_used}" =~ ^[0-9]+$ ]] || cycles_used=0
	[[ "${cap_cycle}" =~ ^[0-9]+$ ]] || cap_cycle=0
	# Issue #9942: past a cap, start the one audit-run listing early enough to
	# cover the oldest pending head attempt above it (set before any fetch, so
	# every caller shares the same cached listing). Off with 0 retries.
	if [ "${lost_audit_retries}" -gt 0 ] && { [ "${cycles_used}" -ge "${effective_max}" ] || [ "${attempts_used}" -ge "${effective_attempts}" ]; }; then
		lost_window_cap="${cap_cycle}"
		if [ "${lost_window_cap}" -eq 0 ] || [ "${lost_window_cap}" -gt "${effective_attempts}" ]; then
			lost_window_cap="${effective_attempts}"
		fi
		SINGLE_PASS_AUDIT_RUNS_FROM="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" -v cap="${lost_window_cap}" '$3 == "pending" && $4 == h && $5 + 0 > cap + 0 { print $1; exit }')"
	fi
	latest="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" '$4 == h' | tail -n 1)"
	latest_created="$(printf '%s' "${latest}" | cut -f1)"
	latest_status="$(printf '%s' "${latest}" | cut -f3)"
	latest_head="$(printf '%s' "${latest}" | cut -f4)"
	completed_findings="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" '$3 == "findings" && $4 == h' | tail -n 1)"
	if [ "${latest_status}" = "clean" ] && [ "${latest_head}" = "${head_sha}" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=clean"
		single_pass_state clean
		single_pass_output false
		return 0
	fi
	# A verified freshness update of a clean head needs no audit of its own
	# (see the header). Only a head with no marker at all is considered, so
	# an audit already pending, found or failed on this head still decides.
	# Issue #9533: the base-sync carry (one verified update of a clean head
	# whose merge is pure over the PR's files) decides first; the multi-hop
	# inheritance walk is the fallback. A carry that proved the merge impure
	# (parents, changed files or patch-id differ) audits the head, and one
	# whose shared verifier already refused this head skips the walk, which
	# would repeat that same check and its reads.
	if [ -z "${latest}" ]; then
		local inherited=""
		if single_pass_base_sync_carry "${comments}" "${head_sha}" "${default_branch}" "${head_ref}" "${markers}"; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=clean reason=base_sync_carry from=${single_pass_carry_from}"
			single_pass_state clean
			single_pass_output false
			return 0
		fi
		case "${single_pass_carry_skip_reason:-}" in
			freshness_unverified|parents_mismatch|changed_files_differ|patch_mismatch) ;;
			*)
				if inherited="$(single_pass_inherited_clean "${comments}" "${markers}" "${head_sha}" "${default_branch}")"; then
					single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=clean reason=freshness_sync_inherited from=${inherited%% *} hops=${inherited##* }"
					single_pass_state clean
					single_pass_output false
					return 0
				fi
				;;
		esac
	fi
	if [ "${latest_status}" = "pending" ] && [ "${latest_head}" = "${head_sha}" ]; then
		age_hours=0
		if [ -n "${latest_created}" ]; then
			age_hours="$(python3 -c 'import datetime,sys; t=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); print(int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()//3600))' "${latest_created}" 2>/dev/null || echo 0)"
		fi
		# An audit cancelled in the concurrency queue (or otherwise finished
		# without a result) is retried now instead of after the stale window.
		if [ "${age_hours}" -lt "${stale_hours}" ] && [ "${cancel_check_minutes}" -gt 0 ] \
			&& [ "$(single_pass_marker_age_minutes "${latest_created}")" -ge "${cancel_check_minutes}" ]; then
			# Fetch in this shell so the cached listing survives for the
			# throttle check; the $(...) below only reads it.
			single_pass_audit_runs || true
			pending_state="$(single_pass_pending_audit_state "${latest_created}")"
		fi
		if [ "${age_hours}" -lt "${stale_hours}" ] && [ "${pending_state}" != "lost" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=audit_pending cycle=${cycles_used}${pending_state:+ audit_run=${pending_state}}"
			single_pass_state pending
			single_pass_output true audit_pending
			return 0
		fi
	fi
	# Issue #9533: a stale pending marker on the pipeline's own base sync of a
	# clean head can still be carried (a head with no marker was decided
	# above). It is evaluated after the fresh-pending hold, so an audit still
	# pending on this head decides. A findings or failed result for this head
	# is never overridden; any unverifiable check falls through.
	if [ "${latest_status}" = "pending" ] \
		&& single_pass_base_sync_carry "${comments}" "${head_sha}" "${default_branch}" "${head_ref}" "${markers}"; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=clean reason=base_sync_carry from=${single_pass_carry_from}"
		single_pass_state clean
		single_pass_output false
		return 0
	fi
	if [ "${latest_status}" = "findings" ] && [ "${latest_head}" = "${head_sha}" ] && [ "${cycles_used}" -lt "${effective_max}" ]; then
		# The follow-up fixes merge into this branch and change the head; the
		# next cycle audits that head.
		if [ "${SINGLE_PASS_STATUS_ONLY}" != "true" ]; then
			# The trusted result comment, not just the branch, binds the open
			# issues to every finding from this particular head and cycle.
			followup_ids="$(jq -r --argjson id "$(printf '%s' "${latest}" | cut -f2)" --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" --arg marker "$(single_pass_marker findings "${head_sha}" "$(printf '%s' "${latest}" | cut -f5)")" '
				[.[] | select(.id == $id and .user.login == $author and (.body | type) == "string")
				| .body | split("\n") | map(select(test("\\S")))
				| select(.[-1] == $marker)
				| map(select(startswith("Finding IDs (base64 JSON): ")))]
				| if length == 1 and (.[0] | length) == 1 then .[0][0] | ltrimstr("Finding IDs (base64 JSON): ") else empty end
			' "${comments}" 2>/dev/null)" || followup_ids=""
			followup_ids="$(printf '%s' "${followup_ids}" | base64 -d 2>/dev/null)" || followup_ids=""
			if ! followup_expected="$(jq -er '
				select(type == "array" and length > 0 and (unique | length) == length and all(.[]; type == "string" and length > 0 and test("^[^<>\\r\\n]+$"))) | length
			' <<< "${followup_ids}" 2>/dev/null)"; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=followups_unverifiable cycle=${cycles_used}"
				single_pass_output true followups_unverifiable
				return 0
			fi
			if ! followup_count="$(single_pass_open_followups "${head_ref}" "${followup_ids}")"; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=followups_unverifiable cycle=${cycles_used}"
				single_pass_output true followups_unverifiable
				return 0
			fi
			if [ "${followup_count}" -ne "${followup_expected}" ]; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=followups_missing cycle=${cycles_used}"
				single_pass_output true followups_missing
				return 0
			fi
			followup_age="$(single_pass_marker_age_hours "${latest_created}")"
			if [ "${followup_age}" = "999999" ] || [ "${followup_age}" -ge "${followup_stale_hours}" ]; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=followups_stalled cycle=${cycles_used} followups=${followup_count} age_hours=${followup_age}"
				single_pass_output true followups_stalled
				return 0
			fi
		fi
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=awaiting_followups cycle=${cycles_used}"
		single_pass_state findings
		single_pass_output true awaiting_followups
		return 0
	fi
	if [ "${pending_state}" = "lost" ]; then
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=retry reason=audit_run_lost cycle=${cycles_used} attempt=${attempts_used}"
	fi
	if [ "${cycles_used}" -ge "${effective_max}" ] || [ "${attempts_used}" -ge "${effective_attempts}" ]; then
		# Post-cap head retries are the attempts numbered above the point the
		# cap was reached (the cycle cap, or the attempts cap if that came first).
		if [ "${cap_cycle}" -eq 0 ] || [ "${cap_cycle}" -gt "${effective_attempts}" ]; then
			cap_cycle="${effective_attempts}"
		fi
		if [ -z "${completed_findings}" ]; then
			if [ "${lost_audit_retries}" -gt 0 ]; then
				# Issue #9942: one row per post-cap head attempt (distinct cycle
				# with a pending or failed marker): first created, cycle, 1 when
				# the cycle has any non-pending marker, and the created_at of
				# the next later-cycle marker on this head (window end). A cycle
				# with a result counts; a pending-only one counts unless its
				# audit run is provably lost. Up to lost_audit_retries lost
				# attempts are forgiven; any beyond count again.
				single_pass_audit_runs || true
				while IFS=$'\t' read -r lost_row_created lost_row_cycle lost_row_result lost_row_next; do
					[[ "${lost_row_cycle:-}" =~ ^[0-9]+$ ]] || continue
					if [ "${lost_row_result}" = "0" ] && [ "$(single_pass_marker_audit_lost "${lost_row_created}" "${lost_row_next}")" = "lost" ]; then
						lost_attempts=$((lost_attempts + 1))
					else
						head_attempts=$((head_attempts + 1))
					fi
				done <<< "$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" -v cap="${cap_cycle}" '
					$4 == h { n++; cr[n] = $1; st[n] = $3; cy[n] = $5 + 0 }
					END {
						for (i = 1; i <= n; i++) {
							c = cy[i]
							if (c <= cap + 0 || (st[i] != "pending" && st[i] != "failed") || (c in done)) continue
							done[c] = 1
							res = 0
							for (j = 1; j <= n; j++) if (cy[j] == c && st[j] != "pending") res = 1
							nx = ""
							for (j = i + 1; j <= n; j++) if (cy[j] > c) { nx = cr[j]; break }
							printf "%s\t%d\t%d\t%s\n", cr[i], c, res, nx
						}
					}')"
				lost_forgiven="${lost_attempts}"
				[ "${lost_forgiven}" -le "${lost_audit_retries}" ] || lost_forgiven="${lost_audit_retries}"
				head_attempts=$((head_attempts + lost_attempts - lost_forgiven))
			else
				head_attempts="$(printf '%s\n' "${markers}" | awk -F'\t' -v h="${head_sha}" -v cap="${cap_cycle}" '($3 == "pending" || $3 == "failed") && $4 == h && $5 + 0 > cap && !seen[$5]++ { n++ } END { print n + 0 }')"
			fi
			if [ "${head_attempts}" -lt "${exhausted_head_limit}" ]; then
				exhausted_retry="true"
				if [ "${lost_forgiven}" -gt 0 ]; then
					single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=retry reason=lost_audit_not_counted head_attempts=${head_attempts} lost_attempts=${lost_attempts}"
				fi
			fi
		fi
		if [ "${exhausted_retry}" != "true" ]; then
			if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
				if [ -n "${completed_findings}" ]; then
					single_pass_log "mode=status pr=${PR_NUMBER} head=${head_sha} outcome=exhausted cycle=${cycles_used} max=${effective_max}"
					single_pass_state exhausted
					echo "SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD=${head_sha}"
				else
					single_pass_state exhausted_unaudited
				fi
				return 0
			fi
			if ! printf ',%s,' "${labels}" | grep -q ',ai:security-pass-failed,'; then
				if ! gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/labels" -f 'labels[]=ai:security-pass-failed' >/dev/null 2>&1; then
					echo "::error::Could not label PR #${PR_NUMBER} ai:security-pass-failed; failing closed so workflow recovery can retry."
					single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=failed reason=label_write_failed cycle=${cycles_used}"
					single_pass_output true label_write_failed
					return 1
				fi
				if [ -n "${completed_findings}" ]; then
					body="## Single-issue security pass exhausted

The security audit of this PR has used ${cycles_used} of ${effective_max} cycles, so auto-merge stays off. The PR is labelled \`ai:security-pass-failed\`. The review-blocked judge may merge with medium/low findings open, fix blocking high/critical/unrated findings while retries remain, or hold the PR for a clean audit or human decision."
				else
					body="## Single-issue security pass: unaudited head

No completed audit exists for \`${head_sha}\`. Auto-merge stays off. A new push starts a fresh per-head audit budget; /re-run rechecks this head."
				fi
				gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1 \
					|| echo "::warning::Could not post the security-pass exhaustion comment on PR #${PR_NUMBER}."
			fi
			if [ -n "${completed_findings}" ]; then
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=cycles_exhausted cycle=${cycles_used}"
				single_pass_output true cycles_exhausted
				single_pass_exhausted_output
			else
				single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=exhausted_without_completed_audit cycle=${cycles_used} head_attempts=${head_attempts} lost_attempts=${lost_attempts}"
				single_pass_output true exhausted_without_completed_audit
			fi
			return 0
		fi
	fi
	# The marker numbers every attempt; the message shows the budget cycle.
	next_cycle=$((attempts_used + 1))
	if [ "${SINGLE_PASS_STATUS_ONLY}" = "true" ]; then
		single_pass_state needs_audit
		return 0
	fi
	workflow="$(single_pass_audit_workflow)"
	# Per-PR audit concurrency groups no longer serialise audits, so bound how
	# many run at once. Throttled: no dispatch and no marker, so no cycle or
	# attempt is used; the next review run (sweep tick) tries again.
	if [ "${max_concurrent}" -gt 0 ]; then
		single_pass_audit_runs || true
		active_audits="$(single_pass_active_audits)"
		if [[ "${active_audits}" =~ ^[0-9]+$ ]] && [ "${active_audits}" -ge "${max_concurrent}" ]; then
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=audit_throttled active=${active_audits} max=${max_concurrent} cycle=${cycles_used}"
			single_pass_output true audit_throttled
			return 0
		fi
	fi
	dispatch_args=(workflow run "${workflow}" -R "${REPOSITORY}" --ref "${default_branch}" -f ref="${head_ref}" -f pr_number="${PR_NUMBER}")
	# Delta re-audit (stuck-states P5a): never for the exhausted-head retry, so
	# the final audit of an exhausted head stays a full merge-base..head audit.
	if [ "${SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED:-true}" != "false" ] && [ "${exhausted_retry}" != "true" ]; then
		delta_source="$(single_pass_delta_source "${markers}" "${head_sha}")"
		if [ -n "${delta_source}" ]; then
			delta_since="$(printf '%s' "${delta_source}" | cut -f1)"
			delta_comment_id="$(printf '%s' "${delta_source}" | cut -f2)"
			delta_status="$(printf '%s' "${delta_source}" | cut -f3)"
			# A findings result without the stored payload (posted before P5a,
			# or omitted over the size cap) can never be verified by the audit,
			# so request a full audit instead of announcing a delta re-audit.
			if [ "${delta_status}" = "findings" ] && ! jq -e --argjson id "${delta_comment_id}" \
				'any(.[]?; type == "object" and .id == $id and ((.body // "") | test("(^|\n)Audited findings sha256: [0-9a-f]{64}")))' \
				"${comments}" >/dev/null 2>&1; then
				delta_since=""
			fi
		fi
		if [ -n "${delta_since}" ]; then
			delta_dispatch_args=("${dispatch_args[@]}" -f audit_diff_since="${delta_since}")
			if [ "${delta_status}" = "findings" ]; then
				delta_dispatch_args+=(-f prior_findings="${delta_comment_id}")
			fi
		fi
	fi
	if [ -n "${delta_since}" ]; then
		if gh "${delta_dispatch_args[@]}" >/dev/null 2>&1; then
			dispatch_ok="true"
			delta_note="This is a delta re-audit of the changes since \`${delta_since}\`, the last audited head"
			if [ "${delta_status}" = "findings" ]; then
				delta_note+="; its open findings are verified again or carried forward."
			else
				delta_note+="."
			fi
		else
			# A consumer wrapper that predates the delta inputs rejects them;
			# retry once with today's arguments instead of burning a cycle.
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=delta_fallback reason=dispatch_rejected cycle=${next_cycle} workflow=${workflow}"
			if gh "${dispatch_args[@]}" >/dev/null 2>&1; then
				dispatch_ok="true"
			fi
		fi
	elif gh "${dispatch_args[@]}" >/dev/null 2>&1; then
		dispatch_ok="true"
	fi
	if [ "${dispatch_ok}" != "true" ]; then
		if [ "${exhausted_retry}" = "true" ]; then
			# Record the failed retry as a used head attempt (head_attempts counts
			# failed markers above the cap), so repeated dispatch failures reach
			# the ai:security-pass-failed label instead of retrying forever.
			body="## Single-issue security pass: retry audit

The retry audit of \`${head_sha}\` could not be dispatched (\`${workflow}\`). Auto-merge stays off, and this counts as head attempt $((head_attempts + 1)) of ${exhausted_head_limit}. Once the head attempts are used up, the PR is labelled \`ai:security-pass-failed\`.

$(single_pass_marker failed "${head_sha}" "${next_cycle}")"
			if ! gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1; then
				echo "::error::Could not post the failed security-pass marker on PR #${PR_NUMBER}; the head attempt is not recorded. Holding the merge and failing closed so workflow recovery retries."
				failed_marker_unrecorded="true"
			fi
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=dispatch_failed_exhausted cycle=${next_cycle}"
		else
			# Below the cycle cap, record the failed dispatch as a used cycle so
			# repeated failures count toward exhaustion.
			echo "::warning::Could not dispatch ${workflow} for PR #${PR_NUMBER}; holding auto-merge and recording cycle $((cycles_used + 1)) of ${effective_max} as failed. A later review run retries the dispatch."
			body="## Single-issue security pass

The audit of \`${head_sha}\` could not be dispatched (\`${workflow}\`). A consumer \`ai-security-audit.yml\` that predates the \`pr_number\` input fails this way; syncing the wrapper fixes it. Auto-merge stays off, and this counts as cycle $((cycles_used + 1)) of ${effective_max}. A later review run retries the dispatch. Once the cycles are used up, the PR is labelled \`ai:security-pass-failed\`.

$(single_pass_marker failed "${head_sha}" "${next_cycle}")"
			if ! gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1; then
				echo "::error::Could not post the failed security-pass marker on PR #${PR_NUMBER}; the cycle is not recorded. Holding the merge and failing closed so workflow recovery retries."
				failed_marker_unrecorded="true"
			fi
			single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=dispatch_failed workflow=${workflow} cycle=${next_cycle}"
		fi
		echo "::warning::Could not dispatch ${workflow} for PR #${PR_NUMBER}; holding the merge until an audit can run."
		single_pass_output true dispatch_failed
		# An unrecorded failure cannot count toward exhaustion; fail the step
		# (hold already written) so the review failure path surfaces it and its
		# identical-failure cap escalates instead of retrying silently forever.
		if [ "${failed_marker_unrecorded}" = "true" ]; then
			return 1
		fi
		return 0
	fi
	if [ "${exhausted_retry}" = "true" ]; then
		body="## Single-issue security pass: retry audit

Auto-merge waits for a retry audit of the exhausted pass on \`${head_sha}\` (head attempt $((head_attempts + 1)) of ${exhausted_head_limit}).

$(single_pass_marker pending "${head_sha}" "${next_cycle}")"
	else
		body="## Single-issue security pass

Auto-merge waits for the security audit of \`${head_sha}\` (cycle $((cycles_used + 1)) of ${effective_max}). Findings become follow-up issues against this branch; a clean audit re-runs the review, which then merges.
${delta_note:+
${delta_note}
}
$(single_pass_marker pending "${head_sha}" "${next_cycle}")"
	fi
	if ! single_pass_post_pending_marker "${body}"; then
		echo "::error::Could not post the pending security-pass marker on PR #${PR_NUMBER} after ${SINGLE_PASS_PENDING_MARKER_ATTEMPTS_USED} attempt(s); the dispatched audit cannot publish its result. Holding the merge and failing closed so workflow recovery retries."
		single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=hold reason=pending_marker_failed cycle=${next_cycle} workflow=${workflow}"
		single_pass_output true pending_marker_failed
		return 1
	fi
	single_pass_log "mode=gate pr=${PR_NUMBER} head=${head_sha} outcome=dispatched cycle=${next_cycle} workflow=${workflow}$([ "${exhausted_retry}" = "true" ] && printf ' reason=exhausted_retry')$([ -n "${delta_note}" ] && printf ' delta_since=%s' "${delta_since}")"
	single_pass_output true audit_dispatched
}

# Shared by report and delta: the pipeline identity, then the
# installation-token fallback (SECURITY_PASS_AUTHOR_LOGIN_FALLBACK).
single_pass_resolve_author()
{
	# No other report/delta-side call provides the identity of the comment author.
	SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	fi
	[ -n "${SECURITY_PASS_AUTHOR_LOGIN}" ]
}

single_pass_delta_full()
{
	single_pass_log "mode=delta pr=${SECURITY_PASS_PR_NUMBER:-} head=${SECURITY_PASS_HEAD_SHA:-} outcome=full reason=$1"
}

# `delta` mode (stuck-states P5a): decide whether this audit may be narrowed to
# the changes since the last audited head. Fails open to a full audit: on any
# doubt it writes nothing to GITHUB_ENV.
single_pass_delta_resolve()
{
	local pr_number="${SECURITY_PASS_PR_NUMBER:-}" head_sha="${SECURITY_PASS_HEAD_SHA:-}"
	local since="${SECURITY_PASS_DELTA_SINCE_INPUT:-}" prior_input="${SECURITY_PASS_PRIOR_FINDINGS_INPUT:-}"
	local checkout="${SECURITY_PASS_AUDIT_CHECKOUT:-${GITHUB_WORKSPACE:-.}/audit-data}"
	local delta_comments_file delta_markers delta_resolved source_head source_id source_status source_cycle
	local delta_payload_line delta_payload_b64 delta_payload_digest prior_out delta_prior_count=0 ancestor_rc=0
	if [ "${SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED:-true}" = "false" ]; then
		single_pass_delta_full disabled
		return 0
	fi
	if ! [[ "${since}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${pr_number}" =~ ^[0-9]+$ ]] \
		|| [ "${since}" = "${head_sha}" ]; then
		single_pass_delta_full no_delta_input
		return 0
	fi
	if [ -z "${GITHUB_ENV:-}" ]; then
		single_pass_delta_full github_env_unset
		return 0
	fi
	if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -f "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh"
	fi
	if ! single_pass_resolve_author; then
		single_pass_delta_full author_unverifiable
		return 0
	fi
	delta_comments_file="$(mktemp)" || { single_pass_delta_full comments_unverifiable; return 0; }
	# No earlier step in the audit job reads the PR comments.
	if ! { { if type gh_retry >/dev/null 2>&1; then
		gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	else
		gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	fi; } 2>/dev/null | jq -s 'add // []' > "${delta_comments_file}" 2>/dev/null; } \
		|| ! jq -e 'type == "array"' "${delta_comments_file}" >/dev/null 2>&1 \
		|| ! delta_markers="$(single_pass_markers "${delta_comments_file}")"; then
		rm -f "${delta_comments_file}"
		single_pass_delta_full comments_unverifiable
		return 0
	fi
	delta_resolved="$(single_pass_delta_source "${delta_markers}" "${head_sha}")"
	source_head="$(printf '%s' "${delta_resolved}" | cut -f1)"
	source_id="$(printf '%s' "${delta_resolved}" | cut -f2)"
	source_status="$(printf '%s' "${delta_resolved}" | cut -f3)"
	source_cycle="$(printf '%s' "${delta_resolved}" | cut -f4)"
	# The dispatch inputs are a request; only the trusted markers decide.
	if [ -z "${delta_resolved}" ] || [ "${source_head}" != "${since}" ] \
		|| { [ "${source_status}" = "findings" ] && [ "${prior_input}" != "${source_id}" ]; } \
		|| { [ "${source_status}" = "clean" ] && [ -n "${prior_input}" ]; }; then
		rm -f "${delta_comments_file}"
		single_pass_delta_full input_mismatch
		return 0
	fi
	if [ "$(git -C "${checkout}" rev-parse HEAD 2>/dev/null || true)" != "${head_sha}" ]; then
		rm -f "${delta_comments_file}"
		single_pass_delta_full checkout_mismatch
		return 0
	fi
	git -C "${checkout}" merge-base --is-ancestor "${since}" "${head_sha}" 2>/dev/null || ancestor_rc=$?
	if [ "${ancestor_rc}" -ne 0 ]; then
		rm -f "${delta_comments_file}"
		single_pass_delta_full not_ancestor
		return 0
	fi
	prior_out=""
	if [ "${source_status}" = "findings" ]; then
		delta_payload_line="$(jq -r --argjson id "${source_id}" --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" --arg marker "$(single_pass_marker findings "${source_head}" "${source_cycle}")" '
			[.[] | select(type == "object" and .id == $id and .user.login == $author and (.body | type) == "string")
			| .body | split("\n") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))
			| select(.[-1] == $marker)]
			| if length == 1 then .[0] else empty end
			| [map(select(startswith("Audited findings (base64 JSON): ")) | ltrimstr("Audited findings (base64 JSON): ")),
			   map(select(startswith("Audited findings sha256: ")) | ltrimstr("Audited findings sha256: "))]
			| if (.[0] | length) == 1 and (.[1] | length) == 1 then .[0][0] + "\t" + .[1][0] else empty end
		' "${delta_comments_file}" 2>/dev/null)" || delta_payload_line=""
		rm -f "${delta_comments_file}"
		delta_payload_b64="$(printf '%s' "${delta_payload_line}" | cut -f1)"
		delta_payload_digest="$(printf '%s' "${delta_payload_line}" | cut -f2)"
		prior_out="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/single-pass-prior-findings.json"
		if [ -z "${delta_payload_line}" ] || ! [[ "${delta_payload_b64}" =~ ^[A-Za-z0-9+/=]+$ ]] || ! [[ "${delta_payload_digest}" =~ ^[0-9a-f]{64}$ ]] \
			|| ! delta_prior_count="$(PYTHONDONTWRITEBYTECODE=1 python3 -c '
import base64, binascii, hashlib, json, os, sys
try:
	raw = base64.b64decode(sys.argv[1], validate=True)
except (binascii.Error, ValueError):
	sys.exit(1)
if hashlib.sha256(raw).hexdigest() != sys.argv[2]:
	sys.exit(1)
try:
	data = json.loads(raw.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError):
	sys.exit(1)
if not isinstance(data, list) or not data or not all(
	isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"].strip() for item in data
):
	sys.exit(1)
tmp = sys.argv[3] + ".tmp"
with open(tmp, "wb") as handle:
	handle.write(raw)
os.replace(tmp, sys.argv[3])
print(len(data))
' "${delta_payload_b64}" "${delta_payload_digest}" "${prior_out}" 2>/dev/null)"; then
			single_pass_delta_full prior_findings_unverifiable
			return 0
		fi
	else
		rm -f "${delta_comments_file}"
	fi
	if ! {
		printf 'SECURITY_AUDIT_DIFF_SINCE=%s\n' "${since}"
		printf 'SECURITY_PASS_DELTA_SINCE=%s\n' "${since}"
		if [ -n "${prior_out}" ]; then
			printf 'SECURITY_AUDIT_PRIOR_FINDINGS=%s\n' "${prior_out}"
			printf 'SECURITY_PASS_PRIOR_FINDINGS_FILE=%s\n' "${prior_out}"
		fi
	} >> "${GITHUB_ENV}"; then
		single_pass_delta_full github_env_write_failed
		return 0
	fi
	single_pass_log "mode=delta pr=${pr_number} head=${head_sha} outcome=delta since=${since} prior_findings=${delta_prior_count}"
}

single_pass_report()
{
	local pr_number="${SECURITY_PASS_PR_NUMBER:-}" head_sha="${SECURITY_PASS_HEAD_SHA:-}" outcome="${SECURITY_PASS_AUDIT_OUTCOME:-}"
	local findings="${SECURITY_PASS_FINDINGS:-}" default_branch="${DEFAULT_BRANCH:-}" comments_file status cycle body review_workflow pr_head report_ids report_ids_invalid="false"
	local extensions=0 report_markers="" report_used=0 report_final="false"
	local max_cycles="${MAX_SECURITY_PASS_CYCLES:-5}" max_attempts="${SECURITY_PASS_MAX_AUDIT_ATTEMPTS:-}"
	local delta_on="false" carry_since="${SECURITY_PASS_DELTA_SINCE:-}" carry_prior="${SECURITY_PASS_PRIOR_FINDINGS_FILE:-}"
	local current_ids_json="" carry_touched carry_git_ok carry_candidates="[]" carry_open carry_listing_ok="true" carried="[]" carried_count=0 carried_ids=""
	local audited_json="" audited_b64="" audited_digest=""
	if [ "${SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED:-true}" != "false" ]; then
		delta_on="true"
	fi
	[[ "${max_cycles}" =~ ^[1-9][0-9]*$ ]] || max_cycles=5
	[[ "${max_attempts}" =~ ^[1-9][0-9]{0,3}$ ]] || max_attempts=$((max_cycles * 2))
	[ "${max_attempts}" -ge "${max_cycles}" ] || max_attempts="${max_cycles}"
	if ! [[ "${pr_number}" =~ ^[0-9]+$ ]] || ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		single_pass_log "mode=report pr=${pr_number} outcome=skip reason=missing_input"
		return 0
	fi
	# Audit data and PR number are independent dispatch inputs. Bind the result
	# to the live same-repository PR before trusting it as a merge authorization.
	if [ -z "${SECURITY_PASS_AUDIT_BRANCH:-}" ]; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_head_mismatch"
		return 0
	fi
	if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -f "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${GITHUB_WORKSPACE}/scripts/gh_helpers.sh"
	fi
	if type gh_retry >/dev/null 2>&1; then
		pr_head="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${pr_number}" 2>/dev/null)" || pr_head=""
	else
		pr_head="$(gh api "repos/${REPOSITORY}/pulls/${pr_number}" 2>/dev/null)" || pr_head=""
	fi
	if [ -z "${pr_head}" ]; then
		echo "::warning::Could not verify PR #${pr_number} for the security-pass report; leaving its audit result unpublished."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_lookup_failed"
		return 0
	fi
	if ! jq -e --arg sha "${head_sha}" --arg branch "${SECURITY_PASS_AUDIT_BRANCH}" --arg repo "${REPOSITORY}" --arg base "${default_branch}" \
			'.state == "open" and .head.sha == $sha and .head.ref == $branch and .head.repo.full_name == $repo and .base.ref == $base' <<< "${pr_head}" >/dev/null 2>&1; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pr_head_mismatch"
		return 0
	fi
	if ! single_pass_resolve_author; then
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=author_unverifiable"
		return 0
	fi
	if [ "${outcome}" = "success" ] && [[ "${findings}" =~ ^[0-9]+$ ]]; then
		if [ "${findings}" -eq 0 ]; then
			status="clean"
		else
			status="findings"
		fi
	else
		status="failed"
	fi
	comments_file="$(mktemp)"
	if { if type gh_retry >/dev/null 2>&1; then
		gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	else
		gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100"
	fi; } 2>/dev/null | jq -s 'add // []' > "${comments_file}" 2>/dev/null \
		&& jq -e 'type == "array"' "${comments_file}" >/dev/null 2>&1; then
		report_markers="$(single_pass_markers "${comments_file}")" || report_markers=""
		cycle="$(printf '%s\n' "${report_markers}" | awk -F'\t' -v h="${head_sha}" '$3 == "pending" && $4 == h { c = $5 } END { print c + 0 }')"
		# The audit result is only for head_sha if the audit checkout sits there,
		# whether or not any extension marker exists (single_pass_extensions now
		# checks the checkout only when a marker needs it).
		if [ "$(git -C "${GITHUB_WORKSPACE:-.}/audit-data" rev-parse HEAD 2>/dev/null || true)" != "${head_sha}" ] \
			|| ! extensions="$(single_pass_extensions "${comments_file}" "${head_sha}" "${GITHUB_WORKSPACE:-.}/audit-data" "${SECURITY_PASS_AUDIT_BRANCH}")"; then
			rm -f "${comments_file}"
			single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=extensions_unverifiable"
			return 0
		fi
	else
		cycle=0
	fi
	rm -f "${comments_file}"
	if [ "${cycle}" -lt 1 ]; then
		echo "::warning::No verified pending marker for PR #${pr_number} at ${head_sha}; refusing to publish an audit result."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=pending_unverifiable"
		return 0
	fi
	if [ "${delta_on}" = "true" ] && [ "${outcome}" = "success" ] && [[ "${findings}" =~ ^[0-9]+$ ]]; then
		current_ids_json="$(printf '%s' "${SECURITY_PASS_FINDING_IDS_B64:-}" | base64 -d 2>/dev/null | jq -c --argjson count "${findings}" 'select(type == "array" and length == $count and (unique | length) == length and all(.[]; type == "string" and length > 0 and test("^[^<>\\r\\n]+$")))' 2>/dev/null)" || current_ids_json=""
		# Delta carry-forward (stuck-states P5a): only after a delta audit that
		# was given prior findings.
		if [[ "${carry_since}" =~ ^[0-9a-f]{40}$ ]] && [ -n "${carry_prior}" ] && [ -r "${carry_prior}" ]; then
			carry_git_ok="true"
			if ! carry_touched="$(git -C "${GITHUB_WORKSPACE:-.}/audit-data" diff --no-renames --name-only "${carry_since}..${head_sha}" 2>/dev/null)"; then
				# Fail closed: without the touched-file list every prior finding
				# that was not re-reported is carried.
				carry_git_ok="false"
				carry_touched=""
			fi
			carry_candidates="$(jq -c --argjson current "${current_ids_json:-[]}" --argjson git_ok "${carry_git_ok}" \
				--argjson touched "$(printf '%s' "${carry_touched}" | jq -R -s -c 'split("\n") | map(select(length > 0))')" '
				[.[]? | select(type == "object" and (.finding_id | type) == "string" and (.finding_id | test("^[^<>\\r\\n]+$")) and (.file | type) == "string")
				| select(.finding_id as $f | any($current[]; . == $f) | not)
				| select(($git_ok | not) or ((.file | gsub("^\\s+|\\s+$"; "") | ltrimstr("./")) as $p | any($touched[]; . == $p) | not))]
				| unique_by(.finding_id)
			' "${carry_prior}" 2>/dev/null)" || carry_candidates="[]"
			if [ "$(jq -r 'length' <<< "${carry_candidates}" 2>/dev/null || echo 0)" -gt 0 ]; then
				if carry_open="$(single_pass_open_followup_ids "${SECURITY_PASS_AUDIT_BRANCH}" "$(jq -c '[.[].finding_id]' <<< "${carry_candidates}")")"; then
					carried="$(jq -c --argjson open "${carry_open}" '[.[] | select(.finding_id as $f | any($open[]; . == $f))]' <<< "${carry_candidates}" 2>/dev/null)" || carried="${carry_candidates}"
				else
					# Fail closed: an unverifiable listing carries every candidate.
					carry_listing_ok="false"
					carried="${carry_candidates}"
				fi
			fi
			carried_count="$(jq -r 'length' <<< "${carried}" 2>/dev/null || echo 0)"
			if [ "${carried_count}" -gt 0 ]; then
				carried_ids="$(jq -r '[.[].finding_id] | join(", ")' <<< "${carried}")"
				findings=$((findings + carried_count))
				status="findings"
				if [ -n "${current_ids_json}" ]; then
					SECURITY_PASS_FINDING_IDS_B64="$(jq -c --argjson add "$(jq -c '[.[].finding_id]' <<< "${carried}")" '. + $add' <<< "${current_ids_json}" | tr -d '
' | base64 | tr -d '
')"
				fi
				single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=carried carried=${carried_count} since=${carry_since} listing_ok=${carry_listing_ok}"
			fi
		fi
		if [ "${status}" = "findings" ]; then
			# Store the audited findings for the next cycle's delta re-audit.
			if [ -n "${SECURITY_PASS_FINDINGS_FILE:-}" ] && [ -r "${SECURITY_PASS_FINDINGS_FILE}" ] \
				&& audited_json="$(jq -c --argjson carried "${carried}" '
					select(type == "array" and all(.[]; type == "object"))
					| [(. + $carried)[] | {finding_id, owasp_or_stride_category, severity, confidence, file, line, exploit_scenario, recommendation} | with_entries(select(.value != null))]
					| unique_by(.finding_id) | select(length > 0)
				' "${SECURITY_PASS_FINDINGS_FILE}" 2>/dev/null)" && [ -n "${audited_json}" ]; then
				audited_b64="$(printf '%s' "${audited_json}" | base64 | tr -d '
')"
				audited_digest="$(printf '%s' "${audited_json}" | sha256sum | cut -d' ' -f1)"
				if [ "${#audited_b64}" -gt 40000 ] || ! [[ "${audited_digest}" =~ ^[0-9a-f]{64}$ ]]; then
					audited_b64=""
				fi
			fi
			if [ -z "${audited_b64}" ]; then
				single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=findings reason=prior_findings_omitted"
			fi
		fi
	fi
	case "${status}" in
		clean) body="## Single-issue security pass: clean

The audit of \`${head_sha}\` found nothing to fix. The review re-runs and merges this head." ;;
		findings) body="## Single-issue security pass: ${findings} finding(s)

The audit of \`${head_sha}\` filed follow-up issues against this branch. Their merges start a new review, and the next cycle audits the new head."
			report_ids="${SECURITY_PASS_FINDING_IDS_B64:-}"
			if [ -n "${report_ids}" ] && [ "$(printf '%s' "${report_ids}" | base64 -d 2>/dev/null | jq -er --argjson count "${findings}" 'select(type == "array" and length == $count and (unique | length) == length and all(.[]; type == "string" and length > 0 and test("^[^<>\\r\\n]+$"))) | length' 2>/dev/null)" = "${findings}" ]; then
				body+=$'\n\n'"Finding IDs (base64 JSON): ${report_ids}"
			else
				report_ids_invalid="true"
				echo "::warning::Audit finding IDs are missing or invalid; re-dispatching review so the gate can alert instead of silencing these findings."
			fi
			if [ -n "${carried_ids}" ]; then
				if [ "${carry_listing_ok}" = "true" ]; then
					body+=$'\n\n'"Carried forward (file untouched since \`${carry_since}\`, follow-up open): ${carried_ids}"
				else
					body+=$'\n\n'"Carried forward (file untouched since \`${carry_since}\`, follow-up state unverifiable): ${carried_ids}"
				fi
			fi
			if [ -n "${audited_b64}" ]; then
				body+=$'\n\n'"Audited findings (base64 JSON): ${audited_b64}"$'\n'"Audited findings sha256: ${audited_digest}"
			fi ;;
		*) body="## Single-issue security pass: audit failed

The audit of \`${head_sha}\` did not finish. The review re-runs and starts the next cycle." ;;
	esac
	body+=$'\n\n'"$(single_pass_marker "${status}" "${head_sha}" "${cycle}")"
	if ! gh api "repos/${REPOSITORY}/issues/${pr_number}/comments" -f body="${body}" >/dev/null 2>&1; then
		echo "::warning::Could not post the security-pass result on PR #${pr_number}."
		single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=skip reason=comment_write_failed cycle=${cycle}"
		return 0
	fi
	if [ "${status}" = "clean" ]; then
		# Retire before the re-dispatch below so a superseded follow-up PR has
		# the smallest window to merge into (and move) the clean head.
		single_pass_retire_followups "${pr_number}" "${SECURITY_PASS_AUDIT_BRANCH}" "${head_sha}"
	fi
	# This result is the last budget cycle (pending-only cycles do not count,
	# see single_pass_cycle_accounting) or the last attempt: re-run the review
	# so the gate escalates instead of waiting for follow-ups.
	read -r _ report_used _ <<< "$(single_pass_cycle_accounting "${report_markers}" "$((max_cycles + extensions))" "${cycle}")"
	[[ "${report_used}" =~ ^[0-9]+$ ]] || report_used="${cycle}"
	if [ "${report_used}" -ge "$((max_cycles + extensions))" ] || [ "${cycle}" -ge "$((max_attempts + extensions))" ]; then
		report_final="true"
	fi
	if { [ "${status}" != "findings" ] || [ "${report_ids_invalid}" = "true" ] || [ "${report_final}" = "true" ]; } && [ -n "${default_branch}" ]; then
		review_workflow="$(single_pass_review_workflow)"
		gh workflow run "${review_workflow}" -R "${REPOSITORY}" --ref "${default_branch}" -f pr_number="${pr_number}" >/dev/null 2>&1 \
			|| echo "::warning::Could not re-dispatch ${review_workflow} for PR #${pr_number}; the next review event picks the result up."
	fi
	single_pass_log "mode=report pr=${pr_number} head=${head_sha} outcome=${status} cycle=${cycle} findings=${findings:-unknown}"
}

single_pass_followup_guard_emit()
{
	local guard_outcome="$1" guard_reason="$2" guard_parent="${3:-}" guard_head="${4:-}" guard_extra="${5:-}"
	single_pass_log "mode=followup_guard pr=${PR_NUMBER:-} parent=${guard_parent} head=${guard_head} outcome=${guard_outcome} reason=${guard_reason}${guard_extra:+ ${guard_extra}}"
	echo "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=${guard_outcome}"
}

# `followup-guard` mode (#9479): a PR whose base is a standalone PR's head
# branch must not merge once that branch's current head has a completed
# status=clean audit, since the merge would move the clean head. Prints
# SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow|refuse|unverifiable.
single_pass_followup_merge_guard()
{
	local guard_payload="${PR_PAYLOAD_FILE:-}" guard_pattern="${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-}"
	local guard_base guard_default guard_owner guard_ref_uri guard_parents guard_parent guard_parent_number guard_parent_head
	local guard_comments_file guard_markers guard_status guard_hold_marker guard_body guard_existing="" guard_dedupe_file
	if [ -f "$(dirname -- "${BASH_SOURCE[0]}")/gh_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "$(dirname -- "${BASH_SOURCE[0]}")/gh_helpers.sh" 2>/dev/null || true
	fi
	# Kill switch: an explicit `false` turns the guard off like the gate. Unset
	# keeps it on, because the auto-merge step env does not pass the variable
	# and a silently disabled guard would let a follow-up move a clean head.
	if [ "${SINGLE_ISSUE_SECURITY_PASS_ENABLED-}" = "false" ]; then
		single_pass_followup_guard_emit allow disabled
		return 0
	fi
	if ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || [ ! -s "${guard_payload}" ] \
		|| ! jq -e 'type == "object"' "${guard_payload}" >/dev/null 2>&1; then
		single_pass_followup_guard_emit unverifiable missing_input
		return 0
	fi
	guard_base="$(jq -r '.base.ref // "" | strings' "${guard_payload}" 2>/dev/null)" || guard_base=""
	guard_default="${DEFAULT_BRANCH:-}"
	if [ -z "${guard_default}" ]; then
		guard_default="$(jq -r '.base.repo.default_branch // "" | strings' "${guard_payload}" 2>/dev/null)" || guard_default=""
	fi
	if [ -z "${guard_base}" ]; then
		single_pass_followup_guard_emit allow no_base
		return 0
	fi
	if [ -n "${guard_default}" ] && [ "${guard_base}" = "${guard_default}" ]; then
		single_pass_followup_guard_emit allow default_base
		return 0
	fi
	if printf '%s\n' "${guard_base}" | grep -Eq -- "${guard_pattern}" 2>/dev/null; then
		single_pass_followup_guard_emit allow integration_base
		return 0
	fi
	if [ -z "${guard_default}" ]; then
		# Only synthetic payloads lack base.repo.default_branch.
		single_pass_followup_guard_emit allow default_branch_unknown
		return 0
	fi
	# Neither the follow-up PR payload nor any other call in this path names the
	# PR whose head is this base branch; one head-filtered listing finds it
	# (paginated like the comment reads, so a parent past page one is not missed).
	guard_owner="${REPOSITORY%%/*}"
	guard_ref_uri="$(jq -rn --arg r "${guard_base}" '$r | @uri' 2>/dev/null)" || guard_ref_uri=""
	if [ -z "${guard_ref_uri}" ] || ! guard_parents="$({ if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api --paginate "repos/${REPOSITORY}/pulls?state=open&head=${guard_owner}:${guard_ref_uri}&per_page=100"
		else
			gh api --paginate "repos/${REPOSITORY}/pulls?state=open&head=${guard_owner}:${guard_ref_uri}&per_page=100"
		fi; } 2>/dev/null | jq -s 'if length > 0 then add else error("empty") end' 2>/dev/null)" \
		|| ! guard_parent="$(jq -c --arg ref "${guard_base}" --arg repo "${REPOSITORY}" --arg base "${guard_default}" '
			if type == "array" then . else error("shape") end
			| [.[] | select(type == "object" and .head.repo.full_name == $repo and .head.ref == $ref and .base.ref == $base)]
			| first // {}
		' <<< "${guard_parents}" 2>/dev/null)"; then
		single_pass_followup_guard_emit unverifiable parent_lookup_failed
		return 0
	fi
	if [ "${guard_parent}" = "{}" ]; then
		single_pass_followup_guard_emit allow no_standalone_parent
		return 0
	fi
	guard_parent_number="$(jq -r '.number // "" | tostring' <<< "${guard_parent}" 2>/dev/null)" || guard_parent_number=""
	guard_parent_head="$(jq -r '.head.sha // "" | tostring' <<< "${guard_parent}" 2>/dev/null)" || guard_parent_head=""
	if ! [[ "${guard_parent_number}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${guard_parent_head}" =~ ^[0-9a-f]{40}$ ]]; then
		single_pass_followup_guard_emit unverifiable parent_lookup_failed
		return 0
	fi
	# The review job's /user result is not exported to this helper. Retried like
	# the guard's other reads: one transient failure would hold the merge.
	SECURITY_PASS_AUTHOR_LOGIN="$({ if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api user --jq '.login // ""'
		else
			gh api user --jq '.login // ""'
		fi; } 2>/dev/null || true)"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	fi
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		single_pass_followup_guard_emit unverifiable author_unverifiable "${guard_parent_number}" "${guard_parent_head}"
		return 0
	fi
	guard_comments_file="$(mktemp 2>/dev/null)" || guard_comments_file=""
	if [ -z "${guard_comments_file}" ] || ! { { if type gh_retry >/dev/null 2>&1; then
			gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${guard_parent_number}/comments?per_page=100"
		else
			gh api --paginate "repos/${REPOSITORY}/issues/${guard_parent_number}/comments?per_page=100"
		fi; } 2>/dev/null | jq -s 'add // []' > "${guard_comments_file}" 2>/dev/null; } \
		|| ! jq -e 'type == "array"' "${guard_comments_file}" >/dev/null 2>&1 \
		|| ! guard_markers="$(single_pass_markers "${guard_comments_file}")"; then
		[ -n "${guard_comments_file}" ] && rm -f "${guard_comments_file}"
		single_pass_followup_guard_emit unverifiable markers_unverifiable "${guard_parent_number}" "${guard_parent_head}"
		return 0
	fi
	rm -f "${guard_comments_file}"
	guard_status="$(printf '%s\n' "${guard_markers}" | awk -F'\t' -v h="${guard_parent_head}" '$4 == h && $3 != "pending" { s = $3 } END { print s }')"
	if [ "${guard_status}" != "clean" ]; then
		single_pass_followup_guard_emit allow "parent_head_${guard_status:-unaudited}" "${guard_parent_number}" "${guard_parent_head}"
		return 0
	fi
	guard_hold_marker="<!-- ai:single-issue-security-pass-followup-hold:v1 parent=${guard_parent_number} head=${guard_parent_head} -->"
	guard_dedupe_file="${PR_ISSUE_COMMENTS_FILE:-}"
	if [ -z "${guard_dedupe_file}" ] || [ ! -s "${guard_dedupe_file}" ] || ! jq -e 'type == "array"' "${guard_dedupe_file}" >/dev/null 2>&1; then
		guard_dedupe_file="$(mktemp 2>/dev/null)" || guard_dedupe_file=""
		if [ -n "${guard_dedupe_file}" ] && ! { { if type gh_retry >/dev/null 2>&1; then
				gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments?per_page=100"
			else
				gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments?per_page=100"
			fi; } 2>/dev/null | jq -s 'add // []' > "${guard_dedupe_file}" 2>/dev/null; }; then
			rm -f "${guard_dedupe_file}"
			guard_dedupe_file=""
		fi
	fi
	if [ -n "${guard_dedupe_file}" ]; then
		guard_existing="$(jq -r --arg author "${SECURITY_PASS_AUTHOR_LOGIN}" --arg m "${guard_hold_marker}" '
			[.[]? | select(type == "object" and .user.login == $author)
			| (.body // "") | strings | split("\n") | map(select(test("\\S"))) | last // "" | gsub("^\\s+|\\s+$"; "")
			| select(. == $m)] | length
		' "${guard_dedupe_file}" 2>/dev/null)" || guard_existing=""
		[ "${guard_dedupe_file}" != "${PR_ISSUE_COMMENTS_FILE:-}" ] && rm -f "${guard_dedupe_file}"
	fi
	if [[ "${guard_existing}" =~ ^[1-9][0-9]*$ ]]; then
		single_pass_followup_guard_emit refuse parent_head_clean "${guard_parent_number}" "${guard_parent_head}" "comment=existing"
		return 0
	fi
	if [ "${guard_existing}" != "0" ]; then
		# This PR's comments could not be read: still refuse, but post nothing
		# so a flaky read on a later round cannot duplicate the hold comment.
		single_pass_followup_guard_emit refuse parent_head_clean "${guard_parent_number}" "${guard_parent_head}" "comment=unverifiable"
		return 0
	fi
	guard_body="## Merge held: the target branch passed its security audit

This PR targets \`${guard_base}\`, the head branch of PR #${guard_parent_number}. The single-issue security audit of \`${guard_parent_head}\` came back clean, so merging this follow-up would move the audited head. Auto-merge stays off and the PR is left open; close it if the clean audit superseded it.

${guard_hold_marker}"
	if ! gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${guard_body}" >/dev/null 2>&1; then
		single_pass_followup_guard_emit refuse parent_head_clean "${guard_parent_number}" "${guard_parent_head}" "comment=failed"
		return 0
	fi
	single_pass_followup_guard_emit refuse parent_head_clean "${guard_parent_number}" "${guard_parent_head}" "comment=posted"
}

# `since` mode (delta audit): run by security-audit.yml's scope step for an
# audit dispatched with pr_number. Prints the newest commit with a trusted
# `status=clean` marker on this PR that is an ancestor of the audited head
# in the audit checkout, or nothing. The workflow passes it as
# SECURITY_AUDIT_DIFF_SINCE, so the audit covers only range files changed
# since that clean audit. Fails open: any missing input, failed read or
# non-ancestor prints nothing and the audit covers the full range.
# Inputs: SECURITY_PASS_PR_NUMBER, SECURITY_PASS_HEAD_SHA, REPOSITORY,
# SECURITY_PASS_AUDIT_CHECKOUT (default audit-data), optional
# SECURITY_PASS_AUTHOR_LOGIN_FALLBACK. API budget: one /user read and one
# paginated PR comments read. At most 10 clean heads are checked, newest
# first, with local git only.
# Log (stderr): SINGLE_ISSUE_SECURITY_PASS mode=since pr= head= outcome=delta|full reason= since=
single_pass_since()
{
	local pr_number="${SECURITY_PASS_PR_NUMBER:-}" head_sha="${SECURITY_PASS_HEAD_SHA:-}" checkout="${SECURITY_PASS_AUDIT_CHECKOUT:-audit-data}"
	local comments_file since_markers since_candidate since_checked=0
	if ! [[ "${pr_number}" =~ ^[0-9]+$ ]] || ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || [ -z "${REPOSITORY:-}" ]; then
		single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=full reason=missing_input" >&2
		return 0
	fi
	if [ "$(git -C "${checkout}" rev-parse HEAD 2>/dev/null || true)" != "${head_sha}" ]; then
		single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=full reason=checkout_mismatch" >&2
		return 0
	fi
	SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
	[ -n "${SECURITY_PASS_AUTHOR_LOGIN}" ] || SECURITY_PASS_AUTHOR_LOGIN="${SECURITY_PASS_AUTHOR_LOGIN_FALLBACK:-}"
	if [ -z "${SECURITY_PASS_AUTHOR_LOGIN}" ]; then
		single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=full reason=author_unverifiable" >&2
		return 0
	fi
	comments_file="$(mktemp)" || return 0
	if ! gh api --paginate "repos/${REPOSITORY}/issues/${pr_number}/comments?per_page=100" 2>/dev/null | jq -s 'add // []' > "${comments_file}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${comments_file}" >/dev/null 2>&1 \
		|| ! since_markers="$(single_pass_markers "${comments_file}")"; then
		rm -f "${comments_file}"
		single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=full reason=markers_unverifiable" >&2
		return 0
	fi
	rm -f "${comments_file}"
	while IFS= read -r since_candidate; do
		[[ "${since_candidate}" =~ ^[0-9a-f]{40}$ ]] || continue
		[ "${since_candidate}" != "${head_sha}" ] || continue
		since_checked=$((since_checked + 1))
		[ "${since_checked}" -le 10 ] || break
		if git -C "${checkout}" merge-base --is-ancestor "${since_candidate}" "${head_sha}" 2>/dev/null; then
			single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=delta since=${since_candidate}" >&2
			printf '%s\n' "${since_candidate}"
			return 0
		fi
	done <<< "$(printf '%s\n' "${since_markers}" | awk -F'\t' '$3 == "clean" { print $4 }' | tac | awk '!seen[$0]++')"
	single_pass_log "mode=since pr=${pr_number} head=${head_sha} outcome=full reason=no_clean_ancestor checked=${since_checked}" >&2
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	case "${1:-}" in
		gate) single_pass_gate ;;
		status)
			SINGLE_PASS_STATUS_ONLY="true"
			single_pass_gate
			;;
		report) single_pass_report ;;
		delta) single_pass_delta_resolve ;;
		followup-guard) single_pass_followup_merge_guard ;;
		since) single_pass_since ;;
		*)
			echo "usage: $0 gate|status|report|delta|followup-guard|since" >&2
			exit 2
			;;
	esac
fi
