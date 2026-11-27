#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

assert_no_path()
{
	local path="$1"
	if [ -e "${path}" ]; then
		echo "expected no path at ${path}" >&2
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
	local source_file="${tmpdir}/output.txt"
	mkdir -p "${workspace}"
	printf 'hello\n' > "${source_file}"

	(
		export UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=false
		export GITHUB_WORKSPACE="${workspace}"
		source "${REPO_ROOT}/scripts/transcript_archive.sh"
		archive_transcript "run-123" "implement" "${source_file}"
	)

	assert_no_path "${workspace}/.transcripts"
}

test_flag_on_writes_archive_and_sanitizes_filename()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local source_file="${tmpdir}/output.txt"
	local archive_path=""
	local archive_base=""
	mkdir -p "${workspace}"
	printf 'hello archive\n' > "${source_file}"

	(
		export UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		source "${REPO_ROOT}/scripts/transcript_archive.sh"
		archive_transcript "run/123" "review:editor" "${source_file}"
	)

	archive_path="$(find "${workspace}/.transcripts" -type f -name '*.json' | head -n 1)"
	if [ -z "${archive_path}" ]; then
		echo "expected transcript archive file" >&2
		exit 1
	fi
	archive_base="$(basename "${archive_path}")"
	case "${archive_base}" in
		run_123-review_editor-*.json) ;;
		*)
			echo "unexpected archive filename: ${archive_base}" >&2
			exit 1
			;;
	 esac

	PYTHONDONTWRITEBYTECODE=1 python3 - "${archive_path}" "${source_file}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


archive_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])
payload = json.loads(archive_path.read_text(encoding="utf-8"))

assert payload["run_id"] == "run/123"
assert payload["phase"] == "review:editor"
assert payload["source_path"] == str(source_path)
assert payload["source_bytes"] == source_path.stat().st_size
assert payload["archived_bytes"] == source_path.stat().st_size
assert payload["truncated"] is False
assert payload["content"] == source_path.read_text(encoding="utf-8")
PY
}

test_missing_source_fails_open()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local stderr_file="${tmpdir}/stderr.txt"
	mkdir -p "${workspace}"

	if ! (
		export UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		source "${REPO_ROOT}/scripts/transcript_archive.sh"
		archive_transcript "run-123" "implement" "${tmpdir}/missing.txt"
	) 2>"${stderr_file}"; then
		echo "archive_transcript should fail open on missing source" >&2
		exit 1
	fi

	grep -q '^TRANSCRIPT_ARCHIVE_FAIL: source missing or not a file:' "${stderr_file}"
	assert_no_path "${workspace}/.transcripts"
}

test_write_failure_fails_open()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local source_file="${tmpdir}/output.txt"
	local stderr_file="${tmpdir}/stderr.txt"
	mkdir -p "${workspace}"
	printf 'hello write failure\n' > "${source_file}"
	: > "${workspace}/.transcripts"

	if ! (
		export UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		source "${REPO_ROOT}/scripts/transcript_archive.sh"
		archive_transcript "run-123" "implement" "${source_file}"
	) 2>"${stderr_file}"; then
		echo "archive_transcript should fail open on write failure" >&2
		exit 1
	fi

	grep -q '^TRANSCRIPT_ARCHIVE_FAIL: run_id=run-123 phase=implement source=' "${stderr_file}"
}

test_size_cap_truncates_content()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local source_file="${tmpdir}/large-output.txt"
	local archive_path=""
	mkdir -p "${workspace}"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${source_file}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path


path = Path(sys.argv[1])
path.write_bytes(b"x" * ((50 * 1024 * 1024) + 1024))
PY

	(
		export UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true
		export GITHUB_WORKSPACE="${workspace}"
		source "${REPO_ROOT}/scripts/transcript_archive.sh"
		archive_transcript "run-123" "validate-discover" "${source_file}"
	)

	archive_path="$(find "${workspace}/.transcripts" -type f -name '*.json' | head -n 1)"
	if [ -z "${archive_path}" ]; then
		echo "expected transcript archive file for size-cap test" >&2
		exit 1
	fi

	PYTHONDONTWRITEBYTECODE=1 python3 - "${archive_path}" "${source_file}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


archive_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])
payload = json.loads(archive_path.read_text(encoding="utf-8"))

assert payload["byte_cap"] == 50 * 1024 * 1024
assert payload["source_bytes"] == source_path.stat().st_size
assert payload["archived_bytes"] == payload["byte_cap"]
assert payload["truncated"] is True
assert len(payload["content"].encode("utf-8")) == payload["archived_bytes"]
PY
}

with_temp_workspace test_flag_off_writes_nothing
with_temp_workspace test_flag_on_writes_archive_and_sanitizes_filename
with_temp_workspace test_missing_source_fails_open
with_temp_workspace test_write_failure_fails_open
with_temp_workspace test_size_cap_truncates_content
echo "test_transcript_archive.sh: PASS"
