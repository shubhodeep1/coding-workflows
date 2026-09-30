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
#   1b. Authorizes it against live GitHub data (issue #4620, `authorize-target`):
#      every dispatcher login of this run has admin or write on the target
#      repo, the target is an open issue (not a pull request) in that repo,
#      and its author passes clarify's gate (OWNER / MEMBER / COLLABORATOR
#      User or github-actions[bot]) or a trusted User commented `/reclarify`.
#      A refusal writes nothing to the target issue (see reject()).
#   2. Opens one `ai:claude-issue-queue` issue in this repo with the fixed-key
#      payload (no issue prose), using the workflow's GITHUB_TOKEN so no
#      workflow reacts to it. An open queue issue for the same target issue is
#      reused, never duplicated: its body is rewritten with the fresh payload
#      and this run's URL.
#   3. Records the queue issue (number, title, payload) in this run's binding
#      file, which the workflow uploads as the `claude-issue-queue-binding`
#      artifact. The pickup starts nothing an artifact of the run named in
#      the item's `Intake run:` line does not list unchanged (issue #4621),
#      so an edited queue issue is refused.
#   4. Comments on the target issue that it is queued.
#
# The pickup session (.claude/commands/claude-issue-pickup.md) starts the Opus
# `/implement-issue-claude` session for each queue issue with create_session
# and closes it. The intake used to fire the "Claude issue dispatcher"
# routine instead, but a routine run gets no claude-code-remote tools and so
# cannot start that session (issue #4525). CLAUDE_ISSUE_ROUTINE_ID and
# CLAUDE_ISSUE_ROUTINE_TOKEN are still accepted and ignored (deprecated).
#
# An invalid payload or a refused authorization writes nothing to the issue
# the payload names (reject()). Every failure after authorization (queue not
# configured, queue read or write failed) marks the target issue
# `ai:claude-handoff-failed`, comments how to retry or switch to Codex (fail()).
# Both send a Telegram ERROR and exit 1 so the run is visibly red. All stable
# log lines are prefixed CLAUDE_ISSUE_INTAKE.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY             this repo (coding-workflows)
#   GH_TOKEN                      GH_PAT: comments / labels on the target issue
#   CLAUDE_ISSUE_PAYLOAD_FILE     claude_issue.v1 payload JSON
#   CLAUDE_ISSUE_QUEUE_TOKEN      the workflow's GITHUB_TOKEN (issues: write on this repo)
#   CLAUDE_ISSUE_DISPATCHER       github.actor of the run (default empty: the
#                                 dispatch is refused as dispatcher_unknown)
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_TRIGGERING_ACTOR github.triggering_actor (default empty; checked
#                                 too when it differs from the dispatcher)
#   CLAUDE_ISSUE_REGISTRY        default .github/ai/consumer_repos.json
#   CLAUDE_ISSUE_ROUTE_PY         default scripts/claude_issue_route.py
#   CLAUDE_ISSUE_ROUTINE_ID       deprecated, ignored (logged when set)
#   CLAUDE_ISSUE_QUEUE_BINDING_FILE  default ${RUNTIME_DIR}/claude-issue-queue-binding/claude_issue_queue_binding.json
#   GITHUB_RUN_ID                 set by Actions; without it no binding is written
#   RUN_URL, RUNTIME_DIR

set -euo pipefail

log()
{
	echo "CLAUDE_ISSUE_INTAKE $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
type gh_retry_to_file >/dev/null 2>&1 || gh_retry_to_file() { local out="$1"; shift; "$@" > "${out}"; }
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
BINDING_FILE="${CLAUDE_ISSUE_QUEUE_BINDING_FILE:-${RUNTIME_DIR}/claude-issue-queue-binding/claude_issue_queue_binding.json}"
RUN_ID="${GITHUB_RUN_ID:-}"

# Best-effort target for the log and alert lines, read before validation so a
# refused payload still names what it asked for. Nothing is written to it
# until step 1b has authorized it (see reject()).
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
	local fail_tg_msg="Claude issue intake FAILED (${reason}) for ${RAW_REPO:-?}#${RAW_ISSUE:-?}: ${detail}"
	# RUN_URL is empty on a hand-driven run; leave the line out rather than send a bare "Run: ".
	[ -z "${RUN_URL}" ] || fail_tg_msg+=$'\n'"Run: ${RUN_URL}"
	tg_send_msg "${fail_tg_msg}" "ERROR" >/dev/null 2>&1 || true
	exit 1
}

# Refuse a dispatch that failed authorization (issue #4620). Unlike fail(),
# nothing is written to the target issue: until authorization succeeds the
# target is unverified, so an unauthorized dispatcher must not be able to make
# GH_PAT label or comment on arbitrary issues. Read failures refuse the same
# way; the handoff's routing comment is already on a legitimate issue, and the
# red run plus the Telegram ERROR name the retry (`/reclarify`).
reject()
{
	local reason="$1"
	local detail="$2"
	log "rejected reason=${reason} repo=${RAW_REPO:-none} issue=${RAW_ISSUE:-none} detail=${detail}"
	echo "::error::Claude issue intake refused ${RAW_REPO:-?}#${RAW_ISSUE:-?} (${reason}): ${detail}"
	local reject_tg_msg="Claude issue intake REFUSED (${reason}) for ${RAW_REPO:-?}#${RAW_ISSUE:-?}: ${detail}"$'\n'"No session was queued and nothing was written to the issue. Retry a legitimate issue with /reclarify."
	# RUN_URL is empty on a hand-driven run; leave the line out rather than send a bare "Run: ".
	[ -z "${RUN_URL}" ] || reject_tg_msg+=$'\n'"Run: ${RUN_URL}"
	tg_send_msg "${reject_tg_msg}" "ERROR" >/dev/null 2>&1 || true
	exit 1
}

# The last 300 bytes of an error file on one line, cut to whole UTF-8
# characters (Telegram refuses a message that is not valid UTF-8).
# gh_retry_to_file writes its retry warnings first and the final attempt's
# error last, so the head of the file can hide the error that decided.
error_tail()
{
	python3 -c 'import sys; data = open(sys.argv[1], "rb").read()[-300:]; sys.stdout.write(" ".join(data.decode("utf-8", "ignore").split()))' "$1" 2>/dev/null || true
}

# --- 1. Validate ----------------------------------------------------------------

VALIDATED_FILE="${RUNTIME_DIR}/validated.json"
if ! python3 "${ROUTE_PY}" validate-payload \
	--payload-json "${PAYLOAD_FILE}" \
	--registry "${REGISTRY}" \
	--self-repo "${SELF_REPO}" \
	> "${VALIDATED_FILE}" 2> "${RUNTIME_DIR}/validate_error.txt"; then
	# reject(), not fail(): the payload's repo + issue are unverified until
	# step 1b, so an invalid payload must not make GH_PAT label or comment on
	# whatever issue it names (an unregistered repo included).
	reject "invalid_payload" "$(head -c 300 "${RUNTIME_DIR}/validate_error.txt" | tr '\n' ' ')"
fi
REPO="$(jq -r '.repo' "${VALIDATED_FILE}")"
ISSUE_NUMBER="$(jq -r '.issue_number' "${VALIDATED_FILE}")"
TRIGGER="$(jq -r '.trigger' "${VALIDATED_FILE}")"
RAW_REPO="${REPO}"
RAW_ISSUE="${ISSUE_NUMBER}"
log "validated repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER}"

# --- 1b. Authorize the target and the dispatcher (issue #4620) --------------------
#
# A registered repo is not enough: whoever can send a `claude-issue` dispatch
# or run this workflow could otherwise name any issue in any registered repo
# and skip clarify's author gate. The dispatcher logins come from GitHub
# (`github.actor` / `github.triggering_actor`), never from the payload.
#
# §15: the only other reads in this script are the open-queue list below,
# which answers none of these questions. New calls: one collaborator
# permission read per distinct dispatcher login, one issue read, and the
# issue's comments only when its author is not trusted.

DISPATCHER_LOGINS=()
for dispatcher_login in "${CLAUDE_ISSUE_DISPATCHER:-}" "${CLAUDE_ISSUE_TRIGGERING_ACTOR:-}"; do
	[ -n "${dispatcher_login}" ] || continue
	if [ "${#DISPATCHER_LOGINS[@]}" -gt 0 ] && printf '%s\n' "${DISPATCHER_LOGINS[@]}" | grep -qxF "${dispatcher_login}"; then
		continue
	fi
	DISPATCHER_LOGINS+=("${dispatcher_login}")
done

PERMISSIONS_FILE="${RUNTIME_DIR}/dispatcher_permissions.json"
printf '{}' > "${PERMISSIONS_FILE}"
if [ "${#DISPATCHER_LOGINS[@]}" -eq 0 ]; then
	reject "dispatcher_unknown" "the run reports no dispatcher login (CLAUDE_ISSUE_DISPATCHER is empty)"
fi
for dispatcher_login in "${DISPATCHER_LOGINS[@]}"; do
	if ! [[ "${dispatcher_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
		reject "dispatcher_not_authorized" "dispatcher login ${dispatcher_login} is not a user login"
	fi
	if ! gh_retry_to_file "${RUNTIME_DIR}/permission.txt" gh api "repos/${REPO}/collaborators/${dispatcher_login}/permission" --jq '.permission' 2> "${RUNTIME_DIR}/permission_error.txt"; then
		reject "authorization_read_failed" "could not read ${dispatcher_login}'s permission on ${REPO}: $(error_tail "${RUNTIME_DIR}/permission_error.txt")"
	fi
	jq --arg login "${dispatcher_login}" --arg perm "$(tr -d '[:space:]' < "${RUNTIME_DIR}/permission.txt")" '. + {($login): $perm}' "${PERMISSIONS_FILE}" > "${PERMISSIONS_FILE}.tmp"
	mv "${PERMISSIONS_FILE}.tmp" "${PERMISSIONS_FILE}"
done

TARGET_ISSUE_FILE="${RUNTIME_DIR}/target_issue.json"
if ! gh_retry_to_file "${TARGET_ISSUE_FILE}" gh api "repos/${REPO}/issues/${ISSUE_NUMBER}" 2> "${RUNTIME_DIR}/target_issue_error.txt"; then
	reject "authorization_read_failed" "could not read ${REPO}#${ISSUE_NUMBER}: $(error_tail "${RUNTIME_DIR}/target_issue_error.txt")"
fi

AUTHORIZE_FILE="${RUNTIME_DIR}/authorize.json"
authorize_target_call()
{
	python3 "${ROUTE_PY}" authorize-target \
		--validated-json "${VALIDATED_FILE}" \
		--issue-json "${TARGET_ISSUE_FILE}" \
		--permissions-json "${PERMISSIONS_FILE}" \
		"$@" > "${AUTHORIZE_FILE}" 2> "${RUNTIME_DIR}/authorize_error.txt"
}
if ! authorize_target_call; then
	reject "authorization_read_failed" "authorize-target failed: $(error_tail "${RUNTIME_DIR}/authorize_error.txt")"
fi
if [ "$(jq -r '.needs_comments' "${AUTHORIZE_FILE}")" = "true" ]; then
	TARGET_COMMENTS_FILE="${RUNTIME_DIR}/target_comments.json"
	if ! gh_retry_to_file "${RUNTIME_DIR}/target_comments_pages.json" gh api --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100" 2> "${RUNTIME_DIR}/target_comments_error.txt" || \
		! jq -s 'add // []' "${RUNTIME_DIR}/target_comments_pages.json" > "${TARGET_COMMENTS_FILE}" 2>> "${RUNTIME_DIR}/target_comments_error.txt"; then
		reject "authorization_read_failed" "could not read the comments of ${REPO}#${ISSUE_NUMBER}: $(error_tail "${RUNTIME_DIR}/target_comments_error.txt")"
	fi
	if ! authorize_target_call --comments-json "${TARGET_COMMENTS_FILE}"; then
		reject "authorization_read_failed" "authorize-target failed: $(error_tail "${RUNTIME_DIR}/authorize_error.txt")"
	fi
fi
AUTHORIZE_REASON="$(jq -r '.reason' "${AUTHORIZE_FILE}")"
if [ "$(jq -r '.authorized' "${AUTHORIZE_FILE}")" != "true" ]; then
	reject "${AUTHORIZE_REASON}" "dispatcher(s) $(jq -c '.' "${PERMISSIONS_FILE}") may not start a Claude session for ${REPO}#${ISSUE_NUMBER}"
fi
log "authorized repo=${REPO} issue=${ISSUE_NUMBER} reason=${AUTHORIZE_REASON} dispatchers=${DISPATCHER_LOGINS[*]}"

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
# The body is read once and checked here: a `$(jq ...)` inside a gh argument
# is exempt from `set -e`, so a missing, empty, or NUL-carrying body (bash
# drops NUL bytes) would otherwise be written and then fail the pickup's
# binding check with no trace in this run (issue #4621).
if ! QUEUE_BODY="$(jq -er '.body | select(type == "string" and length > 0 and (contains("\u0000") | not))' "${QUEUE_FILE}" 2>/dev/null)"; then
	# Name the check that failed, so the operator can tell the cases apart.
	QUEUE_BODY_STATE="$(jq -r '.body | if type != "string" then "missing (\(type))" elif length == 0 then "empty" elif contains("\u0000") then "text with a NUL byte" else "unreadable" end' "${QUEUE_FILE}" 2>/dev/null || true)"
	fail "queue_failed" "the rendered queue issue body is ${QUEUE_BODY_STATE:-unreadable (queue-issue output is not JSON)}"
fi

# One read of the open queue (≤ 100 items; the pickup drains it hourly) to
# reuse an open item for the same target issue instead of duplicating it.
OPEN_QUEUE_FILE="${RUNTIME_DIR}/open_queue.json"
if ! GH_TOKEN="${QUEUE_TOKEN}" gh_retry gh api "repos/${SELF_REPO}/issues?labels=${QUEUE_LABEL}&state=open&per_page=100" > "${OPEN_QUEUE_FILE}" 2> "${RUNTIME_DIR}/queue_read_error.txt"; then
	fail "queue_failed" "could not read the open queue: $(head -c 200 "${RUNTIME_DIR}/queue_read_error.txt" | tr '\n' ' ')"
fi
QUEUE_NUMBER="$(jq -r --arg t "${QUEUE_TITLE}" '[.[]? | select(.title == $t and (.user.login // "") == "github-actions[bot]") | .number] | first // empty' "${OPEN_QUEUE_FILE}" 2>/dev/null || true)"

if [ -n "${QUEUE_NUMBER}" ]; then
	# Rewrite the reused item so its body is exactly what this run binds: an
	# item bound by an earlier run, never bound, or edited since is healed by
	# the next intake for its target (issue #4621).
	if ! GH_TOKEN="${QUEUE_TOKEN}" gh_retry gh api -X PATCH "repos/${SELF_REPO}/issues/${QUEUE_NUMBER}" \
		-f body="${QUEUE_BODY}" \
		--jq '.number' > /dev/null 2> "${RUNTIME_DIR}/queue_update_error.txt"; then
		fail "queue_failed" "could not rewrite queue issue #${QUEUE_NUMBER}: $(head -c 200 "${RUNTIME_DIR}/queue_update_error.txt" | tr '\n' ' ')"
	fi
	log "already_queued repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER} queue_issue=${QUEUE_NUMBER}"
else
	GH_TOKEN="${QUEUE_TOKEN}" ensure_label_exists "${QUEUE_LABEL}" "${SELF_REPO}" || true
	if ! QUEUE_NUMBER="$(GH_TOKEN="${QUEUE_TOKEN}" gh_retry gh api "repos/${SELF_REPO}/issues" \
		-f title="${QUEUE_TITLE}" \
		-f body="${QUEUE_BODY}" \
		-f "labels[]=${QUEUE_LABEL}" \
		--jq '.number' 2> "${RUNTIME_DIR}/queue_create_error.txt")" || ! [[ "${QUEUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then
		fail "queue_failed" "could not open the queue issue: $(head -c 200 "${RUNTIME_DIR}/queue_create_error.txt" | tr '\n' ' ')"
	fi
	log "queued repo=${REPO} issue=${ISSUE_NUMBER} trigger=${TRIGGER} queue_issue=${QUEUE_NUMBER}"
fi
QUEUE_URL="https://github.com/${SELF_REPO}/issues/${QUEUE_NUMBER}"

# --- 3. Bind the queue issue to this run (issue #4621) ---------------------------------

if [[ "${RUN_ID}" =~ ^[1-9][0-9]*$ ]]; then
	if ! python3 "${ROUTE_PY}" add-queue-binding \
		--binding-file "${BINDING_FILE}" \
		--repository "${SELF_REPO}" \
		--run-id "${RUN_ID}" \
		--queue-issue "${QUEUE_NUMBER}" \
		--queue-issue-json "${QUEUE_FILE}" \
		2> "${RUNTIME_DIR}/binding_error.txt"; then
		fail "queue_failed" "could not bind queue issue #${QUEUE_NUMBER} to this run: $(head -c 200 "${RUNTIME_DIR}/binding_error.txt" | tr '\n' ' ')"
	fi
	log "bound queue_issue=${QUEUE_NUMBER} run_id=${RUN_ID} binding_file=${BINDING_FILE}"
else
	log "warn binding_skipped queue_issue=${QUEUE_NUMBER} reason=no_run_id (the pickup refuses unbound items)"
fi

# --- 4. Record on the issue -----------------------------------------------------------

BODY="<!-- ai:claude-issue-dispatched:v1 -->
🤖 Queued for the Claude issue pickup (trigger \`${TRIGGER}\`). Within about an hour it starts the Claude session that implements this issue in \`${REPO}\`, and that session posts its progress here.

Queue item: ${QUEUE_URL}"
[ -z "${RUN_URL}" ] || BODY+=$'\n'"Intake run: ${RUN_URL}"
gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || \
	log "warn dispatched_comment_failed repo=${REPO} issue=${ISSUE_NUMBER}"
tg_send_msg "Claude issue queued for ${REPO}#${ISSUE_NUMBER} (${TRIGGER})."$'\n'"Queue item: ${QUEUE_URL}" "DEBUG" >/dev/null 2>&1 || true
exit 0
