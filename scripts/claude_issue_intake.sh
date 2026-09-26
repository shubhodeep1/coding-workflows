#!/usr/bin/env bash
#
# claude_issue_intake.sh
#
# Driven by .github/workflows/claude-issue-intake.yml in coding-workflows. It
# turns one `claude-issue` payload into one queued item for the Claude issue
# pickup session:
#
#   1. Validates the payload with scripts/claude_issue_route.py: schema, repo
#      slug, issue number, trigger, and that the repo is registered in
#      .github/ai/consumer_repos.json (or is this repo).
#   2. Opens one `ai:claude-issue-queue` issue in this repo with the fixed-key
#      payload (no issue prose), using the workflow's GITHUB_TOKEN so no
#      workflow reacts to it. An open queue issue for the same target issue is
#      reused, never duplicated.
#   3. Comments on the target issue that it is queued.
#
# The pickup session (.claude/commands/claude-issue-pickup.md) starts the Opus
# `/implement-issue-claude` session for each queue issue with create_session
# and closes it. The intake used to fire the "Claude issue dispatcher"
# routine instead, but a routine run gets no claude-code-remote tools and so
# cannot start that session (issue #4525). CLAUDE_ISSUE_ROUTINE_ID and
# CLAUDE_ISSUE_ROUTINE_TOKEN are still accepted and ignored (deprecated).
#
# Every failure after the payload names a well-formed repo + issue marks that
# issue `ai:claude-handoff-failed`, comments how to retry or switch to Codex,
# sends a Telegram ERROR, and exits 1 so the run is visibly red. All stable
# log lines are prefixed CLAUDE_ISSUE_INTAKE.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY             this repo (coding-workflows)
#   GH_TOKEN                      GH_PAT: comments / labels on the target issue
#   CLAUDE_ISSUE_PAYLOAD_FILE     claude_issue.v1 payload JSON
#   CLAUDE_ISSUE_QUEUE_TOKEN      the workflow's GITHUB_TOKEN (issues: write on this repo)
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_REGISTRY         default .github/ai/consumer_repos.json
#   CLAUDE_ISSUE_ROUTE_PY         default scripts/claude_issue_route.py
#   CLAUDE_ISSUE_ROUTINE_ID       deprecated, ignored (logged when set)
#   RUN_URL, RUNTIME_DIR

set -euo pipefail

log()
{
	echo "CLAUDE_ISSUE_INTAKE $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/label_helpers.sh 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { return 0; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { return 0; }

PAYLOAD_FILE="${CLAUDE_ISSUE_PAYLOAD_FILE:?CLAUDE_ISSUE_PAYLOAD_FILE required}"
SELF_REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"
QUEUE_TOKEN="${CLAUDE_ISSUE_QUEUE_TOKEN:-}"
REGISTRY="${CLAUDE_ISSUE_REGISTRY:-.github/ai/consumer_repos.json}"
ROUTE_PY="${CLAUDE_ISSUE_ROUTE_PY:-scripts/claude_issue_route.py}"
RUN_URL="${RUN_URL:-}"
RUNTIME_DIR="${RUNTIME_DIR:-$(mktemp -d)}"
mkdir -p "${RUNTIME_DIR}"

# Best-effort target for failure reporting, read before validation so an
# unregistered-but-well-formed repo still hears why nothing happened.
RAW_REPO="$(jq -r '(.client_payload // .).repo // empty' "${PAYLOAD_FILE}" 2>/dev/null || true)"
RAW_ISSUE="$(jq -r '(.client_payload // .).issue_number // empty' "${PAYLOAD_FILE}" 2>/dev/null || true)"

fail()
{
	local reason="$1"
	local detail="$2"
	log "error reason=${reason} repo=${RAW_REPO:-none} issue=${RAW_ISSUE:-none} detail=${detail}"
	echo "::error::Claude issue intake failed (${reason}) for ${RAW_REPO:-?}#${RAW_ISSUE:-?}: ${detail}"
	if [[ "${RAW_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && [[ "${RAW_ISSUE}" =~ ^[1-9][0-9]*$ ]]; then
		ensure_label_exists "ai:claude-handoff-failed" "${RAW_REPO}" || true
		gh_retry gh api -X POST "repos/${RAW_REPO}/issues/${RAW_ISSUE}/labels" -f 'labels[]=ai:claude-handoff-failed' >/dev/null 2>&1 || true
		local body="<!-- ai:claude-handoff-failed:v1 -->
⚠️ **Claude handoff failed** in \`${SELF_REPO}\` (\`${reason}\`): ${detail}

No Claude session was started for this issue.
- Retry: comment \`/reclarify\`.
- Switch this issue to the Codex pipeline: add the \`ai:codex\` label, then comment \`/reclarify\`."
		[ -z "${RUN_URL}" ] || body+=$'\n\n'"Intake run: ${RUN_URL}"
		gh_retry gh api "repos/${RAW_REPO}/issues/${RAW_ISSUE}/comments" -f body="${body}" >/dev/null 2>&1 || true
	fi
	tg_send_msg "Claude issue intake FAILED (${reason}) for ${RAW_REPO:-?}#${RAW_ISSUE:-?}: ${detail}"$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
	exit 1
}

# --- 1. Validate ----------------------------------------------------------------

VALIDATED_FILE="${RUNTIME_DIR}/validated.json"
if ! python3 "${ROUTE_PY}" validate-payload \
	--payload-json "${PAYLOAD_FILE}" \
	--registry "${REGISTRY}" \
	--self-repo "${SELF_REPO}" \
	> "${VALIDATED_FILE}" 2> "${RUNTIME_DIR}/validate_error.txt"; then
	fail "invalid_payload" "$(head -c 300 "${RUNTIME_DIR}/validate_error.txt" | tr '\n' ' ')"
fi
REPO="$(jq -r '.repo' "${VALIDATED_FILE}")"
ISSUE_NUMBER="$(jq -r '.issue_number' "${VALIDATED_FILE}")"
TRIGGER="$(jq -r '.trigger' "${VALIDATED_FILE}")"
RAW_REPO="${REPO}"
RAW_ISSUE="${ISSUE_NUMBER}"
log "validated repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER}"

# --- 2. Queue for the pickup session ----------------------------------------------------

if [ -n "${CLAUDE_ISSUE_ROUTINE_ID:-}" ]; then
	log "notice routine_deprecated detail=CLAUDE_ISSUE_ROUTINE_ID is set but no longer used; the pickup session starts implementation sessions"
fi
if [ -z "${QUEUE_TOKEN}" ]; then
	fail "queue_not_configured" "CLAUDE_ISSUE_QUEUE_TOKEN (the workflow GITHUB_TOKEN) is not set"
fi

QUEUE_FILE="${RUNTIME_DIR}/queue_issue.json"
python3 "${ROUTE_PY}" queue-issue --validated-json "${VALIDATED_FILE}" --run-url "${RUN_URL}" > "${QUEUE_FILE}"
QUEUE_TITLE="$(jq -r '.title' "${QUEUE_FILE}")"
QUEUE_LABEL="$(jq -r '.label' "${QUEUE_FILE}")"

# One read of the open queue (≤ 100 items; the pickup drains it hourly) to
# reuse an open item for the same target issue instead of duplicating it.
OPEN_QUEUE_FILE="${RUNTIME_DIR}/open_queue.json"
if ! GH_TOKEN="${QUEUE_TOKEN}" gh_retry gh api "repos/${SELF_REPO}/issues?labels=${QUEUE_LABEL}&state=open&per_page=100" > "${OPEN_QUEUE_FILE}" 2> "${RUNTIME_DIR}/queue_read_error.txt"; then
	fail "queue_failed" "could not read the open queue: $(head -c 200 "${RUNTIME_DIR}/queue_read_error.txt" | tr '\n' ' ')"
fi
QUEUE_NUMBER="$(jq -r --arg t "${QUEUE_TITLE}" '[.[]? | select(.title == $t and (.user.login // "") == "github-actions[bot]") | .number] | first // empty' "${OPEN_QUEUE_FILE}" 2>/dev/null || true)"

if [ -n "${QUEUE_NUMBER}" ]; then
	log "already_queued repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER} queue_issue=${QUEUE_NUMBER}"
else
	GH_TOKEN="${QUEUE_TOKEN}" ensure_label_exists "${QUEUE_LABEL}" "${SELF_REPO}" || true
	if ! QUEUE_NUMBER="$(GH_TOKEN="${QUEUE_TOKEN}" gh_retry gh api "repos/${SELF_REPO}/issues" \
		-f title="${QUEUE_TITLE}" \
		-f body="$(jq -r '.body' "${QUEUE_FILE}")" \
		-f "labels[]=${QUEUE_LABEL}" \
		--jq '.number' 2> "${RUNTIME_DIR}/queue_create_error.txt")" || ! [[ "${QUEUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then
		fail "queue_failed" "could not open the queue issue: $(head -c 200 "${RUNTIME_DIR}/queue_create_error.txt" | tr '\n' ' ')"
	fi
	log "queued repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER} queue_issue=${QUEUE_NUMBER}"
fi
QUEUE_URL="https://github.com/${SELF_REPO}/issues/${QUEUE_NUMBER}"

# --- 3. Record on the issue -----------------------------------------------------------

BODY="<!-- ai:claude-issue-dispatched:v1 -->
🤖 Queued for the Claude issue pickup (trigger \`${TRIGGER}\`). Within about an hour it starts the Claude session that implements this issue in \`${REPO}\`, and that session posts its progress here.

Queue item: ${QUEUE_URL}"
[ -z "${RUN_URL}" ] || BODY+=$'\n'"Intake run: ${RUN_URL}"
gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || \
	log "warn dispatched_comment_failed repo=${REPO} issue=${ISSUE_NUMBER}"
tg_send_msg "Claude issue queued for ${REPO}#${ISSUE_NUMBER} (${TRIGGER})."$'\n'"Queue item: ${QUEUE_URL}" "DEBUG" >/dev/null 2>&1 || true
exit 0
