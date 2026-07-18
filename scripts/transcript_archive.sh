#!/usr/bin/env bash
# transcript_archive.sh — fail-open JSON archive helper for captured phase output.

if [ "${_TRANSCRIPT_ARCHIVE_SH_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || exit 0
fi
_TRANSCRIPT_ARCHIVE_SH_LOADED=1

_transcript_archive_enabled()
{
	case "${UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED:-false}" in
		[Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn]) return 0 ;;
		*) return 1 ;;
	esac
}

_transcript_archive_fail()
{
	printf 'TRANSCRIPT_ARCHIVE_FAIL: %s\n' "$1" >&2
}

archive_transcript()
{
	local run_id="${1:-}"
	local phase="${2:-}"
	local source_path="${3:-}"

	if ! _transcript_archive_enabled; then
		return 0
	fi
	if [ -z "${run_id}" ] || [ -z "${phase}" ] || [ -z "${source_path}" ]; then
		_transcript_archive_fail "missing arguments run_id='${run_id}' phase='${phase}' source='${source_path}'"
		return 0
	fi
	if ! command -v python3 >/dev/null 2>&1; then
		_transcript_archive_fail "python3 unavailable for run_id=${run_id} phase=${phase}"
		return 0
	fi

	if ! GITHUB_WORKSPACE="${GITHUB_WORKSPACE:-$PWD}" \
		PYTHONDONTWRITEBYTECODE=1 \
		python3 - "${run_id}" "${phase}" "${source_path}" <<'PY'
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


CAP_BYTES = 50 * 1024 * 1024


def fail(reason: str) -> None:
	sys.stderr.write(f"TRANSCRIPT_ARCHIVE_FAIL: {reason}\n")
	raise SystemExit(1)


def sanitize_segment(value: str, fallback: str) -> str:
	cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
	return cleaned or fallback


run_id, phase, source_path = sys.argv[1:4]
workspace_root = Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
source_file = Path(source_path)

if not source_file.is_file():
	fail(f"source missing or not a file: {source_path}")

try:
	source_bytes = source_file.stat().st_size
	with source_file.open("rb") as handle:
		raw = handle.read(CAP_BYTES + 1)
	truncated = source_bytes > CAP_BYTES
	if len(raw) > CAP_BYTES:
		raw = raw[:CAP_BYTES]
	content = raw.decode("utf-8", errors="replace")

	archive_dir = workspace_root / ".transcripts"
	archive_dir.mkdir(parents=True, exist_ok=True)
	ts = datetime.now(timezone.utc)
	archive_name = (
		f"{sanitize_segment(run_id, 'run')}-"
		f"{sanitize_segment(phase, 'phase')}-"
		f"{ts.strftime('%Y%m%dT%H%M%S%fZ')}.json"
	)
	record = {
		"run_id": run_id,
		"phase": phase,
		"source_path": source_path,
		"archived_at": ts.isoformat().replace("+00:00", "Z"),
		"source_bytes": source_bytes,
		"archived_bytes": len(raw),
		"byte_cap": CAP_BYTES,
		"truncated": truncated,
		"content": content,
	}
	(archive_dir / archive_name).write_text(
		json.dumps(record, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)
except Exception as exc:  # pragma: no cover - shell contract test exercises fail-open behavior.
	fail(f"run_id={run_id} phase={phase} source={source_path} reason={exc}")
PY
	then
		return 0
	fi
	return 0
}

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
	archive_transcript "$@"
fi
