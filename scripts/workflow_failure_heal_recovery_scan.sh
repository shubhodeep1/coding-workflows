#!/usr/bin/env bash
# workflow_failure_heal_recovery_scan.sh — hourly heal-scope recovery scan (#7333).
#
# An open, pipeline-authored ai:workflow-heal issue that has no
# ai:workflow-heal-scope:v1 marker (#7082) is refused by the implement
# preflight, and the intake replaces it only when a new verified report of the
# same failure arrives. This scan, run by the `recovery-scan` job of
# .github/workflows/workflow-failure-heal-intake.yml on its hourly schedule,
# revisits such issues without a person:
#
#   1. List open ai:workflow-heal issues in this repository written by the
#      token's own login, and keep those with no scope marker.
#   2. For each (at most HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK), rebuild the
#      autofix_failure report from the issue's pipeline-written, never-edited
#      body (`workflow_failure_heal.py heal-scope-recovery reconstruct`).
#   3. Run the unchanged intake on it with origin `recovery_scan`. The intake
#      binds the PR, verifies run provenance, recomputes the fingerprint and
#      requires it to equal the issue's `fp`, then runs the existing recovery
#      path; the replacement is filed only after `heal-scope verify` passes.
#   4. A failed attempt leaves one
#      `<!-- ai:heal-scope-recovery-scan:v1 outcome=failed reason=<r> run=<id> -->`
#      comment; after HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS the issue is no
#      longer tried and one WARNING is sent.
#
# Only autofix_failure heal issues are rebuilt (the #7082 shape); other kinds
# log reason=kind_unsupported and still need a fresh verified report.
#
# Usage: workflow_failure_heal_recovery_scan.sh [--list-only]
#   --list-only  write `count=<n>` (candidates) to GITHUB_OUTPUT and stop.
#
# Environment (all defaulted):
#   GITHUB_REPOSITORY                      this repository (empty: nothing to scan)
#   GITHUB_EVENT_NAME                      must be `schedule` (else skip, reason=not_scheduled)
#   WORKFLOW_HEAL_ENABLED                  "false" skips (default true)
#   HEAL_SCOPE_RECOVERY_ENABLED            unset, empty or "true" (any letter case) runs;
#                                          "false" skips; any other value skips with a warning
#   HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK  issues processed per run (default 1, clamped 1-2)
#   HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS  failed attempts before an issue is left alone
#                                          (default 3)
#   HEAL_SCOPE_RECOVERY_SCAN_INTAKE_SCRIPT intake to run (default scripts/workflow_failure_heal_intake.sh)
#   WORKFLOW_HEAL_PY                       helper (default scripts/workflow_failure_heal.py)
#   RUNTIME_DIR                            scratch directory (default /tmp/workflow-heal-recovery-scan-<run>)
#
# GitHub API cost per run (CLAUDE.md §14): one /user read and one to four
# GraphQL issue-list pages (with each issue's last 30 comments, so exhausted
# issues are dropped without a read); --list-only adds one workflow-runs read
# when there are candidates. Per examined candidate one paginated comment
# read and one GraphQL issue read, then one PR read and one paginated PR
# comment read, plus the intake's normal reads. REST has no lastEditedAt,
# which the reconstruct step needs, so the issue reads are GraphQL. Every read failure skips (fail closed, nothing
# filed); the next hourly run retries.

set -euo pipefail

log()
{
	echo "WORKFLOW_HEAL $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

LIST_ONLY=false
if [ "${1:-}" = "--list-only" ]; then
	LIST_ONLY=true
fi

_write_count()
{
	if [ "${LIST_ONLY}" = "true" ] && [ -n "${GITHUB_OUTPUT:-}" ]; then
		echo "count=$1" >> "${GITHUB_OUTPUT}"
	fi
}

SELF_REPO="${GITHUB_REPOSITORY:-}"
HEAL_PY="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
INTAKE_SCRIPT="${HEAL_SCOPE_RECOVERY_SCAN_INTAKE_SCRIPT:-scripts/workflow_failure_heal_intake.sh}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/workflow-heal-recovery-scan-${GITHUB_RUN_ID:-local}}"
RUN_ID="${GITHUB_RUN_ID:-0}"
[[ "${RUN_ID}" =~ ^[0-9]{1,20}$ ]] || RUN_ID=0
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${SELF_REPO}/actions/runs/${RUN_ID}"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "${RUNTIME_DIR}"

_bounded_int()
{
	local value="${1:-}" fallback="$2" low="$3" high="$4"
	case "${value}" in
		''|*[!0-9]*) value="${fallback}" ;;
	esac
	value=$((10#${value}))
	[ "${value}" -ge "${low}" ] || value="${low}"
	[ "${value}" -le "${high}" ] || value="${high}"
	printf '%s' "${value}"
}
MAX_PER_TICK="$(_bounded_int "${HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK:-}" 1 1 2)"
MAX_ATTEMPTS="$(_bounded_int "${HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS:-}" 3 1 20)"
# Candidates examined per run (each costs one comment read), so exhausted
# issues cannot make one tick read every heal issue.
MAX_EXAMINED=20
# Deferred intake runs per scan that give their per-tick slot back, so one
# candidate that keeps deferring cannot starve later candidates; the bound
# keeps a global cap (every candidate defers) from running many intakes.
MAX_DEFERRED=2
# shellcheck disable=SC2016  # GraphQL variables, not shell expansions.
ISSUE_QUERY='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){state body lastEditedAt}}}'

# --- Gates -------------------------------------------------------------------

if [ "${WORKFLOW_HEAL_ENABLED:-true}" = "false" ]; then
	log "heal_scope_recovery outcome=skip reason=disabled stage=scan"
	_write_count 0
	exit 0
fi
case "${HEAL_SCOPE_RECOVERY_ENABLED-}" in
	''|[Tt][Rr][Uu][Ee]) ;;
	[Ff][Aa][Ll][Ss][Ee])
		log "heal_scope_recovery outcome=skip reason=recovery_disabled stage=scan"
		_write_count 0
		exit 0
		;;
	*)
		# The value is never echoed.
		echo "::warning::WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=invalid_switch" >&2
		_write_count 0
		exit 0
		;;
esac
if [ "${GITHUB_EVENT_NAME:-}" != "schedule" ]; then
	log "heal_scope_recovery outcome=skip reason=not_scheduled stage=scan"
	_write_count 0
	exit 0
fi
if ! [[ "${SELF_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	log "heal_scope_recovery outcome=skip reason=repository_unavailable stage=scan"
	_write_count 0
	exit 0
fi

LOGIN="$(gh_retry gh api --method GET user --jq '.login // empty' 2>/dev/null || true)"
if ! [[ "${LOGIN}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]]; then
	log "heal_scope_recovery outcome=skip reason=identity_unavailable stage=scan"
	_write_count 0
	exit 0
fi

# --- Candidates ----------------------------------------------------------------

LIST_FILE="${RUNTIME_DIR}/recovery_scan_list.json"
CANDIDATES_FILE="${RUNTIME_DIR}/recovery_scan_candidates.json"
NODES_FILE="${RUNTIME_DIR}/recovery_scan_nodes.json"
# The list is paged (at most LIST_MAX_PAGES pages of 50) and carries each
# issue's last 30 comments, so an issue whose visible scan markers already
# reach MAX_ATTEMPTS is dropped here: it costs no comment read and never keeps
# the count above zero or takes a slot from a later candidate. A marker outside
# the last 30 comments only undercounts, and the per-candidate paginated read
# below still decides.
LIST_MAX_PAGES=4
# shellcheck disable=SC2016  # GraphQL variables, not shell expansions.
LIST_QUERY='query($owner:String!,$name:String!,$login:String!,$after:String){repository(owner:$owner,name:$name){issues(first:50,after:$after,states:OPEN,labels:["ai:workflow-heal"],filterBy:{createdBy:$login},orderBy:{field:CREATED_AT,direction:ASC}){pageInfo{hasNextPage endCursor} nodes{number title body lastEditedAt url author{login} comments(last:30){nodes{body author{login}}}}}}}'
printf '[]' > "${NODES_FILE}"
LIST_CURSOR=""
LIST_PAGE=0
while :; do
	LIST_PAGE=$((LIST_PAGE + 1))
	LIST_ARGS=(-f query="${LIST_QUERY}" -f owner="${SELF_REPO%%/*}" -f name="${SELF_REPO#*/}" -f login="${LOGIN}")
	[ -z "${LIST_CURSOR}" ] || LIST_ARGS+=(-f after="${LIST_CURSOR}")
	if ! gh_retry gh api graphql "${LIST_ARGS[@]}" > "${LIST_FILE}" 2>/dev/null \
		|| ! jq -e '.data.repository.issues.nodes | type == "array"' "${LIST_FILE}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=list_unavailable stage=scan"
		_write_count 0
		exit 0
	fi
	jq -s '.[0] + (.[1].data.repository.issues.nodes // [])' "${NODES_FILE}" "${LIST_FILE}" > "${NODES_FILE}.next" && mv "${NODES_FILE}.next" "${NODES_FILE}"
	LIST_CURSOR="$(jq -r 'if .data.repository.issues.pageInfo.hasNextPage == true then (.data.repository.issues.pageInfo.endCursor // "") else "" end' "${LIST_FILE}")"
	[ -n "${LIST_CURSOR}" ] || break
	if [ "${LIST_PAGE}" -ge "${LIST_MAX_PAGES}" ]; then
		log "heal_scope_recovery outcome=scan reason=list_truncated pages=${LIST_PAGE}"
		break
	fi
done
jq --arg login "${LOGIN}" --argjson max "${MAX_ATTEMPTS}" '[.[]
	| select(type == "object" and (.number | type == "number"))
	| select(((.author.login // "") | ascii_downcase) == ($login | ascii_downcase))
	| select((.body // "") | contains("ai:workflow-heal-scope") | not)
	| select(([(.comments.nodes // [])[]
		| select(type == "object" and ((.author.login // "") | ascii_downcase) == ($login | ascii_downcase))
		| (.body // "") | sub("\\s+$"; "") | split("\n") | (last // "") | gsub("^\\s+|\\s+$"; "")
		| select(test("^<!-- ai:heal-scope-recovery-scan:v1 outcome=failed reason=[a-z][a-z0-9_]{0,59} run=[0-9]{1,20} -->$"))] | length) < $max)
	| {number, title: (.title // ""), body: (.body // ""), author: (.author.login // ""), last_edited_at: .lastEditedAt, html_url: (.url // "")}]' \
	"${NODES_FILE}" > "${CANDIDATES_FILE}"
COUNT="$(jq 'length' "${CANDIDATES_FILE}")"
log "heal_scope_recovery outcome=scan reason=listed count=${COUNT}"
if [ "${LIST_ONLY}" = "true" ] && [ "${COUNT}" -gt 0 ]; then
	# With HEAL_INTAKE_QUEUE_ENABLED=false the recovery-scan job joins the
	# shared intake concurrency group, and a newly queued member replaces the
	# group's pending one. Skip this tick while another intake run is active,
	# so a scan queued now cannot displace a report waiting behind it; the next
	# hourly run retries. With the report queue on (the default) nothing is
	# displaced, and the skip only defers the scan to a quiet hour. §14 audit: no
	# existing call in this script lists workflow runs; one GET of the newest
	# 100 runs of this workflow. A failed read also skips (fail closed).
	RUNS_FILE="${RUNTIME_DIR}/recovery_scan_runs.json"
	ACTIVE_RUNS=""
	if gh_retry gh api --method GET "repos/${SELF_REPO}/actions/workflows/workflow-failure-heal-intake.yml/runs" -F per_page=100 > "${RUNS_FILE}" 2>/dev/null; then
		ACTIVE_RUNS="$(jq --arg self "${RUN_ID}" '[.workflow_runs[]? | select(.status != "completed" and ((.id | tostring) != $self))] | length' "${RUNS_FILE}" 2>/dev/null || true)"
	fi
	if ! [[ "${ACTIVE_RUNS}" =~ ^[0-9]+$ ]]; then
		log "heal_scope_recovery outcome=skip reason=runs_unavailable stage=scan"
		COUNT=0
	elif [ "${ACTIVE_RUNS}" -gt 0 ]; then
		log "heal_scope_recovery outcome=skip reason=intake_busy active_runs=${ACTIVE_RUNS} stage=scan"
		COUNT=0
	fi
fi
if [ "${LIST_ONLY}" = "true" ]; then
	_write_count "${COUNT}"
	exit 0
fi

# --- Per candidate -------------------------------------------------------------

_post_attempt_marker()
{
	local number="$1" reason="$2" attempts="$3" marker body_file summary action
	ATTEMPT_MARKER_POSTED=false
	reason="$(printf '%s' "${reason}" | tr -cd 'a-z0-9_' | head -c 60)"
	[[ "${reason}" =~ ^[a-z] ]] || reason="intake_failed"
	marker="$(python3 "${HEAL_PY}" heal-scope-recovery scan-marker --reason "${reason}" --run-id "${RUN_ID}" 2>/dev/null || true)"
	if [ -z "${marker}" ]; then
		log "heal_scope_recovery outcome=attempt_failed reason=${reason} issue=${number} marker=unavailable"
		return 0
	fi
	body_file="${RUNTIME_DIR}/recovery_scan_marker_${number}.md"
	summary="The heal-scope recovery scan could not replace this issue"
	action="It needs a fresh authenticated failure report or a person."
	if [ "${reason}" = "close_failed" ]; then
		# Already replaced: only the close of this original issue is missing.
		summary="This issue is already replaced, but the heal-scope recovery scan could not close it"
		action="It is already replaced; close it by hand."
	fi
	{
		echo "${summary} (reason \`${reason}\`). Attempt $((attempts + 1)) of ${MAX_ATTEMPTS}."
		echo
		echo "Scan run: ${RUN_URL}"
		echo
		echo "${marker}"
	} > "${body_file}"
	if gh_retry gh api "repos/${SELF_REPO}/issues/${number}/comments" -F body=@"${body_file}" >/dev/null 2>&1; then
		ATTEMPT_MARKER_POSTED=true
		log "heal_scope_recovery outcome=attempt_failed reason=${reason} issue=${number} attempts=$((attempts + 1)) max=${MAX_ATTEMPTS} marker=posted"
	else
		log "heal_scope_recovery outcome=attempt_failed reason=${reason} issue=${number} attempts=$((attempts + 1)) max=${MAX_ATTEMPTS} marker=failed"
		return 0
	fi
	if [ $((attempts + 1)) -ge "${MAX_ATTEMPTS}" ]; then
		tg_send_msg "Workflow failure heal: the heal-scope recovery scan stopped trying https://github.com/${SELF_REPO}/issues/${number} after ${MAX_ATTEMPTS} attempts (last reason ${reason}). ${action}"$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	fi
}

PROCESSED=0
EXAMINED=0
DEFERRED=0
ATTEMPT_MARKER_POSTED=false
INTAKE_FAILURES=0
# The loop reads fd 3 so nothing it runs can consume the candidate list.
while IFS= read -r -u 3 candidate; do
	[ "${PROCESSED}" -lt "${MAX_PER_TICK}" ] || break
	[ "${EXAMINED}" -lt "${MAX_EXAMINED}" ] || break
	EXAMINED=$((EXAMINED + 1))
	NUMBER="$(printf '%s' "${candidate}" | jq -r '.number')"
	[[ "${NUMBER}" =~ ^[1-9][0-9]*$ ]] || continue
	WORK="${RUNTIME_DIR}/issue-${NUMBER}"
	mkdir -p "${WORK}"
	ISSUE_FILE="${WORK}/issue.json"
	COMMENTS_FILE="${WORK}/comments.json"
	printf '%s' "${candidate}" > "${ISSUE_FILE}"
	if ! gh_retry gh api --method GET --paginate "repos/${SELF_REPO}/issues/${NUMBER}/comments" -F per_page=100 \
		--jq '.[] | {id, body: (.body // ""), user: (.user // {})}' 2>/dev/null \
		| jq -s '.' > "${COMMENTS_FILE}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${COMMENTS_FILE}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=comments_unavailable issue=${NUMBER}"
		continue
	fi
	CHECK_FILE="${WORK}/check.json"
	if ! python3 "${HEAL_PY}" heal-scope-recovery check --issue-json "${ISSUE_FILE}" --comments-json "${COMMENTS_FILE}" \
		--pipeline-login "${LOGIN}" > "${CHECK_FILE}" 2>/dev/null; then
		log "heal_scope_recovery outcome=skip reason=check_failed issue=${NUMBER}"
		continue
	fi
	ATTEMPTS="$(jq -r '.scan_attempts // 0' "${CHECK_FILE}")"
	[[ "${ATTEMPTS}" =~ ^[0-9]+$ ]] || ATTEMPTS=0
	if [ "$(jq -r '.eligible // false' "${CHECK_FILE}")" != "true" ]; then
		CHECK_REASON="$(jq -r '.reason // "not_eligible"' "${CHECK_FILE}" | tr -cd 'a-z_' | head -c 40)"
		if [ "${CHECK_REASON}" = "already_replaced" ]; then
			# A finalize whose close failed left this issue open with a trusted
			# recovery marker; close it so it leaves the candidate list.
			CLOSE_STATE="failed"
			if gh_retry gh issue close "${NUMBER}" --repo "${SELF_REPO}" --reason "not planned" >/dev/null 2>&1; then
				CLOSE_STATE="done"
			fi
			log "heal_scope_recovery outcome=skip reason=already_replaced issue=${NUMBER} close=${CLOSE_STATE}"
			if [ "${CLOSE_STATE}" = "failed" ] && [ "${ATTEMPTS}" -lt "${MAX_ATTEMPTS}" ]; then
				# An attempt marker bounds the retries; the last one sends a
				# single WARNING and drops the issue from the candidate list.
				_post_attempt_marker "${NUMBER}" "close_failed" "${ATTEMPTS}"
				if [ "${ATTEMPT_MARKER_POSTED}" != "true" ]; then
					# Neither the close nor the marker landed, so no attempt was
					# counted: warn instead of retrying silently every hour.
					tg_send_msg "Workflow failure heal: the recovery scan could not close already-replaced https://github.com/${SELF_REPO}/issues/${NUMBER} or record the attempt; close it by hand."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
				fi
			fi
			continue
		fi
		log "heal_scope_recovery outcome=skip reason=${CHECK_REASON} issue=${NUMBER}"
		continue
	fi
	if [ "${ATTEMPTS}" -ge "${MAX_ATTEMPTS}" ]; then
		log "heal_scope_recovery outcome=attempts_exhausted reason=attempts_exhausted issue=${NUMBER} attempts=${ATTEMPTS} max=${MAX_ATTEMPTS}"
		continue
	fi
	# Re-read the issue right before it is rebuilt: the list snapshot can be
	# an earlier intake run old, and the never-edited check needs the current
	# lastEditedAt (REST has no such field). A failed read is transient.
	FRESH_FILE="${WORK}/issue_fresh.json"
	if ! gh_retry gh api graphql -f query="${ISSUE_QUERY}" -f owner="${SELF_REPO%%/*}" -f name="${SELF_REPO#*/}" -F number="${NUMBER}" > "${FRESH_FILE}" 2>/dev/null \
		|| ! jq -e '.data.repository.issue | type == "object"' "${FRESH_FILE}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=issue_unavailable issue=${NUMBER}"
		continue
	fi
	if [ "$(jq -r '.data.repository.issue.state // ""' "${FRESH_FILE}")" != "OPEN" ]; then
		log "heal_scope_recovery outcome=skip reason=issue_closed issue=${NUMBER}"
		continue
	fi
	jq -s '.[0] + {body: (.[1].data.repository.issue.body // ""), last_edited_at: .[1].data.repository.issue.lastEditedAt}' \
		"${ISSUE_FILE}" "${FRESH_FILE}" > "${ISSUE_FILE}.fresh" && mv "${ISSUE_FILE}.fresh" "${ISSUE_FILE}"
	PULL_FILE="${WORK}/pull.json"
	PULL_COMMENTS_FILE="${WORK}/pull_comments.json"
	printf '{}' > "${PULL_FILE}"
	printf '[]' > "${PULL_COMMENTS_FILE}"
	SOURCE="$(jq -r '(.body // "") | capture("<!--\\s*workflow-failure-heal:source=(?<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?<n>[1-9][0-9]*)\\s*-->") | "\(.repo) \(.n)"' "${ISSUE_FILE}" 2>/dev/null | head -n 1 || true)"
	SOURCE_REPO="${SOURCE% *}"
	SOURCE_NUMBER="${SOURCE#* }"
	# The PR is read only for an unedited autofix heal issue with a parseable
	# source; anything else reaches reconstruct with an empty PR, which names
	# the skip (an edited body never chooses which repository is read).
	if [[ "${SOURCE_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && [[ "${SOURCE_NUMBER}" =~ ^[1-9][0-9]*$ ]] \
		&& jq -e '.last_edited_at == null' "${ISSUE_FILE}" >/dev/null 2>&1 \
		&& jq -e '(.body // "") | contains("- **Consecutive failed review runs on this PR:** ")' "${ISSUE_FILE}" >/dev/null 2>&1; then
		if ! gh_retry gh api --method GET "repos/${SOURCE_REPO}/pulls/${SOURCE_NUMBER}" > "${PULL_FILE}" 2>/dev/null \
			|| ! jq -e 'type == "object"' "${PULL_FILE}" >/dev/null 2>&1 \
			|| ! gh_retry gh api --method GET --paginate "repos/${SOURCE_REPO}/issues/${SOURCE_NUMBER}/comments" -F per_page=100 \
				--jq '.[] | {id, body: (.body // ""), user: (.user // {}), author_association: (.author_association // "")}' 2>/dev/null \
				| jq -s '.' > "${PULL_COMMENTS_FILE}" 2>/dev/null \
			|| ! jq -e 'type == "array"' "${PULL_COMMENTS_FILE}" >/dev/null 2>&1; then
			log "heal_scope_recovery outcome=skip reason=pull_unavailable issue=${NUMBER}"
			continue
		fi
	fi
	RECONSTRUCT_FILE="${WORK}/reconstruct.json"
	if ! python3 "${HEAL_PY}" heal-scope-recovery reconstruct --issue-json "${ISSUE_FILE}" --comments-json "${COMMENTS_FILE}" \
		--pull-json "${PULL_FILE}" --pull-comments-json "${PULL_COMMENTS_FILE}" --pipeline-login "${LOGIN}" \
		--self-repo "${SELF_REPO}" > "${RECONSTRUCT_FILE}" 2>/dev/null \
		|| ! jq -e '.status == "ok" or .status == "skip"' "${RECONSTRUCT_FILE}" >/dev/null 2>&1; then
		log "heal_scope_recovery outcome=skip reason=reconstruct_failed issue=${NUMBER}"
		continue
	fi
	if [ "$(jq -r '.status' "${RECONSTRUCT_FILE}")" != "ok" ]; then
		SKIP="$(jq -r '.reason // "unknown"' "${RECONSTRUCT_FILE}" | tr -cd 'a-z0-9_' | head -c 60)"
		log "heal_scope_recovery outcome=skip reason=${SKIP:-unknown} issue=${NUMBER}"
		if python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import workflow_failure_heal as h; sys.exit(0 if sys.argv[2] in h.HEAL_SCOPE_RECOVERY_SCAN_PERMANENT_REASONS else 1)' \
			"$(dirname "${HEAL_PY}")" "${SKIP}" 2>/dev/null; then
			# No intake ran, so the skip does not take this tick's slot from a
			# later candidate; MAX_EXAMINED still bounds the marker writes.
			_post_attempt_marker "${NUMBER}" "${SKIP}" "${ATTEMPTS}"
		fi
		continue
	fi
	PROCESSED=$((PROCESSED + 1))
	EXPECTED_FP="$(jq -r '.expected_fp' "${RECONSTRUCT_FILE}")"
	EXPECTED_ISSUE="$(jq -r '.expected_issue' "${RECONSTRUCT_FILE}")"
	PAYLOAD_FILE="${WORK}/payload_raw.json"
	jq '.payload' "${RECONSTRUCT_FILE}" > "${PAYLOAD_FILE}"
	INTAKE_LOG="${WORK}/intake.log"
	INTAKE_RC=0
	env -u WORKFLOW_HEAL_REPORT_IDENTITY_STATE -u WORKFLOW_HEAL_REPORT_IDENTITY_FILE \
		RUNTIME_DIR="${WORK}/intake" \
		WORKFLOW_HEAL_PAYLOAD_FILE="${PAYLOAD_FILE}" \
		WORKFLOW_HEAL_REPORT_ORIGIN="recovery_scan" \
		WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP="${EXPECTED_FP}" \
		WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE="${EXPECTED_ISSUE}" \
		bash "${INTAKE_SCRIPT}" < /dev/null > "${INTAKE_LOG}" 2>&1 || INTAKE_RC=$?
	cat "${INTAKE_LOG}"
	if grep -q '^WORKFLOW_HEAL heal_scope_recovery outcome=replaced ' "${INTAKE_LOG}"; then
		log "heal_scope_recovery outcome=scan reason=replaced issue=${NUMBER} intake_rc=${INTAKE_RC}"
		continue
	fi
	# The intake ends a replay that recovered nothing with a generic
	# `reason=not_recovered` line; the specific reason logged before it wins.
	REASON="$(grep -E '^WORKFLOW_HEAL (heal_scope_recovery outcome=(skip|no_replacement)|skip|report_auth=rejected|error) reason=' "${INTAKE_LOG}" \
		| grep -v ' reason=not_recovered ' | tail -n 1 | sed -n 's/.* reason=\([a-z0-9_]*\).*/\1/p' || true)"
	if [ -z "${REASON}" ] && grep -q '^WORKFLOW_HEAL error heal_issue_list_failed' "${INTAKE_LOG}"; then
		# The intake's heal-issue list read failed (its log line has no reason= token).
		REASON="heal_issue_list_failed"
	fi
	if [ -z "${REASON}" ] && grep -q ' reason=not_recovered ' "${INTAKE_LOG}"; then
		REASON="not_recovered"
	fi
	[ -n "${REASON}" ] || REASON="intake_failed"
	case "${REASON}" in
		budget_budget_exhausted|budget_failed|comments_unavailable|identity_unavailable|issue_record_missing|heal_issue_list_failed)
			# A cap or read failure that clears on its own: no attempt is
			# recorded, so it cannot retire the issue, and it does not count as
			# an intake failure. Only heal_issue_list_failed alerts from the
			# intake; the others are logged only. The next run retries.
			log "heal_scope_recovery outcome=deferred reason=${REASON} issue=${NUMBER} attempts=${ATTEMPTS} max=${MAX_ATTEMPTS} intake_rc=${INTAKE_RC}"
			if [ "${DEFERRED}" -lt "${MAX_DEFERRED}" ]; then
				DEFERRED=$((DEFERRED + 1))
				PROCESSED=$((PROCESSED - 1))
			fi
			continue
			;;
	esac
	if [ "${INTAKE_RC}" -ne 0 ]; then
		INTAKE_FAILURES=$((INTAKE_FAILURES + 1))
	fi
	_post_attempt_marker "${NUMBER}" "${REASON}" "${ATTEMPTS}"
done 3< <(jq -c '.[]' "${CANDIDATES_FILE}")

log "heal_scope_recovery outcome=scan reason=done processed=${PROCESSED} examined=${EXAMINED} deferred=${DEFERRED} intake_failures=${INTAKE_FAILURES}"
if [ "${PROCESSED}" -gt 0 ] && [ "${INTAKE_FAILURES}" -ge "${PROCESSED}" ]; then
	exit 1
fi
exit 0
