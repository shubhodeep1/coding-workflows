#!/usr/bin/env bash
# stage_switch_watch.sh — daily report of pipeline stage switches left off
# (issue #9994). Many stages read an on/off repository variable
# (`vars.<NAME>_ENABLED || 'true'`); when one is left `false` the stage logs
# `outcome=disabled` and returns success, so nobody notices. This wrapper runs
# in .github/workflows/workflow-log-analysis.yml (job collect-logs, daily
# 06:00 UTC) and never fails the job.
#
# Subcommands (each in its own workflow step, so GH_PAT and TG_BOT_SECRET
# never share a step):
#   read    one paginated `actions/variables` listing (GH_TOKEN = GH_PAT),
#           filtered to the names in scripts/stage_switches.json and written
#           as a sanitized snapshot (no raw values) to
#           ${RUNNER_TEMP}/stage-switch-watch/snapshot.json.
#           GITHUB_OUTPUT: day=<YYYY-MM-DD UTC>, snapshot_ready=true|false
#   report  evaluates the snapshot, writes a step-summary table and sends one
#           Telegram WARNING per switch off for longer than the wait time,
#           unless today's alert was already sent (cache marker) or the switch
#           is marked off on purpose in the registry's `off_on_purpose` list.
#           GITHUB_OUTPUT: alerted=true when at least one message was
#           delivered. A run in which no message was delivered writes no day
#           marker, so a later run that day retries; a partly delivered run
#           warns and its failed alerts wait for the next day's run (a retry
#           would resend the delivered ones). The marker covers the whole
#           watch: a switch that passes its wait time after the day's first
#           alert waits for the next day's run.
#
# Env:
#   STAGE_SWITCH_WATCH_ENABLED     `true` runs the watch (default true)
#   STAGE_SWITCH_OFF_ALERT_HOURS   hours a switch must be off before it alerts (default 24)
#   STAGE_SWITCH_ALERTED_TODAY     `true` when today's alert marker exists (default false)
#   STAGE_SWITCH_REGISTRY          registry path (default scripts/stage_switches.json)
#   STAGE_SWITCH_SNAPSHOT_DIR      snapshot directory (default ${RUNNER_TEMP}/stage-switch-watch)
#   STAGE_SWITCH_NOW               ISO timestamp override (tests; default now)
#   STAGE_SWITCH_ALERT_MARKER_DIR  day-marker directory the cache step saves
#                                  (default ${RUNNER_TEMP}/stage-switch-alert-day)
#   GH_TOKEN, GITHUB_REPOSITORY    read only
#   TG_BOT_SECRET / TG_ADMIN_CHAT_ID / ALERT_MSG_LEVEL   scripts/tg_helpers.sh (report only)
#
# Log: STAGE_SWITCH_WATCH outcome=disabled|read_failed|registry_invalid|snapshot_ready|ok ...
#      STAGE_SWITCH_OFF name=... value=... default=... off_since=... hours_off=... status=... marker=...
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
stage_switch_registry="${STAGE_SWITCH_REGISTRY:-${script_dir}/stage_switches.json}"
stage_switch_dir="${STAGE_SWITCH_SNAPSHOT_DIR:-${RUNNER_TEMP:-/tmp}/stage-switch-watch}"
stage_switch_snapshot="${stage_switch_dir}/snapshot.json"
stage_switch_output="${GITHUB_OUTPUT:-/dev/null}"
stage_switch_alert_marker_dir="${STAGE_SWITCH_ALERT_MARKER_DIR:-${RUNNER_TEMP:-/tmp}/stage-switch-alert-day}"

stage_switch_enabled()
{
	[ "$(printf '%s' "${STAGE_SWITCH_WATCH_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" = "true" ]
}

stage_switch_read()
{
	echo "day=$(date -u +%F)" >> "${stage_switch_output}" 2>/dev/null || true
	if ! stage_switch_enabled; then
		echo "STAGE_SWITCH_WATCH outcome=disabled"
		echo "snapshot_ready=false" >> "${stage_switch_output}" 2>/dev/null || true
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		echo "::warning::STAGE_SWITCH_WATCH cannot read repository variables: GH_TOKEN (GH_PAT) or GITHUB_REPOSITORY is empty."
		echo "STAGE_SWITCH_WATCH outcome=read_failed reason=no_token"
		echo "snapshot_ready=false" >> "${stage_switch_output}" 2>/dev/null || true
		return 0
	fi
	if ! mkdir -p "${stage_switch_dir}"; then
		echo "::warning::STAGE_SWITCH_WATCH cannot create ${stage_switch_dir}."
		echo "STAGE_SWITCH_WATCH outcome=read_failed reason=workdir"
		echo "snapshot_ready=false" >> "${stage_switch_output}" 2>/dev/null || true
		return 0
	fi
	local listing="${stage_switch_dir}/listing.jsonl" rc
	# §14 audit: this job makes no other variables call. The collect step
	# uses github.token, which cannot read repository variables, and
	# `toJSON(vars)` through `env:` would print every variable's value in the
	# step's log header and has no `updated_at` for the wait time. One
	# paginated listing (ceil(N/30) calls) per run is the smallest call that
	# gives both; the raw listing is deleted right after the snapshot.
	# `tojson` keeps one compact object per line: `gh api --jq` pretty-prints
	# object results across several lines (see scripts/review_merge_train.sh),
	# and the snapshot command parses the listing line by line.
	gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/variables?per_page=30" \
		--jq '.variables[] | {name, value, updated_at} | tojson' > "${listing}" 2>"${stage_switch_dir}/listing.err"
	rc=$?
	if [ "${rc}" -ne 0 ]; then
		local gh_error=""
		# gh's error text (never the stdout listing, which can hold values):
		# first line, printable characters only, at most 200 characters.
		gh_error="$(head -n 1 "${stage_switch_dir}/listing.err" 2>/dev/null | tr -cd '[:print:]' | cut -c1-200)" || gh_error=""
		rm -f "${listing}" "${stage_switch_dir}/listing.err"
		echo "::warning::STAGE_SWITCH_WATCH repository variable listing failed (gh exit ${rc}: ${gh_error:-no error text}); check rate limits and API status, and that GH_PAT has repo scope or Variables: read."
		echo "STAGE_SWITCH_WATCH outcome=read_failed reason=gh_api rc=${rc}"
		echo "snapshot_ready=false" >> "${stage_switch_output}" 2>/dev/null || true
		return 0
	fi
	rm -f "${stage_switch_dir}/listing.err"
	PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/stage_switch_watch.py" snapshot \
		--registry "${stage_switch_registry}" < "${listing}" > "${stage_switch_snapshot}.tmp"
	rc=$?
	# The raw listing holds every variable's value; never keep it.
	rm -f "${listing}"
	if [ "${rc}" -ne 0 ]; then
		rm -f "${stage_switch_snapshot}.tmp"
		if [ "${rc}" -eq 2 ]; then
			echo "::warning::STAGE_SWITCH_WATCH registry ${stage_switch_registry} is invalid."
			echo "STAGE_SWITCH_WATCH outcome=registry_invalid"
		else
			echo "::warning::STAGE_SWITCH_WATCH repository variable listing could not be parsed."
			echo "STAGE_SWITCH_WATCH outcome=read_failed reason=parse"
		fi
		echo "snapshot_ready=false" >> "${stage_switch_output}" 2>/dev/null || true
		return 0
	fi
	mv -f "${stage_switch_snapshot}.tmp" "${stage_switch_snapshot}"
	echo "STAGE_SWITCH_WATCH outcome=snapshot_ready"
	echo "snapshot_ready=true" >> "${stage_switch_output}" 2>/dev/null || true
	return 0
}

stage_switch_report()
{
	if ! stage_switch_enabled; then
		echo "STAGE_SWITCH_WATCH outcome=disabled"
		return 0
	fi
	if [ ! -f "${stage_switch_snapshot}" ]; then
		echo "::warning::STAGE_SWITCH_WATCH snapshot ${stage_switch_snapshot} is missing."
		echo "STAGE_SWITCH_WATCH outcome=read_failed reason=no_snapshot"
		return 0
	fi
	local result rc wait_hours
	wait_hours="${STAGE_SWITCH_OFF_ALERT_HOURS:-24}"
	result="$(PYTHONDONTWRITEBYTECODE=1 python3 "${script_dir}/stage_switch_watch.py" evaluate \
		--registry "${stage_switch_registry}" --snapshot "${stage_switch_snapshot}" \
		--now "${STAGE_SWITCH_NOW:-}" --wait-hours "${wait_hours}")"
	rc=$?
	if [ "${rc}" -ne 0 ] || [ -z "${result}" ]; then
		echo "::warning::STAGE_SWITCH_WATCH evaluation failed (exit ${rc})."
		if [ "${rc}" -eq 2 ]; then
			echo "STAGE_SWITCH_WATCH outcome=registry_invalid"
		else
			echo "STAGE_SWITCH_WATCH outcome=read_failed reason=evaluate"
		fi
		return 0
	fi

	local counts off_count waiting_count on_purpose_count alert_count
	counts="$(printf '%s' "${result}" | jq -r '[.counts.off, .counts.waiting, .counts.on_purpose, .counts.alerts] | map(tostring) | join(" ")' 2>/dev/null)" || counts=""
	if [ -z "${counts}" ]; then
		echo "::warning::STAGE_SWITCH_WATCH could not parse the evaluation counts; the counts below read 0 and alerts are skipped this run (no day marker is written, so a later run retries)."
	fi
	read -r off_count waiting_count on_purpose_count alert_count <<< "${counts:-0 0 0 0}"

	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		{
			echo "### Stage switches left off"
			echo
			if [ "${off_count:-0}" = "0" ]; then
				echo "No stage switch is off."
			else
				echo "| Variable | Value | Default | Off since | Hours off | Status |"
				echo "| --- | --- | --- | --- | --- | --- |"
				printf '%s' "${result}" | jq -r '.off[] | "| `\(.name)` | `\(.display_value)` | `\(.default)` | \(.off_since) | \(.hours_off // "unknown") | \(.status)\(if .marker == "none" then "" else " (marker \(.marker), until \(.until))" end) |"' 2>/dev/null
				echo
				echo "Mark a switch off on purpose in \`scripts/stage_switches.json\` \`off_on_purpose\` (with an \`until\` date)."
			fi
		} >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
	fi

	local alert_state="none"
	if [ "${alert_count:-0}" != "0" ]; then
		if [ "$(printf '%s' "${STAGE_SWITCH_ALERTED_TODAY:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
			alert_state="already_sent_today"
		else
			if [ -f "${script_dir}/tg_helpers.sh" ]; then
				# shellcheck disable=SC1091
				source "${script_dir}/tg_helpers.sh"
			fi
			if ! type tg_alert_configured >/dev/null 2>&1 || ! type tg_send_msg >/dev/null 2>&1 || ! tg_alert_configured WARNING; then
				alert_state="telegram_unconfigured"
			else
				local run_url msg msg_id delivered=0 attempted=0 idx
				run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
				for ((idx = 0; idx < alert_count; idx++)); do
					msg="$(printf '%s' "${result}" | jq -r --argjson i "${idx}" --arg repo "${GITHUB_REPOSITORY:-}" --arg run "${run_url}" '
						.alerts[$i] as $a
						| "Stage switch left off in \($repo): \($a.name)\n"
						+ "Value: \($a.display_value) (default \($a.default))\n"
						+ "Stage: \($a.stage)\n"
						+ "Off since: \($a.off_since) (\(if $a.hours_off == null then "unknown" else "\($a.hours_off)h" end))"
						+ (if $a.marker == "expired" then "\nOff-on-purpose marker expired on \($a.until)." else "" end)
						+ "\nIf this is on purpose, add it to scripts/stage_switches.json off_on_purpose with an until date; otherwise delete the variable or set it to true."
						+ "\nRun: \($run)"' 2>/dev/null)" || msg=""
					[ -n "${msg}" ] || continue
					attempted=$((attempted + 1))
					# tg_send_msg's stderr (fixed text or Telegram's sanitized description,
					# never the token) stays in the log so a failed send shows why.
					msg_id="$(tg_send_msg "${msg}" WARNING)" || msg_id=""
					[ -n "${msg_id}" ] && delivered=$((delivered + 1))
				done
				if [ "${delivered}" -gt 0 ]; then
					# Marker file the workflow's actions/cache/save step stores
					# under stage-switch-off-alert-<day>. Only after a delivered
					# send, so a run where every send failed is retried later
					# that day; a partly delivered run is not (see the warning).
					mkdir -p "${stage_switch_alert_marker_dir}" 2>/dev/null \
						&& date -u +%F > "${stage_switch_alert_marker_dir}/day.txt" 2>/dev/null || true
					echo "alerted=true" >> "${stage_switch_output}" 2>/dev/null || true
				fi
				if [ "${attempted}" -gt 0 ]; then
					if [ "${delivered}" -eq "${attempted}" ]; then
						alert_state="sent"
					else
						alert_state="not_delivered"
						if [ "${delivered}" -gt 0 ]; then
							echo "::warning::STAGE_SWITCH_WATCH delivered ${delivered} of ${attempted} alerts; today's marker is saved, so the undelivered alerts repeat at the next day's run."
						fi
					fi
				fi
			fi
		fi
	fi
	echo "STAGE_SWITCH_WATCH outcome=ok off=${off_count:-0} waiting=${waiting_count:-0} on_purpose=${on_purpose_count:-0} alerts=${alert_count:-0} alert=${alert_state}"
	return 0
}

case "${1:-}" in
	read) stage_switch_read ;;
	report) stage_switch_report ;;
	*)
		echo "::warning::stage_switch_watch.sh: unknown subcommand '${1:-}' (expected read|report)."
		;;
esac
exit 0
