#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

workspace="${tmpdir}/workspace"
events_file="${workspace}/.events/run-concurrent.jsonl"
mkdir -p "${workspace}"

for worker in 1 2 3 4 5; do
	(
		export EVENTS_JSONL_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		export GITHUB_RUN_ID="concurrent"
		export UNATTENDED_PHASE="implement"
		source "${REPO_ROOT}/scripts/emit_event.sh"
		for sequence in 1 2 3 4 5; do
			emit_event "LABEL_REPAIR" \
				"worker=${worker}" \
				"sequence=${sequence}" \
				"detail=worker ${worker} sequence ${sequence}"
		done
	) &
done

wait

PYTHONDONTWRITEBYTECODE=1 python3 - "${events_file}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
	raise SystemExit(f"missing events file: {path}")

lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
assert len(lines) == 25, len(lines)
records = [json.loads(line) for line in lines]
for record in records:
	assert record["prefix"] == "LABEL_REPAIR"
	assert record["phase"] == "implement"
	assert record["run_id"] == "concurrent"
	assert record["fields"]["detail"].startswith("worker ")
PY

echo "test_emit_event_concurrent.sh: PASS"
