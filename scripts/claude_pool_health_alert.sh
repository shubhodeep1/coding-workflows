#!/usr/bin/env bash
# claude_pool_health_alert.sh — hourly Telegram WARNING when a Claude pool
# account is at or above the usage gate (#6951).
#
# Runs in orchestrate_poll.yml right after .github/actions/claude-pool-token,
# whose `probes` output (the probe records: account, 5-hour and 7-day
# utilization, reset times, status, error; never a token) arrives as
# CLAUDE_POOL_PROBES. `claude_engine.py pool-health` turns the records into one
# message naming every account at or above `gate_utilization` with both
# utilizations and the reset time of each window at the gate, plus every
# account whose token was rejected; nothing is sent while every account is
# below the gate. The poller runs every five minutes, so the message goes out
# at most once an hour: only the cycle whose clock minute is below
# CLAUDE_POOL_HEALTH_WINDOW_MINUTES (default 5) sends it. A cycle delayed past
# that window skips the hour rather than sending twice.
#
# Env:
#   CLAUDE_POOL_PROBES                  JSON list from the action's `probes` output
#   CLAUDE_POOL_HEALTH_ALERT_ENABLED    `false` disables the alert (default true)
#   CLAUDE_POOL_HEALTH_WINDOW_MINUTES   send only when the UTC minute is below this (default 5)
#   CLAUDE_POOL_HEALTH_GATE             override of claude_engine.json gate_utilization (tests)
#   CLAUDE_ENGINE_CONFIG                claude_engine.json to read the gate from
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID / ALERT_MSG_LEVEL   scripts/tg_helpers.sh
#
# Log: CLAUDE_POOL_HEALTH accounts=N gated=N auth_failed=N probe_failed=N
#      alert=sent|none|outside_window|disabled|no_probes|invalid_probes
# Never fails the job.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${script_dir}/tg_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/tg_helpers.sh"
fi
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

pool_health_log()
{
	echo "CLAUDE_POOL_HEALTH accounts=${1:-0} gated=${2:-0} auth_failed=${3:-0} probe_failed=${4:-0} alert=${5}"
}

case "$(printf '%s' "${CLAUDE_POOL_HEALTH_ALERT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	false|0|no|off) pool_health_log 0 0 0 0 disabled; exit 0 ;;
esac
probes_json="${CLAUDE_POOL_PROBES:-}"
if [ -z "${probes_json}" ] || [ "${probes_json}" = "[]" ]; then
	pool_health_log 0 0 0 0 no_probes
	exit 0
fi

health_args=()
if [ -n "${CLAUDE_POOL_HEALTH_GATE:-}" ]; then
	health_args+=(--gate "${CLAUDE_POOL_HEALTH_GATE}")
elif [ -n "${CLAUDE_ENGINE_CONFIG:-}" ]; then
	health_args+=(--config "${CLAUDE_ENGINE_CONFIG}")
fi
report="$(printf '%s' "${probes_json}" | PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" pool-health "${health_args[@]}")" || {
	pool_health_log 0 0 0 0 invalid_probes
	exit 0
}
read -r accounts gated auth_failed probe_failed alert < <(printf '%s' "${report}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print(r["accounts"], len(r["gated"]), len(r["auth_failed"]), len(r["probe_failed"]), "true" if r["alert"] else "false")
')
if [ "${alert}" != "true" ]; then
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" none
	exit 0
fi

window="${CLAUDE_POOL_HEALTH_WINDOW_MINUTES:-5}"
[[ "${window}" =~ ^[0-9]+$ ]] || window=5
minute=$((10#$(date -u +%M)))
if [ "${minute}" -ge "${window}" ]; then
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" outside_window
	exit 0
fi

text="$(printf '%s' "${report}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["text"])')"
if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
	text="${text}
Probed by ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi
tg_send_msg "${text}" "WARNING" > /dev/null || true
pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" sent
exit 0
