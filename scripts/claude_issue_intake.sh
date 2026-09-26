#!/usr/bin/env bash
#
# claude_issue_intake.sh
#
# Driven by .github/workflows/claude-issue-intake.yml in coding-workflows. It
# turns one `claude-issue` payload into one run of the "Claude issue
# dispatcher" routine on claude.ai:
#
#   1. Validates the payload with scripts/claude_issue_route.py: schema, repo
#      slug, issue number, trigger, and that the repo is registered in
#      .github/ai/consumer_repos.json (or is this repo).
#   2. POSTs the routine's /fire endpoint with fixed-key fire text (repo, issue,
#      url, trigger, skip_security_pass). No issue prose is forwarded.
#   3. Comments the started dispatcher session URL on the issue.
#
# Every failure after the payload names a well-formed repo + issue marks that
# issue `ai:claude-handoff-failed`, comments how to retry or switch to Codex,
# sends a Telegram ERROR, and exits 1 so the run is visibly red. All stable
# log lines are prefixed CLAUDE_ISSUE_INTAKE. The routine token is only ever
# read from the environment and written to a 0600 header file; it is never
# echoed.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY             this repo (coding-workflows)
#   GH_TOKEN                      GH_PAT: comments / labels on the target issue
#   CLAUDE_ISSUE_PAYLOAD_FILE     claude_issue.v1 payload JSON
#   CLAUDE_ISSUE_ROUTINE_ID       routine trigger id (trig_…)
#   CLAUDE_ISSUE_ROUTINE_TOKEN    routine API token
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_ROUTINE_BETA     anthropic-beta header (default experimental-cc-routine-2026-04-01)
#   CLAUDE_ISSUE_FIRE_URL_BASE    default https://api.anthropic.com/v1/claude_code/routines
#   CLAUDE_ISSUE_REGISTRY         default .github/ai/consumer_repos.json
#   CLAUDE_ISSUE_ROUTE_PY         default scripts/claude_issue_route.py
#   CLAUDE_ISSUE_RETRY_DELAYS     space-separated backoff seconds (default "2 4 8 16")
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
ROUTINE_ID="${CLAUDE_ISSUE_ROUTINE_ID:-}"
ROUTINE_BETA="${CLAUDE_ISSUE_ROUTINE_BETA:-experimental-cc-routine-2026-04-01}"
FIRE_URL_BASE="${CLAUDE_ISSUE_FIRE_URL_BASE:-https://api.anthropic.com/v1/claude_code/routines}"
REGISTRY="${CLAUDE_ISSUE_REGISTRY:-.github/ai/consumer_repos.json}"
ROUTE_PY="${CLAUDE_ISSUE_ROUTE_PY:-scripts/claude_issue_route.py}"
RETRY_DELAYS="${CLAUDE_ISSUE_RETRY_DELAYS:-2 4 8 16}"
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

# --- 2. Fire the routine ------------------------------------------------------------

if ! [[ "${ROUTINE_ID}" =~ ^trig_[A-Za-z0-9]+$ ]]; then
	fail "routine_not_configured" "repository variable CLAUDE_ISSUE_ROUTINE_ID is missing or not a trig_… id"
fi
if [ -z "${CLAUDE_ISSUE_ROUTINE_TOKEN:-}" ]; then
	fail "routine_not_configured" "secret CLAUDE_ISSUE_ROUTINE_TOKEN is not set"
fi

FIRE_BODY_FILE="${RUNTIME_DIR}/fire_body.json"
python3 "${ROUTE_PY}" fire-body --validated-json "${VALIDATED_FILE}" > "${FIRE_BODY_FILE}"

HEADER_FILE="${RUNTIME_DIR}/fire_headers.txt"
(
	umask 077
	{
		printf 'Authorization: Bearer %s\n' "${CLAUDE_ISSUE_ROUTINE_TOKEN}"
		printf 'anthropic-beta: %s\n' "${ROUTINE_BETA}"
		printf 'anthropic-version: 2023-06-01\n'
		printf 'Content-Type: application/json\n'
	} > "${HEADER_FILE}"
)
trap 'rm -f "${HEADER_FILE}"' EXIT

FIRE_URL="${FIRE_URL_BASE}/${ROUTINE_ID}/fire"
RESPONSE_FILE="${RUNTIME_DIR}/fire_response.json"
HTTP_CODE="000"
attempt=0
for delay in "" ${RETRY_DELAYS}; do
	if [ -n "${delay}" ]; then
		log "retry attempt=$((attempt + 1)) after_seconds=${delay} last_http=${HTTP_CODE}"
		sleep "${delay}"
	fi
	attempt=$((attempt + 1))
	HTTP_CODE="$(curl -sS -o "${RESPONSE_FILE}" -w '%{http_code}' -X POST \
		-H @"${HEADER_FILE}" \
		--data-binary @"${FIRE_BODY_FILE}" \
		"${FIRE_URL}" 2> "${RUNTIME_DIR}/curl_error.txt" || echo "000")"
	case "${HTTP_CODE}" in
		2??) break ;;
		000|408|429|5??) continue ;;
		*) break ;;
	esac
done
rm -f "${HEADER_FILE}"

case "${HTTP_CODE}" in
	2??) ;;
	*)
		DETAIL="HTTP ${HTTP_CODE} after ${attempt} attempt(s): $(head -c 200 "${RESPONSE_FILE}" 2>/dev/null | tr '\n' ' ') $(head -c 100 "${RUNTIME_DIR}/curl_error.txt" 2>/dev/null | tr '\n' ' ')"
		fail "fire_failed" "${DETAIL}"
		;;
esac

SESSION_URL="$(jq -r '.claude_code_session_url // empty' "${RESPONSE_FILE}" 2>/dev/null || true)"
SESSION_ID="$(jq -r '.claude_code_session_id // empty' "${RESPONSE_FILE}" 2>/dev/null || true)"
log "fired repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER} http=${HTTP_CODE} session=${SESSION_ID:-unknown}"

# --- 3. Record on the issue -----------------------------------------------------------

BODY="<!-- ai:claude-issue-dispatched:v1 -->
🤖 Claude issue dispatcher started (trigger \`${TRIGGER}\`). It opens the implementation session for this issue in \`${REPO}\`."
[ -z "${SESSION_URL}" ] || BODY+=$'\n\n'"Dispatcher session: ${SESSION_URL}"
[ -z "${RUN_URL}" ] || BODY+=$'\n'"Intake run: ${RUN_URL}"
gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || \
	log "warn dispatched_comment_failed repo=${REPO} issue=${ISSUE_NUMBER}"
tg_send_msg "Claude issue dispatcher fired for ${REPO}#${ISSUE_NUMBER} (${TRIGGER})."$'\n'"Session: ${SESSION_URL:-unknown}" "DEBUG" >/dev/null 2>&1 || true
exit 0
