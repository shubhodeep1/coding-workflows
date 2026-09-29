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
# Required env (set by the workflow):
#   GITHUB_REPOSITORY                  this repo (coding-workflows)
#   GH_TOKEN                           the workflow's GITHUB_TOKEN (issues: write)
#
# Optional env (have defaults):
#   CLAUDE_ISSUE_QUEUE_STALE_HOURS     default 3
#   CLAUDE_ISSUE_ROUTE_PY              default scripts/claude_issue_route.py
#   CLAUDE_ISSUE_WATCHDOG_MODE         queue-stale (default) | env-requeue (issue #4938,
#                                      see env_requeue below; GH_TOKEN is then GH_PAT)
#   CLAUDE_ISSUE_QUEUE_TOKEN           env-requeue: GITHUB_TOKEN for queue-issue writes
#   CLAUDE_ISSUE_REGISTRY              env-requeue: default .github/ai/consumer_repos.json
#   CLAUDE_ISSUE_ENV_REQUEUE_MAX       env-requeue: default 2 re-queues per issue per window
#   CLAUDE_ISSUE_ENV_REQUEUE_WINDOW_HOURS  env-requeue: default 24
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

# --- env-requeue mode (issue #4938) ---------------------------------------------
#
# CLAUDE_ISSUE_WATCHDOG_MODE=env-requeue runs this instead of the stale check,
# as its own workflow step with GH_TOKEN = GH_PAT (the registered consumer
# repos, and the `claude-issue` dispatch) and CLAUDE_ISSUE_QUEUE_TOKEN = the
# workflow's GITHUB_TOKEN (queue-issue writes start no workflow). It:
#
#   1. closes open queue issues whose target issue was closed after it was
#      queued, so the pickup never starts a session for it (#4912);
#   2. re-queues each open `ai:claude` + `ai:claude-blocked` issue whose latest
#      trusted blocker is `<!-- ai:claude-blocked:v1 reason=environment-… -->`,
#      exactly as `/reclarify` does: the same `claude-issue` repository_dispatch
#      (trigger `reclarify`), which the intake authorizes, queues, and binds.
#      Each re-queue leaves `<!-- ai:claude-env-requeue:v1 blocker=<id> reason=<r> -->`
#      on the issue; after CLAUDE_ISSUE_ENV_REQUEUE_MAX (default 2) in
#      CLAUDE_ISSUE_ENV_REQUEUE_WINDOW_HOURS (default 24) it posts the
#      exhausted marker and one Telegram ERROR instead, and the label stays.
#      It then leaves that issue alone until a trusted `/reclarify` comment,
#      which restarts the count (env_requeue_decision).
#      Both markers count only when GH_TOKEN's own account posted them (issue
#      #5135): the login comes from `gh api user` and goes to env-requeue-plan
#      as --watchdog-login. When it cannot be read, step 2 does nothing that run
#      (`env_requeue_skipped reason=watchdog_login_unknown`).
#
# claude_issue_route.py decides (env-requeue-plan, queue-closed-targets); this
# function only writes. Reads: see env_requeue_plan's batching contract, plus
# one `GET /user` per run for the watchdog login.
# Writes: one PATCH per closed-target queue issue, one dispatch plus one
# comment per re-queue, one comment plus one Telegram message per alert.
# Fail open: every failure is logged and the run still exits 0.
env_requeue()
{
	local registry="${CLAUDE_ISSUE_REGISTRY:-.github/ai/consumer_repos.json}"
	local queue_token="${CLAUDE_ISSUE_QUEUE_TOKEN:-}"
	local max_retries="${CLAUDE_ISSUE_ENV_REQUEUE_MAX:-2}"
	local window_hours="${CLAUDE_ISSUE_ENV_REQUEUE_WINDOW_HOURS:-24}"
	[[ "${max_retries}" =~ ^[1-9][0-9]*$ ]] || max_retries="2"
	[[ "${window_hours}" =~ ^[0-9]+([.][0-9]+)?$ ]] || window_hours="24"
	if [ -z "${GH_TOKEN:-}" ]; then
		log "warn env_requeue_skipped reason=no_gh_token"
		return 0
	fi

	# 1. Queue issues whose target issue is closed.
	local closed_file="${RUNTIME_DIR}/closed_targets.json"
	if python3 "${ROUTE_PY}" queue-closed-targets --fetch-repo "${SELF_REPO}" --registry "${registry}" --self-repo "${SELF_REPO}" \
		> "${closed_file}" 2> "${RUNTIME_DIR}/closed_targets_error.txt"; then
		local queue_issue target_repo target_number
		while IFS=$'\t' read -r queue_issue target_repo target_number; do
			[[ "${queue_issue}" =~ ^[1-9][0-9]*$ ]] || continue
			if [ -z "${queue_token}" ]; then
				log "closed_target_report_only queue_issue=${queue_issue} target=${target_repo}#${target_number}"
				continue
			fi
			if GH_TOKEN="${queue_token}" gh_retry gh api -X PATCH "repos/${SELF_REPO}/issues/${queue_issue}" \
				-f state=closed -f state_reason=not_planned >/dev/null 2>&1; then
				log "closed_target queue_issue=${queue_issue} target=${target_repo}#${target_number}"
			else
				log "warn closed_target_close_failed queue_issue=${queue_issue} target=${target_repo}#${target_number}"
			fi
		done < <(jq -r '.[]? | [.queue_issue, .repo, .issue_number] | @tsv' "${closed_file}" 2>/dev/null || true)
	else
		log "warn closed_targets_read_failed detail=$(head -c 200 "${RUNTIME_DIR}/closed_targets_error.txt" 2>/dev/null | tr '\n' ' ')"
	fi

	# 2. Environment blockers.
	# The re-queue and exhausted markers below are posted with GH_TOKEN, so only
	# that account's markers are watchdog state (issue #5135). CLAUDE.md §15: one
	# `GET /user` per run, outside every loop. The search and comment reads
	# env-requeue-plan makes carry no identity for the token, and
	# review_autofix.yml resolves its marker author the same way. Fail closed:
	# without the login no marker can be verified, so nothing is re-queued or
	# alerted this run.
	local watchdog_login=""
	watchdog_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
	if ! [[ "${watchdog_login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]]; then
		log "warn env_requeue_skipped reason=watchdog_login_unknown"
		return 0
	fi
	local plan_file="${RUNTIME_DIR}/env_requeue_plan.json"
	if ! python3 "${ROUTE_PY}" env-requeue-plan --registry "${registry}" --self-repo "${SELF_REPO}" \
		--max-retries "${max_retries}" --window-hours "${window_hours}" --stale-hours "${STALE_HOURS}" \
		--watchdog-login "${watchdog_login}" \
		> "${plan_file}" 2> "${RUNTIME_DIR}/env_requeue_plan_error.txt"; then
		log "warn env_requeue_plan_failed detail=$(head -c 200 "${RUNTIME_DIR}/env_requeue_plan_error.txt" 2>/dev/null | tr '\n' ' ')"
		return 0
	fi
	local error_line
	while IFS= read -r error_line; do
		log "warn env_requeue_read_failed detail=${error_line:0:200}"
	done < <(jq -r '.errors[]?' "${plan_file}" 2>/dev/null || true)
	log "env_requeue checked candidates=$(jq '.actions | length' "${plan_file}" 2>/dev/null || echo 0) skipped=$(jq '.skipped | length' "${plan_file}" 2>/dev/null || echo 0) searches=$(jq '.searches // 0' "${plan_file}" 2>/dev/null || echo 0)"

	local index=0 action repo number reason blocker_id blocker_reason retry retries skip_security issue_url body alert_msg
	# Fields are joined with the unit separator, not a tab: bash collapses runs of
	# whitespace IFS characters, so an empty field (a plain blocker's reason)
	# would shift the ones after it.
	while IFS=$'\x1f' read -r action repo number reason blocker_id blocker_reason retry retries skip_security; do
		index=$((index + 1))
		# Every field is re-validated before it reaches a path, a comment, or a message.
		if ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && "${number}" =~ ^[1-9][0-9]*$ && "${blocker_id}" =~ ^[0-9]+$ ]] \
			|| ! [[ "${blocker_reason}" =~ ^[a-z0-9-]*$ && "${reason}" =~ ^[a-z_]+$ ]] \
			|| ! [[ "${retry}" =~ ^[0-9]+$ && "${retries}" =~ ^[0-9]+$ ]]; then
			log "warn env_requeue_bad_action index=${index}"
			continue
		fi
		issue_url="https://github.com/${repo}/issues/${number}"
		case "${action}" in
			requeue)
				local issue_file="${RUNTIME_DIR}/env_requeue_issue_${index}.json"
				local dispatch_file="${RUNTIME_DIR}/env_requeue_dispatch_${index}.json"
				jq -n --argjson n "${number}" '{number: $n}' > "${issue_file}"
				if ! python3 "${ROUTE_PY}" build-dispatch --repo "${repo}" --issue-json "${issue_file}" --trigger reclarify \
					--reporter-run-url "${RUN_URL}" --skip-security-pass "${skip_security}" > "${dispatch_file}" 2>/dev/null; then
					log "warn env_requeue_dispatch_build_failed repo=${repo} issue=${number}"
					continue
				fi
				if ! gh_retry gh api -X POST "repos/${SELF_REPO}/dispatches" --input "${dispatch_file}" >/dev/null 2> "${RUNTIME_DIR}/env_requeue_dispatch_error.txt"; then
					log "warn env_requeue_dispatch_failed repo=${repo} issue=${number} detail=$(head -c 200 "${RUNTIME_DIR}/env_requeue_dispatch_error.txt" | tr '\n' ' ')"
					continue
				fi
				body="<!-- ai:claude-env-requeue:v1 blocker=${blocker_id} reason=${blocker_reason} -->
🔁 **Re-queued automatically.** The last Claude session stopped on an environment failure (\`${blocker_reason}\`), not on a decision, so this issue was sent back to the Claude issue pickup the same way \`/reclarify\` does. A fresh session on a fresh container starts within about an hour.

Retry ${retry} of ${max_retries} in ${window_hours}h. After that the queue watchdog alerts instead and leaves \`ai:claude-blocked\` in place."
				[ -z "${RUN_URL}" ] || body+=$'\n\n'"Run: ${RUN_URL}"
				if gh_retry gh api "repos/${repo}/issues/${number}/comments" -f body="${body}" >/dev/null 2>&1; then
					log "env_requeue requeued repo=${repo} issue=${number} blocker=${blocker_id} reason=${blocker_reason} retry=${retry} why=${reason}"
				else
					log "warn env_requeue_marker_failed repo=${repo} issue=${number} blocker=${blocker_id}"
				fi
				;;
			alert)
				body="<!-- ai:claude-env-requeue-exhausted:v1 blocker=${blocker_id} reason=${blocker_reason} -->
⚠️ **Automatic re-queue stopped.** Claude sessions for this issue stopped on environment failures ${retries} times in the last ${window_hours}h (latest: \`${blocker_reason}\`). The \`ai:claude-blocked\` label stays. Fix the session environment, then comment \`/reclarify\`."
				[ -z "${RUN_URL}" ] || body+=$'\n\n'"Run: ${RUN_URL}"
				if gh_retry gh api "repos/${repo}/issues/${number}/comments" -f body="${body}" >/dev/null 2>&1; then
					log "env_requeue exhausted repo=${repo} issue=${number} blocker=${blocker_id} reason=${blocker_reason} retries=${retries}"
					echo "::warning::Claude issue ${repo}#${number}: ${retries} environment re-queues in ${window_hours}h; automatic re-queue stopped"
					alert_msg="Claude issue re-queue stopped for ${repo}#${number}: ${retries} environment re-queues in ${window_hours}h (latest ${blocker_reason}). The ai:claude-blocked label stays; fix the session environment, then comment /reclarify."$'\n'"Issue: ${issue_url}"
					[ -z "${RUN_URL}" ] || alert_msg+=$'\n'"Run: ${RUN_URL}"
					tg_send_msg "${alert_msg}" "ERROR" >/dev/null 2>&1 || true
				else
					log "warn env_requeue_alert_marker_failed repo=${repo} issue=${number} blocker=${blocker_id}"
				fi
				;;
			*)
				log "env_requeue skip repo=${repo} issue=${number} reason=${reason} blocker_reason=${blocker_reason:-plain}"
				;;
		esac
	done < <(jq -r '.actions[]? | [.action, .repo, .issue_number, .reason, .blocker_id, .blocker_reason, .retry, .retries_in_window, (if .skip_security_pass then "true" else "false" end)] | map(tostring) | join("\u001f")' "${plan_file}" 2>/dev/null || true)
	return 0
}

if [ "${CLAUDE_ISSUE_WATCHDOG_MODE:-queue-stale}" = "env-requeue" ]; then
	env_requeue || log "warn env_requeue_failed"
	exit 0
fi

QUEUE_FILE="${RUNTIME_DIR}/open_queue.json"
if ! gh_retry gh api "repos/${SELF_REPO}/issues?labels=${QUEUE_LABEL}&state=open&per_page=100" > "${QUEUE_FILE}"; then
	# Fail open: a missed hourly check is recovered by the next run.
	log "warn queue_read_failed"
	exit 0
fi

STALE_FILE="${RUNTIME_DIR}/stale.json"
python3 "${ROUTE_PY}" queue-stale --issues-json "${QUEUE_FILE}" --stale-hours "${STALE_HOURS}" > "${STALE_FILE}"
OPEN_COUNT="$(jq '[.[]? | select(.pull_request == null)] | length' "${QUEUE_FILE}")"
STALE_COUNT="$(jq 'length' "${STALE_FILE}")"
log "checked open=${OPEN_COUNT} newly_stale=${STALE_COUNT} stale_hours=${STALE_HOURS}"
if [ "${STALE_COUNT}" = "0" ]; then
	exit 0
fi

ensure_label_exists "${STALE_LABEL}" "${SELF_REPO}" || true
LINES=""
while IFS=$'\t' read -r number title age; do
	[[ "${number}" =~ ^[1-9][0-9]*$ ]] || continue
	gh_retry gh api -X POST "repos/${SELF_REPO}/issues/${number}/labels" -f "labels[]=${STALE_LABEL}" >/dev/null 2>&1 || \
		log "warn label_failed queue_issue=${number}"
	log "stale queue_issue=${number} age_hours=${age} title=${title}"
	echo "::warning::Claude issue queue item #${number} (${title}) has waited ${age}h without being picked up"
	LINES+=$'\n'"- #${number} ${title} (${age}h)"
done < <(jq -r '.[] | [.number, .title, .age_hours] | @tsv' "${STALE_FILE}")

tg_send_msg "Claude issue queue: ${STALE_COUNT} item(s) waiting over ${STALE_HOURS}h in ${SELF_REPO}:${LINES}"$'\n'"The pickup has likely stopped. Restart it from a new cloud session opened in the app, in Auto mode: /claude-issue-pickup start — restart"$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
exit 0
