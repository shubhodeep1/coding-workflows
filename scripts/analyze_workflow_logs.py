#!/usr/bin/env python3
"""Prepare aggregated workflow telemetry context for the Codex analysis pass.

Reads `workflow_log_report.json` produced by `scripts/collect_workflow_logs.py`,
computes per-repo / per-workflow-family aggregates plus capped quick-index
lists of failing/slow/recent runs, and writes `analysis_context.json` beside
the resolved output markdown path: when `--output <report.md>` is provided,
the JSON lands at `dirname(<report.md>)/analysis_context.json`; otherwise the
markdown path is derived from `--output-dir` (default `analysis/`) and the
JSON sits in that directory. The Codex pass in
`.github/workflows/workflow-log-analysis.yml` then consumes that JSON as a
quick index; full untruncated logs are read by Codex directly from the
`workflow-log-output` artifact (see `--log-output-dir` in the collector).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
	from cost_audit import aggregate_run_cost_telemetry
except ModuleNotFoundError:
	from scripts.cost_audit import aggregate_run_cost_telemetry

DEFAULT_OUTPUT_DIR = "analysis"
DEFAULT_INPUT_FILE = "workflow_log_report.json"

# Quick-index list caps. Codex reads the full per-run logs from the
# workflow-log-output artifact, so these only need to be wide enough to give
# the model a per-run summary index.
RUN_LIST_CAP = 100
ERRORS_CAP = 250


def _parse_iso8601(value: str | None) -> datetime | None:
	if not value:
		return None
	normalized = value.strip()
	if normalized.endswith("Z"):
		normalized = normalized[:-1] + "+00:00"
	try:
		dt = datetime.fromisoformat(normalized)
	except ValueError:
		return None
	if dt.tzinfo is None:
		return dt.replace(tzinfo=timezone.utc)
	return dt.astimezone(timezone.utc)


def _to_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _percentile(values: list[int], pct: int) -> float:
	if not values:
		return 0.0
	sorted_values = sorted(values)
	if len(sorted_values) == 1:
		return float(sorted_values[0])
	rank = max(0.0, min(1.0, pct / 100.0)) * (len(sorted_values) - 1)
	lower = int(rank)
	upper = min(lower + 1, len(sorted_values) - 1)
	weight = rank - lower
	return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def _load_json_file(path: Path) -> Any:
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError as exc:
		raise ValueError(f"input file not found: {path}") from exc
	except UnicodeDecodeError as exc:
		raise ValueError(f"invalid UTF-8 in {path}: {exc}") from exc
	except OSError as exc:
		raise ValueError(f"unable to read input file {path}: {exc}") from exc
	except json.JSONDecodeError as exc:
		raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Prepare workflow telemetry context for the Codex log analysis pass."
	)
	input_group = parser.add_mutually_exclusive_group(required=False)
	input_group.add_argument(
		"--input",
		help="Path to input JSON payload (collector report or combined analysis bundle).",
	)
	input_group.add_argument(
		"--data-dir",
		default=".",
		help="Directory containing workflow_log_report.json or run_metrics.json + summary_stats.json.",
	)
	output_group = parser.add_mutually_exclusive_group(required=False)
	output_group.add_argument(
		"--output",
		help="Exact output markdown path. If omitted, a dated filename is generated.",
	)
	output_group.add_argument(
		"--output-dir",
		default=DEFAULT_OUTPUT_DIR,
		help="Output directory used to derive the analysis_context.json sibling path.",
	)
	# `--codex-mode` is retained as a no-op alias for backward compatibility
	# with callers that still pass it; the analyzer always emits
	# `analysis_context.json` for the Codex pass to consume.
	parser.add_argument(
		"--codex-mode",
		action="store_true",
		help="Deprecated no-op flag; analyzer always emits analysis_context.json.",
	)
	return parser


def load_input_data(args: argparse.Namespace) -> dict[str, Any]:
	source_files: list[str] = []
	collector_report: dict[str, Any] | None = None
	run_metrics: list[dict[str, Any]] | None = None
	summary_stats: dict[str, Any] | None = None

	if args.input:
		payload_path = Path(args.input)
		raw = _load_json_file(payload_path)
		source_files.append(str(payload_path))
		if isinstance(raw, dict) and isinstance(raw.get("runs"), list):
			collector_report = raw
		elif isinstance(raw, dict):
			run_metrics_value = raw.get("run_metrics")
			if isinstance(run_metrics_value, list):
				run_metrics = [item for item in run_metrics_value if isinstance(item, dict)]
			summary_value = raw.get("summary_stats")
			if isinstance(summary_value, dict):
				summary_stats = summary_value
		elif isinstance(raw, list):
			run_metrics = [item for item in raw if isinstance(item, dict)]
		else:
			raise ValueError("unsupported input payload type; expected JSON object or array")
	else:
		data_dir = Path(args.data_dir)
		if not data_dir.exists() or not data_dir.is_dir():
			raise ValueError(f"data directory not found: {data_dir}")

		collector_path = data_dir / DEFAULT_INPUT_FILE
		if collector_path.exists():
			raw_collector = _load_json_file(collector_path)
			if not isinstance(raw_collector, dict):
				raise ValueError(f"expected JSON object in {collector_path}")
			collector_report = raw_collector
			source_files.append(str(collector_path))

		run_metrics_path = data_dir / "run_metrics.json"
		if run_metrics_path.exists():
			raw_run_metrics = _load_json_file(run_metrics_path)
			if not isinstance(raw_run_metrics, list):
				raise ValueError(f"expected JSON array in {run_metrics_path}")
			run_metrics = [item for item in raw_run_metrics if isinstance(item, dict)]
			source_files.append(str(run_metrics_path))

		summary_path = data_dir / "summary_stats.json"
		if summary_path.exists():
			raw_summary = _load_json_file(summary_path)
			if not isinstance(raw_summary, dict):
				raise ValueError(f"expected JSON object in {summary_path}")
			summary_stats = raw_summary
			source_files.append(str(summary_path))

	if collector_report is None and run_metrics is None and summary_stats is None:
		raise ValueError(
			"no input data found; provide --input or --data-dir with workflow_log_report.json "
			"or run_metrics.json + summary_stats.json"
		)

	return {
		"source_files": source_files,
		"collector_report": collector_report,
		"run_metrics": run_metrics,
		"summary_stats": summary_stats,
	}


def _normalized_run_view(run: dict[str, Any]) -> dict[str, Any]:
	view: dict[str, Any] = {
		"repository": run.get("repository"),
		"run_id": run.get("run_id") or run.get("id"),
		"workflow_name": run.get("workflow_name") or run.get("workflow"),
		"workflow_family": run.get("workflow_family") or run.get("phase") or "unknown",
		"conclusion": run.get("conclusion") or "unknown",
		"duration_seconds": _to_int(run.get("duration_seconds"), 0),
		"run_attempt": _to_int(run.get("run_attempt"), 1),
		"retries": _to_int(run.get("retries"), 0),
		"created_at": run.get("created_at"),
		"failure_point": run.get("failure_point") or {},
	}
	# `log_summary` is populated by scripts/summarize_unselected_runs.py for
	# runs outside the collector's deep-dive top-15. Carry it through so the
	# Codex analysis pass can cite signals from coverage-widened runs.
	log_summary = run.get("log_summary")
	if isinstance(log_summary, str) and log_summary.strip():
		view["log_summary"] = log_summary
	cost_telemetry = run.get("cost_telemetry")
	if isinstance(cost_telemetry, dict) and cost_telemetry:
		view["cost_telemetry"] = dict(cost_telemetry)
	return view


def _summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
	durations = [_to_int(run.get("duration_seconds"), 0) for run in runs]
	success_count = sum(1 for run in runs if (run.get("conclusion") or "").lower() == "success")
	failure_count = sum(1 for run in runs if (run.get("conclusion") or "").lower() == "failure")
	cancelled_count = sum(1 for run in runs if (run.get("conclusion") or "").lower() == "cancelled")
	other_count = sum(
		1
		for run in runs
		if (run.get("conclusion") or "").lower() not in {"success", "failure", "cancelled"}
	)
	avg_duration = float(sum(durations) / len(durations)) if durations else 0.0
	return {
		"total_runs": len(runs),
		"success_count": success_count,
		"failure_count": failure_count,
		"cancelled_count": cancelled_count,
		"other_count": other_count,
		"avg_duration_seconds": avg_duration,
		"p50_duration_seconds": _percentile(durations, 50),
		"p95_duration_seconds": _percentile(durations, 95),
		"cost_telemetry": aggregate_run_cost_telemetry(runs),
	}


def _aggregate_runs_by_key(runs: list[dict[str, Any]], key_name: str) -> dict[str, dict[str, Any]]:
	agg: dict[str, dict[str, Any]] = {}
	for run in runs:
		key_value = str(run.get(key_name) or "unknown")
		entry = agg.setdefault(
			key_value,
			{
				"total_runs": 0,
				"success_count": 0,
				"failure_count": 0,
				"cancelled_count": 0,
				"other_count": 0,
				"durations": [],
				"rows": [],
			},
		)
		entry["total_runs"] += 1
		conclusion = (run.get("conclusion") or "").lower()
		if conclusion == "success":
			entry["success_count"] += 1
		elif conclusion == "failure":
			entry["failure_count"] += 1
		elif conclusion == "cancelled":
			entry["cancelled_count"] += 1
		else:
			entry["other_count"] += 1
		entry["durations"].append(_to_int(run.get("duration_seconds"), 0))
		entry["rows"].append(run)

	result: dict[str, dict[str, Any]] = {}
	for key_value, entry in agg.items():
		durations = [int(v) for v in entry.pop("durations") if isinstance(v, int)]
		rows = [item for item in entry.pop("rows") if isinstance(item, dict)]
		total_runs = max(1, _to_int(entry.get("total_runs"), 1))
		entry["avg_duration_seconds"] = float(sum(durations) / len(durations)) if durations else 0.0
		entry["p50_duration_seconds"] = _percentile(durations, 50)
		entry["p95_duration_seconds"] = _percentile(durations, 95)
		entry["failure_rate"] = float(entry.get("failure_count", 0) / total_runs)
		entry["cost_telemetry"] = aggregate_run_cost_telemetry(rows)
		result[key_value] = entry
	return result


def prepare_analysis_context(input_data: dict[str, Any]) -> dict[str, Any]:
	collector_report = input_data.get("collector_report")
	run_metrics = input_data.get("run_metrics")
	summary_stats = input_data.get("summary_stats")

	runs: list[dict[str, Any]] = []
	scope: dict[str, Any] = {}
	errors: list[dict[str, Any]] = []
	summary: dict[str, Any] = {}

	if isinstance(collector_report, dict):
		raw_runs = collector_report.get("runs")
		if isinstance(raw_runs, list):
			runs = [item for item in raw_runs if isinstance(item, dict)]
		raw_scope = collector_report.get("scope")
		if isinstance(raw_scope, dict):
			scope = raw_scope
		raw_summary = collector_report.get("summary")
		if isinstance(raw_summary, dict):
			summary = raw_summary
		raw_errors = collector_report.get("errors")
		if isinstance(raw_errors, list):
			errors = [item for item in raw_errors if isinstance(item, dict)]

	if not runs and isinstance(run_metrics, list):
		runs = [item for item in run_metrics if isinstance(item, dict)]

	if not summary:
		overall = None
		if isinstance(summary_stats, dict):
			overall_value = summary_stats.get("overall")
			if isinstance(overall_value, dict):
				overall = overall_value
		if isinstance(overall, dict):
			summary = overall
		else:
			summary = _summarize_runs(runs)
	summary = dict(summary)
	summary["cost_telemetry"] = aggregate_run_cost_telemetry(runs)

	per_repo: dict[str, dict[str, Any]] = {}
	per_workflow_family: dict[str, dict[str, Any]] = {}
	if isinstance(summary_stats, dict):
		candidate_per_repo = summary_stats.get("per_repo")
		if isinstance(candidate_per_repo, dict):
			per_repo = {
				str(key): value
				for key, value in candidate_per_repo.items()
				if isinstance(value, dict)
			}
		candidate_per_family = summary_stats.get("per_workflow_family") or summary_stats.get("per_phase")
		if isinstance(candidate_per_family, dict):
			per_workflow_family = {
				str(key): value
				for key, value in candidate_per_family.items()
				if isinstance(value, dict)
			}

	if not per_repo:
		per_repo = _aggregate_runs_by_key(runs, "repository")
	else:
		computed_per_repo = _aggregate_runs_by_key(runs, "repository")
		for key, value in computed_per_repo.items():
			entry = per_repo.setdefault(key, {})
			if isinstance(entry, dict):
				entry["cost_telemetry"] = value.get("cost_telemetry")
	if not per_workflow_family:
		per_workflow_family = _aggregate_runs_by_key(runs, "workflow_family")
	else:
		computed_per_family = _aggregate_runs_by_key(runs, "workflow_family")
		for key, value in computed_per_family.items():
			entry = per_workflow_family.setdefault(key, {})
			if isinstance(entry, dict):
				entry["cost_telemetry"] = value.get("cost_telemetry")

	normalized_runs = [_normalized_run_view(run) for run in runs]
	failing_runs = [
		run for run in normalized_runs if (run.get("conclusion") or "").lower() == "failure"
	]
	failing_runs.sort(key=lambda item: item.get("duration_seconds") or 0, reverse=True)

	slow_runs = list(normalized_runs)
	slow_runs.sort(key=lambda item: item.get("duration_seconds") or 0, reverse=True)

	recent_runs = list(normalized_runs)
	recent_runs.sort(
		key=lambda item: _parse_iso8601(item.get("created_at") or "") or datetime.min.replace(tzinfo=timezone.utc),
		reverse=True,
	)

	total_runs = _to_int(summary.get("total_runs"), len(runs))
	insufficient_data = total_runs <= 0

	# `deep_dive_logs` is intentionally omitted from this payload — full
	# untruncated step logs are exported by the collector under
	# `--log-output-dir` and shipped via the `workflow-log-output` artifact
	# for Codex to read directly.
	return {
		"source_files": input_data.get("source_files") or [],
		"scope": scope,
		"summary": summary,
		"per_repo": per_repo,
		"per_workflow_family": per_workflow_family,
		"failing_runs": failing_runs[:RUN_LIST_CAP],
		"slow_runs": slow_runs[:RUN_LIST_CAP],
		"recent_runs": recent_runs[:RUN_LIST_CAP],
		"errors": errors[:ERRORS_CAP],
		"insufficient_data": insufficient_data,
		"analysis_guidance": (
			"No workflow runs were available in the selected window. "
			"Explain that data is insufficient and list concrete next collection steps."
			if insufficient_data
			else ""
		),
	}


def resolve_dated_output_path(
	output_path: str | None,
	output_dir: str,
	*,
	today: date | None = None,
) -> Path:
	if output_path:
		return Path(output_path)

	resolved_today = today or datetime.now(timezone.utc).date()
	base_name = f"workflow-optimization-{resolved_today.isoformat()}"
	base_path = Path(output_dir) / f"{base_name}.md"
	if not base_path.exists():
		return base_path

	suffix = 2
	while True:
		candidate = Path(output_dir) / f"{base_name}-{suffix}.md"
		if not candidate.exists():
			return candidate
		suffix += 1


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path = path.with_suffix(path.suffix + ".tmp")
	tmp_path.write_text(
		json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	tmp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	try:
		input_data = load_input_data(args)
	except ValueError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 2

	analysis_context = prepare_analysis_context(input_data)

	context_output_base = resolve_dated_output_path(args.output, args.output_dir)
	context_path = context_output_base.parent / "analysis_context.json"
	try:
		_write_json_atomic(context_path, analysis_context)
	except OSError as exc:
		print(f"ERROR: failed to write analysis context {context_path}: {exc}", file=sys.stderr)
		return 1

	print(str(context_path))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
