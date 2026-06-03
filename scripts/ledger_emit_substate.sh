#!/usr/bin/env bash
set -euo pipefail

ledger_substate_warn()
{
	printf '::warning::ledger_emit_substate %s\n' "$*" >&2
}

ledger_substates_enabled()
{
	local enabled
	enabled="$(printf '%s' "${LEDGER_SUBSTATES_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${enabled}" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

resolve_ai_memory_script()
{
	local script_dir repo_root candidate

	script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

	for candidate in \
		"${LEDGER_AI_MEMORY_SCRIPT:-}" \
		"${script_dir}/ai_memory.py" \
		"${repo_root}/scripts/ai_memory.py"; do
		[ -n "${candidate}" ] || continue
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

build_metadata_payload()
{
	LEDGER_METADATA_JSON="${metadata_json}" \
	LEDGER_PHASE="${phase}" \
	LEDGER_MODE="${mode}" \
	LEDGER_SUBSTATE="${substate}" \
	LEDGER_ATTEMPT="${attempt}" \
	LEDGER_LANE="${lane}" \
	LEDGER_MODEL="${model}" \
	LEDGER_TOKENS_INPUT="${tokens_input}" \
	LEDGER_TOKENS_OUTPUT="${tokens_output}" \
	LEDGER_TOKENS_TOTAL="${tokens_total}" \
	LEDGER_TOKENS_LOG_FILE="${tokens_log_file}" \
	PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
import os
import re
from pathlib import Path


def parse_int(value):
	if value is None:
		return None
	text = str(value).strip()
	if not text or text.lower() in {"na", "null", "none", "-"}:
		return None
	text = text.replace(",", "")
	if re.fullmatch(r"[0-9]+", text):
		return int(text)
	return None


def find_usage_dict(payload):
	if isinstance(payload, dict):
		usage = payload.get("usage")
		if isinstance(usage, dict):
			return usage
		for value in payload.values():
			found = find_usage_dict(value)
			if isinstance(found, dict):
				return found
	elif isinstance(payload, list):
		for value in payload:
			found = find_usage_dict(value)
			if isinstance(found, dict):
				return found
	return None


def parse_usage_from_text(text):
	decoder = json.JSONDecoder()
	usage = None
	index = 0
	text_length = len(text)
	while index < text_length:
		next_object_start = text.find("{", index)
		if next_object_start == -1:
			break
		try:
			payload, end = decoder.raw_decode(text, next_object_start)
		except json.JSONDecodeError:
			index = next_object_start + 1
			continue
		found = find_usage_dict(payload)
		if isinstance(found, dict):
			usage = found
		index = max(end, next_object_start + 1)
	if isinstance(usage, dict):
		return {
			"input": parse_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
			"output": parse_int(usage.get("completion_tokens") or usage.get("output_tokens")),
			"total": parse_int(usage.get("total_tokens")),
		}
	return {}


def parse_openrouter_usage_line(text):
	matched_line = ""
	for line in text.splitlines():
		if "openrouter usage" in line.lower():
			matched_line = line
	if not matched_line:
		return {}
	result = {}
	for key, target in (("prompt_tokens", "input"), ("completion_tokens", "output"), ("total_tokens", "total")):
		match = re.search(rf"{key}=([0-9][0-9,]*|na|null|none|-)", matched_line, flags=re.IGNORECASE)
		if match:
			result[target] = parse_int(match.group(1))
	return result


def parse_tokens_used(text):
	matches = re.findall(r"tokens used[^0-9]*([0-9][0-9,]*)", text, flags=re.IGNORECASE)
	if not matches:
		return None
	return parse_int(matches[-1])


metadata = json.loads(os.environ.get("LEDGER_METADATA_JSON", "{}") or "{}")
if not isinstance(metadata, dict):
	raise SystemExit("metadata JSON must be an object")

metadata["phase"] = os.environ["LEDGER_PHASE"]
metadata["mode"] = os.environ["LEDGER_MODE"]

substate = os.environ.get("LEDGER_SUBSTATE", "").strip()
if substate:
	metadata["run_substate"] = substate

attempt = parse_int(os.environ.get("LEDGER_ATTEMPT"))
if attempt is not None:
	metadata["attempt"] = attempt

lane = os.environ.get("LEDGER_LANE", "").strip()
if lane:
	metadata["lane"] = lane

model = os.environ.get("LEDGER_MODEL", "").strip()
if model:
	metadata["model"] = model

token_values = {
	"input": parse_int(os.environ.get("LEDGER_TOKENS_INPUT")),
	"output": parse_int(os.environ.get("LEDGER_TOKENS_OUTPUT")),
	"total": parse_int(os.environ.get("LEDGER_TOKENS_TOTAL")),
}

log_path_raw = os.environ.get("LEDGER_TOKENS_LOG_FILE", "").strip()
if log_path_raw:
	log_path = Path(log_path_raw)
	if log_path.exists():
		text = log_path.read_text(encoding="utf-8", errors="replace")
		for parsed in (parse_usage_from_text(text), parse_openrouter_usage_line(text)):
			for key, value in parsed.items():
				if token_values.get(key) is None and value is not None:
					token_values[key] = value
		tokens_used_total = parse_tokens_used(text)
		if token_values.get("total") is None and tokens_used_total is not None:
			token_values["total"] = tokens_used_total

if token_values.get("total") is None and token_values.get("input") is not None and token_values.get("output") is not None:
	token_values["total"] = token_values["input"] + token_values["output"]

token_payload = {key: value for key, value in token_values.items() if value is not None}
if token_payload:
	metadata["tokens"] = token_payload

print(json.dumps(metadata, ensure_ascii=True, sort_keys=True))
PY
}

emit_deduped_run_event()
{
	LEDGER_MARKER_DIR="${marker_dir}" \
	LEDGER_DEDUPE_KEY="${dedupe_key}" \
	LEDGER_AI_MEMORY_SCRIPT="${ai_memory_script}" \
	LEDGER_REPO_ROOT="${repo_root}" \
	LEDGER_RUN_ID="${run_id}" \
	LEDGER_WORKFLOW="${workflow}" \
	LEDGER_EVENT_TYPE="${event_type}" \
	LEDGER_STATUS="${status}" \
	LEDGER_MESSAGE="${message}" \
	LEDGER_ISSUE_NUMBER="${issue_number}" \
	LEDGER_PR_NUMBER="${pr_number}" \
	LEDGER_ACTOR="${actor}" \
	LEDGER_METADATA_PAYLOAD="${metadata_payload}" \
	PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


marker_dir = Path(os.environ["LEDGER_MARKER_DIR"])
dedupe_key = os.environ["LEDGER_DEDUPE_KEY"]
ai_memory_script = os.environ["LEDGER_AI_MEMORY_SCRIPT"]
marker_dir.mkdir(parents=True, exist_ok=True)
key_hash = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
lock_path = marker_dir / f"{key_hash}.lock"
state_path = marker_dir / f"{key_hash}.json"

with lock_path.open("a+", encoding="utf-8") as handle:
	fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
	if state_path.is_file():
		try:
			state = json.loads(state_path.read_text(encoding="utf-8"))
		except json.JSONDecodeError:
			state = {}
		if state.get("dedupe_key") == dedupe_key and state.get("status") == "emitted":
			print("duplicate")
			raise SystemExit(0)

	cmd = [
		sys.executable,
		ai_memory_script,
		"record-run-event",
		"--repo-root",
		os.environ["LEDGER_REPO_ROOT"],
		"--run-id",
		os.environ["LEDGER_RUN_ID"],
		"--workflow",
		os.environ["LEDGER_WORKFLOW"],
		"--event-type",
		os.environ["LEDGER_EVENT_TYPE"],
		"--status",
		os.environ["LEDGER_STATUS"],
		"--message",
		os.environ["LEDGER_MESSAGE"],
		"--issue-number",
		os.environ.get("LEDGER_ISSUE_NUMBER", ""),
		"--pr-number",
		os.environ.get("LEDGER_PR_NUMBER", ""),
		"--actor",
		os.environ["LEDGER_ACTOR"],
		"--metadata-json",
		os.environ["LEDGER_METADATA_PAYLOAD"],
	]
	completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
	if completed.returncode != 0:
		detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " | ")
		if detail:
			print(f"emit_failed:{completed.returncode}:{detail[:500]}")
		else:
			print(f"emit_failed:{completed.returncode}")
		raise SystemExit(0)

	state_path.write_text(
		json.dumps({"dedupe_key": dedupe_key, "status": "emitted"}, ensure_ascii=True, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	print("emitted")
PY
}

run_id=""
workflow=""
phase=""
mode=""
substate=""
attempt=""
lane=""
model=""
issue_number=""
pr_number=""
actor=""
event_type="run_substate"
status=""
message=""
metadata_json='{}'
tokens_input=""
tokens_output=""
tokens_total=""
tokens_log_file=""
repo_root=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--run-id)
			run_id="${2:-}"
			shift 2
			;;
		--workflow)
			workflow="${2:-}"
			shift 2
			;;
		--phase)
			phase="${2:-}"
			shift 2
			;;
		--mode)
			mode="${2:-}"
			shift 2
			;;
		--substate)
			substate="${2:-}"
			shift 2
			;;
		--attempt)
			attempt="${2:-}"
			shift 2
			;;
		--lane)
			lane="${2:-}"
			shift 2
			;;
		--model)
			model="${2:-}"
			shift 2
			;;
		--issue-number)
			issue_number="${2:-}"
			shift 2
			;;
		--pr-number)
			pr_number="${2:-}"
			shift 2
			;;
		--actor)
			actor="${2:-}"
			shift 2
			;;
		--event-type)
			event_type="${2:-}"
			shift 2
			;;
		--status)
			status="${2:-}"
			shift 2
			;;
		--message)
			message="${2:-}"
			shift 2
			;;
		--metadata-json)
			metadata_json="${2:-{}}"
			shift 2
			;;
		--tokens-input)
			tokens_input="${2:-}"
			shift 2
			;;
		--tokens-output)
			tokens_output="${2:-}"
			shift 2
			;;
		--tokens-total)
			tokens_total="${2:-}"
			shift 2
			;;
		--tokens-log-file)
			tokens_log_file="${2:-}"
			shift 2
			;;
		--repo-root)
			repo_root="${2:-}"
			shift 2
			;;
		--)
			shift
			break
			;;
		*)
			ledger_substate_warn "unknown argument: $1"
			exit 0
			;;
	esac
done

if ! ledger_substates_enabled; then
	exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
	ledger_substate_warn "python3 unavailable; skipping substate emission"
	exit 0
fi

if [ -z "${run_id}" ] || [ -z "${workflow}" ] || [ -z "${phase}" ]; then
	ledger_substate_warn "run_id, workflow, and phase are required"
	exit 0
fi

if [ "${event_type}" = "run_substate" ] && [ -z "${substate}" ]; then
	ledger_substate_warn "substate is required when event_type=run_substate"
	exit 0
fi

if [ -z "${mode}" ]; then
	mode="default"
fi
if [ -z "${actor}" ]; then
	actor="${GITHUB_ACTOR:-codex-bot}"
fi
if [ -z "${repo_root}" ]; then
	repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

ai_memory_script="$(resolve_ai_memory_script || true)"
if [ -z "${ai_memory_script}" ]; then
	ledger_substate_warn "ai_memory.py unavailable; skipping substate emission"
	exit 0
fi

dedupe_state="${substate:-${event_type}}"
seen_scope="$(printf '%s' "${run_id}_${GITHUB_RUN_ATTEMPT:-1}_${GITHUB_JOB:-default}" | tr '/[:space:]:' '_')"
seen_file_default="${RUNNER_TEMP:-/tmp}/ledger-substates/${seen_scope}.seen"
seen_file="${LEDGER_SUBSTATES_SEEN_FILE:-${seen_file_default}}"
marker_dir="${seen_file}.d"
dedupe_key="${workflow}|${phase}|${mode}|${attempt:-0}|${lane:-}|${event_type}|${dedupe_state}"

if [ -z "${status}" ]; then
	case "${event_type}" in
		run_substate)
			case "${substate}" in
				Succeeded)
					status="ok"
					;;
				Failed)
					status="error"
					;;
				TimedOut)
					status="timeout"
					;;
				Stalled)
					status="stalled"
					;;
				*)
					status="info"
					;;
			esac
			;;
		codex_stall_killed)
			status="stalled"
			;;
		*)
			status="info"
			;;
	esac
fi

if [ -z "${message}" ]; then
	if [ "${event_type}" = "run_substate" ]; then
		message="Run substate ${substate}"
	else
		message="Run ledger event ${event_type}"
	fi
fi

metadata_error_file="$(mktemp)"
if ! metadata_payload="$(build_metadata_payload 2>"${metadata_error_file}")"; then
	if [ -s "${metadata_error_file}" ]; then
		while IFS= read -r line; do
			ledger_substate_warn "metadata build failed: ${line}"
		done < "${metadata_error_file}"
	else
		ledger_substate_warn "could not build metadata payload; skipping substate emission"
	fi
	rm -f "${metadata_error_file}"
	exit 0
fi
rm -f "${metadata_error_file}"

if [ -z "${metadata_payload}" ]; then
	ledger_substate_warn "could not build metadata payload; skipping substate emission"
	exit 0
fi

emit_error_file="$(mktemp)"
if ! emit_result="$(emit_deduped_run_event 2>"${emit_error_file}")"; then
	if [ -s "${emit_error_file}" ]; then
		while IFS= read -r line; do
			ledger_substate_warn "emit failed: ${line}"
		done < "${emit_error_file}"
	else
		ledger_substate_warn "could not emit substate; skipping substate emission"
	fi
	rm -f "${emit_error_file}"
	exit 0
fi
rm -f "${emit_error_file}"

case "${emit_result}" in
	duplicate|emitted)
		exit 0
		;;
	emit_failed:*)
		ledger_substate_warn "record-run-event failed (${emit_result#emit_failed:}); skipping substate emission"
		exit 0
		;;
	*)
		ledger_substate_warn "could not emit substate; skipping substate emission"
		exit 0
		;;
esac
