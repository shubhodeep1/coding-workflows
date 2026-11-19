#!/usr/bin/env python3
"""Fail-open append-only JSONL mirror for stable workflow event prefixes."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RESERVED_PREFIXES = frozenset({"EVENTS_EMIT", "EVENTS_EMIT_FAIL"})
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "y"})


def _events_enabled() -> bool:
	return os.getenv("EVENTS_JSONL_ENABLED", "").strip().lower() in _TRUTHY_VALUES


def _sanitize_log_value(value: str) -> str:
	sanitized_value = "_".join(value.replace("=", "_").split())
	return sanitized_value or "unknown"


def _sanitize_path_segment(raw_value: str, *, fallback: str) -> str:
	sanitized_value = "".join(
		character if character.isascii() and (character.isalnum() or character in "._-") else "_"
		for character in raw_value
	).strip("._")
	return sanitized_value or fallback


def _workspace_root() -> Path:
	workspace = os.getenv("GITHUB_WORKSPACE")
	if workspace:
		return Path(workspace)
	return Path.cwd()


def _events_path() -> Path:
	raw_run_id = os.getenv("GITHUB_RUN_ID") or "local"
	run_id_for_path = _sanitize_path_segment(raw_run_id, fallback="local")
	return _workspace_root() / ".events" / f"run-{run_id_for_path}.jsonl"


def _utc_now_rfc3339() -> str:
	return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_ready(value: Any) -> Any:
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, Path):
		return str(value)
	if isinstance(value, (list, tuple)):
		return [_json_ready(item) for item in value]
	if isinstance(value, dict):
		return {str(key): _json_ready(inner) for key, inner in value.items()}
	return str(value)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a", encoding="utf-8") as handle:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		try:
			handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
			handle.flush()
			os.fsync(handle.fileno())
		finally:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _emit_fail(reason: str) -> None:
	sys.stderr.write(f"EVENTS_EMIT_FAIL reason={_sanitize_log_value(reason)}\n")


def emit_event(prefix: str, **fields: Any) -> bool:
	if not _events_enabled():
		return False
	if not prefix:
		_emit_fail("missing prefix")
		return False
	if prefix in _RESERVED_PREFIXES:
		return False

	path = _events_path()
	record = {
		"schema_version": "events.v1.json",
		"ts": _utc_now_rfc3339(),
		"run_id": os.getenv("GITHUB_RUN_ID") or "local",
		"phase": os.getenv("UNATTENDED_PHASE") or "unknown",
		"prefix": prefix,
		"fields": {str(key): _json_ready(value) for key, value in fields.items()},
	}

	try:
		_append_jsonl(path, record)
	except Exception as exc:  # pragma: no cover - exercised by shell tests / runtime failure paths.
		_emit_fail(f"prefix={prefix} path={path} error={exc}")
		return False
	return True


def _parse_field_tokens(tokens: list[str]) -> dict[str, str] | None:
	fields: dict[str, str] = {}
	for token in tokens:
		if "=" not in token:
			_emit_fail(f"invalid field token {token!r}")
			return None
		key, value = token.split("=", 1)
		if not key:
			_emit_fail(f"invalid empty field key in token {token!r}")
			return None
		fields[key] = value
	return fields


def main(argv: list[str] | None = None) -> int:
	if not _events_enabled():
		return 0

	args = list(argv) if argv is not None else sys.argv[1:]
	if not args:
		_emit_fail("missing prefix")
		return 0

	fields = _parse_field_tokens(args[1:])
	if fields is None:
		return 0

	emit_event(args[0], **fields)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
