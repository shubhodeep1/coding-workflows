#!/usr/bin/env bash
#
# claude_issue_queue_watchdog.sh
#
# Driven hourly by .github/workflows/claude-issue-queue-watchdog.yml in
# coding-workflows. It catches queued Claude issues that nobody picked up:
# the Claude issue pickup session (.claude/commands/claude-issue-pickup.md)
# normally closes each `ai:claude-issue-queue` issue within about an hour, so
# an item older than CLAUDE_ISSUE_QUEUE_STALE_HOURS means the pickup has
# stopped (its session failed or was archived, its `Claude issue pickup: hourly`
# trigger was deleted, or it hit the session depth limit) or create_session
# keeps failing.
#
# For each stale item not yet flagged it adds `ai:claude-issue-queue-stale`
# (with GITHUB_TOKEN, so no workflow reacts), then sends one Telegram ERROR
# naming the items and how to restart the pickup. It exits 0: the alert is the
# Telegram message and the label, not a red run. One REST read per run, plus
# one label write per newly stale item (CLAUDE.md §15). Log lines are prefixed
# CLAUDE_ISSUE_QUEUE_WATCHDOG.
#
# An item that waits on a `Depends on: #N` issue (issue #4934) is held by the
# pickup on purpose, so `queue-stale --fetch-dependencies` judges it from its
# dependencies instead: not stale while one is open, flagged at once when one
# was closed without `ai:merged`, and aged from the latest close once all are
# merged. That costs one more REST read per distinct dependency of the open
# dependent items (none when there are none); an unreadable dependency skips
# the item this run.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY                  this repo (coding-workflows)
#   GH_TOKEN                           the workflow's GITHUB_TOKEN (issues: write)
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_QUEUE_STALE_HOURS     default 3
#   CLAUDE_ISSUE_ROUTE_PY              default scripts/claude_issue_route.py
#   RUN_URL, RUNTIME_DIR

set -euo pipefail

log()
{
	echo "CLAUDE_ISSUE_QUEUE_WATCHDOG $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/label_helpers.sh 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { return 0; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { return 0; }

SELF_REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"
STALE_HOURS="${CLAUDE_ISSUE_QUEUE_STALE_HOURS:-3}"
ROUTE_PY="${CLAUDE_ISSUE_ROUTE_PY:-scripts/claude_issue_route.py}"
RUN_URL="${RUN_URL:-}"
RUNTIME_DIR="${RUNTIME_DIR:-$(mktemp -d)}"
mkdir -p "${RUNTIME_DIR}"

QUEUE_LABEL="ai:claude-issue-queue"
STALE_LABEL="ai:claude-issue-queue-stale"

if ! [[ "${STALE_HOURS}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	log "warn invalid_stale_hours value=${STALE_HOURS} using=3"
	STALE_HOURS="3"
fi

QUEUE_FILE="${RUNTIME_DIR}/open_queue.json"
if ! gh_retry gh api "repos/${SELF_REPO}/issues?labels=${QUEUE_LABEL}&state=open&per_page=100" > "${QUEUE_FILE}"; then
	# Fail open: a missed hourly check is recovered by the next run.
	log "warn queue_read_failed"
	exit 0
fi

STALE_FILE="${RUNTIME_DIR}/stale.json"
python3 "${ROUTE_PY}" queue-stale --issues-json "${QUEUE_FILE}" --stale-hours "${STALE_HOURS}" --fetch-dependencies > "${STALE_FILE}"
OPEN_COUNT="$(jq '[.[]? | select(.pull_request == null)] | length' "${QUEUE_FILE}")"
STALE_COUNT="$(jq 'length' "${STALE_FILE}")"
log "checked open=${OPEN_COUNT} newly_stale=${STALE_COUNT} stale_hours=${STALE_HOURS}"
if [ "${STALE_COUNT}" = "0" ]; then
	exit 0
fi

ensure_label_exists "${STALE_LABEL}" "${SELF_REPO}" || true
LINES=""
NOT_PICKED_UP=0
HELD=0
while IFS=$'\t' read -r number title age reason; do
	[[ "${number}" =~ ^[1-9][0-9]*$ ]] || continue
	gh_retry gh api -X POST "repos/${SELF_REPO}/issues/${number}/labels" -f "labels[]=${STALE_LABEL}" >/dev/null 2>&1 || \
		log "warn label_failed queue_issue=${number}"
	log "stale queue_issue=${number} age_hours=${age} title=${title}"
	if [ "${reason}" = "not picked up" ]; then
		NOT_PICKED_UP=$((NOT_PICKED_UP + 1))
		echo "::warning::Claude issue queue item #${number} (${title}) has waited ${age}h without being picked up"
		LINES+=$'\n'"- #${number} ${title} (${age}h)"
	else
		HELD=$((HELD + 1))
		log "held queue_issue=${number} reason=${reason}"
		echo "::warning::Claude issue queue item #${number} (${title}) is held: ${reason}"
		LINES+=$'\n'"- #${number} ${title} (${age}h; held: ${reason})"
	fi
done < <(jq -r '.[] | [.number, .title, .age_hours, (.reason // "not picked up")] | @tsv' "${STALE_FILE}")

TG_HINTS=""
if [ "${NOT_PICKED_UP}" -gt 0 ]; then
	TG_HINTS+=$'\n'"The pickup has likely stopped. Restart it from a new cloud session opened in the app, in Auto mode: /claude-issue-pickup start — restart"
fi
if [ "${HELD}" -gt 0 ]; then
	TG_HINTS+=$'\n'"A held item waits for its Depends on: issue to be closed with ai:merged. Finish or reopen that issue, or remove the Depends on: line from the item's issue and comment /reclarify there."
fi
tg_send_msg "Claude issue queue: ${STALE_COUNT} item(s) need attention in ${SELF_REPO}:${LINES}${TG_HINTS}"$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
exit 0
