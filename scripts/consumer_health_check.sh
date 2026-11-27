#!/usr/bin/env bash
# consumer_health_check.sh — daily health check of the consumer repositories
# listed in .github/ai/consumer_repos.json.
#
# Runs from the "Run daily consumer health check" step of
# .github/workflows/orchestrate_poll.yml, in this repository only, once per UTC
# day (the step's date-keyed cache entry skips later ticks of the same day).
# For each consumer it checks:
#   - the AI workflow wrappers (the *.yml files under workflow-templates/):
#     one GitHub disabled for inactivity (`disabled_inactivity`) is switched
#     back on; one disabled by a person or token, or in any other non-active
#     state, is reported (`workflows_disabled`);
#   - the poll wrapper (ai-orchestrate-poll.yml), when active, started a run in
#     the last CONSUMER_HEALTH_POLLER_SILENT_HOURS hours (`poller_silent`);
#   - no open ai:orchestrator-tracking issue has
#     CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT or more comments
#     (`tracking_issue_comments`);
#   - the repository can be read at all (`unreadable`).
# Each (consumer, problem) gets one ai:consumer-health issue in this
# repository, opened once with one Telegram WARNING and closed automatically
# when a later check no longer finds the problem. scripts/consumer_health_check.py
# makes every decision; this script only reads and writes.
#
# API calls (§15): 1 /user read, 1 paginated read of this repository's open
# ai:consumer-health issues, and per consumer at most 3 reads (paginated
# workflow list, newest poll run, paginated open tracking issues; one page
# each below 100 entries): about 41 a day for 13 consumers.
# Writes happen only for a change: one PUT per re-enabled workflow, one issue
# create per new problem, and one comment plus one close per cleared problem.
#
# Environment:
#   GH_TOKEN, GITHUB_REPOSITORY            required
#   CONSUMER_HEALTH_CHECK_ENABLED          default on; "false" turns the check
#                                          off, any other value except unset,
#                                          empty or "true" also turns it off
#                                          with a warning
#   CONSUMER_HEALTH_POLLER_SILENT_HOURS    default 6
#   CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT default 2000
#   CONSUMER_HEALTH_CONSUMERS_FILE         default .github/ai/consumer_repos.json
#   CONSUMER_HEALTH_TEMPLATES_FILE         required: wrapper file names, one per line
#   CONSUMER_HEALTH_NOW                    test hook: UTC time for the check
#   CONSUMER_HEALTH_FAILURE_ALERTED        "true" when today's failure WARNING was
#                                          already sent (default false)
#
# Logs (stable prefix CONSUMER_HEALTH):
#   CONSUMER_HEALTH skip reason=disabled|invalid_switch
#   CONSUMER_HEALTH enable repo=<r> workflow=<path> outcome=enabled|failed
#   CONSUMER_HEALTH issue repo=<r> problem=<p> outcome=opened|open_failed number=<n>
#   CONSUMER_HEALTH issue repo=<r> problem=<p> outcome=closed|comment_failed|close_failed number=<n>
#     (comment_failed: the issue closed but its explanation comment failed)
#   CONSUMER_HEALTH outcome=done repos=<n> problems=<n> opened=<n> closed=<n> enabled=<n> write_failures=<n>
#     (a failed issue create or close counts as a write failure and exits 1, so
#     the day is retried on the next tick; a failed enable is retried the next day)
#   CONSUMER_HEALTH outcome=failed reason=<token>    (exit 1: the day is retried)

set -euo pipefail

log()
{
	echo "CONSUMER_HEALTH $*"
}

fail()
{
	log "outcome=failed reason=$1"
	echo "::warning::CONSUMER_HEALTH outcome=failed reason=$1"
	exit 1
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }
source scripts/label_helpers.sh 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { :; }

case "${CONSUMER_HEALTH_CHECK_ENABLED-}" in
	''|[Tt][Rr][Uu][Ee]) ;;
	[Ff][Aa][Ll][Ss][Ee])
		log "skip reason=disabled"
		exit 0
		;;
	*)
		echo "::warning::CONSUMER_HEALTH_CHECK_ENABLED has an unrecognised value; the consumer check is off."
		log "skip reason=invalid_switch"
		exit 0
		;;
esac

positive_int_or()
{
	if [[ "$1" =~ ^[1-9][0-9]{0,5}$ ]]; then
		printf '%s' "$1"
	else
		printf '%s' "$2"
	fi
}

SELF_REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"
SILENT_HOURS="$(positive_int_or "${CONSUMER_HEALTH_POLLER_SILENT_HOURS:-6}" 6)"
COMMENT_LIMIT="$(positive_int_or "${CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT:-2000}" 2000)"
CONSUMERS_FILE="${CONSUMER_HEALTH_CONSUMERS_FILE:-.github/ai/consumer_repos.json}"
TEMPLATES_FILE="${CONSUMER_HEALTH_TEMPLATES_FILE:?CONSUMER_HEALTH_TEMPLATES_FILE required}"
CHECK_PY="scripts/consumer_health_check.py"
LABEL="ai:consumer-health"
WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/consumer-health.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

[ -s "${TEMPLATES_FILE}" ] || fail templates_missing
# A process substitution hides the helper's exit status, so read the list
# through a file to keep the consumers_unreadable failure observable.
python3 "${CHECK_PY}" consumers --consumers-json "${CONSUMERS_FILE}" > "${WORK}/consumers.txt" || fail consumers_unreadable
mapfile -t CONSUMERS < "${WORK}/consumers.txt"

PIPELINE_LOGIN="$(gh_retry gh api user --jq .login 2>/dev/null)" || fail pipeline_login_unavailable
[ -n "${PIPELINE_LOGIN}" ] || fail pipeline_login_unavailable

gh_retry gh api --paginate "repos/${SELF_REPO}/issues?state=open&labels=ai%3Aconsumer-health&per_page=100" > "${WORK}/open_pages.json" 2>/dev/null \
	|| fail open_issues_unavailable
jq -s 'add // []' "${WORK}/open_pages.json" > "${WORK}/open_issues.json" 2>/dev/null || fail open_issues_unavailable

# Classify a failed read without echoing GitHub's text into the issue.
read_error_token()
{
	if grep -qiE 'HTTP 404|Not Found' "$1" 2>/dev/null; then
		printf 'not_found_or_no_access'
	elif grep -qiE 'HTTP 403|Resource not accessible|not enabled for this session' "$1" 2>/dev/null; then
		printf 'forbidden'
	else
		printf 'api_error'
	fi
}

for repo in "${CONSUMERS[@]}"; do
	snap="${WORK}/snapshot/${repo//\//__}"
	mkdir -p "${snap}"
	# Both listings are paginated so entries past the first 100 are checked.
	if ! gh_retry gh api --paginate "repos/${repo}/actions/workflows?per_page=100" > "${snap}/workflows_pages.json" 2> "${snap}/workflows.err"; then
		read_error_token "${snap}/workflows.err" > "${snap}/error"
		continue
	fi
	if ! jq -s '{workflows: (map(.workflows // []) | add // [])}' "${snap}/workflows_pages.json" > "${snap}/workflows.json" 2>/dev/null; then
		printf 'api_error' > "${snap}/error"
		continue
	fi
	poll_id="$(jq -r '[.workflows[]? | select(.state == "active" and (.path | endswith("/ai-orchestrate-poll.yml")))][0].id // empty' "${snap}/workflows.json" 2>/dev/null || true)"
	if [[ "${poll_id}" =~ ^[0-9]+$ ]]; then
		gh_retry gh api "repos/${repo}/actions/workflows/${poll_id}/runs?per_page=1" > "${snap}/poll_runs.json" 2>/dev/null \
			|| rm -f "${snap}/poll_runs.json"
	fi
	if ! gh_retry gh api --paginate "repos/${repo}/issues?state=open&labels=ai%3Aorchestrator-tracking&per_page=100" > "${snap}/tracking_pages.json" 2>/dev/null \
		|| ! jq -s 'add // []' "${snap}/tracking_pages.json" > "${snap}/tracking.json" 2>/dev/null; then
		rm -f "${snap}/tracking.json"
	fi
done

now_args=()
if [ -n "${CONSUMER_HEALTH_NOW:-}" ]; then
	now_args=(--now "${CONSUMER_HEALTH_NOW}")
fi
python3 "${CHECK_PY}" evaluate --consumers-json "${CONSUMERS_FILE}" --snapshot-dir "${WORK}/snapshot" \
	--open-issues-json "${WORK}/open_issues.json" --templates-file "${TEMPLATES_FILE}" \
	--pipeline-login "${PIPELINE_LOGIN}" --silent-hours "${SILENT_HOURS}" --comment-limit "${COMMENT_LIMIT}" \
	"${now_args[@]}" > "${WORK}/result.json" || fail evaluate_failed

# A failed issue create or close is counted and makes the run exit 1 after the
# summary, so the workflow does not record the day as checked and the next tick
# retries it (issues already opened are deduplicated by the open-issue read).
write_failures=0
enabled=0
enable_failed_msgs=()
while IFS=$'\t' read -r repo workflow_id path; do
	[ -n "${repo}" ] || continue
	if gh_retry gh api -X PUT "repos/${repo}/actions/workflows/${workflow_id}/enable" >/dev/null 2>&1; then
		enabled=$((enabled + 1))
		log "enable repo=${repo} workflow=${path} outcome=enabled"
	else
		# Not counted as a write failure: an enable that keeps failing (archived
		# repo, missing scope) would otherwise rerun the check and re-page on
		# every tick. The next day's check retries it.
		log "enable repo=${repo} workflow=${path} outcome=failed"
		enable_failed_msgs+=("Consumer check: could not switch ${path} back on in ${repo} after GitHub disabled it for inactivity.")
	fi
done < <(jq -r '.enable[] | [.repo, (.workflow_id|tostring), .path] | @tsv' "${WORK}/result.json")

problem_text()
{
	case "$1" in
		unreadable) printf 'cannot be read' ;;
		workflows_disabled) printf 'AI workflows are switched off' ;;
		poller_silent) printf 'the orchestrator poller has not run for %s hours' "${SILENT_HOURS}" ;;
		tracking_issue_comments) printf 'a tracking issue is near the comment cap' ;;
		*) printf '%s' "$1" ;;
	esac
}

problem_advice()
{
	case "$1" in
		unreadable) printf 'Check that the GH_PAT secret still has access to this repository and that it still exists.' ;;
		workflows_disabled) printf 'These wrappers were switched off by a person or token, so the check leaves them alone. Switch them back on when the reason for pausing is resolved, or remove the repository from `.github/ai/consumer_repos.json` if it is retired.' ;;
		poller_silent) printf 'Look at the latest runs of `ai-orchestrate-poll.yml` in the repository: GitHub may have stopped the schedule, or every run may be queued behind a stuck one.' ;;
		tracking_issue_comments) printf 'GitHub refuses comments past 2500. The poller prunes superseded state comments (#7376) once its `stable` sync includes that change; check that the sync ran.' ;;
	esac
}

opened=0
ensured_label=false
while IFS=$'\t' read -r repo problem detail; do
	[ -n "${repo}" ] || continue
	if [ "${ensured_label}" = false ]; then
		ensure_label_exists "${LABEL}" "${SELF_REPO}" >/dev/null 2>&1 || true
		ensured_label=true
	fi
	title="Consumer check: ${repo}: $(problem_text "${problem}")"
	body="$(printf '<!-- ai:consumer-health:v1 repo=%s problem=%s -->\n## Consumer check: %s\n\n**Repository:** https://github.com/%s\n**Problem:** %s\n**Found:** %s\n\n%s\n\nThe daily consumer check (`scripts/consumer_health_check.sh`, run by the orchestrator poller) closes this issue by itself once the problem is gone.\n' \
		"${repo}" "${problem}" "$(problem_text "${problem}")" "${repo}" "$(problem_text "${problem}")" "${detail}" "$(problem_advice "${problem}")")"
	if number="$(gh_retry gh api -X POST "repos/${SELF_REPO}/issues" -f title="${title}" -f body="${body}" -f "labels[]=${LABEL}" --jq .number 2>/dev/null)" \
		&& [[ "${number}" =~ ^[0-9]+$ ]]; then
		opened=$((opened + 1))
		log "issue repo=${repo} problem=${problem} outcome=opened number=${number}"
		tg_send_msg "Consumer check: ${repo}: $(problem_text "${problem}") (${detail}).
Issue: https://github.com/${SELF_REPO}/issues/${number}" "WARNING" >/dev/null || true
	else
		write_failures=$((write_failures + 1))
		log "issue repo=${repo} problem=${problem} outcome=open_failed number=none"
	fi
done < <(jq -r '.open[] | [.repo, .problem, .detail] | @tsv' "${WORK}/result.json")

closed=0
while IFS=$'\t' read -r number repo problem; do
	[ -n "${number}" ] || continue
	# Close first, then explain: a failed close is retried on the next tick
	# without having posted a comment, so retries never duplicate it. A failed
	# explanation on an already-closed issue is logged but not retried.
	if ! gh_retry gh api -X PATCH "repos/${SELF_REPO}/issues/${number}" -f state=closed -f state_reason=completed >/dev/null 2>&1; then
		write_failures=$((write_failures + 1))
		log "issue repo=${repo} problem=${problem} outcome=close_failed number=${number}"
		continue
	fi
	closed=$((closed + 1))
	if gh_retry gh api -X POST "repos/${SELF_REPO}/issues/${number}/comments" \
		-f body="The daily consumer check no longer finds this problem in ${repo}; closed." >/dev/null 2>&1; then
		log "issue repo=${repo} problem=${problem} outcome=closed number=${number}"
	else
		log "issue repo=${repo} problem=${problem} outcome=comment_failed number=${number}"
	fi
done < <(jq -r '.close[] | [(.number|tostring), .repo, .problem] | @tsv' "${WORK}/result.json")

log "outcome=done repos=$(jq -r .repos "${WORK}/result.json") problems=$(jq -r '.problems | length' "${WORK}/result.json") opened=${opened} closed=${closed} enabled=${enabled} write_failures=${write_failures}"
# Page failed enables only on a run that records the day, so a retry tick
# caused by another failed write does not re-send the same WARNING.
if [ "${write_failures}" -eq 0 ] && [ "${#enable_failed_msgs[@]}" -gt 0 ]; then
	for msg in "${enable_failed_msgs[@]}"; do
		tg_send_msg "${msg}" "WARNING" >/dev/null || true
	done
fi
if [ "${write_failures}" -gt 0 ]; then
	echo "::warning::CONSUMER_HEALTH ${write_failures} write(s) failed; the day is retried on the next tick."
	# Page once per UTC day for a failing run: the workflow records
	# failure_alerted=true in a date-keyed cache entry and passes it back as
	# CONSUMER_HEALTH_FAILURE_ALERTED, so retry ticks stay quiet. Failed
	# enables held back above go into this one message.
	if [ "${CONSUMER_HEALTH_FAILURE_ALERTED:-false}" != "true" ]; then
		alert="Consumer check: ${write_failures} issue write(s) failed in ${SELF_REPO}; the check retries on every poll tick today."
		for msg in ${enable_failed_msgs[@]+"${enable_failed_msgs[@]}"}; do
			alert="${alert}
${msg}"
		done
		# tg_send_msg returns 0 even when it sends nothing (level filter, no
		# secret or chat id, curl failure); only a returned message id proves
		# delivery, so a failed send is retried on the next tick.
		alert_msg_id="$(tg_send_msg "${alert}" "WARNING" 2>/dev/null || true)"
		if [ -n "${alert_msg_id}" ] && [ -n "${GITHUB_OUTPUT:-}" ]; then
			echo "failure_alerted=true" >> "${GITHUB_OUTPUT}"
		fi
	fi
	exit 1
fi
