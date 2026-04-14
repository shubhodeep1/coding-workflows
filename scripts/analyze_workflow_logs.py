#!/usr/bin/env python3
"""Generate dated workflow optimization reports from workflow telemetry."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
	from openrouter_prompt_cache import (
		add_ephemeral_cache_breakpoint,
		format_usage_value,
		is_cache_disabled,
		normalize_usage,
		should_retry_without_breakpoint,
	)
except ModuleNotFoundError:
	from scripts.openrouter_prompt_cache import (
		add_ephemeral_cache_breakpoint,
		format_usage_value,
		is_cache_disabled,
		normalize_usage,
		should_retry_without_breakpoint,
	)

DEFAULT_MODEL = "openai/gpt-5.3-codex"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_PROMPT_FILE = "prompts/mode-workflow-analysis.txt"
DEFAULT_OUTPUT_DIR = "analysis"
DEFAULT_INPUT_FILE = "workflow_log_report.json"
DEFAULT_BATCH_PROVIDER = "auto"
DEFAULT_BATCH_POLL_TIMEOUT_HOURS = 24
DEFAULT_BATCH_API_DISABLED = "false"
DEFAULT_BATCH_STATE_SCHEMA_VERSION = "workflow_log_analysis_batch_state.v1"
PENDING_BATCH_STATUSES = {"pending", "submitted", "queued", "in_progress"}
SERENA_PLACEHOLDER_PATTERN = re.compile(r"\{\{SERENA_EFFICIENCY_BLOCK_[A-Z_]+\}\}")


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


def _estimate_tokens(payload: Any) -> int:
	if isinstance(payload, str):
		text = payload
	else:
		text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
	return max(1, len(text) // 4)


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


def _collect_deep_dive_logs(run_logs_dir: Path) -> list[dict[str, str]]:
	if not run_logs_dir.exists() or not run_logs_dir.is_dir():
		return []
	entries: list[dict[str, str]] = []
	for log_file in sorted(path for path in run_logs_dir.iterdir() if path.is_file()):
		try:
			with log_file.open("r", encoding="utf-8", errors="replace") as handle:
				excerpt = handle.read(4000)
		except OSError:
			continue
		entries.append(
			{
				"name": log_file.name,
				"excerpt": excerpt,
			}
		)
	return entries


def _collect_deep_dive_logs_from_runs(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
	entries: list[dict[str, str]] = []
	for run in runs:
		repository = str(run.get("repository") or "").strip()
		run_id = _to_int(run.get("run_id"), 0)
		if not repository or run_id <= 0:
			continue
		log_excerpts = run.get("log_excerpts")
		if not isinstance(log_excerpts, list):
			continue
		for item in log_excerpts:
			if not isinstance(item, dict):
				continue
			step_name = item.get("step_name")
			excerpt = item.get("excerpt")
			if not isinstance(step_name, str) or not isinstance(excerpt, str):
				continue
			entries.append(
				{
					"name": f"{repository}/{run_id}/{step_name}",
					"excerpt": excerpt,
				}
			)
	return entries


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Analyze workflow log telemetry with OpenRouter and write a dated markdown report."
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
		help="Output directory for generated dated markdown reports.",
	)
	parser.add_argument(
		"--prompt-file",
		default=DEFAULT_PROMPT_FILE,
		help="Path to system prompt template.",
	)
	parser.add_argument(
		"--model",
		default=os.getenv("WORKFLOW_EDITOR_MODEL", DEFAULT_MODEL),
		help="OpenRouter model ID.",
	)
	parser.add_argument(
		"--thinking-level",
		choices=["xhigh", "high", "medium", "low"],
		default="medium",
		help="Reasoning effort for the OpenRouter request.",
	)
	parser.add_argument(
		"--max-prompt-tokens",
		"--prompt-token-budget",
		dest="prompt_token_budget",
		type=int,
		default=24000,
		help="Approximate max prompt tokens before deterministic truncation.",
	)
	parser.add_argument(
		"--max-output-tokens",
		type=int,
		default=100000,
		help="Max completion tokens for the analysis report.",
	)
	parser.add_argument(
		"--temperature",
		type=float,
		default=0.0,
		help="Sampling temperature for the chat completion.",
	)
	parser.add_argument(
		"--openrouter-base-url",
		default=DEFAULT_OPENROUTER_BASE_URL,
		help="Base URL for OpenRouter API.",
	)
	parser.add_argument(
		"--request-timeout-seconds",
		type=int,
		default=90,
		help="HTTP timeout in seconds for OpenRouter request.",
	)
	parser.add_argument(
		"--batch-mode",
		choices=["auto", "submit", "poll", "sync"],
		default="auto",
		help="Batch flow mode: auto submit/poll, forced submit, forced poll, or sync only.",
	)
	parser.add_argument(
		"--batch-state-file",
		default="",
		help="Path to persisted batch state JSON for deferred polling.",
	)
	parser.add_argument(
		"--batch-provider",
		default=os.getenv("BATCH_API_PROVIDER", DEFAULT_BATCH_PROVIDER),
		help="Batch provider routing hint.",
	)
	parser.add_argument(
		"--batch-api-disabled",
		default=os.getenv("BATCH_API_DISABLED", DEFAULT_BATCH_API_DISABLED),
		help="Disable batch API path when set to true.",
	)
	parser.add_argument(
		"--batch-poll-timeout-hours",
		type=int,
		default=_to_int(
			os.getenv("BATCH_API_POLL_TIMEOUT_HOURS", str(DEFAULT_BATCH_POLL_TIMEOUT_HOURS)),
			DEFAULT_BATCH_POLL_TIMEOUT_HOURS,
		),
		help="Maximum age of pending batch job before sync fallback.",
	)
	return parser


def load_input_data(args: argparse.Namespace) -> dict[str, Any]:
	source_files: list[str] = []
	collector_report: dict[str, Any] | None = None
	run_metrics: list[dict[str, Any]] | None = None
	summary_stats: dict[str, Any] | None = None
	deep_dive_logs: list[dict[str, str]] = []

	if args.input:
		payload_path = Path(args.input)
		raw = _load_json_file(payload_path)
		source_files.append(str(payload_path))
		if isinstance(raw, dict) and isinstance(raw.get("runs"), list):
			collector_report = raw
			deep_dive_logs = _collect_deep_dive_logs_from_runs(raw.get("runs") or [])
		elif isinstance(raw, dict):
			run_metrics_value = raw.get("run_metrics")
			if isinstance(run_metrics_value, list):
				run_metrics = [item for item in run_metrics_value if isinstance(item, dict)]
			summary_value = raw.get("summary_stats")
			if isinstance(summary_value, dict):
				summary_stats = summary_value
			run_logs_value = raw.get("deep_dive_logs")
			if isinstance(run_logs_value, list):
				deep_dive_logs = [
					item
					for item in run_logs_value
					if isinstance(item, dict)
					and isinstance(item.get("name"), str)
					and isinstance(item.get("excerpt"), str)
				]
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
			if isinstance(raw_collector.get("runs"), list):
				deep_dive_logs = _collect_deep_dive_logs_from_runs(raw_collector.get("runs") or [])
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

		run_logs_dir = data_dir / "run_logs"
		run_logs = _collect_deep_dive_logs(run_logs_dir)
		if run_logs:
			deep_dive_logs.extend(run_logs)
			source_files.append(str(run_logs_dir))

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
		"deep_dive_logs": deep_dive_logs,
	}


def _normalized_run_view(run: dict[str, Any]) -> dict[str, Any]:
	return {
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

	result: dict[str, dict[str, Any]] = {}
	for key_value, entry in agg.items():
		durations = [int(v) for v in entry.pop("durations") if isinstance(v, int)]
		total_runs = max(1, _to_int(entry.get("total_runs"), 1))
		entry["avg_duration_seconds"] = float(sum(durations) / len(durations)) if durations else 0.0
		entry["p50_duration_seconds"] = _percentile(durations, 50)
		entry["p95_duration_seconds"] = _percentile(durations, 95)
		entry["failure_rate"] = float(entry.get("failure_count", 0) / total_runs)
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
	if not per_workflow_family:
		per_workflow_family = _aggregate_runs_by_key(runs, "workflow_family")

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

	return {
		"source_files": input_data.get("source_files") or [],
		"scope": scope,
		"summary": summary,
		"per_repo": per_repo,
		"per_workflow_family": per_workflow_family,
		"failing_runs": failing_runs[:25],
		"slow_runs": slow_runs[:25],
		"recent_runs": recent_runs[:25],
		"errors": errors[:100],
		"deep_dive_logs": input_data.get("deep_dive_logs") or [],
		"insufficient_data": insufficient_data,
		"analysis_guidance": (
			"No workflow runs were available in the selected window. "
			"Explain that data is insufficient and list concrete next collection steps."
			if insufficient_data
			else ""
		),
	}


def truncate_to_budget(context: dict[str, Any], prompt_token_budget: int) -> dict[str, Any]:
	trimmed = copy.deepcopy(context)
	removed = {
		"deep_dive_logs": 0,
		"recent_runs": 0,
		"slow_runs": 0,
		"failing_runs": 0,
		"errors": 0,
		"per_repo": 0,
		"per_workflow_family": 0,
	}

	if prompt_token_budget <= 0:
		trimmed["truncation"] = {
			"estimated_tokens": _estimate_tokens(trimmed),
			"prompt_token_budget": prompt_token_budget,
			"removed": removed,
		}
		return trimmed

	def estimate() -> int:
		return _estimate_tokens(trimmed)

	def trim_list_field(field_name: str) -> None:
		nonlocal trimmed
		while estimate() > prompt_token_budget and isinstance(trimmed.get(field_name), list) and trimmed[field_name]:
			trimmed[field_name].pop()
			removed[field_name] += 1

	for field in ("deep_dive_logs", "recent_runs", "slow_runs", "failing_runs", "errors"):
		trim_list_field(field)

	for field in ("per_repo", "per_workflow_family"):
		field_dict = trimmed.get(field)
		if not isinstance(field_dict, dict) or not field_dict:
			continue
		ordered_keys = sorted(
			field_dict.keys(),
			key=lambda key: (_to_int((field_dict.get(key) or {}).get("total_runs"), 0), key),
		)
		for key in ordered_keys:
			if estimate() <= prompt_token_budget:
				break
			del field_dict[key]
			removed[field] += 1

	trimmed["truncation"] = {
		"estimated_tokens": estimate(),
		"prompt_token_budget": prompt_token_budget,
		"removed": removed,
		"budget_exceeded": estimate() > prompt_token_budget,
	}
	return trimmed


def build_prompt_messages(prompt_template: str, analysis_context: dict[str, Any]) -> list[dict[str, str]]:
	context_json = json.dumps(analysis_context, ensure_ascii=True, indent=2, sort_keys=True)
	user_content = (
		"Analyze the workflow telemetry payload and produce a markdown optimization report.\n"
		"If `insufficient_data` is true, explicitly state that the data window is insufficient and "
		"provide concrete next collection steps.\n\n"
		"Telemetry payload:\n"
		"```json\n"
		f"{context_json}\n"
		"```\n"
	)
	return [
		{"role": "system", "content": prompt_template.strip()},
		{"role": "user", "content": user_content},
	]


def _extract_content_value(content: Any) -> str:
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		parts: list[str] = []
		for item in content:
			if isinstance(item, str):
				parts.append(item)
			elif isinstance(item, dict):
				text_value = item.get("text")
				if isinstance(text_value, str):
					parts.append(text_value)
		return "\n".join(part for part in parts if part)
	return ""


def _is_truthy(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	normalized = str(value or "").strip().lower()
	return normalized in {"1", "true", "yes", "on"}


def _normalize_batch_provider(value: str | None) -> str:
	normalized = str(value or "").strip().lower()
	if normalized in {"openai", "anthropic"}:
		return normalized
	return "auto"


def _batch_log(event: str, *, severity: str = "INFO", **fields: Any) -> None:
	payload: dict[str, Any] = {"event": event}
	payload.update(fields)
	message = json.dumps(payload, ensure_ascii=True, sort_keys=True)
	normalized_severity = severity.upper()
	if normalized_severity == "WARNING":
		print(f"::warning::{message}", file=sys.stderr)
	elif normalized_severity == "ERROR":
		print(f"::error::{message}", file=sys.stderr)
	print(f"{normalized_severity}: {message}", file=sys.stderr)


def _openrouter_request_json(
	*,
	api_key: str,
	base_url: str,
	path: str,
	request_timeout_seconds: int,
	method: str,
	payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
	request_data = None
	headers = {
		"Authorization": f"Bearer {api_key}",
	}
	if payload is not None:
		request_data = json.dumps(payload).encode("utf-8")
		headers["Content-Type"] = "application/json"
	request = urllib.request.Request(
		f"{base_url.rstrip('/')}/{path.lstrip('/')}",
		data=request_data,
		headers=headers,
		method=method,
	)
	try:
		with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
			response_text = response.read().decode("utf-8")
	except urllib.error.HTTPError as exc:
		try:
			error_text = exc.read().decode("utf-8", errors="replace")
		except Exception:  # noqa: BLE001
			error_text = ""
		raise RuntimeError(f"OpenRouter request failed (HTTP {exc.code}): {error_text or exc.reason}") from exc
	except (urllib.error.URLError, OSError, TimeoutError) as exc:
		raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

	try:
		response_payload = json.loads(response_text)
	except json.JSONDecodeError as exc:
		raise RuntimeError(f"OpenRouter returned invalid JSON: {exc}") from exc

	if not isinstance(response_payload, dict):
		raise RuntimeError("OpenRouter returned non-object JSON response")
	return response_payload


def _extract_endpoint_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
	entries: list[dict[str, Any]] = []
	for key in ("data", "endpoints"):
		value = payload.get(key)
		if isinstance(value, list):
			for item in value:
				if isinstance(item, dict):
					entries.append(item)
		if isinstance(value, dict):
			for subvalue in value.values():
				if isinstance(subvalue, dict):
					entries.append(subvalue)
	if isinstance(payload.get("result"), list):
		for item in payload["result"]:
			if isinstance(item, dict):
				entries.append(item)
	return entries


def _entry_supports_background(entry: dict[str, Any]) -> bool:
	if _is_truthy(entry.get("supports_background")) or _is_truthy(entry.get("background")):
		return True
	for key in ("parameters", "supported_parameters", "params"):
		value = entry.get(key)
		if isinstance(value, list):
			for param in value:
				if isinstance(param, str) and param.strip().lower() == "background":
					return True
				if isinstance(param, dict):
					name = str(param.get("name") or "").strip().lower()
					if name == "background":
						return True
	return False


def _entry_matches_responses(entry: dict[str, Any]) -> bool:
	for key in ("endpoint", "name", "path", "type", "id"):
		value = str(entry.get(key) or "").strip().lower()
		if "responses" in value:
			return True
		if "chat/completions" in value:
			return False
	return False


def _entry_supports_provider(entry: dict[str, Any], provider_hint: str) -> bool:
	if provider_hint == "auto":
		return True
	for key in ("provider", "providers", "supported_providers"):
		value = entry.get(key)
		if isinstance(value, str):
			if provider_hint == value.strip().lower():
				return True
		if isinstance(value, list):
			for item in value:
				if isinstance(item, str) and provider_hint == item.strip().lower():
					return True
				if isinstance(item, dict):
					name = str(item.get("name") or item.get("slug") or "").strip().lower()
					if provider_hint == name:
						return True
	return False


def _batch_capability_decision(
	*,
	api_key: str,
	model: str,
	base_url: str,
	request_timeout_seconds: int,
	provider_hint: str,
) -> tuple[str, str]:
	if "/" not in model:
		return "unsupported", "model_id_missing_author"
	author, slug = model.split("/", 1)
	if not author.strip() or not slug.strip():
		return "unsupported", "model_id_missing_author"
	path = f"models/{urllib.parse.quote(author, safe='')}/{urllib.parse.quote(slug, safe='')}/endpoints"
	try:
		payload = _openrouter_request_json(
			api_key=api_key,
			base_url=base_url,
			path=path,
			request_timeout_seconds=request_timeout_seconds,
			method="GET",
		)
	except RuntimeError as exc:
		return "unknown", f"capability_probe_failed:{exc}"

	entries = _extract_endpoint_entries(payload)
	if not entries:
		return "unknown", "capability_schema_unknown"

	response_entries = [entry for entry in entries if _entry_matches_responses(entry)]
	if not response_entries:
		return "unsupported", "responses_endpoint_missing"

	provider_compatible: list[dict[str, Any]] = []
	for entry in response_entries:
		if _entry_supports_provider(entry, provider_hint):
			provider_compatible.append(entry)
	if provider_hint != "auto" and not provider_compatible:
		return "unsupported", "provider_not_supported"

	background_compatible = [entry for entry in provider_compatible or response_entries if _entry_supports_background(entry)]
	if not background_compatible:
		return "unsupported", "background_param_not_supported"
	return "supported", "batch_supported"


def _build_batch_state(
	*,
	batch_id: str,
	status: str,
	provider_hint: str,
	model: str,
	poll_timeout_hours: int,
	submitted_at: str,
	last_polled_at: str | None = None,
	fallback_reason: str | None = None,
) -> dict[str, Any]:
	source_run_id = str(os.getenv("GITHUB_RUN_ID", "")).strip()
	state: dict[str, Any] = {
		"schema_version": DEFAULT_BATCH_STATE_SCHEMA_VERSION,
		"batch_id": batch_id,
		"status": status,
		"submitted_at": submitted_at,
		"provider_hint": provider_hint,
		"model": model,
		"poll_timeout_hours": poll_timeout_hours,
		"source_workflow": str(os.getenv("GITHUB_WORKFLOW", "workflow-log-analysis") or "workflow-log-analysis"),
		"source_run_id": source_run_id,
	}
	if last_polled_at:
		state["last_polled_at"] = last_polled_at
	if fallback_reason:
		state["fallback_reason"] = fallback_reason
	return state


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
		json.dump(payload, tmp, ensure_ascii=True, indent=2, sort_keys=True)
		tmp.write("\n")
		tmp_path = Path(tmp.name)
	tmp_path.replace(path)


def _load_batch_state(path: Path) -> dict[str, Any] | None:
	if not path.exists() or not path.is_file():
		return None
	try:
		raw = _load_json_file(path)
	except ValueError:
		return None
	if not isinstance(raw, dict):
		return None
	batch_id = str(raw.get("batch_id") or "").strip()
	status = str(raw.get("status") or "").strip().lower()
	if not batch_id or not status:
		return None
	raw["batch_id"] = batch_id
	raw["status"] = status
	return raw


def _extract_response_text(payload: dict[str, Any]) -> str:
	text_fields = [
		payload.get("output_text"),
		(payload.get("response") or {}).get("output_text") if isinstance(payload.get("response"), dict) else None,
	]
	for candidate in text_fields:
		if isinstance(candidate, str) and candidate.strip():
			return candidate

	output = payload.get("output")
	if isinstance(output, list):
		parts: list[str] = []
		for item in output:
			if not isinstance(item, dict):
				continue
			content = item.get("content")
			if isinstance(content, list):
				for block in content:
					if not isinstance(block, dict):
						continue
					text_value = block.get("text")
					if isinstance(text_value, str) and text_value:
						parts.append(text_value)
		if parts:
			return "\n".join(parts)

	choices = payload.get("choices")
	if isinstance(choices, list) and choices:
		first_choice = choices[0] if isinstance(choices[0], dict) else {}
		message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
		content = _extract_content_value(message.get("content"))
		if content.strip():
			return content

	return ""


def submit_openrouter_batch(
	*,
	api_key: str,
	model: str,
	messages: list[dict[str, str]],
	temperature: float,
	max_tokens: int,
	base_url: str,
	request_timeout_seconds: int,
	thinking_level: str | None,
	provider_hint: str,
) -> tuple[str, str]:
	input_items: list[dict[str, Any]] = []
	for message in messages:
		role = str(message.get("role") or "user").strip() or "user"
		content_text = str(message.get("content") or "")
		input_items.append(
			{
				"role": role,
				"content": [{"type": "input_text", "text": content_text}],
			}
		)

	payload: dict[str, Any] = {
		"model": model,
		"input": input_items,
		"background": True,
		"temperature": temperature,
		"max_output_tokens": max_tokens,
	}
	normalized_thinking_level = ""
	if thinking_level is not None:
		normalized_thinking_level = str(thinking_level).strip()
	if normalized_thinking_level:
		payload["reasoning"] = normalized_thinking_level
	if provider_hint in {"openai", "anthropic"}:
		payload["provider"] = {"order": [provider_hint]}

	response_payload = _openrouter_request_json(
		api_key=api_key,
		base_url=base_url,
		path="responses",
		request_timeout_seconds=request_timeout_seconds,
		method="POST",
		payload=payload,
	)
	batch_id = str(response_payload.get("id") or "").strip()
	if not batch_id:
		raise RuntimeError("OpenRouter batch submit response missing id")
	status = str(response_payload.get("status") or "submitted").strip().lower() or "submitted"
	return batch_id, status


def poll_openrouter_batch(
	*,
	api_key: str,
	base_url: str,
	batch_id: str,
	request_timeout_seconds: int,
) -> tuple[str, str, dict[str, Any], str, dict[str, Any] | None]:
	response_payload = _openrouter_request_json(
		api_key=api_key,
		base_url=base_url,
		path=f"responses/{urllib.parse.quote(batch_id, safe='')}",
		request_timeout_seconds=request_timeout_seconds,
		method="GET",
	)
	status = str(response_payload.get("status") or "unknown").strip().lower() or "unknown"
	content = _extract_response_text(response_payload).rstrip()
	usage_value = response_payload.get("usage")
	usage = usage_value if isinstance(usage_value, dict) else None
	batch_status = str(response_payload.get("status") or "unknown").strip().lower() or "unknown"
	return status, batch_status, response_payload, content, usage


def call_openrouter(
	*,
	api_key: str,
	model: str,
	messages: list[dict[str, str]],
	temperature: float,
	max_tokens: int,
	base_url: str,
	request_timeout_seconds: int,
	thinking_level: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
	base_messages = [dict(message) for message in messages]
	messages_with_breakpoint, breakpoint_enabled = add_ephemeral_cache_breakpoint(
		base_messages,
		model_id=model,
	)
	request_messages = messages_with_breakpoint
	had_cache_fallback_retry = False

	def build_payload(payload_messages: list[dict[str, Any]]) -> dict[str, Any]:
		payload_data = {
			"model": model,
			"messages": payload_messages,
			"temperature": temperature,
			"max_tokens": max_tokens,
		}
		normalized_thinking_level = ""
		if thinking_level is not None:
			normalized_thinking_level = str(thinking_level).strip()
		if normalized_thinking_level:
			payload_data["reasoning"] = normalized_thinking_level
		return payload_data

	response_text = ""
	while True:
		payload = build_payload(request_messages)
		request_body = json.dumps(payload).encode("utf-8")
		request = urllib.request.Request(
			f"{base_url.rstrip('/')}/chat/completions",
			data=request_body,
			headers={
				"Content-Type": "application/json",
				"Authorization": f"Bearer {api_key}",
			},
			method="POST",
		)

		try:
			with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
				response_text = response.read().decode("utf-8")
			break
		except urllib.error.HTTPError as exc:
			try:
				error_text = exc.read().decode("utf-8", errors="replace")
			except Exception:  # noqa: BLE001
				error_text = ""
			if (
				breakpoint_enabled
				and not had_cache_fallback_retry
				and should_retry_without_breakpoint(exc.code, error_text)
			):
				had_cache_fallback_retry = True
				request_messages = base_messages
				continue
			raise RuntimeError(
				f"OpenRouter request failed (HTTP {exc.code}): {error_text or exc.reason}"
			) from exc
		except (urllib.error.URLError, OSError, TimeoutError) as exc:
			raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

	try:
		response_payload = json.loads(response_text)
	except json.JSONDecodeError as exc:
		raise RuntimeError(f"OpenRouter returned invalid JSON: {exc}") from exc

	if not isinstance(response_payload, dict):
		raise RuntimeError("OpenRouter returned non-object JSON response")

	choices = response_payload.get("choices")
	if not isinstance(choices, list) or not choices:
		raise RuntimeError("OpenRouter response did not include choices")
	first_choice = choices[0] if isinstance(choices[0], dict) else {}
	message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
	content = _extract_content_value(message.get("content"))
	if not content.strip():
		raise RuntimeError("OpenRouter response did not include message content")

	usage_value = response_payload.get("usage")
	usage = normalize_usage(usage_value) if isinstance(usage_value, dict) else None
	if usage is not None:
		usage["cache_breakpoint_enabled"] = breakpoint_enabled and not is_cache_disabled()
		usage["cache_breakpoint_fallback_retry"] = had_cache_fallback_retry
	return content.rstrip() + "\n", usage


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


def write_report_atomic(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
		tmp.write(content)
		tmp_path = Path(tmp.name)
	tmp_path.replace(path)


def load_prompt_template(path: Path) -> str:
	prompt_path = path.resolve()
	try:
		template = prompt_path.read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError) as exc:
		raise RuntimeError(f"unable to read prompt file {path}: {exc}") from exc
	if not SERENA_PLACEHOLDER_PATTERN.search(template):
		return template

	render_script = Path(__file__).resolve().with_name("render_prompt.sh")
	if not render_script.is_file():
		raise RuntimeError(
			f"prompt contains Serena placeholders but renderer not found at expected location: {render_script}"
		)

	# scripts/render_prompt.sh is expected at <repo_root>/scripts/render_prompt.sh.
	repo_root = render_script.parent.parent
	try:
		result = subprocess.run(
			["bash", str(render_script), str(prompt_path)],
			check=True,
			capture_output=True,
			text=True,
			cwd=repo_root,
		)
	except OSError as exc:
		raise RuntimeError(f"unable to execute prompt renderer for {path}: {exc}") from exc
	except subprocess.CalledProcessError as exc:
		stderr = (exc.stderr or "").strip()
		detail = f": {stderr}" if stderr else ""
		raise RuntimeError(f"unable to render prompt file {path}{detail}") from exc
	return result.stdout


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	if args.prompt_token_budget <= 0:
		print("ERROR: --prompt-token-budget must be > 0", file=sys.stderr)
		return 2
	if args.max_output_tokens <= 0:
		print("ERROR: --max-output-tokens must be > 0", file=sys.stderr)
		return 2
	if args.request_timeout_seconds <= 0:
		print("ERROR: --request-timeout-seconds must be > 0", file=sys.stderr)
		return 2
	if args.batch_poll_timeout_hours <= 0:
		print("ERROR: --batch-poll-timeout-hours must be > 0", file=sys.stderr)
		return 2

	api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
	if not api_key:
		print("ERROR: OPENROUTER_API_KEY is required", file=sys.stderr)
		return 2

	try:
		input_data = load_input_data(args)
	except ValueError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 2

	prompt_path = Path(args.prompt_file)
	try:
		prompt_template = load_prompt_template(prompt_path)
	except RuntimeError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 2

	analysis_context = prepare_analysis_context(input_data)
	trimmed_context = truncate_to_budget(analysis_context, args.prompt_token_budget)
	messages = build_prompt_messages(prompt_template, trimmed_context)

	model = str(args.model or DEFAULT_MODEL)
	provider_hint = _normalize_batch_provider(args.batch_provider)
	batch_disabled = _is_truthy(args.batch_api_disabled)
	batch_mode = str(args.batch_mode or "auto").strip().lower() or "auto"
	state_path = Path(str(args.batch_state_file or "").strip()) if str(args.batch_state_file or "").strip() else None
	state = _load_batch_state(state_path) if state_path is not None else None
	now = datetime.now(timezone.utc)

	def run_sync(reason: str) -> int:
		state_for_fallback = _load_batch_state(state_path) if state_path is not None else None
		state_for_fallback_status = str((state_for_fallback or {}).get("status") or "").strip().lower()
		if state_path is not None and state_for_fallback is not None and state_for_fallback_status in PENDING_BATCH_STATUSES:
			fallback_batch_id = str(state_for_fallback.get("batch_id") or "").strip()
			if fallback_batch_id:
				fallback_state = _build_batch_state(
					batch_id=fallback_batch_id,
					status="fallback_sync",
					provider_hint=provider_hint,
					model=model,
					poll_timeout_hours=int(args.batch_poll_timeout_hours),
					submitted_at=str(state_for_fallback.get("submitted_at") or now.isoformat()),
					last_polled_at=now.isoformat(),
					fallback_reason=reason,
				)
				try:
					_write_json_atomic(state_path, fallback_state)
				except OSError as exc:
					_batch_log(
						"batch_fallback",
						severity="WARNING",
						reason=f"fallback_state_write_failed:{exc}",
						provider=provider_hint,
						model=model,
						batch_id=fallback_batch_id,
					)

		_batch_log(
			"batch_fallback",
			severity="WARNING",
			reason=reason,
			provider=provider_hint,
			model=model,
		)
		try:
			report_markdown, usage = call_openrouter(
				api_key=api_key,
				model=model,
				messages=messages,
				temperature=float(args.temperature),
				max_tokens=args.max_output_tokens,
				base_url=args.openrouter_base_url,
				request_timeout_seconds=args.request_timeout_seconds,
				thinking_level=args.thinking_level,
			)
		except RuntimeError as exc:
			print(f"ERROR: {exc}", file=sys.stderr)
			return 1

		output_path = resolve_dated_output_path(args.output, args.output_dir)
		try:
			write_report_atomic(output_path, report_markdown)
		except OSError as exc:
			print(f"ERROR: failed to write report {output_path}: {exc}", file=sys.stderr)
			return 1

		if usage is not None:
			prompt_tokens = _to_int(usage.get("prompt_tokens"), 0)
			completion_tokens = _to_int(usage.get("completion_tokens"), 0)
			total_tokens = _to_int(usage.get("total_tokens"), 0)
			cache_creation_tokens = usage.get("cache_creation_input_tokens")
			cache_read_tokens = usage.get("cache_read_input_tokens")
			cache_breakpoint_enabled = usage.get("cache_breakpoint_enabled")
			cache_breakpoint_retry = usage.get("cache_breakpoint_fallback_retry")
			cache_enabled = "false" if is_cache_disabled() else "true"
			print(
				"INFO: openrouter usage "
				f"phase=workflow_log_analysis model={model} "
				f"cache_enabled={cache_enabled} "
				f"cache_breakpoint_enabled={str(bool(cache_breakpoint_enabled)).lower()} "
				f"cache_breakpoint_fallback_retry={str(bool(cache_breakpoint_retry)).lower()} "
				f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} total_tokens={total_tokens} "
				f"cache_creation_input_tokens={format_usage_value(cache_creation_tokens)} "
				f"cache_read_input_tokens={format_usage_value(cache_read_tokens)}",
				file=sys.stderr,
			)
		print(str(output_path))
		return 0

	if batch_mode == "sync":
		return run_sync("batch_mode_sync")

	if batch_disabled:
		return run_sync("batch_api_disabled")

	state_status = str((state or {}).get("status") or "").strip().lower()
	has_pending_state = state is not None and state_status in PENDING_BATCH_STATUSES

	if batch_mode in {"auto", "poll"} and has_pending_state:
		batch_id = str(state.get("batch_id") or "").strip()
		submitted_at = _parse_iso8601(str(state.get("submitted_at") or ""))
		if batch_id and submitted_at is not None:
			timeout_at = submitted_at + timedelta(hours=int(args.batch_poll_timeout_hours))
			if now >= timeout_at:
				return run_sync("batch_poll_timeout")
			try:
				status, batch_status, _response_payload, report_content, usage = poll_openrouter_batch(
					api_key=api_key,
					base_url=args.openrouter_base_url,
					batch_id=batch_id,
					request_timeout_seconds=args.request_timeout_seconds,
				)
			except RuntimeError as exc:
				_batch_log(
					"batch_fallback",
					severity="WARNING",
					reason=f"batch_poll_error:{exc}",
					provider=provider_hint,
					model=model,
					batch_id=batch_id,
				)
				return run_sync("batch_poll_error")

			_batch_log(
				"batch_poll",
				status=status,
				batch_status=batch_status,
				batch_id=batch_id,
				provider=provider_hint,
				model=model,
			)

			if status in {"completed"}:
				if not report_content.strip():
					return run_sync("batch_completed_without_content")
				output_path = resolve_dated_output_path(args.output, args.output_dir)
				try:
					write_report_atomic(output_path, report_content.rstrip() + "\n")
				except OSError as exc:
					print(f"ERROR: failed to write report {output_path}: {exc}", file=sys.stderr)
					return 1
				if state_path is not None:
					completed_state = _build_batch_state(
						batch_id=batch_id,
						status="completed",
						provider_hint=provider_hint,
						model=model,
						poll_timeout_hours=int(args.batch_poll_timeout_hours),
						submitted_at=str(state.get("submitted_at") or now.isoformat()),
						last_polled_at=now.isoformat(),
					)
					_write_json_atomic(state_path, completed_state)
				_batch_log(
					"batch_complete",
					batch_id=batch_id,
					provider=provider_hint,
					model=model,
				)
				if usage is not None:
					prompt_tokens = _to_int(usage.get("prompt_tokens"), 0)
					completion_tokens = _to_int(usage.get("completion_tokens"), 0)
					total_tokens = _to_int(usage.get("total_tokens"), 0)
					print(
						f"INFO: openrouter usage prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} total_tokens={total_tokens}",
						file=sys.stderr,
					)
				print(str(output_path))
				return 0

			if status in PENDING_BATCH_STATUSES:
				if state_path is not None:
					pending_state = _build_batch_state(
						batch_id=batch_id,
						status=batch_status,
						provider_hint=provider_hint,
						model=model,
						poll_timeout_hours=int(args.batch_poll_timeout_hours),
						submitted_at=str(state.get("submitted_at") or now.isoformat()),
						last_polled_at=now.isoformat(),
					)
					_write_json_atomic(state_path, pending_state)
				_batch_log(
					"batch_poll",
					status="pending",
					batch_id=batch_id,
					provider=provider_hint,
					model=model,
				)
				return 3

			if status in {"incomplete", "failed", "cancelled"}:
				return run_sync(f"batch_status_{status}")

			return run_sync(f"batch_unknown_status_{status}")

	if batch_mode == "poll":
		return run_sync("batch_poll_requested_without_state")

	if batch_mode in {"auto", "submit"}:
		capability, reason = _batch_capability_decision(
			api_key=api_key,
			model=model,
			base_url=args.openrouter_base_url,
			request_timeout_seconds=args.request_timeout_seconds,
			provider_hint=provider_hint,
		)
		if capability == "unsupported":
			return run_sync(reason)
		if capability == "unknown" and batch_mode == "auto":
			return run_sync(reason)
		try:
			batch_id, batch_status = submit_openrouter_batch(
				api_key=api_key,
				model=model,
				messages=messages,
				temperature=float(args.temperature),
				max_tokens=args.max_output_tokens,
				base_url=args.openrouter_base_url,
				request_timeout_seconds=args.request_timeout_seconds,
				thinking_level=args.thinking_level,
				provider_hint=provider_hint,
			)
		except RuntimeError as exc:
			if batch_mode == "submit":
				print(f"ERROR: {exc}", file=sys.stderr)
				return 1
			return run_sync(f"batch_submit_error:{exc}")

		submitted_at_iso = now.isoformat()
		if state_path is not None:
			state_payload = _build_batch_state(
				batch_id=batch_id,
				status=batch_status,
				provider_hint=provider_hint,
				model=model,
				poll_timeout_hours=int(args.batch_poll_timeout_hours),
				submitted_at=submitted_at_iso,
			)
			_write_json_atomic(state_path, state_payload)
		_batch_log(
			"batch_submit",
			batch_id=batch_id,
			status=batch_status,
			provider=provider_hint,
			model=model,
		)
		return 3

	return run_sync("batch_mode_unhandled")


if __name__ == "__main__":
	raise SystemExit(main())
