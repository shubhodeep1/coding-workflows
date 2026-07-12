#!/usr/bin/env python3
"""Aggregate Serena tool-call rollups from Codex logs."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from collections import defaultdict
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	from emit_event import emit_event as _emit_event_helper
except Exception:
	_emit_event_helper = None


SERVER_KEYS = ("mcp_server", "server", "server_name", "mcpServer", "mcpServerName")
TOOL_KEYS = ("tool", "tool_name", "toolName")
MS_KEYS = ("duration_ms", "ms", "elapsed_ms")
BYTES_KEYS = ("response_bytes", "bytes", "size_bytes")

SERVER_LINE_RE = re.compile(r"(?:^|\s)(?:server|mcp_server)=serena(?:\s|$)", re.IGNORECASE)
TOOL_LINE_RE = re.compile(r"(?:^|\s)(?:tool|tool_name)=(?P<tool>[A-Za-z0-9_./-]+)(?:\s|$)")
SERENA_CALL_RE = re.compile(r"\bserena\.(?P<tool>[A-Za-z_][A-Za-z0-9_.]*)\(")
INT_FIELDS_RE = {
	"ms": re.compile(r"(?:^|\s)(?:ms|duration_ms|elapsed_ms)=(?P<value>\d+)(?:\s|$)"),
	"response_bytes": re.compile(r"(?:^|\s)(?:response_bytes|bytes|size_bytes)=(?P<value>\d+)(?:\s|$)"),
}


def _mirror_event(prefix: str, **fields: object) -> None:
	if _emit_event_helper is None:
		return
	try:
		_emit_event_helper(prefix, **fields)
	except Exception:
		return


def _sanitize_log_value(value: Any) -> str:
	text = str(value)
	parts = text.split()
	if not parts:
		return "unknown"
	return "_".join(parts).replace("=", "_")


def _extract_nested_value(payload: Any, keys: tuple[str, ...]) -> Any | None:
	if isinstance(payload, dict):
		for key in keys:
			if key in payload:
				return payload[key]
		for value in payload.values():
			found = _extract_nested_value(value, keys)
			if found is not None:
				return found
	elif isinstance(payload, list):
		for value in payload:
			found = _extract_nested_value(value, keys)
			if found is not None:
				return found
	return None


def _coerce_non_negative_int(value: Any) -> int:
	try:
		parsed = int(value)
	except (TypeError, ValueError):
		return 0
	return parsed if parsed >= 0 else 0


def _parse_json_line(line: str) -> dict[str, int | str] | None:
	stripped = line.strip()
	if not stripped.startswith("{"):
		return None
	try:
		payload = json.loads(stripped)
	except json.JSONDecodeError:
		return None
	server = _extract_nested_value(payload, SERVER_KEYS)
	if not isinstance(server, str) or server.lower() != "serena":
		return None
	tool = _extract_nested_value(payload, TOOL_KEYS)
	if not isinstance(tool, str) or not tool:
		return None
	return {
		"tool": tool,
		"ms": _coerce_non_negative_int(_extract_nested_value(payload, MS_KEYS)),
		"response_bytes": _coerce_non_negative_int(_extract_nested_value(payload, BYTES_KEYS)),
	}


def _parse_plaintext_line(line: str) -> dict[str, int | str] | None:
	tool = None
	if SERVER_LINE_RE.search(line):
		match = TOOL_LINE_RE.search(line)
		if match:
			tool = match.group("tool")
	else:
		match = SERENA_CALL_RE.search(line)
		if match:
			tool = match.group("tool")
	if not tool:
		return None
	metrics = {
		"tool": tool,
		"ms": 0,
		"response_bytes": 0,
	}
	for key, regex in INT_FIELDS_RE.items():
		match = regex.search(line)
		if match:
			metrics[key] = _coerce_non_negative_int(match.group("value"))
	return metrics


def _parse_serena_lines(lines: Iterable[str]) -> dict[str, dict[str, int]]:
	aggregate: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "ms": 0, "response_bytes": 0})
	for raw_line in lines:
		parsed = _parse_json_line(raw_line)
		if parsed is None:
			parsed = _parse_plaintext_line(raw_line)
		if parsed is None:
			continue
		tool = str(parsed["tool"])
		aggregate[tool]["calls"] += 1
		aggregate[tool]["ms"] += _coerce_non_negative_int(parsed.get("ms"))
		aggregate[tool]["response_bytes"] += _coerce_non_negative_int(parsed.get("response_bytes"))
	return dict(aggregate)


def parse_serena_log(text: str) -> dict[str, dict[str, int]]:
	"""Return per-tool Serena rollups for *text*."""
	return _parse_serena_lines(text.splitlines())


def emit_rollups(target: str, rollups: dict[str, dict[str, int]]) -> None:
	safe_target = _sanitize_log_value(target)
	for tool in sorted(
		rollups,
		key=lambda name: (-rollups[name]["calls"], -rollups[name]["response_bytes"], name),
	):
		entry = rollups[tool]
		safe_tool = _sanitize_log_value(tool)
		sys.stderr.write(
			f"SERENA_QUERY target={safe_target} tool={safe_tool} calls={entry['calls']} response_bytes={entry['response_bytes']} ms={entry['ms']}\n"
		)
		_mirror_event(
			"SERENA_QUERY",
			target=safe_target,
			tool=safe_tool,
			calls=entry["calls"],
			response_bytes=entry["response_bytes"],
			ms=entry["ms"],
		)


def _parse_args(argv: list[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--target", required=True, help="Workflow/phase target label for emitted SERENA_QUERY lines.")
	parser.add_argument("--log", action="append", default=[], help="Codex log path to parse. May be provided more than once.")
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
	totals: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "ms": 0, "response_bytes": 0})

	for raw_path in args.log:
		path = Path(raw_path)
		if not path.is_file():
			continue
		try:
			with path.open("r", encoding="utf-8", errors="replace") as handle:
				parsed_rollups = _parse_serena_lines(handle)
		except OSError:
			continue
		for tool, values in parsed_rollups.items():
			totals[tool]["calls"] += values["calls"]
			totals[tool]["ms"] += values["ms"]
			totals[tool]["response_bytes"] += values["response_bytes"]

	emit_rollups(args.target, dict(totals))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
