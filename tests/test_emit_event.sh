#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

assert_no_file()
{
	local path="$1"
	if [ -e "${path}" ]; then
		echo "expected no file at ${path}" >&2
		exit 1
	fi
}

with_temp_workspace()
{
	local callback="$1"
	local tmpdir=""
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	"${callback}" "${tmpdir}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_flag_off_writes_nothing()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local events_file="${workspace}/.events/run-run-123.jsonl"
	mkdir -p "${workspace}"

	(
		export EVENTS_JSONL_ENABLED=false
		export GITHUB_WORKSPACE="${workspace}"
		export GITHUB_RUN_ID="run-123"
		export UNATTENDED_PHASE="validate"
		source "${REPO_ROOT}/scripts/emit_event.sh"
		emit_event "SEMBLE_FALLBACK" "target=review-autofix" "reason=disabled"
	)

	assert_no_file "${events_file}"
}

test_flag_on_writes_shell_and_python_records()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local events_file="${workspace}/.events/run-run-123.jsonl"
	mkdir -p "${workspace}"

	(
		export EVENTS_JSONL_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		export GITHUB_RUN_ID="run-123"
		export UNATTENDED_PHASE="validate"
		source "${REPO_ROOT}/scripts/emit_event.sh"
		emit_event "SEMBLE_FALLBACK" "target=review-autofix" "reason=exit=7 raw failure" "ms=15"
	)

	PYTHONDONTWRITEBYTECODE=1 \
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="run-123" \
	UNATTENDED_PHASE="validate" \
	python3 - "${REPO_ROOT}/scripts" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1])
from emit_event import emit_event

emit_event("SERENA_QUERY", target="validate", calls=2, response_bytes=12, ms=10)
PY

	PYTHONDONTWRITEBYTECODE=1 python3 - "${events_file}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
	raise SystemExit(f"missing events file: {path}")

records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
assert len(records) == 2, records

shell_record = records[0]
assert shell_record["schema_version"] == "events.v1.json"
assert shell_record["run_id"] == "run-123"
assert shell_record["phase"] == "validate"
assert shell_record["prefix"] == "SEMBLE_FALLBACK"
assert shell_record["ts"].endswith("Z")
assert shell_record["fields"] == {
	"target": "review-autofix",
	"reason": "exit=7 raw failure",
	"ms": "15",
}

python_record = records[1]
assert python_record["prefix"] == "SERENA_QUERY"
assert python_record["fields"] == {
	"target": "validate",
	"calls": 2,
	"response_bytes": 12,
	"ms": 10,
}
PY
}

with_temp_workspace test_flag_off_writes_nothing
with_temp_workspace test_flag_on_writes_shell_and_python_records
echo "test_emit_event.sh: PASS"
