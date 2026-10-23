#!/usr/bin/env bash
#
# workflow_failure_heal_queue.sh — first-come, first-served queue for the heal
# intake (.github/workflows/workflow-failure-heal-intake.yml).
#
# The intake used to share one workflow-level concurrency group. GitHub keeps
# one running and one pending run per group and cancels the pending run when a
# newer one is queued, so in a burst every report but the first and the last
# was lost before it was read (about 15 cancelled intake runs on 2026-10-21).
# The group is now per run, and the dedup / lineage / budget decisions are
# serialised here instead, by waiting rather than by cancelling:
#
#   acquire  Find the open `ai:heal-intake-queue` issue written by the pipeline
#            login (create it when there is none), post this run's ticket
#            comment, and wait until no live ticket is older than it
#            (`workflow_failure_heal.py heal-queue position`). Stale tickets
#            ahead (lease expired, run completed or re-attempted, an earlier
#            ticket of this run) are deleted. The oldest live ticket's run is
#            read at most once per HEAL_INTAKE_QUEUE_HOLDER_CHECK_SECONDS.
#            Exports HEAL_INTAKE_QUEUE_ISSUE, HEAL_INTAKE_QUEUE_TICKET_ID and
#            HEAL_INTAKE_QUEUE_WAITED_SECONDS through GITHUB_ENV; the intake
#            adds the wait to WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS so a report
#            is not rejected as stale for the time it spent queued.
#   release  Delete this run's ticket (run it with `if: always()`).
#
# Never drops a report: when the queue cannot be used (no login, issue or
# ticket, unreadable comments, or HEAL_INTAKE_QUEUE_MAX_WAIT_SECONDS passed)
# the run proceeds without the queue (`outcome=unlocked reason=<r>`, one
# WARNING). The worst case is then two intakes deciding at once, which can at
# most duplicate a heal issue, instead of losing the report. Every exit is 0.
#
# Env (all optional):
#   HEAL_INTAKE_QUEUE_ENABLED              "false" (any letter case) turns the queue off;
#                                          the workflow then restores the shared
#                                          concurrency group (default true)
#   HEAL_INTAKE_QUEUE_JOB                  intake | recovery-scan (default intake)
#   HEAL_INTAKE_QUEUE_POLL_SECONDS         seconds between queue reads (default 20)
#   HEAL_INTAKE_QUEUE_MAX_WAIT_SECONDS     wait before proceeding unlocked (default 4800)
#   HEAL_INTAKE_QUEUE_LEASE_SECONDS        age after which a ticket no longer counts (default 10800)
#   HEAL_INTAKE_QUEUE_HOLDER_CHECK_SECONDS min seconds between reads of one holder's run (default 300)
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_ENV, RUNNER_TEMP, GH_TOKEN
#   WORKFLOW_HEAL_PY                       helper (default: beside this script)
#
# GitHub API calls (CLAUDE.md §15). No earlier step of either job reads the
# login, the queue issue or its comments, so none can be reused. acquire: one
# `user` read, one open-issue list, one ticket post, then one comment list per
# poll (tickets are deleted after use, so the list stays one page), plus one
# run read per holder per HOLDER_CHECK window and one delete per stale ticket.
# The queue issue is created (with its label, a re-list and, on a lost create
# race, a close) only when none is open. release: one delete. A run that finds
# the queue empty costs five calls in all.
#
# Log lines are prefixed WORKFLOW_HEAL_QUEUE.

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_QUEUE $*"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
# shellcheck disable=SC1091
source "${script_dir}/tg_helpers.sh" 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }
# shellcheck disable=SC1091
source "${script_dir}/label_helpers.sh" 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { :; }
export PYTHONDONTWRITEBYTECODE=1

MODE="${1:-}"
SELF_REPO="${GITHUB_REPOSITORY:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
[[ "${RUN_ATTEMPT}" =~ ^[1-9][0-9]{0,3}$ ]] || RUN_ATTEMPT=1
JOB="${HEAL_INTAKE_QUEUE_JOB:-intake}"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"
QUEUE_LABEL="ai:heal-intake-queue"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${SELF_REPO}/actions/runs/${RUN_ID}"

_seconds_or_default()
{
	local value="${1:-}" fallback="$2"
	case "${value}" in
		''|*[!0-9]*) printf '%s' "${fallback}" ;;
		*) printf '%s' "$((10#${value}))" ;;
	esac
}
POLL_SECONDS="$(_seconds_or_default "${HEAL_INTAKE_QUEUE_POLL_SECONDS:-}" 20)"
MAX_WAIT_SECONDS="$(_seconds_or_default "${HEAL_INTAKE_QUEUE_MAX_WAIT_SECONDS:-}" 4800)"
LEASE_SECONDS="$(_seconds_or_default "${HEAL_INTAKE_QUEUE_LEASE_SECONDS:-}" 10800)"
[ "${LEASE_SECONDS}" -gt 0 ] || LEASE_SECONDS=10800
HOLDER_CHECK_SECONDS="$(_seconds_or_default "${HEAL_INTAKE_QUEUE_HOLDER_CHECK_SECONDS:-}" 300)"

_env_out()
{
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >> "${GITHUB_ENV}"
	fi
}

ENABLED="${HEAL_INTAKE_QUEUE_ENABLED:-true}"
if [ "${ENABLED,,}" = "false" ]; then
	log "outcome=skip reason=disabled mode=${MODE:-none} job=${JOB}"
	exit 0
fi

case "${MODE}" in
	acquire|release) ;;
	*)
		log "outcome=skip reason=invalid_mode"
		exit 0
		;;
esac

# --- release -------------------------------------------------------------------

if [ "${MODE}" = "release" ]; then
	TICKET_ID="${HEAL_INTAKE_QUEUE_TICKET_ID:-}"
	if ! [[ "${TICKET_ID}" =~ ^[1-9][0-9]{0,19}$ ]] || ! [[ "${SELF_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		log "outcome=skip reason=no_ticket job=${JOB} run=${RUN_ID:-none}"
		exit 0
	fi
	if GH_RETRY_IDEMPOTENT=true gh_retry gh api --method DELETE "repos/${SELF_REPO}/issues/comments/${TICKET_ID}" >/dev/null 2>&1; then
		log "outcome=released ticket=${TICKET_ID} job=${JOB} run=${RUN_ID}"
	else
		# The next waiter reads this run as completed and skips the ticket.
		log "outcome=release_failed ticket=${TICKET_ID} job=${JOB} run=${RUN_ID}"
	fi
	exit 0
fi

# --- acquire -------------------------------------------------------------------

START_EPOCH="$(date +%s)"
WORK_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/heal-intake-queue-XXXXXXXX" 2>/dev/null || mktemp -d)"
QUEUE_ISSUE=""
TICKET_ID=""

_waited()
{
	echo "$(( $(date +%s) - START_EPOCH ))"
}

# Proceed without the queue; never drop the report.
_unlocked()
{
	local reason="$1" waited
	waited="$(_waited)"
	_env_out HEAL_INTAKE_QUEUE_WAITED_SECONDS "${waited}"
	log "outcome=unlocked reason=${reason} job=${JOB} run=${RUN_ID:-none} issue=${QUEUE_ISSUE:-none} ticket=${TICKET_ID:-none} waited_seconds=${waited}"
	echo "::warning::Heal intake queue unavailable (${reason}); this run proceeds without waiting for its turn."
	tg_send_msg "Heal intake queue unavailable (${reason}) for ${JOB} run ${RUN_ID:-unknown}; it proceeds without waiting for its turn, so two intakes may decide at once."$'\n'"Run: ${RUN_URL}" "WARNING" >/dev/null 2>&1 || true
	rm -rf -- "${WORK_DIR}" 2>/dev/null || true
	exit 0
}

if ! [[ "${SELF_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${RUN_ID}" =~ ^[1-9][0-9]{0,19}$ ]] \
	|| { [ "${JOB}" != "intake" ] && [ "${JOB}" != "recovery-scan" ]; } || [ ! -f "${HEAL_PY}" ]; then
	JOB="$(printf '%s' "${JOB}" | tr -cd 'a-z-' | head -c 20)"
	_unlocked missing_context
fi

LOGIN="$(gh_retry gh api --method GET user --jq .login 2>/dev/null | tr -d '[:space:]')"
[[ "${LOGIN}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]] || _unlocked identity_unavailable

# The lowest-numbered open queue issue written by the pipeline login.
_find_queue_issue()
{
	local issues_file="${WORK_DIR}/queue_issues.json"
	gh_retry gh api --method GET "repos/${SELF_REPO}/issues" -F state=open -F labels="${QUEUE_LABEL}" -F per_page=100 > "${issues_file}" 2>/dev/null || return 1
	jq -r --arg login "${LOGIN,,}" '[.[]? | select(type == "object" and .pull_request == null and ((.user.login // "") | ascii_downcase) == $login) | .number] | min // empty' "${issues_file}" 2>/dev/null
}

QUEUE_ISSUE="$(_find_queue_issue)" || _unlocked issue_list_failed
if [ -z "${QUEUE_ISSUE}" ]; then
	ensure_label_exists "${QUEUE_LABEL}" "${SELF_REPO}" || true
	QUEUE_BODY="Queue for the Workflow Failure Heal Intake (\`scripts/workflow_failure_heal_queue.sh\`, automation). Each intake run posts a ticket comment here, waits until it is the oldest live ticket, and deletes it when it finishes. Leave this issue open; when none is open, the next run opens a new one."
	CREATED_URL="$(gh_retry gh issue create --repo "${SELF_REPO}" --title "Heal intake queue (automation)" --body "${QUEUE_BODY}" --label "${QUEUE_LABEL}" 2>/dev/null || true)"
	CREATED_URL="${CREATED_URL%%[[:space:]]*}"
	CREATED_NUMBER=""
	if [[ "${CREATED_URL}" =~ /issues/([1-9][0-9]*)$ ]]; then
		CREATED_NUMBER="${BASH_REMATCH[1]}"
	fi
	[ -n "${CREATED_NUMBER}" ] || _unlocked issue_create_failed
	# Another run may have opened one at the same time: everyone uses the
	# lowest number, and the loser closes its own.
	QUEUE_ISSUE="$(_find_queue_issue)" || QUEUE_ISSUE=""
	[ -n "${QUEUE_ISSUE}" ] || QUEUE_ISSUE="${CREATED_NUMBER}"
	if [ "${QUEUE_ISSUE}" != "${CREATED_NUMBER}" ]; then
		gh_retry gh issue close "${CREATED_NUMBER}" --repo "${SELF_REPO}" --reason "not planned" --comment "Duplicate of #${QUEUE_ISSUE} (opened at the same time)." >/dev/null 2>&1 || true
		log "issue_created issue=${CREATED_NUMBER} outcome=duplicate_closed using=${QUEUE_ISSUE}"
	else
		log "issue_created issue=${QUEUE_ISSUE}"
	fi
fi
[[ "${QUEUE_ISSUE}" =~ ^[1-9][0-9]*$ ]] || _unlocked issue_list_failed

TICKET_BODY="$(python3 -I -B "${HEAL_PY}" heal-queue ticket --run-id "${RUN_ID}" --attempt "${RUN_ATTEMPT}" --job "${JOB}" 2>/dev/null)" || _unlocked ticket_render_failed
TICKET_ID="$(gh_retry gh api --method POST "repos/${SELF_REPO}/issues/${QUEUE_ISSUE}/comments" -f body="${TICKET_BODY}" --jq .id 2>/dev/null | tr -d '[:space:]')"
[[ "${TICKET_ID}" =~ ^[1-9][0-9]{0,19}$ ]] || { TICKET_ID=""; _unlocked ticket_post_failed; }
# Exported before the wait, so a cancelled run's release step still deletes it.
_env_out HEAL_INTAKE_QUEUE_ISSUE "${QUEUE_ISSUE}"
_env_out HEAL_INTAKE_QUEUE_TICKET_ID "${TICKET_ID}"
log "enqueued issue=${QUEUE_ISSUE} ticket=${TICKET_ID} job=${JOB} run=${RUN_ID} attempt=${RUN_ATTEMPT}"

COMMENTS_FILE="${WORK_DIR}/queue_comments.json"
RUNS_FILE="${WORK_DIR}/holder_runs.json"
DECISION_FILE="${WORK_DIR}/queue_position.json"
printf '{}\n' > "${RUNS_FILE}"
READ_FAILURES=0
LAST_STATE=""
declare -A HOLDER_CHECKED_AT=()

while :; do
	if ! gh_retry gh api --method GET --paginate --slurp "repos/${SELF_REPO}/issues/${QUEUE_ISSUE}/comments?per_page=100" > "${COMMENTS_FILE}" 2>/dev/null; then
		READ_FAILURES=$((READ_FAILURES + 1))
		[ "${READ_FAILURES}" -lt 3 ] || _unlocked comments_unavailable
		sleep "${POLL_SECONDS}"
		continue
	fi
	READ_FAILURES=0
	if ! python3 -I -B "${HEAL_PY}" heal-queue position --comments-json "${COMMENTS_FILE}" --ticket-id "${TICKET_ID}" --login "${LOGIN}" \
		--lease-seconds "${LEASE_SECONDS}" --runs-json "${RUNS_FILE}" > "${DECISION_FILE}" 2>/dev/null \
		|| ! jq -e '(.status | type == "string") and (.stale | type == "array")' "${DECISION_FILE}" >/dev/null 2>&1; then
		_unlocked position_failed
	fi
	while IFS= read -r stale_id; do
		[[ "${stale_id}" =~ ^[1-9][0-9]{0,19}$ ]] || continue
		if GH_RETRY_IDEMPOTENT=true gh_retry gh api --method DELETE "repos/${SELF_REPO}/issues/comments/${stale_id}" >/dev/null 2>&1; then
			log "stale_ticket_removed ticket=${stale_id} issue=${QUEUE_ISSUE}"
		fi
	done < <(jq -r '.stale[]' "${DECISION_FILE}")
	STATUS="$(jq -r '.status' "${DECISION_FILE}")"
	case "${STATUS}" in
		go)
			WAITED="$(_waited)"
			_env_out HEAL_INTAKE_QUEUE_WAITED_SECONDS "${WAITED}"
			log "acquired issue=${QUEUE_ISSUE} ticket=${TICKET_ID} job=${JOB} run=${RUN_ID} waited_seconds=${WAITED}"
			rm -rf -- "${WORK_DIR}" 2>/dev/null || true
			exit 0
			;;
		lost)
			_unlocked ticket_lost
			;;
	esac
	HOLDER_RUN="$(jq -r '.holder.run_id // ""' "${DECISION_FILE}")"
	POSITION="$(jq -r '.position // 0' "${DECISION_FILE}")"
	NOW_EPOCH="$(date +%s)"
	if [[ "${HOLDER_RUN}" =~ ^[1-9][0-9]{0,19}$ ]] \
		&& [ $((NOW_EPOCH - ${HOLDER_CHECKED_AT[${HOLDER_RUN}]:-0})) -ge "${HOLDER_CHECK_SECONDS}" ]; then
		HOLDER_CHECKED_AT[${HOLDER_RUN}]="${NOW_EPOCH}"
		HOLDER_RUN_FILE="${WORK_DIR}/holder_run.json"
		if gh_retry gh api --method GET "repos/${SELF_REPO}/actions/runs/${HOLDER_RUN}" > "${HOLDER_RUN_FILE}" 2>/dev/null \
			&& jq -e 'type == "object"' "${HOLDER_RUN_FILE}" >/dev/null 2>&1; then
			jq --arg id "${HOLDER_RUN}" --slurpfile run "${HOLDER_RUN_FILE}" '. + {($id): {status: ($run[0].status // ""), run_attempt: ($run[0].run_attempt // 0)}}' \
				"${RUNS_FILE}" > "${RUNS_FILE}.tmp" && mv "${RUNS_FILE}.tmp" "${RUNS_FILE}"
			if jq -e --arg id "${HOLDER_RUN}" --argjson attempt "$(jq -r '.holder.attempt // 0' "${DECISION_FILE}")" \
				'.[$id].status == "completed" or (.[$id].run_attempt > $attempt)' "${RUNS_FILE}" >/dev/null 2>&1; then
				# The holder is gone: re-read the queue now instead of after a poll.
				continue
			fi
		fi
	fi
	STATE="${POSITION}:${HOLDER_RUN}"
	if [ "${STATE}" != "${LAST_STATE}" ]; then
		log "waiting issue=${QUEUE_ISSUE} ticket=${TICKET_ID} position=${POSITION} holder_run=${HOLDER_RUN:-none} job=${JOB} run=${RUN_ID} waited_seconds=$(_waited)"
		LAST_STATE="${STATE}"
	fi
	if [ "$(_waited)" -ge "${MAX_WAIT_SECONDS}" ]; then
		_unlocked max_wait
	fi
	sleep "${POLL_SECONDS}"
done
