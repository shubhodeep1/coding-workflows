#!/usr/bin/env bash
#
# workflow_failure_heal_non_retryable_backfill.sh
#
# Scheduled step helper (issue #7291, activation gap of PR #7065). Runs from
# the "Backfill non-retryable autofix failure reports" step of
# .github/workflows/review_autofix_sweep.yml (every 30 minutes, this repo) and
# the `non-retryable-backfill` job of workflow-templates/ai-review.yml (hourly
# schedule, consumers). It re-sends a non-retryable review/autofix failure
# (NON_RETRYABLE_FAILURE_REASONS in workflow_failure_heal.py) that the heal
# intake never diagnosed, such as a run from before #6756 or a lost dispatch:
#
#   1. Reads the GH_PAT login once (`gh api user`); only that account's PR
#      comments are trusted. No login: the tick is skipped.
#   2. Lists open PRs with their last 100 comments in one batched, paginated
#      GraphQL query (see "PR listing" below).
#   3. `workflow_failure_heal.py non-retryable-backfill-candidates` picks PRs
#      whose newest trusted current-head `review-autofix-failure:v1` marker
#      names a non-retryable reason and a run, is at least
#      WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES old, and has no
#      trusted `ai:workflow-heal-non-retryable:v1` diagnosis or
#      `ai:workflow-heal-non-retryable-backfill:v1` marker yet.
#   4. Calls the unchanged scripts/workflow_failure_heal_autofix_report.sh
#      for each candidate (at most WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK),
#      with the marker's run as GITHUB_RUN_ID, so the report's run_refs name
#      the historical failed run and the intake's provenance gate checks it.
#      No OIDC report identity is attached (the job has no id-token), so the
#      intake checks the report through claim binding and provenance only; an
#      intake with WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=true skips these reports.
#   5. After a confirmed dispatch it posts one PR comment ending in the
#      backfill marker, so a report the intake rejects is not sent again.
#
# The intake's routing is unchanged: a verified report gets one diagnosis
# comment, ai:needs-human and a WARNING, never a heal issue.
#
# Every exit is 0. Every line is prefixed WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL
# and built from sanitized tokens only.
#
# PR listing (CLAUDE.md §15 / unattended §14): the sweep keeps no PR comment
# snapshot to reuse, so this pass makes one GraphQL query per page of 10 open
# PRs (least recently updated first, so a PR the pass or the intake just
# commented on moves to the back of the window), at most ceil(MAX_PRS / 10)
# calls (5 by default). A listing cut off at MAX_PRS logs reason=listing_truncated;
# with more than MAX_PRS open PRs each tick starts at a rotated window (see
# "Window rotation" below: one totalCount call, plus one call per 100 skipped PRs).
# Input: the repository. Output: a JSON array of PR nodes (number,
# isCrossRepository, headRefOid, comments.nodes{databaseId, body, createdAt,
# author.login}). Any failure skips the tick (fail open). A marker older than
# the last 100 comments is not seen, so nothing is sent for it.
# Other calls per tick: `gh api user` (1); per dispatched candidate at most 3
# (the PR read that binds the report to the selected head, the reporter's
# dispatch, the marker comment).
#
# Required env:
#   REPOSITORY                     owner/repo whose open PRs are scanned
#   GH_TOKEN                       GH_PAT (reads PRs, dispatches upstream, posts the marker)
#
# Optional env (defaults set here):
#   WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED        unset/empty/true: on; false: off; other: off with a warning
#   WORKFLOW_HEAL_ENABLED                               false: skip
#   WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK   default 3 (1-10)
#   WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS        default 50 (1-200)
#   WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES default 60 (0-10080)
#   DRY_RUN                                             true: log candidates only
#   HEAD_REF_FILTER                                     non-empty: only PRs whose head branch contains it
#   GITHUB_RUN_NUMBER                                   set by Actions; selects the rotated PR window
#   WORKFLOW_HEAL_UPSTREAM_REPO                         passed to the reporter
#   WORKFLOW_HEAL_PY, WORKFLOW_HEAL_AUTOFIX_REPORT_SH   helper paths (default: this directory)

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL $*"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"
REPORT_SH="${WORKFLOW_HEAL_AUTOFIX_REPORT_SH:-${script_dir}/workflow_failure_heal_autofix_report.sh}"
REPO="${REPOSITORY:-}"
export PYTHONDONTWRITEBYTECODE=1

# bounded_int <value> <default> <min> <max> <name>
bounded_int()
{
	local value="$1" default="$2" min="$3" max="$4" name="$5"
	if ! [[ "${value}" =~ ^[0-9]{1,6}$ ]] || [ "${value}" -lt "${min}" ]; then
		echo "::warning::WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL invalid ${name}; using ${default}" >&2
		value="${default}"
	fi
	# Read as base 10, so a leading zero ("08") is not parsed as octal later.
	value=$((10#${value}))
	[ "${value}" -gt "${max}" ] && value="${max}"
	printf '%s' "${value}"
}

# --- Gates -----------------------------------------------------------------

enabled_raw="$(printf '%s' "${WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
case "${enabled_raw}" in
	''|true) ;;
	false)
		log "outcome=skip reason=disabled"
		exit 0
		;;
	*)
		echo "::warning::WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED has an unrecognized value; the backfill is off"
		log "outcome=skip reason=invalid_switch"
		exit 0
		;;
esac
if [ "$(printf '%s' "${WORKFLOW_HEAL_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
	log "outcome=skip reason=workflow_heal_disabled"
	exit 0
fi
if [ -z "${GH_TOKEN:-}" ]; then
	log "outcome=skip reason=gh_pat_missing"
	exit 0
fi
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	log "outcome=skip reason=missing_repository"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ] || [ ! -f "${REPORT_SH}" ]; then
	log "outcome=skip reason=helper_missing"
	exit 0
fi

MAX_PER_TICK="$(bounded_int "${WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK:-3}" 3 1 10 MAX_PER_TICK)"
MAX_PRS="$(bounded_int "${WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS:-50}" 50 1 200 MAX_PRS)"
MIN_AGE="$(bounded_int "${WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES:-60}" 60 0 10080 MIN_AGE_MINUTES)"
DRY_RUN_MODE=false
[ "$(printf '%s' "${DRY_RUN:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ] && DRY_RUN_MODE=true

if [ -f "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

WORK_ROOT="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/non-retryable-backfill.XXXXXX" 2>/dev/null || true)"
if [ -z "${WORK_ROOT}" ] || [ ! -d "${WORK_ROOT}" ]; then
	log "outcome=skip reason=workdir_unavailable"
	exit 0
fi
trap 'rm -rf -- "${WORK_ROOT}"' EXIT

# --- Identity --------------------------------------------------------------

LOGIN="$(gh api user --jq .login 2>/dev/null | head -1 || true)"
if ! [[ "${LOGIN}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]]; then
	log "outcome=skip reason=identity_unavailable"
	exit 0
fi

# --- PR listing ------------------------------------------------------------

OWNER="${REPO%%/*}"
NAME="${REPO#*/}"
# shellcheck disable=SC2016 # GraphQL variables, not shell expansions.
QUERY='query($owner: String!, $name: String!, $first: Int!, $cursor: String) { repository(owner: $owner, name: $name) { pullRequests(states: OPEN, first: $first, after: $cursor, orderBy: {field: UPDATED_AT, direction: ASC}) { pageInfo { hasNextPage endCursor } nodes { number isCrossRepository headRefOid headRefName comments(last: 100) { nodes { databaseId body createdAt author { login } } } } } } }'
NODES_FILE="${WORK_ROOT}/pr_nodes.jsonl"
: > "${NODES_FILE}"
cursor=""
# Window rotation: with more than MAX_PRS open PRs, each tick starts at a
# different window of MAX_PRS PRs (GITHUB_RUN_NUMBER modulo the window count;
# epoch / 1800 without a run number), so idle, ineligible PRs at the front of
# the oldest-updated order cannot hide an eligible PR behind them on every
# tick. Cost: one totalCount call per tick, plus one pageInfo-only call per
# 100 skipped PRs. Any failure starts from the first PR, as before.
# shellcheck disable=SC2016 # GraphQL variables, not shell expansions.
COUNT_QUERY='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { pullRequests(states: OPEN) { totalCount } } }'
# shellcheck disable=SC2016 # GraphQL variables, not shell expansions.
SKIP_QUERY='query($owner: String!, $name: String!, $first: Int!, $cursor: String) { repository(owner: $owner, name: $name) { pullRequests(states: OPEN, first: $first, after: $cursor, orderBy: {field: UPDATED_AT, direction: ASC}) { pageInfo { hasNextPage endCursor } } } }'
count_file="${WORK_ROOT}/count.json"
total_open=0
if gh_retry gh api graphql -f "query=${COUNT_QUERY}" -f "owner=${OWNER}" -f "name=${NAME}" > "${count_file}" 2>/dev/null; then
	total_open="$(python3 -I -B -c '
import json, sys
page = json.load(open(sys.argv[1]))
count = 0
if isinstance(page, dict) and not page.get("errors"):
	count = ((((page.get("data") or {}).get("repository") or {}).get("pullRequests")) or {}).get("totalCount")
print(count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0)
' "${count_file}" 2>/dev/null || echo 0)"
fi
[[ "${total_open}" =~ ^[0-9]{1,7}$ ]] || total_open=0
if [ "${total_open}" -gt "${MAX_PRS}" ]; then
	windows=$(( (total_open + MAX_PRS - 1) / MAX_PRS ))
	tick="${GITHUB_RUN_NUMBER:-}"
	[[ "${tick}" =~ ^[0-9]{1,12}$ ]] || tick=$(( $(date +%s) / 1800 ))
	window=$(( 10#${tick} % windows ))
	skip=$(( window * MAX_PRS ))
	skipped=0
	while [ "${skipped}" -lt "${skip}" ]; do
		chunk=$(( skip - skipped ))
		[ "${chunk}" -gt 100 ] && chunk=100
		skip_file="${WORK_ROOT}/skip.json"
		skip_args=(api graphql -f "query=${SKIP_QUERY}" -f "owner=${OWNER}" -f "name=${NAME}" -F "first=${chunk}")
		[ -n "${cursor}" ] && skip_args+=(-f "cursor=${cursor}")
		skip_info=""
		if gh_retry gh "${skip_args[@]}" > "${skip_file}" 2>/dev/null; then
			skip_info="$(python3 -I -B -c '
import json, re, sys
page = json.load(open(sys.argv[1]))
if not isinstance(page, dict) or page.get("errors"):
	sys.exit(1)
info = ((((page.get("data") or {}).get("repository") or {}).get("pullRequests")) or {}).get("pageInfo") or {}
cursor = info.get("endCursor") if isinstance(info.get("endCursor"), str) else ""
if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,200}", cursor):
	sys.exit(1)
print("true" if info.get("hasNextPage") is True else "false", cursor)
' "${skip_file}" 2>/dev/null || true)"
		fi
		read -r skip_has_next skip_cursor <<< "${skip_info}"
		if [ -z "${skip_cursor:-}" ] || [ "${skip_has_next:-false}" != "true" ]; then
			# Failed read, or fewer PRs than counted: start from the first PR.
			cursor=""
			window=0
			break
		fi
		cursor="${skip_cursor}"
		skipped=$(( skipped + chunk ))
	done
	log "outcome=info reason=window_rotated window=${window} windows=${windows} total_open=${total_open}"
fi
fetched=0
listing_ok=true
while [ "${fetched}" -lt "${MAX_PRS}" ]; do
	page_size=$((MAX_PRS - fetched))
	[ "${page_size}" -gt 10 ] && page_size=10
	page_file="${WORK_ROOT}/page.json"
	page_args=(api graphql -f "query=${QUERY}" -f "owner=${OWNER}" -f "name=${NAME}" -F "first=${page_size}")
	[ -n "${cursor}" ] && page_args+=(-f "cursor=${cursor}")
	if ! gh_retry gh "${page_args[@]}" > "${page_file}" 2>/dev/null; then
		listing_ok=false
		break
	fi
	# Validate the page and append its nodes; prints "<count> <has_next> <cursor>".
	page_info="$(python3 -I -B -c '
import json, re, sys
page = json.load(open(sys.argv[1]))
if not isinstance(page, dict) or page.get("errors"):
	sys.exit(1)
prs = (((page.get("data") or {}).get("repository") or {}).get("pullRequests")) or {}
nodes = prs.get("nodes")
if not isinstance(nodes, list):
	sys.exit(1)
with open(sys.argv[2], "a", encoding="utf-8") as out:
	for node in nodes:
		out.write(json.dumps(node) + "\n")
info = prs.get("pageInfo") or {}
cursor = info.get("endCursor") if isinstance(info.get("endCursor"), str) else ""
cursor = cursor if re.fullmatch(r"[A-Za-z0-9+/=_-]{1,200}", cursor or "") else ""
print(len(nodes), "true" if info.get("hasNextPage") is True else "false", cursor)
' "${page_file}" "${NODES_FILE}" 2>/dev/null)" || { listing_ok=false; break; }
	read -r page_count has_next cursor <<< "${page_info}"
	[[ "${page_count:-}" =~ ^[0-9]+$ ]] || { listing_ok=false; break; }
	fetched=$((fetched + page_count))
	if [ "${has_next:-false}" != "true" ] || [ -z "${cursor:-}" ] || [ "${page_count}" -eq 0 ]; then
		break
	fi
done
if [ "${listing_ok}" != "true" ]; then
	log "outcome=skip reason=pr_listing_unavailable"
	exit 0
fi
if [ "${has_next:-false}" = "true" ] && [ "${fetched}" -ge "${MAX_PRS}" ]; then
	log "outcome=warn reason=listing_truncated fetched=${fetched} max=${MAX_PRS}"
fi
PRS_FILE="${WORK_ROOT}/prs.json"
# HEAD_REF_FILTER (the sweep's head_ref_filter dispatch input): keep only PRs
# whose head branch contains it, the same substring match the enumerate step
# uses. Empty keeps every PR.
if ! python3 -I -B -c '
import json, sys
nodes = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
head_filter = sys.argv[3]
if head_filter:
	nodes = [node for node in nodes if isinstance(node, dict) and isinstance(node.get("headRefName"), str) and head_filter in node["headRefName"]]
json.dump(nodes, open(sys.argv[2], "w", encoding="utf-8"))
' "${NODES_FILE}" "${PRS_FILE}" "${HEAD_REF_FILTER:-}" 2>/dev/null; then
	log "outcome=skip reason=pr_listing_unavailable"
	exit 0
fi

# --- Candidates ------------------------------------------------------------

SELECTION_FILE="${WORK_ROOT}/selection.json"
if ! python3 -I -B "${HEAL_PY}" non-retryable-backfill-candidates --prs-json "${PRS_FILE}" --repo "${REPO}" \
	--author-login "${LOGIN}" --min-age-minutes "${MIN_AGE}" > "${SELECTION_FILE}" 2>/dev/null; then
	log "outcome=skip reason=selector_failed"
	exit 0
fi
# First line: the candidate count. Then "skip<TAB>pr<TAB>reason" rows and
# "candidate<TAB>pr<TAB>head<TAB>failure<TAB>fp<TAB>run<TAB>support" rows; each
# candidate's comments go to <WORK_ROOT>/pr-<n>/comments.json. Every field is
# re-checked against a fixed pattern before it reaches a log line or command.
ROWS_FILE="${WORK_ROOT}/rows.tsv"
if ! python3 -I -B -c '
import json, os, re, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
root = sys.argv[2]
if not isinstance(data, dict) or not isinstance(data.get("candidates"), list) or not isinstance(data.get("skipped"), list):
	sys.exit(1)
def tok(value, pattern):
	value = str(value if value is not None else "")
	return value if re.fullmatch(pattern, value) else ""
print(len(data["candidates"]))
for row in data["skipped"]:
	if isinstance(row, dict) and tok(row.get("pr"), r"[1-9][0-9]{0,9}"):
		print("skip", tok(row.get("pr"), r"[1-9][0-9]{0,9}"), tok(row.get("reason"), r"[a-z_]{1,40}") or "unknown", sep="\t")
for item in data["candidates"]:
	if not isinstance(item, dict):
		continue
	fields = [
		tok(item.get("pr"), r"[1-9][0-9]{0,9}"),
		tok(item.get("head"), r"[0-9a-f]{40}"),
		tok(item.get("failure_reason"), r"[a-z][a-z0-9_]{0,79}"),
		tok(item.get("fp"), r"[0-9a-f]{64}"),
		tok(item.get("run"), r"[0-9]{1,20}"),
	]
	if not all(fields) or not isinstance(item.get("comments"), list):
		print("invalid")
		continue
	cand_dir = os.path.join(root, "pr-" + fields[0])
	os.makedirs(os.path.join(cand_dir, "runtime"), exist_ok=True)
	json.dump(item["comments"], open(os.path.join(cand_dir, "comments.json"), "w", encoding="utf-8"))
	print("candidate", *fields, tok(item.get("support"), r"[0-9a-f]{40}") or "-", sep="\t")
' "${SELECTION_FILE}" "${WORK_ROOT}" > "${ROWS_FILE}" 2>/dev/null; then
	log "outcome=skip reason=selector_failed"
	exit 0
fi

scanned="$(python3 -I -B -c 'import json, sys; print(len(json.load(open(sys.argv[1]))))' "${PRS_FILE}" 2>/dev/null || echo 0)"
candidate_count="$(head -1 "${ROWS_FILE}")"
[[ "${candidate_count}" =~ ^[0-9]+$ ]] || candidate_count=0
dispatched=0
attempted=0
while IFS=$'\t' read -r kind pr head failure fp run support; do
	case "${kind}" in
		skip)
			# For a skip row the third column is the reason.
			log "pr=${pr} outcome=skip reason=${head}"
			continue
			;;
		invalid)
			log "outcome=skip reason=invalid_candidate"
			continue
			;;
		candidate) ;;
		*) continue ;;
	esac
	[ "${support}" = "-" ] && support=""
	fields="pr=${pr} head=${head} failure=${failure} run=${run}"
	if [ "${attempted}" -ge "${MAX_PER_TICK}" ]; then
		log "${fields} outcome=skip reason=tick_cap"
		continue
	fi
	attempted=$((attempted + 1))
	cand_dir="${WORK_ROOT}/pr-${pr}"
	comments_file="${cand_dir}/comments.json"
	if [ "${DRY_RUN_MODE}" = "true" ]; then
		log "${fields} outcome=skip reason=dry_run"
		continue
	fi
	report_out="${cand_dir}/report.txt"
	# Bind the report to the selected head: read the live PR once and hand
	# that snapshot to the reporter, so a push after the listing cannot get
	# the old head's failure reported against the new head.
	pr_json="${cand_dir}/pr.json"
	if ! gh_retry gh api --method GET "repos/${REPO}/pulls/${pr}" > "${pr_json}" 2>/dev/null; then
		log "${fields} outcome=skip reason=pr_read_failed"
		continue
	fi
	live_head="$(python3 -I -B -c '
import json, sys
pr = json.load(open(sys.argv[1], encoding="utf-8"))
head = (pr.get("head") or {}).get("sha") if isinstance(pr, dict) else ""
print(head.lower() if isinstance(head, str) else "")
' "${pr_json}" 2>/dev/null || true)"
	if ! [[ "${live_head}" =~ ^[0-9a-f]{40}$ ]]; then
		log "${fields} outcome=skip reason=head_unreadable"
		continue
	fi
	if [ "${live_head}" != "${head}" ]; then
		log "${fields} outcome=skip reason=head_moved"
		continue
	fi
	# The unchanged reporter, with the historical run as GITHUB_RUN_ID so the
	# report's run_refs name it, the head-checked PR snapshot as its PR
	# payload, no run flags, and no OIDC request variables.
	env -u PR_CHANGED_FILES_FILE -u FINGERPRINT_CAP_REASON -u REPORT_SUMMARY_LINE_FILE \
		-u AUTOFIX_FAILURE_FIRST_ERROR -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN \
		GITHUB_REPOSITORY="${REPO}" \
		PR_NUMBER="${pr}" \
		PR_PAYLOAD_FILE="${pr_json}" \
		GITHUB_RUN_ID="${run}" \
		REPORT_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${run}" \
		REPORT_WORKFLOW_NAME="review_autofix" \
		REPORT_WRAPPER_SHA="${support}" \
		AUTOFIX_FAILURE_REASON="${failure}" \
		AUTOFIX_FAILURE_FP="${fp}" \
		AUTOFIX_FAILURE_MARKER_AUTHOR="${LOGIN}" \
		PR_ISSUE_COMMENTS_FILE="${comments_file}" \
		RUNTIME_DIR="${cand_dir}/runtime" \
		WORKFLOW_HEAL_PY="${HEAL_PY}" \
		RESOLVER_ESCALATED=false \
		AUTOFIX_EDITOR_EMPTY_NOOP=false \
		EDITOR_CHANGES_LOST=false \
		EDITOR_NOOP_REFUSAL=false \
		EDITOR_NOOP_SUSPICIOUS=false \
		EDITOR_PREFLIGHT_FAILED=false \
		AUTOFIX_REVIEWERS_FAILED=false \
		AUTOFIX_SANDBOX_PREPARE_FAILED=false \
		bash "${REPORT_SH}" > "${report_out}" 2>&1 || true
	sed -n '/^WORKFLOW_HEAL_AUTOFIX_REPORT /p' "${report_out}" | cut -c1-400
	if grep -Eq "^WORKFLOW_HEAL_AUTOFIX_REPORT dispatched pr=${pr} " "${report_out}"; then
		dispatched=$((dispatched + 1))
		marker_status=posted
		comment_file="${cand_dir}/marker_comment.md"
		if ! python3 -I -B "${HEAL_PY}" non-retryable-backfill-comment --pr "${pr}" --head "${head}" --reason "${failure}" --run "${run}" > "${comment_file}" 2>/dev/null \
			|| ! gh api -X POST "repos/${REPO}/issues/${pr}/comments" -F "body=@${comment_file}" >/dev/null 2>&1; then
			marker_status=failed
		fi
		log "${fields} outcome=dispatched marker=${marker_status}"
	else
		report_reason="$(sed -n 's/^WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=\([A-Za-z0-9_]*\).*/\1/p' "${report_out}" | head -1)"
		log "${fields} outcome=failed reason=report_${report_reason:-unknown}"
	fi
done < <(tail -n +2 "${ROWS_FILE}")

log "outcome=complete scanned=${scanned} candidates=${candidate_count} dispatched=${dispatched} dry_run=${DRY_RUN_MODE}"
exit 0
