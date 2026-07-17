#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

assert_equals()
{
	local expected="$1"
	local actual="$2"
	if [ "${expected}" != "${actual}" ]; then
		echo "expected: ${expected}" >&2
		echo "actual:   ${actual}" >&2
		exit 1
	fi
}

extract_prefixed_line()
{
	local prefix="$1"
	local text="$2"
	printf '%s\n' "${text}" | grep -m 1 "^${prefix} "
}

assert_review_autofix_dispatch_mirrors()
{
	PYTHONDONTWRITEBYTECODE=1 python3 - "${REPO_ROOT}/.github/workflows/review_autofix.yml" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
prefixes = ("AUTOFIX_DISPATCH_SKIPPED", "AUTOFIX_DISPATCH_ISSUED")
seen = {prefix: 0 for prefix in prefixes}

for index, line in enumerate(lines):
	for prefix in prefixes:
		marker = f'echo "{prefix} '
		if marker not in line:
			continue
		seen[prefix] += 1
		next_index = index + 1
		while next_index < len(lines) and not lines[next_index].strip():
			next_index += 1
		if next_index >= len(lines):
			raise SystemExit(f"{path}: {prefix} echo at line {index + 1} is missing an emit_event mirror")
		expected = f'emit_event "{prefix}"'
		if expected not in lines[next_index]:
			raise SystemExit(
				f"{path}: {prefix} echo at line {index + 1} is not immediately followed by {expected}"
			)

for prefix, count in seen.items():
	if count == 0:
		raise SystemExit(f"{path}: no {prefix} echo callsites found")
PY
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

workspace="${tmpdir}/workspace"
events_file="${workspace}/.events/run-prefix-test.jsonl"
mkdir -p "${workspace}"

peer_output="$({
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="prefix-test" \
	UNATTENDED_PHASE="review_autofix" \
	GITHUB_REPOSITORY="owner/repo" \
	bash -c '
		set -euo pipefail
		source "'$REPO_ROOT'/scripts/gh_helpers.sh"
		gh_retry() {
			cat <<'"'"'JSON'"'"'
{"workflow_runs":[{"id":12345,"status":"queued","path":".github/workflows/review_autofix.yml"}]}
JSON
		}
		autofix_retrigger_has_inflight_peer 17 feature/test 99
	'
} 2>/dev/null)"
assert_equals "AUTOFIX_PEER_CHECK pr=17 branch=feature/test current_run=99 peer_count=1 peer_run=12345 peer_path=.github/workflows/review_autofix.yml" "${peer_output}"

semble_output="$({
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="prefix-test" \
	UNATTENDED_PHASE="implement" \
	SEMBLE_LOG_CONTEXT='' \
	bash -c '
		set -euo pipefail
		source "'$REPO_ROOT'/scripts/semble_helpers.sh"
		_semble_log_event "SEMBLE_FALLBACK" "target=editor-context" "reason=index-unavailable"
	' >/dev/null
} 2>&1)"
assert_equals "SEMBLE_FALLBACK target=editor-context reason=index-unavailable" "${semble_output}"

serena_fallback_output="$({
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="prefix-test" \
	UNATTENDED_PHASE="implement" \
	bash -c '
		set -euo pipefail
		source "'$REPO_ROOT'/scripts/setup_serena.sh"
		SERENA_FALLBACK_TARGET=review-autofix-editor emit_serena_fallback "setup-failure"
	' >/dev/null
} 2>&1)"
assert_equals \
	"SERENA_FALLBACK target=review-autofix-editor reason=setup-failure" \
	"$(extract_prefixed_line "SERENA_FALLBACK" "${serena_fallback_output}")"

serena_query_output="$({
	PYTHONDONTWRITEBYTECODE=1 \
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="prefix-test" \
	UNATTENDED_PHASE="implement" \
	python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from serena_stats_emit import emit_rollups

emit_rollups("implement", {"find_symbol": {"calls": 2, "response_bytes": 12, "ms": 10}})
PY
} 2>&1 >/dev/null)"
assert_equals "SERENA_QUERY target=implement tool=find_symbol calls=2 response_bytes=12 ms=10" "${serena_query_output}"

probe_output="$({
	PYTHONDONTWRITEBYTECODE=1 \
	EVENTS_JSONL_ENABLED=true \
	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_RUN_ID="prefix-test" \
	UNATTENDED_PHASE="implement" \
	python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from mcp_handshake_probe import _emit_probe_line

_emit_probe_line("serena", "ok", server_info={"name": "mock-serena", "version": "1.2.0"})
PY
} 2>&1 >/dev/null)"
assert_equals "SERENA_PROBE target=serena result=ok server_name=mock-serena server_version=1.2.0" "${probe_output}"

# The AUTOFIX_DISPATCH_* mirrors live in review_autofix.yml rather than a
# sourceable helper script; lock in the historical Phase D pairing there with
# a source-level regression check.
assert_review_autofix_dispatch_mirrors

PYTHONDONTWRITEBYTECODE=1 python3 - "${events_file}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
	raise SystemExit(f"missing events file: {path}")

records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
assert len(records) == 5, records
assert {record["prefix"] for record in records} == {
	"AUTOFIX_PEER_CHECK",
	"SEMBLE_FALLBACK",
	"SERENA_FALLBACK",
	"SERENA_QUERY",
	"SERENA_PROBE",
}
PY

echo "test_log_prefix_regressions.sh: PASS"
