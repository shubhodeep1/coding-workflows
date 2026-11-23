#!/usr/bin/env bash
# Per-PR cap on dispatched review rounds for the review sweep
# (review_autofix_sweep.yml).
#
# Sourced by the "Enumerate open PRs and dispatch internal-review.yml" step
# from the trusted default-branch checkout (.ci-rerun-support). Not a manual
# script: nothing here runs when the file is executed directly.
#
# Why: the sweep dispatches internal-review.yml for every open non-draft PR
# that has no active review run, every 30 minutes, for as long as the PR stays
# open. Each mechanism inside a review run has its own cap (autofix iterations,
# security-pass cycles, the fingerprint cap), but nothing bounded the number of
# runs per PR. Between 2026-11-10 and 2026-11-17 `Internal: AI Review &
# Autofix` ran 3,778 times over 398 PRs: median 4 per PR, 90th percentile 26,
# and 81 on PR #9178 (#9134: 74, #9010 and #9180: 70 each); 2,430 of those
# runs were workflow_dispatch runs. PR #9134 was dispatched every two to five
# hours for four days (run 44153595157: autofix count 5 of 5, security pass
# cycle 14), and each round ended without a merge.
#
# review_sweep_round_counts <owner/repo> <default branch> <window hours> [<snapshot json>]
#   stdout: one JSON object mapping PR number (string) to the number of
#           dispatched review runs for that PR created in the last <window
#           hours>, e.g. {"9134":17,"9178":21}. A PR with no run is absent.
#           Prints nothing when the listing failed (callers then apply no cap).
#   Head scope: <snapshot json> is the sweep's annotated open-PR array; when
#           the argument is omitted, the caller's `prs_json` (the sweep step's
#           snapshot variable) is used if set. With a snapshot, a PR's runs
#           count only from its current head's `head_committed_at`
#           (scripts/review_sweep_head_gate.sh, same GraphQL read, no extra
#           call) and a PR without that field is left out (no cap). The
#           count starts at the commit's committer date, not at the push, so
#           a commit made well before it was pushed keeps the rounds
#           dispatched in between. When no row has the field the listing is
#           skipped (`read_failed reason=head_date_unavailable`). Without
#           this, the cheap same-head dispatches an approved head drew while
#           it waited for CI or a merge (about 48 a day) counted against the
#           next push, which was then escalated before any round on it.
#           Without a snapshot every run in the window counts.
#   Counted: workflow_dispatch runs of internal-review.yml that GitHub reports
#           on the default branch (or with a null head_branch, #4928), whose
#           path is internal-review.yml and whose title is exactly
#           `Internal: AI Review & Autofix [pr:<N>]` — the provenance rule the
#           sweep's active-run snapshot uses (issue #6629), so a run from a
#           branch copy of the workflow cannot count against another PR.
#           Every dispatcher counts (the sweep, the poller, the security pass,
#           a manual dispatch). Cancelled runs are not counted: they reviewed
#           nothing. pull_request-event runs are not counted either: each one
#           answers a push, which is progress, not a repeat. A run that ended
#           at the gate's cheap same-head skip cannot be told from a full
#           round in the listing, which is why the caller never caps a PR
#           whose head is already approved (see the sweep step).
#   API:    one paginated listing per tick for all PRs, never one per PR (§15):
#           `GET actions/workflows/internal-review.yml/runs?event=workflow_dispatch
#           &created=>=<cutoff>&per_page=100`, ceil(runs in window / 100) calls
#           (about 7 at 350 dispatched runs a day and the default 48 hours;
#           fewer as the cap takes effect). Each page is projected to the six
#           fields the count needs. The sweep's own snapshot only lists active
#           runs, so it cannot supply completed ones. Uses gh_api_retry when
#           the caller defined it.
#   Limit:  GitHub returns at most 1,000 runs for a filtered listing. When the
#           window holds more, the oldest are missing and the counts are lower
#           bounds; the log line then carries `truncated=true`. A lower count
#           only means a PR is capped later, never wrongly.
#   Fail-open: a failed listing (any page), or an unparseable one, prints
#           nothing and logs `REVIEW_SWEEP_ROUND_CAP outcome=read_failed`; the
#           tick then dispatches exactly as before this helper existed.
#   Infra-failed rounds (issue #9917): a review run whose engine was refused
#           for lack of credentials (or found every pool account gated, or the
#           pool broker refused it) reviewed nothing, so it does not count.
#           review_autofix.yml's "Classify infra-failed review round" step
#           (review_round_infra_failure_reason below) uploads an artifact named
#           `review-round-infra-failed` from such a dispatched run; one more
#           paginated listing per tick, `GET actions/artifacts?name=
#           review-round-infra-failed&per_page=100`, yields their run ids, and
#           a counted run whose `id` is in that set is dropped (logged
#           `REVIEW_ROUND_INFRA_EXCLUDED pr=<n> run=<id> reason=infra_failed`
#           on stderr). Only the dispatched run itself can upload into its own
#           run id. A failed or unparseable artifacts listing excludes nothing
#           (`outcome=infra_marker_read_failed`, today's count).
#           REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED=false (default true) skips
#           the listing and counts every round, as before.
#   Raw counts: when REVIEW_SWEEP_ROUND_RAW_COUNTS_FILE (default unset) names a
#           path, the counts before the infra exclusion are written there as
#           the same JSON shape (the sweep's dispatch backoff reads them).
#
# review_sweep_round_cap_escalate <owner/repo> <pr> <rounds> <max> <window hours> <labels json> <dry run>
#   Called for a PR at or over the cap, in place of its dispatch. When the PR
#   does not carry `ai:needs-human` yet, posts one comment (marker
#   `<!-- ai:review-round-cap:v1 -->`) and then adds the label; when it does,
#   writes nothing, so a capped PR costs no write per tick. The comment goes
#   first so a failed comment write leaves nothing behind and the next tick
#   retries both; a failed label write after the comment makes the next tick
#   post the comment again with the label. <dry run> = true writes nothing.
#   `ai:needs-human` is the pipeline's existing hand-over label: the
#   workflow-failure-heal reporter, the unblock scan and the merge train all
#   read it, so the escalation reaches the same consumers as every other
#   needs-human hold. Nothing here removes the label. When the optional 8th
#   argument <head sha> is a 40-hex SHA, the comment also carries
#   `<!-- ai:review-round-cap-head:v1 head=<sha> -->`, and the poller's
#   `release_head_bound_needs_human_latches` removes the label once the PR's
#   head has moved past that SHA (the count is head-scoped, so a new head
#   starts at zero). Without it, a person removes the label.
#   API: 1 label write + 1 comment, once per escalation. Never fails the tick.
#
# Logs (stderr for the counts, stdout for the escalation):
#   REVIEW_SWEEP_ROUND_CAP outcome=counted window_hours=<n> runs=<n> prs=<n> truncated=<true|false>
#   REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=<invalid_input|head_date_unavailable|cutoff|listing|parse> window_hours=<n>
#   REVIEW_SWEEP_ROUND_CAP outcome=<escalated|already_escalated|dry_run> pr=#<n> rounds=<n> max=<n> window_hours=<n>
#   REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason=<invalid_input|comment_write|label_write> pr=#<n> ...
#   REVIEW_SWEEP_ROUND_CAP outcome=infra_marker_read_failed window_hours=<n>
#   REVIEW_ROUND_INFRA_EXCLUDED pr=<n> run=<id> reason=<infra_failed|no_credential|all_gated|broker_refused_<code>>
#
# review_round_infra_failure_reason
#   Called by review_autofix.yml's "Classify infra-failed review round" step.
#   Prints one reason ([a-z0-9_], at most 64 characters) when the round is
#   infra-failed, otherwise nothing; never fails. A round is infra-failed only
#   when REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED is not false/0/no/off, the
#   editor produced no usable output (EDITOR_SUMMARY_FILE missing, empty, or
#   the editor's fallback summary), and one infra signal is present:
#     - an AI_ENGINE_FALLBACK_RECORDS_FILE v1 record (scripts/ai_engine.sh:
#       v1, ts, role, reason, class, action, policy) for a REVIEW* role with
#       action refused or wait and reason no_credential or all_gated;
#     - an `::error::AI_ENGINE_FALLBACK_REFUSED role=REVIEW* reason=
#       <no_credential|all_gated>` line in ${RUNTIME_DIR}/editor_stage_stderr.txt
#       or ${RUNTIME_DIR}/reviewers_stage_stderr.txt (a sandboxed call whose
#       record never reached the host file);
#     - CLAUDE_POOL_STEP_AVAILABLE=false with CLAUDE_POOL_STEP_REASON
#       broker_refused_<code>.
#   Symlinked or unreadable files count as absent. A round that landed real
#   editor output keeps counting. No API call.

REVIEW_SWEEP_ROUND_CAP_LABEL="ai:needs-human"
REVIEW_SWEEP_ROUND_CAP_MARKER="<!-- ai:review-round-cap:v1 -->"
REVIEW_ROUND_INFRA_ARTIFACT_NAME="review-round-infra-failed"

# True when REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED (default true) is on.
_review_round_infra_exclusion_enabled()
{
	case "$(printf '%s' "${REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		false|0|no|off) return 1 ;;
	esac
	return 0
}

_review_round_infra_readable()
{
	[ -n "${1:-}" ] && [ -f "$1" ] && [ ! -L "$1" ] && [ -r "$1" ]
}

review_round_infra_failure_reason()
{
	local rif_summary="${EDITOR_SUMMARY_FILE:-}" rif_records rif_file rif_reason=""
	_review_round_infra_exclusion_enabled || return 0
	# A round whose editor wrote a real summary did work: it keeps counting.
	if _review_round_infra_readable "${rif_summary}" && [ -s "${rif_summary}" ] \
		&& ! grep -qiE 'editor failed before producing|unavailable \(editor fallback\)' "${rif_summary}" 2>/dev/null; then
		return 0
	fi
	rif_records="${AI_ENGINE_FALLBACK_RECORDS_FILE:-${RUNNER_TEMP:-/tmp}/ai-engine-fallback-records.tsv}"
	if _review_round_infra_readable "${rif_records}"; then
		rif_reason="$(awk -F '\t' '$1 == "v1" && $3 ~ /^REVIEW/ && ($6 == "refused" || $6 == "wait") && ($4 == "no_credential" || $4 == "all_gated") { print $4; exit }' "${rif_records}" 2>/dev/null || true)"
	fi
	if [ -z "${rif_reason}" ] && [ -n "${RUNTIME_DIR:-}" ]; then
		for rif_file in "${RUNTIME_DIR}/editor_stage_stderr.txt" "${RUNTIME_DIR}/reviewers_stage_stderr.txt"; do
			_review_round_infra_readable "${rif_file}" || continue
			rif_reason="$(grep -oE '::error::AI_ENGINE_FALLBACK_REFUSED role=REVIEW[A-Z0-9_]* reason=(no_credential|all_gated)([^a-z0-9_]|$)' "${rif_file}" 2>/dev/null \
				| head -n 1 | sed -E 's/.* reason=([a-z_]+).*/\1/' || true)"
			[ -z "${rif_reason}" ] || break
		done
	fi
	if [ -z "${rif_reason}" ] && [ "${CLAUDE_POOL_STEP_AVAILABLE:-}" = "false" ] \
		&& [[ "${CLAUDE_POOL_STEP_REASON:-}" =~ ^broker_refused_[a-z0-9_]+$ ]]; then
		rif_reason="${CLAUDE_POOL_STEP_REASON}"
	fi
	[[ "${rif_reason}" =~ ^[a-z0-9_]{1,64}$ ]] || return 0
	printf '%s\n' "${rif_reason}"
	return 0
}

review_sweep_round_counts()
{
	local cap_repo="${1:-}" cap_default_branch="${2:-}" cap_window_hours="${3:-}" cap_cutoff cap_raw cap_rc=0 cap_counts cap_summary
	local cap_snapshot="${4-${prs_json:-}}" cap_since='{}' cap_scoped=false
	local cap_infra='{}' cap_infra_raw cap_infra_rc=0 cap_infra_parsed cap_raw_file="${REVIEW_SWEEP_ROUND_RAW_COUNTS_FILE:-}"
	local -a cap_api=(gh api)
	if ! [[ "${cap_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${cap_repo}" == *..* ]] \
		|| ! [[ "${cap_default_branch}" =~ ^[A-Za-z0-9._/-]+$ ]] \
		|| ! [[ "${cap_window_hours}" =~ ^[1-9][0-9]{0,3}$ ]]; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=invalid_input window_hours=${cap_window_hours:-unset}" >&2
		return 0
	fi
	if [ -n "${cap_snapshot}" ]; then
		# Per-PR lower bound: the current head's commit time (UTC, normalised
		# by the head-gate annotation). Rows without it are left out.
		cap_since="$(printf '%s' "${cap_snapshot}" | jq -c 'if type == "array" then
			[.[] | select(type == "object" and ((.number // null) | type) == "number")
				| select(((.head_committed_at // null) | type) == "string")
				| select(.head_committed_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))
				| {key: (.number | tostring), value: .head_committed_at}] | from_entries
			else error("snapshot is not an array") end' 2>/dev/null || true)"
		if [ -z "${cap_since}" ]; then
			echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=invalid_input window_hours=${cap_window_hours}" >&2
			return 0
		fi
		cap_scoped=true
		# No row has a usable head date (the head-gate read failed open): the
		# scoped filter would drop every run, so skip the listing (§14).
		if [ "${cap_since}" = "{}" ]; then
			echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=head_date_unavailable window_hours=${cap_window_hours}" >&2
			return 0
		fi
	fi
	cap_cutoff="$(date -u -d "-${cap_window_hours} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
	if [ -z "${cap_cutoff}" ]; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=cutoff window_hours=${cap_window_hours}" >&2
		return 0
	fi
	if type gh_api_retry >/dev/null 2>&1; then
		cap_api=(gh_api_retry)
	fi
	# The listing writes to a file so its exit status is checked on its own:
	# a failure on a later page must not pass for a complete, shorter count.
	cap_raw="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/review-round-cap.XXXXXX" 2>/dev/null || true)"
	if [ -z "${cap_raw}" ]; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=listing window_hours=${cap_window_hours}" >&2
		return 0
	fi
	"${cap_api[@]}" --paginate -X GET "repos/${cap_repo}/actions/workflows/internal-review.yml/runs" \
		-f event=workflow_dispatch -f "created=>=${cap_cutoff}" -f per_page=100 \
		--jq '{total_count: .total_count, workflow_runs: [.workflow_runs[]? | {id, event, conclusion, head_branch, path, display_title, created_at}]}' \
		> "${cap_raw}" 2>/dev/null || cap_rc=$?
	if [ "${cap_rc}" -ne 0 ]; then
		rm -f -- "${cap_raw}"
		echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=listing window_hours=${cap_window_hours} rc=${cap_rc}" >&2
		return 0
	fi
	# Infra-failed rounds (issue #9917): one artifacts listing per tick, read
	# into its own file so a failed page is not taken for a shorter set.
	if _review_round_infra_exclusion_enabled; then
		cap_infra_raw="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/review-round-infra.XXXXXX" 2>/dev/null || true)"
		cap_infra_parsed=""
		if [ -n "${cap_infra_raw}" ]; then
			"${cap_api[@]}" --paginate -X GET "repos/${cap_repo}/actions/artifacts" \
				-f "name=${REVIEW_ROUND_INFRA_ARTIFACT_NAME}" -f per_page=100 \
				--jq '[.artifacts[]? | {run: (.workflow_run.id // null), created_at, expired}]' \
				> "${cap_infra_raw}" 2>/dev/null || cap_infra_rc=$?
			if [ "${cap_infra_rc}" -eq 0 ]; then
				cap_infra_parsed="$(jq -c -s --arg cutoff "${cap_cutoff}" '
					if all(.[]; type == "array") | not then error("bad page") else . end
					| [.[][] | select(type == "object")
						| select((.run | type) == "number")
						| select(((.created_at // "") | type) == "string" and (.created_at // "") >= $cutoff)
						| {key: (.run | tostring), value: true}] | from_entries' "${cap_infra_raw}" 2>/dev/null || true)"
			fi
			rm -f -- "${cap_infra_raw}"
		fi
		if [ -n "${cap_infra_parsed}" ] && printf '%s' "${cap_infra_parsed}" | jq -e 'type == "object"' >/dev/null 2>&1; then
			cap_infra="${cap_infra_parsed}"
		else
			echo "REVIEW_SWEEP_ROUND_CAP outcome=infra_marker_read_failed window_hours=${cap_window_hours}" >&2
		fi
	fi
	cap_counts="$(jq -c -s --arg default_branch "${cap_default_branch}" --argjson since "${cap_since}" --argjson scoped "${cap_scoped}" --argjson infra "${cap_infra}" '
		if length == 0 or (all(.[]; type == "object" and (.workflow_runs | type) == "array") | not) then error("bad page") else . end
		| (([.[].total_count | select(type == "number")] | max) // 0) as $total
		| ([.[].workflow_runs[]] | length) as $received
		| [ .[].workflow_runs[]
					| select(type == "object")
					| select((.event // "") == "workflow_dispatch")
					| select((.conclusion // "") != "cancelled")
					| select(.head_branch == null or .head_branch == "" or .head_branch == $default_branch)
					| select((((.path // "") | if type == "string" then . else "" end) | sub("@.*$"; "")) == ".github/workflows/internal-review.yml")
					| ((.created_at // "") | if type == "string" then . else "" end) as $created
					| ((.id // "") | tostring) as $run
					| ((.display_title // "") | if type == "string" then . else "" end
						| [match("^Internal: AI Review & Autofix \\[pr:([1-9][0-9]*)\\]$").captures[0].string] | first // "")
					| select(. != "")
					| select(($scoped | not) or (($since[.] // null) != null and $created >= $since[.]))
					| {pr: ., run: $run} ] as $rows
		| def tally: group_by(.) | map({key: .[0], value: length}) | from_entries;
		{
			truncated: ($total > $received),
			raw: ([$rows[].pr] | tally),
			excluded: [$rows[] | select($infra[.run] == true)],
			counts: ([$rows[] | select($infra[.run] != true) | .pr] | tally)
		}' "${cap_raw}" 2>/dev/null || true)"
	rm -f -- "${cap_raw}"
	# The empty check matters: jq 1.6 `-e` exits 0 on empty input.
	if [ -z "${cap_counts}" ] || ! printf '%s' "${cap_counts}" | jq -e 'type == "object" and (.counts | type) == "object"' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=parse window_hours=${cap_window_hours}" >&2
		return 0
	fi
	printf '%s' "${cap_counts}" | jq -r '.excluded[]? | "REVIEW_ROUND_INFRA_EXCLUDED pr=\(.pr) run=\(.run) reason=infra_failed"' >&2 2>/dev/null || true
	if [ -n "${cap_raw_file}" ] && [ ! -L "${cap_raw_file}" ]; then
		printf '%s' "${cap_counts}" | jq -c '.raw' > "${cap_raw_file}" 2>/dev/null || true
	fi
	cap_summary="$(printf '%s' "${cap_counts}" | jq -r '"runs=\([.counts[]] | add // 0) prs=\(.counts | length) truncated=\(.truncated)"')"
	echo "REVIEW_SWEEP_ROUND_CAP outcome=counted window_hours=${cap_window_hours} ${cap_summary}" >&2
	printf '%s' "${cap_counts}" | jq -c '.counts'
	return 0
}

review_sweep_round_cap_escalate()
{
	local cap_repo="${1:-}" cap_pr="${2:-}" cap_rounds="${3:-}" cap_max="${4:-}" cap_window_hours="${5:-}" cap_labels="${6:-[]}" cap_dry_run="${7:-false}" cap_head_sha="${8:-}"
	local cap_fields="pr=#${cap_pr} rounds=${cap_rounds} max=${cap_max} window_hours=${cap_window_hours}" cap_body cap_head_marker=""
	if [[ "${cap_head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		cap_head_marker="<!-- ai:review-round-cap-head:v1 head=${cap_head_sha} -->"$'\n'
	fi
	if ! [[ "${cap_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${cap_repo}" == *..* ]] || ! [[ "${cap_pr}" =~ ^[1-9][0-9]*$ ]] \
		|| ! [[ "${cap_rounds}" =~ ^[0-9]+$ ]] || ! [[ "${cap_max}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${cap_window_hours}" =~ ^[1-9][0-9]{0,3}$ ]]; then
		echo "::warning::REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason=invalid_input ${cap_fields}"
		return 0
	fi
	if printf '%s' "${cap_labels}" | jq -e --arg l "${REVIEW_SWEEP_ROUND_CAP_LABEL}" 'type == "array" and index($l) != null' >/dev/null 2>&1; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=already_escalated ${cap_fields}"
		return 0
	fi
	if [ "${cap_dry_run}" = "true" ]; then
		echo "REVIEW_SWEEP_ROUND_CAP outcome=dry_run ${cap_fields}"
		return 0
	fi
	cap_body="The review sweep has stopped dispatching reviews for this PR: it had ${cap_rounds} dispatched review rounds in the last ${cap_window_hours} hours (limit ${cap_max}), its head is still not approved, and another round is unlikely to change that. The PR is labelled \`${REVIEW_SWEEP_ROUND_CAP_LABEL}\`.

What to do: read the latest review and CI results, then fix or close the PR. A new push is reviewed as usual, and \`Internal: AI Review & Autofix\` can be dispatched by hand for this PR. The sweep dispatches again by itself once fewer than ${cap_max} rounds fall inside the last ${cap_window_hours} hours. The label stays until a person removes it or the PR moves to a new head (the poller then removes it, because the count starts again for that head); removing it while the PR is still over the limit makes the next sweep tick add it back.

${cap_head_marker}${REVIEW_SWEEP_ROUND_CAP_MARKER}"
	# Comment first, label second: the label is what stops the next tick from
	# commenting again, so a failed comment write leaves nothing behind and
	# the next tick retries both (a label-first order left a bare label whose
	# comment was never retried).
	if ! gh api "repos/${cap_repo}/issues/${cap_pr}/comments" -f body="${cap_body}" >/dev/null 2>&1; then
		echo "::warning::REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason=comment_write ${cap_fields}"
		return 0
	fi
	# Adding a label is idempotent, so it may go through the caller's retry
	# wrapper; a transient failure here would otherwise repeat the comment.
	local -a cap_label_api=(gh api)
	if type gh_api_retry >/dev/null 2>&1; then
		cap_label_api=(gh_api_retry --idempotent)
	fi
	if ! "${cap_label_api[@]}" "repos/${cap_repo}/issues/${cap_pr}/labels" -f "labels[]=${REVIEW_SWEEP_ROUND_CAP_LABEL}" >/dev/null 2>&1; then
		echo "::warning::REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason=label_write ${cap_fields}"
		return 0
	fi
	echo "REVIEW_SWEEP_ROUND_CAP outcome=escalated ${cap_fields}"
	return 0
}
