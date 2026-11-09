#!/usr/bin/env bash
# claude_pool_health_alert.sh — hourly Telegram note when a Claude pool
# account is at or above the usage gate (#6951): DEBUG when accounts are only
# gated, WARNING when a token is rejected or a probe fails.
#
# Runs in orchestrate_poll.yml right after .github/actions/claude-pool-token,
# whose `probes` output (the probe records: account, 5-hour and 7-day
# utilization, reset times, status, error; never a token) arrives as
# CLAUDE_POOL_PROBES. `claude_engine.py pool-health` turns the records into one
# message listing every probed account with both utilizations and the hours
# until each window resets (plus the UTC reset time of each window at or above
# `gate_utilization`), and every account whose token was rejected; nothing is
# sent while every account is below the gate. The poller runs every five minutes, so the message goes out
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
#   CLAUDE_CAPACITY_WAIT_ITEMS          items JSON of provider_outage.py capacity-resume
#                                       (issue #7370); with count > 0 the message
#                                       starts with the waiting work and is sent in
#                                       the window even when no account is gated
#   CHATGPT_POOL_STATUS                 `status` JSON of .github/actions/chatgpt-pool-token
#                                       (mode: status; never a token, never echoed). Adds a
#                                       ChatGPT block to the same hourly message when an
#                                       account's login was rejected, its last refresh failed,
#                                       its last refresh is stale, or usage is at the gate
#                                       (ChatGPT trial plan Phase 4)
#   CHATGPT_POOL_HEALTH_ALERT_ENABLED   `false` drops only the ChatGPT part (default true);
#                                       CLAUDE_POOL_HEALTH_ALERT_ENABLED=false still disables all
#   CHATGPT_POOL_ALERT_STALE_HOURS      a last refresh at least this old is stale (default 12)
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID / ALERT_MSG_LEVEL   scripts/tg_helpers.sh
#
# Log: CLAUDE_POOL_HEALTH accounts=N gated=N auth_failed=N probe_failed=N
#      alert=sent|not_delivered|none|outside_window|disabled|no_probes|invalid_probes waiting=N
#      chatgpt_accounts=N chatgpt_alerts=N
# Level: WARNING for a rejected Claude token or failed Claude probe, and for a
# ChatGPT login rejection, failed refresh or stale refresh; DEBUG otherwise.
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
	echo "CLAUDE_POOL_HEALTH accounts=${1:-0} gated=${2:-0} auth_failed=${3:-0} probe_failed=${4:-0} alert=${5} waiting=${waiting_count:-0} chatgpt_accounts=${chatgpt_accounts:-0} chatgpt_alerts=${chatgpt_alerts:-0}"
}

case "$(printf '%s' "${CLAUDE_POOL_HEALTH_ALERT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	false|0|no|off) pool_health_log 0 0 0 0 disabled; exit 0 ;;
esac
# ChatGPT part (trial plan Phase 4): on unless switched off, and only with a status.
chatgpt_on=false
case "$(printf '%s' "${CHATGPT_POOL_HEALTH_ALERT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	false|0|no|off) ;;
	*) [ -z "${CHATGPT_POOL_STATUS:-}" ] || chatgpt_on=true ;;
esac
# An oversized status (over one argv element's limit) would fail the whole
# engine call and drop the Claude alert too; skip only the ChatGPT part.
if [ "${chatgpt_on}" = "true" ] && [ "${#CHATGPT_POOL_STATUS}" -gt 100000 ]; then
	echo "::warning::CLAUDE_POOL_HEALTH chatgpt_status=too_large; ChatGPT part skipped"
	chatgpt_on=false
fi
chatgpt_stale_hours="${CHATGPT_POOL_ALERT_STALE_HOURS:-12}"
[[ "${chatgpt_stale_hours}" =~ ^[0-9]+([.][0-9]+)?$ ]] || chatgpt_stale_hours=12
probes_json="${CLAUDE_POOL_PROBES:-}"
claude_probes_empty=false
if [ -z "${probes_json}" ] || [ "${probes_json}" = "[]" ]; then
	if [ "${chatgpt_on}" != "true" ]; then
		pool_health_log 0 0 0 0 no_probes
		exit 0
	fi
	probes_json="[]"
	claude_probes_empty=true
fi

health_args=()
if [ -n "${CLAUDE_POOL_HEALTH_GATE:-}" ]; then
	health_args+=(--gate "${CLAUDE_POOL_HEALTH_GATE}")
elif [ -n "${CLAUDE_ENGINE_CONFIG:-}" ]; then
	health_args+=(--config "${CLAUDE_ENGINE_CONFIG}")
fi
if [ "${chatgpt_on}" = "true" ]; then
	health_args+=("--chatgpt-status=${CHATGPT_POOL_STATUS}" --chatgpt-stale-hours "${chatgpt_stale_hours}")
fi
report="$(printf '%s' "${probes_json}" | PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/claude_engine.py" pool-health "${health_args[@]}")" || {
	pool_health_log 0 0 0 0 invalid_probes
	exit 0
}
# Waiting work recorded on the Claude capacity tracker (issue #7370).
waiting_count=0
waiting_text=""
# Empty Claude probes keep their pre-ChatGPT behaviour: no Claude
# capacity-wait notice (it would have exited with no_probes before).
if [ -n "${CLAUDE_CAPACITY_WAIT_ITEMS:-}" ] && [ "${claude_probes_empty}" != "true" ]; then
	waiting_text="$(printf '%s' "${report}" | CLAUDE_CAPACITY_WAIT_ITEMS="${CLAUDE_CAPACITY_WAIT_ITEMS}" python3 -I -c '
import json, os, re, sys
try:
	items = json.loads(os.environ["CLAUDE_CAPACITY_WAIT_ITEMS"])
	report = json.load(sys.stdin)
except ValueError:
	sys.exit(0)
count = items.get("count") if isinstance(items, dict) else None
entries = items.get("items") if isinstance(items, dict) else None
if not isinstance(count, int) or count <= 0 or not isinstance(entries, list):
	sys.exit(0)
server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
repo = os.environ.get("GITHUB_REPOSITORY", "")
since = str(items.get("opened_at") or "unknown")
reset = str(report.get("next_usable_at") or "unknown")
reset_in = str(report.get("next_usable_in") or "")
if reset_in:
	reset += f" ({reset_in})"
lines = [f"Claude pool exhausted: {count} item(s) waiting since {since}; earliest reset {reset}."]
for entry in entries[:10]:
	if not isinstance(entry, dict):
		continue
	kind, item, run = str(entry.get("kind")), str(entry.get("item")), str(entry.get("run"))
	if kind == "issue" and re.fullmatch(r"[0-9]+", item):
		lines.append(f"- {server}/{repo}/issues/{item}")
	elif kind == "pr" and re.fullmatch(r"[0-9]+", item):
		lines.append(f"- {server}/{repo}/pull/{item}")
	elif re.fullmatch(r"[0-9]+", run):
		lines.append(f"- {server}/{repo}/actions/runs/{run}")
if count > 10:
	lines.append(f"... and {count - 10} more")
print(str(count))
print("\n".join(lines))
' 2>/dev/null)" || waiting_text=""
	if [ -n "${waiting_text}" ]; then
		waiting_count="${waiting_text%%$'\n'*}"
		waiting_text="${waiting_text#*$'\n'}"
	fi
fi
read -r accounts gated auth_failed probe_failed alert claude_alert chatgpt_accounts chatgpt_alerts chatgpt_warning < <(printf '%s' "${report}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
flag = lambda key, default=False: "true" if r.get(key, default) else "false"
print(
	r["accounts"], len(r["gated"]), len(r["auth_failed"]), len(r["probe_failed"]), flag("alert"),
	flag("claude_alert", r["alert"]), int(r.get("chatgpt_accounts") or 0), len(r.get("chatgpt_alerts") or []), flag("chatgpt_warning"),
)
')
[[ "${chatgpt_accounts:-}" =~ ^[0-9]+$ ]] || chatgpt_accounts=0
[[ "${chatgpt_alerts:-}" =~ ^[0-9]+$ ]] || chatgpt_alerts=0
if [ "${claude_probes_empty}" = "true" ] && [ "${chatgpt_accounts}" -eq 0 ]; then
	pool_health_log 0 0 0 0 no_probes
	exit 0
fi
if [ "${alert}" != "true" ] && [ "${waiting_count}" -gt 0 ]; then
	alert=true
fi
if [ "${alert}" != "true" ]; then
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" none
	exit 0
fi

window="${CLAUDE_POOL_HEALTH_WINDOW_MINUTES:-5}"
[[ "${window}" =~ ^[0-9]+$ ]] || window=5
minute=$((10#$(date -u +%M)))
# The capacity-resume step sets CLAUDE_CAPACITY_WAIT_ITEMS only when it ran,
# i.e. in orchestrate_poll.yml's pool_window (which also opens an hour after
# the last pass), so its waiting-items notice is not dropped here (#7370).
if [ "${minute}" -ge "${window}" ] && [ "${waiting_count}" -eq 0 ]; then
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" outside_window
	exit 0
fi

text="$(printf '%s' "${report}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["text"])')"
if [ -n "${waiting_text}" ]; then
	text="${waiting_text}${text:+
${text}}"
fi
# Telegram rejects messages over 4096 characters, and a rejected send loses
# the whole alert. Cut the text before the run link is added (the Claude part
# comes first, so it survives) and keep the full text in the job log.
if [ "${#text}" -gt 3700 ]; then
	printf '::group::CLAUDE_POOL_HEALTH full alert\n%s\n::endgroup::\n' "${text//::/: :}"
	text="${text:0:3700}
… (truncated: the alert is longer than 3700 characters; see the run log)"
fi
if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
	text="${text}
Probed by ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi
# tg_send_msg returns success without a message id when the level is filtered,
# credentials are missing or the request fails; only a message id means sent.
# Gated accounts reset on their own and roles fall back meanwhile (DEBUG);
# a rejected token or a failed probe needs a person (WARNING). ChatGPT: a
# rejected login, a failed or stale refresh needs a person (WARNING); usage at
# the gate resets on its own (DEBUG). The Claude rule applies whenever the
# message is sent for Claude (an alert or waiting items), as before; a message
# sent only for ChatGPT takes the ChatGPT level.
claude_send=false
if [ "${claude_alert}" = "true" ] || [ "${waiting_count}" -gt 0 ]; then
	claude_send=true
fi
pool_health_level="DEBUG"
if { [ "${claude_send}" = "true" ] && { [ "${auth_failed}" -gt 0 ] || [ "${probe_failed}" -gt 0 ]; }; } || [ "${chatgpt_warning}" = "true" ]; then
	pool_health_level="WARNING"
fi
# Backstop for the 4096-character Telegram limit (the level prefix is short).
text="${text:0:3900}"
message_id="$(tg_send_msg "${text}" "${pool_health_level}" 2>/dev/null)" || message_id=""
if [ -n "${message_id}" ]; then
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" sent
else
	pool_health_log "${accounts}" "${gated}" "${auth_failed}" "${probe_failed}" not_delivered
fi
exit 0
