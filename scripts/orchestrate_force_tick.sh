#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/memory_helpers.sh" ]; then
	# shellcheck source=memory_helpers.sh
	source "${SCRIPT_DIR}/memory_helpers.sh" 2>/dev/null || true
fi

_force_tick_truthy()
{
	local value="${1:-}"
	value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
	case "${value}" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

_force_tick_now_utc()
{
	python3 - <<'PY'
from datetime import datetime, timezone

print(datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
PY
}

_force_tick_tracking_issue_from_ref()
{
	local ref="${1:-}"
	if [[ "${ref}" =~ ^orchestrator/project-([0-9]+)$ ]]; then
		printf '%s\n' "${BASH_REMATCH[1]}"
	fi
}

_force_tick_extract_tracking_issue()
{
	local body="${1:-}"
	python3 - <<'PY' "${body}"
import re
import sys

body = sys.argv[1]
match = re.search(r"(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)", body, re.MULTILINE)
if match:
	print(match.group(1))
PY
}

_force_tick_fetch_pull_request_json()
{
	local repository="${1:-}"
	local pr_number="${2:-}"

	if [ -z "${repository}" ] || [ -z "${pr_number}" ]; then
		return 1
	fi

	GH_TOKEN="${GH_PAT:-${GH_TOKEN:-}}" gh api "repos/${repository}/pulls/${pr_number}" 2>/dev/null
}

_force_tick_extract_tracking_issue_from_pull_request_json()
{
	local pr_json="${1:-}"
	python3 - <<'PY' "${pr_json}"
import json
import re
import sys

payload = {}
if sys.argv[1]:
	try:
		payload = json.loads(sys.argv[1])
	except json.JSONDecodeError:
		payload = {}

branch_re = re.compile(r"^orchestrator/project-(\d+)$")
body_re = re.compile(r"(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)")

for ref in (
	((payload.get("head") or {}).get("ref") or "").strip(),
	((payload.get("base") or {}).get("ref") or "").strip(),
):
	match = branch_re.fullmatch(ref)
	if match:
		print(match.group(1))
		raise SystemExit(0)

body = payload.get("body") or ""
match = body_re.search(body)
if match:
	print(match.group(1))
PY
}

_force_tick_fetch_issue_body()
{
	local repository="${1:-}"
	local issue_number="${2:-}"

	if [ -z "${repository}" ] || [ -z "${issue_number}" ]; then
		return 1
	fi

	# Call GitHub only when the caller did not already supply a tracking issue:
	# the phase-end callsites do not consistently retain the issue/PR body.
	GH_TOKEN="${GH_PAT:-${GH_TOKEN:-}}" gh api "repos/${repository}/issues/${issue_number}" --jq '.body // ""' 2>/dev/null
}

_force_tick_latest_gate_tsv()
{
	local wrapper_json="${1:-}"
	local cooldown_seconds="${2:-30}"

	python3 - <<'PY' "${wrapper_json}" "${cooldown_seconds}"
import datetime as dt
import json
import sys

try:
	wrapper = json.loads(sys.argv[1]) if sys.argv[1] else {}
except json.JSONDecodeError:
	wrapper = {}
try:
	cooldown = int(sys.argv[2]) if sys.argv[2] else 30
except ValueError:
	cooldown = 30
record = wrapper.get("record") or {}
status = record.get("dispatch_status") or ""
if status == "disabled":
	candidate = record.get("last_dispatch_timestamp") or ""
else:
	candidate = record.get("last_attempted_timestamp") or record.get("last_dispatch_timestamp") or ""
within = False
age = ""
if candidate:
	try:
		parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
		age_value = max(0, int((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()))
		age = str(age_value)
		within = age_value < cooldown
	except ValueError:
		print("force_tick_latest_gate_tsv: unparseable timestamp", file=sys.stderr)
		candidate = ""
		age = ""
print(f"{'true' if within else 'false'}\t{candidate}\t{age}")
PY
}

_force_tick_render_record_file()
{
	local output_file="${1:?output file required}"
	local wrapper_json="${2:-}"
	local tracking_issue="${3:?tracking issue required}"
	local attempt_timestamp="${4:?attempt timestamp required}"
	local payload_json="${5:?payload json required}"
	local dispatch_status="${6:?dispatch status required}"
	local update_dispatch="${7:-false}"

	python3 - <<'PY' "${output_file}" "${wrapper_json}" "${tracking_issue}" "${attempt_timestamp}" "${payload_json}" "${dispatch_status}" "${update_dispatch}"
import json
import pathlib
import sys

output_file = pathlib.Path(sys.argv[1])
try:
	wrapper = json.loads(sys.argv[2]) if sys.argv[2] else {}
except json.JSONDecodeError:
	wrapper = {}
tracking_issue = int(sys.argv[3])
attempt_timestamp = sys.argv[4]
try:
	payload = json.loads(sys.argv[5])
except json.JSONDecodeError:
	payload = {}
dispatch_status = sys.argv[6]
update_dispatch = sys.argv[7].lower() == "true"

previous = wrapper.get("record") or {}
record = {
	"schema_version": previous.get("schema_version") or "force_tick.v1",
	"tracking_issue": tracking_issue,
	"last_attempted_timestamp": attempt_timestamp,
	"last_attempt_payload": payload,
	"dispatch_status": dispatch_status,
	"last_dispatch_timestamp": previous.get("last_dispatch_timestamp"),
	"last_dispatch_payload": previous.get("last_dispatch_payload"),
}
if update_dispatch:
	record["last_dispatch_timestamp"] = attempt_timestamp
	record["last_dispatch_payload"] = payload

output_file.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
PY
}

_force_tick_json_bool()
{
	local wrapper_json="${1:-}"
	local key="${2:?key required}"
	python3 - <<'PY' "${wrapper_json}" "${key}"
import json
import sys

try:
	payload = json.loads(sys.argv[1]) if sys.argv[1] else {}
except json.JSONDecodeError:
	payload = {}
print("true" if payload.get(sys.argv[2]) is True else "false")
PY
}

REPO_ROOT=""
REPOSITORY="${GITHUB_REPOSITORY:-}"
TRACKING_ISSUE=""
ISSUE_NUMBER=""
REASON="force-tick"
SOURCE_WORKFLOW="${GITHUB_WORKFLOW:-orchestrate_force_tick}"
RUN_ID="${GITHUB_RUN_ID:-}"
pr_lookup_failed="false"
issue_lookup_failed="false"

while [ $# -gt 0 ]; do
	case "$1" in
		--repo-root)
			REPO_ROOT="${2:-}"
			shift 2
			;;
		--repo)
			REPOSITORY="${2:-}"
			shift 2
			;;
		--tracking-issue)
			TRACKING_ISSUE="${2:-}"
			shift 2
			;;
		--issue)
			ISSUE_NUMBER="${2:-}"
			shift 2
			;;
		--reason)
			REASON="${2:-}"
			shift 2
			;;
		--source-workflow)
			SOURCE_WORKFLOW="${2:-}"
			shift 2
			;;
		--run-id)
			RUN_ID="${2:-}"
			shift 2
			;;
		*)
			echo "::error::Unknown orchestrate_force_tick.sh argument: $1" >&2
			exit 2
			;;
		esac
	done

if ! [[ "${TRACKING_ISSUE:-}" =~ ^[0-9]+$ ]] || [ "${TRACKING_ISSUE:-0}" -le 0 ]; then
	TRACKING_ISSUE=""
fi
if ! [[ "${ISSUE_NUMBER:-}" =~ ^[0-9]+$ ]] || [ "${ISSUE_NUMBER:-0}" -le 0 ]; then
	ISSUE_NUMBER=""
fi

if [ -z "${TRACKING_ISSUE}" ] && [ -n "${ISSUE_NUMBER}" ]; then
	pr_json=""
	if ! pr_json="$(_force_tick_fetch_pull_request_json "${REPOSITORY}" "${ISSUE_NUMBER}")"; then
		pr_lookup_failed="true"
		pr_json=""
	fi
	TRACKING_ISSUE="$(_force_tick_extract_tracking_issue_from_pull_request_json "${pr_json}")"
	if ! [[ "${TRACKING_ISSUE:-}" =~ ^[0-9]+$ ]] || [ "${TRACKING_ISSUE:-0}" -le 0 ]; then
		TRACKING_ISSUE=""
	fi
fi

if [ -z "${TRACKING_ISSUE}" ] && [ -n "${ISSUE_NUMBER}" ]; then
	issue_body=""
	if ! issue_body="$(_force_tick_fetch_issue_body "${REPOSITORY}" "${ISSUE_NUMBER}")"; then
		issue_lookup_failed="true"
		issue_body=""
	fi
	TRACKING_ISSUE="$(_force_tick_extract_tracking_issue "${issue_body}")"
	if ! [[ "${TRACKING_ISSUE:-}" =~ ^[0-9]+$ ]] || [ "${TRACKING_ISSUE:-0}" -le 0 ]; then
		TRACKING_ISSUE=""
	fi
fi

if [ -z "${TRACKING_ISSUE}" ]; then
	if [ "${pr_lookup_failed}" = "true" ] && [ "${issue_lookup_failed}" = "true" ]; then
		echo "::warning::No tracking issue resolved for force-tick dispatch after GitHub metadata lookup failed; skipping fast-follow tick."
	else
		echo "No tracking issue resolved for force-tick dispatch; skipping."
	fi
	exit 0
fi

cooldown_seconds="${FORCE_TICK_COOLDOWN_SECONDS:-30}"
if ! [[ "${cooldown_seconds}" =~ ^[0-9]+$ ]]; then
	cooldown_seconds=30
fi

payload_json="$(python3 - <<'PY' "${REASON}" "${SOURCE_WORKFLOW}" "${ISSUE_NUMBER:-${TRACKING_ISSUE}}" "${RUN_ID}"
import json
import sys

reason, source_workflow, issue_value, run_id_value = sys.argv[1:5]

def _coerce(value: str):
	if not value:
		return None
	if value.isdigit():
		return int(value)
	return value

print(json.dumps({
	"reason": reason,
	"source_workflow": source_workflow,
	"issue": _coerce(issue_value),
	"run_id": _coerce(run_id_value),
}, sort_keys=True))
PY
)"

record_wrapper='{"ok": true, "enabled": true, "hit": false, "record": null}'
if declare -F memory_force_tick_get >/dev/null 2>&1; then
	if ! record_wrapper="$(memory_force_tick_get \
		--repo-root "${REPO_ROOT}" \
		--repo "${REPOSITORY}" \
		--tracking-issue "${TRACKING_ISSUE}")"; then
		echo "::warning::force-tick memory read failed; continuing without cooldown state."
		record_wrapper='{"ok": true, "enabled": true, "hit": false, "record": null}'
	fi
fi

IFS=$'\t' read -r within_cooldown last_gate_timestamp last_gate_age < <(_force_tick_latest_gate_tsv "${record_wrapper}" "${cooldown_seconds}")
if [ "${within_cooldown}" = "true" ]; then
	echo "Skipping force-tick dispatch for tracking issue #${TRACKING_ISSUE}: cooldown active (${last_gate_age:-0}s < ${cooldown_seconds}s; last_gate=${last_gate_timestamp:-unknown})."
	exit 0
fi

attempt_timestamp="$(_force_tick_now_utc)"
pending_record_file=""
final_record_file=""

cleanup_force_tick_files()
{
	rm -f "${pending_record_file:-}" "${final_record_file:-}"
}
trap cleanup_force_tick_files EXIT

if ! _force_tick_truthy "${FORCE_TICK_ENABLED:-true}"; then
	final_record_file="$(mktemp)"
	_force_tick_render_record_file \
		"${final_record_file}" \
		"${record_wrapper}" \
		"${TRACKING_ISSUE}" \
		"${attempt_timestamp}" \
		"${payload_json}" \
		"disabled" \
		"false"
	if declare -F memory_force_tick_put >/dev/null 2>&1; then
		memory_force_tick_put \
			--repo-root "${REPO_ROOT}" \
			--repo "${REPOSITORY}" \
			--tracking-issue "${TRACKING_ISSUE}" \
			--record-file "${final_record_file}" >/dev/null || true
	fi
	echo "Force-tick dispatch is disabled; skipping."
	exit 0
fi

pending_record_file="$(mktemp)"
_force_tick_render_record_file \
	"${pending_record_file}" \
	"${record_wrapper}" \
	"${TRACKING_ISSUE}" \
	"${attempt_timestamp}" \
	"${payload_json}" \
	"pending" \
	"false"

claim_stored="false"
if declare -F memory_force_tick_put >/dev/null 2>&1; then
	claim_result='{"ok": true, "enabled": true, "stored": false, "record": null}'
	if ! claim_result="$(memory_force_tick_put \
		--repo-root "${REPO_ROOT}" \
		--repo "${REPOSITORY}" \
		--tracking-issue "${TRACKING_ISSUE}" \
		--record-file "${pending_record_file}")"; then
		echo "::warning::force-tick memory claim failed unexpectedly; dispatching without persisted cooldown claim."
	fi
	claim_stored="$(_force_tick_json_bool "${claim_result}" stored)"
	if [ "${claim_stored}" != "true" ]; then
		peer_wrapper='{"ok": true, "enabled": true, "hit": false, "record": null}'
		if ! peer_wrapper="$(memory_force_tick_get \
			--repo-root "${REPO_ROOT}" \
			--repo "${REPOSITORY}" \
			--tracking-issue "${TRACKING_ISSUE}")"; then
			echo "::warning::force-tick memory re-read failed; continuing without peer cooldown confirmation."
		fi
		IFS=$'\t' read -r peer_within _peer_timestamp _peer_age < <(_force_tick_latest_gate_tsv "${peer_wrapper}" "${cooldown_seconds}")
		if [ "${peer_within}" = "true" ]; then
			echo "Skipping force-tick dispatch for tracking issue #${TRACKING_ISSUE}: another run already claimed the cooldown window."
			exit 0
		fi
		echo "::warning::force-tick memory claim failed; dispatching without persisted cooldown claim."
	fi
fi

poll_workflow="${ORCHESTRATE_POLL_WORKFLOW_FILE:-internal-orchestrate-poll.yml}"
dispatch_status="failed"
if [ -z "${REPOSITORY}" ] || [ -z "${GH_PAT:-${GH_TOKEN:-}}" ]; then
	echo "::warning::Skipping force-tick dispatch: repository or GitHub token is unavailable."
else
	if GH_TOKEN="${GH_PAT:-${GH_TOKEN:-}}" gh workflow run "${poll_workflow}" --repo "${REPOSITORY}" >/dev/null 2>&1; then
		dispatch_status="sent"
		echo "Dispatched ${poll_workflow} for tracking issue #${TRACKING_ISSUE}."
	else
		echo "::warning::Failed to dispatch ${poll_workflow} for tracking issue #${TRACKING_ISSUE}; cron remains the fallback."
	fi
fi

if declare -F memory_force_tick_put >/dev/null 2>&1; then
	final_record_file="$(mktemp)"
	_force_tick_render_record_file \
		"${final_record_file}" \
		"${record_wrapper}" \
		"${TRACKING_ISSUE}" \
		"${attempt_timestamp}" \
		"${payload_json}" \
		"${dispatch_status}" \
		"$( [ "${dispatch_status}" = "sent" ] && printf 'true' || printf 'false' )"
	memory_force_tick_put \
		--repo-root "${REPO_ROOT}" \
		--repo "${REPOSITORY}" \
		--tracking-issue "${TRACKING_ISSUE}" \
		--record-file "${final_record_file}" >/dev/null || true
fi

exit 0
