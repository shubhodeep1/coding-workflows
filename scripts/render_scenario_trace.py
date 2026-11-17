#!/usr/bin/env python3
"""Render replayable scenario traces from workflow log collector excerpts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

try:
	from scripts.emit_event import emit_event as _emit_jsonl_event
except Exception:  # noqa: BLE001
	def _emit_jsonl_event(prefix: str, **fields: Any) -> bool:
		return False


SCHEMA_VERSION = "workflow_scenario_trace.v1.json"
COLLECTOR_SCHEMA_VERSION = "workflow_log_collector.v2"
MODEL_REQUEST_MARKER = ">>> [model]"
MODEL_RESPONSE_MARKER = "<<< [model]"
TOOL_CALL_MARKER = "[tool_call]"
TOOL_RESULT_MARKER = "[tool_result]"
TRACE_WRITTEN_PREFIX = "WORKFLOW_SCENARIO_TRACE_WRITTEN"
TRACE_PARSE_FAIL_PREFIX = "WORKFLOW_SCENARIO_TRACE_PARSE_FAIL"
TIMESTAMP_RE = re.compile(
	r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(?P<rest>.*)$"
)
EXIT_STATUS_RE = re.compile(r"^exit_status=(?P<status>-?\d+)(?:\s+(?P<rest>.*))?$")


@dataclass
class _Block:
	kind: str
	ts: str | None
	lines: list[str]


class TraceParseError(RuntimeError):
	"""Raised when a marked log block cannot be normalized safely."""


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, help="Path to workflow_log_report.json")
	parser.add_argument(
		"--output-dir",
		required=True,
		help="Directory that receives <run_id>.scenario.json files",
	)
	parser.add_argument(
		"--run-id",
		default="",
		help="Optional single run id to render; renders every parseable run when omitted",
	)
	return parser


def _one_line(value: Any) -> str:
	text = str(value)
	text = text.replace("\r", " ").replace("\n", " ")
	return " ".join(text.split())


def _emit_trace_log(prefix: str, **fields: Any) -> None:
	parts = [f"{key}={_one_line(value)}" for key, value in fields.items()]
	if parts:
		print(f"{prefix}: {' '.join(parts)}")
	else:
		print(f"{prefix}:")
	try:
		_emit_jsonl_event(prefix, **fields)
	except Exception:  # noqa: BLE001
		pass


def _load_collector_report(path: Path) -> dict[str, Any]:
	payload = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		raise TraceParseError("collector report root is not a JSON object")
	return payload


def _normalize_requested_run_id(run_id: str | int | None) -> str | None:
	if run_id is None:
		return None
	text = str(run_id).strip()
	return text or None


def _run_identifier(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def _normalize_excerpts(value: Any) -> list[dict[str, str]]:
	if not isinstance(value, list):
		return []

	excerpts: list[dict[str, str]] = []
	for item in value:
		if not isinstance(item, dict):
			continue
		step_name = str(item.get("step_name") or "")
		excerpt = str(item.get("excerpt") or "")
		if not excerpt:
			continue
		excerpts.append({"step_name": step_name, "excerpt": excerpt})
	return excerpts


def _split_timestamp(line: str) -> tuple[str | None, str]:
	match = TIMESTAMP_RE.match(line)
	if not match:
		return None, line
	return match.group("ts"), match.group("rest")


def _detect_marker(text: str) -> tuple[str | None, str]:
	marker_map = (
		(MODEL_REQUEST_MARKER, "user_message"),
		(MODEL_RESPONSE_MARKER, "assistant_text"),
		(TOOL_CALL_MARKER, "tool_call"),
		(TOOL_RESULT_MARKER, "tool_result"),
	)
	for marker, kind in marker_map:
		if text.startswith(marker):
			return kind, text[len(marker) :].lstrip(" :")
	return None, text


def _join_block_lines(lines: list[str]) -> str:
	return "\n".join(lines).strip("\n")


def _looks_like_json(raw: str) -> bool:
	stripped = raw.lstrip()
	return stripped.startswith("{")


def _parse_json(raw: str, *, context: str) -> Any:
	try:
		return json.loads(raw)
	except json.JSONDecodeError as exc:
		raise TraceParseError(f"{context} is not valid JSON") from exc


def _maybe_int(value: Any) -> int | None:
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _parse_tool_call_payload(raw: str) -> tuple[str, Any]:
	if not raw.strip():
		raise TraceParseError("tool_call payload is empty")

	if _looks_like_json(raw):
		payload = _parse_json(raw, context="tool_call payload")
		if not isinstance(payload, dict):
			raise TraceParseError("tool_call payload must be an object when JSON encoded")
		name = str(payload.get("name") or payload.get("tool") or payload.get("tool_name") or "").strip()
		if not name:
			raise TraceParseError("tool_call payload is missing a name")
		if "args" in payload:
			args = payload.get("args")
		else:
			args = {
				key: value
				for key, value in payload.items()
				if key not in {"name", "tool", "tool_name"}
			}
		return name, args

	first_line, *remaining_lines = raw.splitlines()
	parts = first_line.strip().split(None, 1)
	if not parts:
		raise TraceParseError("tool_call payload is empty")
	name = parts[0].strip()
	if not name:
		raise TraceParseError("tool_call payload is missing a name")
	remainder_parts: list[str] = []
	if len(parts) == 2:
		remainder_parts.append(parts[1])
	remainder_parts.extend(remaining_lines)
	args_text = "\n".join(remainder_parts).strip()
	if not args_text:
		return name, {}
	if _looks_like_json(args_text):
		return name, _parse_json(args_text, context="tool_call args")
	return name, args_text


def _build_tool_result_step(block: _Block) -> dict[str, Any]:
	raw = _join_block_lines(block.lines)
	stripped = raw.strip()
	if not stripped:
		return {"type": "tool_result", "ts": block.ts, "output": "", "exit_status": None}

	if _looks_like_json(stripped):
		payload = _parse_json(stripped, context="tool_result payload")
		if isinstance(payload, dict):
			output_value = payload.get("output")
			if output_value is None and "result" in payload:
				output_value = payload.get("result")
			if output_value is None:
				output_value = json.dumps(payload, ensure_ascii=False, sort_keys=True)
			return {
				"type": "tool_result",
				"ts": block.ts,
				"output": str(output_value),
				"exit_status": _maybe_int(payload.get("exit_status")),
			}
		return {
			"type": "tool_result",
			"ts": block.ts,
			"output": json.dumps(payload, ensure_ascii=False),
			"exit_status": None,
		}

	first_line, *remaining_lines = raw.splitlines()
	match = EXIT_STATUS_RE.match(first_line.strip())
	if match:
		rest_lines: list[str] = []
		rest = match.group("rest") or ""
		if rest:
			rest_lines.append(rest)
		rest_lines.extend(remaining_lines)
		return {
			"type": "tool_result",
			"ts": block.ts,
			"output": _join_block_lines(rest_lines),
			"exit_status": int(match.group("status")),
		}

	return {"type": "tool_result", "ts": block.ts, "output": raw, "exit_status": None}


def _finalize_block(block: _Block | None) -> dict[str, Any] | None:
	if block is None:
		return None

	if block.kind in {"user_message", "assistant_text"}:
		content = _join_block_lines(block.lines)
		if not content:
			return None
		return {"type": block.kind, "ts": block.ts, "content": content, "tokens": None}

	if block.kind == "tool_call":
		payload_text = _join_block_lines(block.lines).strip()
		name, args = _parse_tool_call_payload(payload_text)
		return {"type": "tool_call", "ts": block.ts, "name": name, "args": args}

	if block.kind == "tool_result":
		return _build_tool_result_step(block)

	return None


def _parse_steps(log_excerpts: list[dict[str, str]]) -> list[dict[str, Any]]:
	steps: list[dict[str, Any]] = []
	saw_marker = False
	# Keep the active block open across the full collector excerpt list: one
	# marked scenario step may be split across excerpt boundaries.
	current: _Block | None = None

	for excerpt in log_excerpts:
		for raw_line in excerpt["excerpt"].splitlines():
			ts, text = _split_timestamp(raw_line)
			kind, remainder = _detect_marker(text)
			if kind is not None:
				finalized = _finalize_block(current)
				if finalized is not None:
					steps.append(finalized)
				current = _Block(kind=kind, ts=ts, lines=[])
				saw_marker = True
				if remainder:
					current.lines.append(remainder)
				continue

			if current is None:
				continue
			if current.ts is None and ts is not None:
				current.ts = ts
			current.lines.append(text)

	finalized = _finalize_block(current)
	if finalized is not None:
		steps.append(finalized)

	if not saw_marker:
		raise TraceParseError("log excerpts did not contain any supported scenario markers")
	if saw_marker and not steps:
		raise TraceParseError("marked excerpts did not yield any scenario steps")
	return steps


def _write_trace(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path = path.with_name(f"{path.name}.tmp")
	tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	tmp_path.replace(path)


def render(run_id: str | int | None, collector_output_path: str | Path, out_path: str | Path) -> list[Path]:
	written_paths: list[Path] = []
	requested_run_id = _normalize_requested_run_id(run_id)
	collector_path = Path(collector_output_path)
	output_dir = Path(out_path)

	try:
		report = _load_collector_report(collector_path)
	except Exception as exc:  # noqa: BLE001
		_emit_trace_log(
			TRACE_PARSE_FAIL_PREFIX,
			run_id=requested_run_id or "all",
			phase="unknown",
			reason=f"collector_report_load_failed:{_one_line(exc)}",
			input=str(collector_path),
		)
		return written_paths

	runs = report.get("runs")
	if not isinstance(runs, list):
		_emit_trace_log(
			TRACE_PARSE_FAIL_PREFIX,
			run_id=requested_run_id or "all",
			phase="unknown",
			reason="collector_report_missing_runs_array",
			input=str(collector_path),
		)
		return written_paths

	for run in runs:
		if not isinstance(run, dict):
			continue

		run_identifier = _run_identifier(run.get("run_id"))
		if requested_run_id is not None and run_identifier != requested_run_id:
			continue

		phase = str(run.get("workflow_family") or "").strip()
		log_excerpts = _normalize_excerpts(run.get("log_excerpts"))
		if not log_excerpts:
			continue

		if not run_identifier or not phase:
			_emit_trace_log(
				TRACE_PARSE_FAIL_PREFIX,
				run_id=run_identifier or "unknown",
				phase=phase or "unknown",
				reason="missing_run_id_or_workflow_family",
			)
			continue

		try:
			steps = _parse_steps(log_excerpts)
		except Exception as exc:  # noqa: BLE001
			_emit_trace_log(
				TRACE_PARSE_FAIL_PREFIX,
				run_id=run_identifier,
				phase=phase,
				reason=_one_line(exc),
			)
			continue

		payload = {
			"schema_version": SCHEMA_VERSION,
			"run_id": run.get("run_id"),
			"phase": phase,
			"steps": steps,
		}
		trace_path = output_dir / f"{run_identifier}.scenario.json"
		try:
			_write_trace(trace_path, payload)
		except Exception as exc:  # noqa: BLE001
			_emit_trace_log(
				TRACE_PARSE_FAIL_PREFIX,
				run_id=run_identifier,
				phase=phase,
				reason=f"trace_write_failed:{_one_line(exc)}",
				path=str(trace_path),
			)
			continue

		written_paths.append(trace_path)
		_emit_trace_log(
			TRACE_WRITTEN_PREFIX,
			run_id=run_identifier,
			phase=phase,
			path=str(trace_path),
		)

	return written_paths


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	render(args.run_id or None, args.input, args.output_dir)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
