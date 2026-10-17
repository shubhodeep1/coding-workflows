#!/usr/bin/env bash
# claude_pool_daily_usage.sh — once-a-day Telegram message with the usage of
# every Claude pool account (5-hour and 7-day utilization), in one message.
#
# Wired into orchestrate_poll.yml, in shubhodeep1/coding-workflows only: the
# pool is shared by every consumer repo, so one repo sends it.
#
#   slot  Runs in the poll job on every tick. Prints in_slot=true|false and
#         date=YYYY-MM-DD (UTC) to $GITHUB_OUTPUT. in_slot is true from
#         CLAUDE_POOL_DAILY_USAGE_HOUR_UTC (default 4, i.e. 12:00 SGT) to the
#         end of the UTC day, when the report is enabled, Telegram is
#         configured and ALERT_MSG_LEVEL is not SILENT. The workflow then
#         looks up the day's actions/cache marker; the report job runs only
#         when the marker is missing, so it is sent once a day.
#   send  Runs in the claude_pool_daily_usage job after
#         .github/actions/claude-pool-token. Turns its `probes` output into
#         one message (`claude_engine.py pool-usage`) and sends it with
#         tg_send_report_msg (no level filter, except SILENT). Prints
#         sent=true|false to $GITHUB_OUTPUT; the job saves the day's marker
#         only on sent=true, so a failed send is retried on the next tick.
#
# Env:
#   CLAUDE_POOL_DAILY_USAGE_ENABLED   `false` disables the report (default true)
#   CLAUDE_POOL_DAILY_USAGE_HOUR_UTC  first UTC hour of the daily slot, 0-23 (default 4)
#   CLAUDE_POOL_DAILY_USAGE_NOW       epoch seconds to use as "now" (tests)
#   CLAUDE_POOL_PROBES                send: JSON list from the action's `probes` output
#   CLAUDE_POOL_REASON                send: the action's `reason` output, shown when no probes ran
#   CLAUDE_POOL_DAILY_USAGE_GATE      send: override of claude_engine.json gate_utilization (tests)
#   CLAUDE_ENGINE_CONFIG              send: claude_engine.json to read the gate from
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID / ALERT_MSG_LEVEL   scripts/tg_helpers.sh
#
# Log: CLAUDE_POOL_DAILY_USAGE op=slot in_slot=true|false reason=... date=... hour=...
#      CLAUDE_POOL_DAILY_USAGE op=send accounts=N usable=N gated=N auth_failed=N probe_failed=N
#          outcome=sent|not_delivered
# Never fails the job.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

daily_usage_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s\n' "$1" >> "${GITHUB_OUTPUT}"
	fi
}

daily_usage_slot()
{
	local now hour date_utc start_hour in_slot=false reason
	now="${CLAUDE_POOL_DAILY_USAGE_NOW:-}"
	[[ "${now}" =~ ^[0-9]+$ ]] || now="$(date -u +%s)"
	hour=$((10#$(date -u -d "@${now}" +%H)))
	date_utc="$(date -u -d "@${now}" +%Y-%m-%d)"
	start_hour="${CLAUDE_POOL_DAILY_USAGE_HOUR_UTC:-4}"
	if ! [[ "${start_hour}" =~ ^[0-9]+$ ]] || [ "$((10#${start_hour}))" -gt 23 ]; then
		start_hour=4
	fi
	start_hour=$((10#${start_hour}))
	case "$(printf '%s' "${CLAUDE_POOL_DAILY_USAGE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		false|0|no|off) reason=disabled ;;
		*)
			if [ "$(printf '%s' "${ALERT_MSG_LEVEL:-}" | tr '[:lower:]' '[:upper:]')" = "SILENT" ]; then
				reason=silent
			elif [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ]; then
				reason=telegram_not_configured
			elif [ "${hour}" -lt "${start_hour}" ]; then
				reason=before_hour
			else
				reason=due
				in_slot=true
			fi
			;;
	esac
	daily_usage_output "in_slot=${in_slot}"
	daily_usage_output "date=${date_utc}"
	echo "CLAUDE_POOL_DAILY_USAGE op=slot in_slot=${in_slot} reason=${reason} date=${date_utc} hour=${hour} start_hour=${start_hour}"
}

daily_usage_send()
{
	if [ -f "${script_dir}/tg_helpers.sh" ]; then
		# shellcheck disable=SC1091
		source "${script_dir}/tg_helpers.sh"
	fi
	type tg_send_report_msg >/dev/null 2>&1 || tg_send_report_msg() { :; }

	local probes_json report_args=() report text counts message_id outcome
	probes_json="${CLAUDE_POOL_PROBES:-}"
	[ -n "${probes_json}" ] || probes_json="[]"
	if [ -n "${CLAUDE_POOL_DAILY_USAGE_GATE:-}" ]; then
		report_args+=(--gate "${CLAUDE_POOL_DAILY_USAGE_GATE}")
	elif [ -n "${CLAUDE_ENGINE_CONFIG:-}" ]; then
		report_args+=(--config "${CLAUDE_ENGINE_CONFIG}")
	fi
	if report="$(printf '%s' "${probes_json}" | PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" pool-usage "${report_args[@]}" 2>/dev/null)"; then
		text="$(printf '%s' "${report}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["text"])')"
		counts="$(printf '%s' "${report}" | python3 -c '
import json, sys
print("accounts=%(accounts)s usable=%(usable)s gated=%(gated)s auth_failed=%(auth_failed)s probe_failed=%(probe_failed)s" % json.load(sys.stdin))
')"
	else
		echo "::warning::CLAUDE_POOL_DAILY_USAGE op=send reason=pool_usage_failed; sending the fallback text"
		text="Claude pool daily usage: probe results unreadable."
		counts="accounts=0 usable=0 gated=0 auth_failed=0 probe_failed=0"
	fi
	if [ "${probes_json}" = "[]" ] && [ -n "${CLAUDE_POOL_REASON:-}" ]; then
		text="${text%.} (pool: ${CLAUDE_POOL_REASON})."
	fi
	text="📊 ${text}"
	if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
		text="${text}
Probed by ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
	fi
	# Telegram rejects messages over 4096 characters; a rejected send would
	# be retried on every tick for the rest of the day.
	text="${text:0:3900}"
	# Only a message id means the message reached Telegram.
	message_id="$(tg_send_report_msg "${text}" 2>/dev/null)" || message_id=""
	if [ -n "${message_id}" ]; then
		outcome=sent
		daily_usage_output "sent=true"
	else
		outcome=not_delivered
		daily_usage_output "sent=false"
	fi
	echo "CLAUDE_POOL_DAILY_USAGE op=send ${counts} outcome=${outcome}"
}

case "${1:-}" in
	slot) daily_usage_slot ;;
	send) daily_usage_send ;;
	*) echo "usage: $0 slot|send" >&2 ;;
esac
exit 0
