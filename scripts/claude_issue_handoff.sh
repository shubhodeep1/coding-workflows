#!/usr/bin/env bash
#
# claude_issue_handoff.sh
#
# Driven by the reusable AI Clarify workflow (.github/workflows/clarify.yml)
# when scripts/claude_issue_route.py routes a standalone issue to Claude
# (the default; see AI_ISSUE_IMPLEMENTER). Runs in the repository where the
# issue was opened. It:
#
#   1. Claims the issue for Claude with the `ai:claude` label, so the Codex
#      plan / implement phases and the poller's standalone stall recovery
#      leave it alone, and clears a stale `ai:claude-handoff-failed` label.
#      On `/reclarify` it also drops the Codex phase labels the issue may
#      still carry from an earlier Codex run.
#   2. Sends one thin `repository_dispatch` (event type `claude-issue`) to
#      coding-workflows. Its intake workflow (claude-issue-intake.yml)
#      validates the repo against .github/ai/consumer_repos.json and fires the
#      "Claude issue dispatcher" routine, which starts the Claude session.
#   3. Posts one routing comment on the issue.
#
# A failed dispatch is never silent: the issue gets `ai:claude-handoff-failed`,
# a comment naming how to retry or switch to Codex, and a Telegram ERROR. The
# script still exits 0 in that case so clarify's generic failure comment does
# not duplicate the handoff comment. All stable log lines are prefixed
# CLAUDE_ISSUE_HANDOFF.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY          owner/repo the issue lives in
#   GH_TOKEN                   GH_PAT: labels/comments here, dispatch upstream
#   ISSUE_NUMBER               issue number
#   ISSUE_META_FILE            issue JSON already fetched by clarify
#   CLAUDE_ISSUE_TRIGGER       opened | reclarify
#   CLAUDE_ISSUE_ROUTE_REASON  reason reported by claude_issue_route.py
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_SKIP_SECURITY_PASS  "true" for automation-produced issues
#   CLAUDE_ISSUE_UPSTREAM_REPO       dispatch target (default shubhodeep1/coding-workflows)
#   CLAUDE_ISSUE_ROUTE_PY            path of claude_issue_route.py
#                                    (default scripts/claude_issue_route.py)
#   RUN_URL                          URL of the calling run (for comments / alerts)
#   RUNTIME_DIR                      scratch dir (default mktemp -d)

set -euo pipefail

log()
{
	echo "CLAUDE_ISSUE_HANDOFF $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/label_helpers.sh 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { return 0; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { return 0; }

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"
ISSUE_NUMBER="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
ISSUE_META_FILE="${ISSUE_META_FILE:?ISSUE_META_FILE required}"
TRIGGER="${CLAUDE_ISSUE_TRIGGER:-opened}"
ROUTE_REASON="${CLAUDE_ISSUE_ROUTE_REASON:-default}"
SKIP_SECURITY_PASS="${CLAUDE_ISSUE_SKIP_SECURITY_PASS:-false}"
UPSTREAM_REPO="${CLAUDE_ISSUE_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
ROUTE_PY="${CLAUDE_ISSUE_ROUTE_PY:-scripts/claude_issue_route.py}"
RUN_URL="${RUN_URL:-}"
RUNTIME_DIR="${RUNTIME_DIR:-$(mktemp -d)}"
mkdir -p "${RUNTIME_DIR}"
ISSUE_URL="https://github.com/${REPO}/issues/${ISSUE_NUMBER}"

# --- 1. Claim the issue ------------------------------------------------------

ensure_label_exists "ai:claude" "${REPO}" || true
if ! gh_retry gh api -X POST "repos/${REPO}/issues/${ISSUE_NUMBER}/labels" -f 'labels[]=ai:claude' >/dev/null 2>&1; then
	log "warn claim_label_failed issue=${ISSUE_NUMBER}"
fi
# Label removals go one REST call each so a label the repo never created
# (404) cannot abort the others, which `gh issue edit --remove-label` would.
STALE_LABELS=("ai:claude-handoff-failed")
if [ "${TRIGGER}" = "reclarify" ]; then
	STALE_LABELS+=("ai:clarification" "ai:planning" "ai:awaiting-approval" "ai:blocked")
fi
ISSUE_LABEL_NAMES="$(jq -r '[(.labels // [])[]? | (.name // .)] | .[]' "${ISSUE_META_FILE}" 2>/dev/null || true)"
for stale_label in "${STALE_LABELS[@]}"; do
	if printf '%s\n' "${ISSUE_LABEL_NAMES}" | grep -qxF "${stale_label}"; then
		gh api -X DELETE "repos/${REPO}/issues/${ISSUE_NUMBER}/labels/$(jq -rn --arg l "${stale_label}" '$l|@uri')" >/dev/null 2>&1 || true
	fi
done
log "claimed issue=${ISSUE_NUMBER} trigger=${TRIGGER} reason=${ROUTE_REASON}"

# --- 2. Dispatch to coding-workflows ------------------------------------------

DISPATCH_FILE="${RUNTIME_DIR}/claude_issue_dispatch.json"
DISPATCH_ERROR_FILE="${RUNTIME_DIR}/claude_issue_dispatch_error.txt"
DISPATCH_OK="false"
if python3 "${ROUTE_PY}" build-dispatch \
	--repo "${REPO}" \
	--issue-json "${ISSUE_META_FILE}" \
	--trigger "${TRIGGER}" \
	--reporter-run-url "${RUN_URL}" \
	--skip-security-pass "${SKIP_SECURITY_PASS}" \
	> "${DISPATCH_FILE}" 2> "${DISPATCH_ERROR_FILE}"; then
	if gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2> "${DISPATCH_ERROR_FILE}"; then
		DISPATCH_OK="true"
	fi
fi

# --- 3. Comment -----------------------------------------------------------------

if [ "${DISPATCH_OK}" = "true" ]; then
	log "dispatched issue=${ISSUE_NUMBER} trigger=${TRIGGER} upstream=${UPSTREAM_REPO} skip_security_pass=${SKIP_SECURITY_PASS}"
	BODY="<!-- ai:claude-issue-routed:v1 -->
🤖 **Routed to Claude Code** (\`${ROUTE_REASON}\`, trigger \`${TRIGGER}\`).

A Claude session will implement this issue with the \`/implement-issue-claude\` flow: a single-phase plan and PR, then conformance, security, validation, and activation checks. This issue closes when the completion PR merges.

- To hand this issue to the Codex pipeline instead, add the \`ai:codex\` label and comment \`/reclarify\`.
- To send every new issue in this repo to Codex, set the repository variable \`AI_ISSUE_IMPLEMENTER=codex\`."
	[ -z "${RUN_URL}" ] || BODY+=$'\n\n'"Run: ${RUN_URL}"
	gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || \
		log "warn routing_comment_failed issue=${ISSUE_NUMBER}"
	tg_send_msg "Claude issue handoff dispatched for ${REPO}#${ISSUE_NUMBER} (${TRIGGER})."$'\n'"Issue: ${ISSUE_URL}" "DEBUG" >/dev/null 2>&1 || true
	exit 0
fi

DETAIL="$(head -c 300 "${DISPATCH_ERROR_FILE}" 2>/dev/null | tr '\n' ' ' || true)"
log "error dispatch_failed issue=${ISSUE_NUMBER} trigger=${TRIGGER} upstream=${UPSTREAM_REPO} detail=${DETAIL}"
echo "::error::Claude issue handoff failed for #${ISSUE_NUMBER}: ${DETAIL}"
ensure_label_exists "ai:claude-handoff-failed" "${REPO}" || true
gh_retry gh api -X POST "repos/${REPO}/issues/${ISSUE_NUMBER}/labels" -f 'labels[]=ai:claude-handoff-failed' >/dev/null 2>&1 || true
BODY="<!-- ai:claude-handoff-failed:v1 -->
⚠️ **Claude handoff failed** — the \`claude-issue\` dispatch to \`${UPSTREAM_REPO}\` was rejected, so no Claude session was started.

- Retry: comment \`/reclarify\`.
- Switch this issue to the Codex pipeline: add the \`ai:codex\` label, then comment \`/reclarify\`."
[ -z "${RUN_URL}" ] || BODY+=$'\n\n'"Run: ${RUN_URL}"
gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || true
tg_send_msg "Claude issue handoff FAILED for ${REPO}#${ISSUE_NUMBER} (${TRIGGER}): dispatch to ${UPSTREAM_REPO} rejected."$'\n'"Issue: ${ISSUE_URL}"$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
exit 0
