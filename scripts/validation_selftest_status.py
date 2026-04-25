#!/usr/bin/env python3
"""Publish deterministic nightly validation self-test streak status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Publish nightly validation self-test streak status.")
	parser.add_argument(
		"--summary-path",
		default="artifacts/validation-selftest-summary.json",
		help="Path to the validation self-test matrix summary JSON.",
	)
	parser.add_argument(
		"--status-path",
		default="analysis/validation-selftest-status.json",
		help="Path to the committed status JSON artifact.",
	)
	return parser


def _read_json(path: Path, description: str) -> dict[str, Any]:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError as exc:
		raise ValueError(f"{description} file not found: {path.as_posix()}") from exc
	except json.JSONDecodeError as exc:
		raise ValueError(f"{description} file is not valid JSON: {path.as_posix()}: {exc}") from exc
	if not isinstance(payload, dict):
		raise ValueError(f"{description} JSON must be an object: {path.as_posix()}")
	return payload


def _coerce_non_negative_int(value: Any, field_name: str) -> int:
	try:
		coerced = int(value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{field_name} must be an integer: got {value!r}") from exc
	if coerced < 0:
		raise ValueError(f"{field_name} must be >= 0: got {coerced}")
	return coerced


def _normalize_overall_status(value: Any) -> str:
	status = str(value or "unknown").strip().lower()
	if status not in {"pass", "fail"}:
		return "unknown"
	return status


def _build_latest_run(summary: dict[str, Any]) -> dict[str, Any]:
	totals_raw = summary.get("totals")
	if not isinstance(totals_raw, dict):
		totals_raw = {}
	latest_run = {
		"generated_at": str(summary.get("generated_at") or "unknown"),
		"overall_status": _normalize_overall_status(summary.get("overall_status")),
		"summary_schema_version": str(summary.get("schema_version") or "unknown"),
		"totals": {
			"failed": _coerce_non_negative_int(totals_raw.get("failed", 0), "summary.totals.failed"),
			"fixtures": _coerce_non_negative_int(totals_raw.get("fixtures", 0), "summary.totals.fixtures"),
			"passed": _coerce_non_negative_int(totals_raw.get("passed", 0), "summary.totals.passed"),
		},
	}
	return latest_run


def _same_latest_run(previous_latest: dict[str, Any], latest_run: dict[str, Any]) -> bool:
	return (
		str(previous_latest.get("generated_at") or "") == latest_run["generated_at"]
		and str(previous_latest.get("overall_status") or "") == latest_run["overall_status"]
		and str(previous_latest.get("summary_schema_version") or "") == latest_run["summary_schema_version"]
		and previous_latest.get("totals") == latest_run["totals"]
	)


def _load_previous_status(status_path: Path) -> dict[str, Any]:
	if not status_path.exists():
		return {
			"consecutive_green_runs": 0,
			"latest_run": {},
		}
	status = _read_json(status_path, "status")
	consecutive_green_runs = _coerce_non_negative_int(status.get("consecutive_green_runs", 0), "status.consecutive_green_runs")
	latest_run = status.get("latest_run", {})
	if not isinstance(latest_run, dict):
		raise ValueError("status.latest_run must be an object")
	return {
		"consecutive_green_runs": consecutive_green_runs,
		"latest_run": latest_run,
	}


def _build_next_status(previous_status: dict[str, Any], latest_run: dict[str, Any]) -> dict[str, Any]:
	previous_latest = previous_status.get("latest_run", {})
	if not isinstance(previous_latest, dict):
		previous_latest = {}
	previous_count = _coerce_non_negative_int(
		previous_status.get("consecutive_green_runs", 0),
		"status.consecutive_green_runs",
	)
	if _same_latest_run(previous_latest, latest_run):
		next_count = previous_count
	elif latest_run["overall_status"] == "pass":
		next_count = previous_count + 1
	else:
		next_count = 0
	return {
		"consecutive_green_runs": next_count,
		"latest_run": latest_run,
		"schema_version": SCHEMA_VERSION,
	}


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	summary_path = Path(args.summary_path)
	status_path = Path(args.status_path)
	try:
		summary = _read_json(summary_path, "summary")
		previous_status = _load_previous_status(status_path)
		latest_run = _build_latest_run(summary)
		next_status = _build_next_status(previous_status, latest_run)
	except ValueError as exc:
		print(f"validation-selftest-status: {exc}", file=sys.stderr)
		return 1

	status_path.parent.mkdir(parents=True, exist_ok=True)
	status_path.write_text(json.dumps(next_status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	print(
		"validation-selftest-status: "
		f"overall_status={next_status['latest_run']['overall_status']} "
		f"consecutive_green_runs={next_status['consecutive_green_runs']} "
		f"status_path={status_path.as_posix()}"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
