#!/usr/bin/env python3
"""Summarize unselected workflow runs via gpt-5.4-mini to widen analysis coverage.

Runs as an optional pre-step of the Codex analysis pass in
`.github/workflows/workflow-log-analysis.yml`. The collector writes per-step
`log_excerpts` only for the top-15 "notable" runs (failures, retries, slow)
plus a small successful-run sample (~7%); every other run in the window has
metadata but no log content. This script picks up to `--max-summaries` of
those unselected runs (newest-first), fetches each run's log archive from
GitHub, and asks gpt-5.4-mini for a terse per-run summary that preserves the
signals the downstream analyzer Codex pass looks for (outcome, failure step,
warnings, token/API hot-spots, AI_MEMORY_TELEMETRY lines, retries).

The summary is written into the run row as `log_summary` plus a
`log_summary_meta` field. The script is fail-open: missing creds, archive
fetch errors, or model errors skip the affected run with a warning instead of
failing the job, and the token budget caps total spend per collection.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


DEFAULT_MODEL = "openai/gpt-5.4-mini"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_SUMMARIES = 100
DEFAULT_TOKEN_BUDGET = 1_500_000
DEFAULT_PER_RUN_INPUT_CHARS = 12_000
DEFAULT_OUTPUT_TOKENS = 500
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_PER_STEP_HEAD_CHARS = 1_000
DEFAULT_PER_STEP_TAIL_CHARS = 4_000

SUMMARIZER_TELEMETRY_OP = "summarize_unselected_runs"


SYSTEM_PROMPT = (
	"You are a concise log summarizer for an AI-powered GitHub Actions workflow analyzer.\n"
	"Given the step logs of a single workflow run, extract the signals the analyzer needs to spot\n"
	"reliability, cost, and performance regressions, and emit a terse summary in markdown bullets.\n"
	"\n"
	"Required signals (include only those actually present in the logs):\n"
	"- Outcome (success/failure/cancelled) and the failing step name if any\n"
	"- Notable warnings (deprecation, cache invalidation, secondary rate limit)\n"
	"- Token usage / model usage lines (e.g. tokens_used=..., model=openai/...)\n"
	"- AI_MEMORY_TELEMETRY: lines (preserve verbatim, max 3)\n"
	"- GH API call hot-spots (high call counts, HTTP 429, secondary rate limit)\n"
	"- MCP/Serena tool patterns (broad reads, repeated lookups, onboarding fired)\n"
	"- Retry/backoff events with attempt counts\n"
	"- Performance outliers (single steps that dominate runtime)\n"
	"\n"
	"Constraints:\n"
	"- Maximum 12 bullets, each under 30 words.\n"
	"- No preamble, no closing summary, no headings.\n"
	"- If a signal is absent, omit the bullet (do NOT say 'no X observed').\n"
	"- Quote concrete numbers and step names verbatim from the logs.\n"
)


def _load_collector_module() -> Any:
	"""Import collect_workflow_logs without exposing it as a runtime hard dep.

	The collector pulls in `ai_memory_lib`; we still want this script to fail
	open if that import surface ever moves. Loading via `importlib.util` lets
	us catch ModuleNotFoundError and degrade gracefully (the caller will then
	skip summarization and emit a warning).
	"""
	spec = importlib.util.spec_from_file_location(
		"_collect_workflow_logs", SCRIPTS_DIR / "collect_workflow_logs.py"
	)
	if spec is None or spec.loader is None:
		raise ModuleNotFoundError("scripts/collect_workflow_logs.py spec unavailable")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _to_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


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


def _warn(message: str) -> None:
	print(f"::warning::{message}", file=sys.stderr)


def _is_run_eligible(run: Any) -> bool:
	if not isinstance(run, dict):
		return False
	if run.get("log_summary"):
		return False
	excerpts = run.get("log_excerpts")
	if isinstance(excerpts, list) and excerpts:
		return False
	if not run.get("repository"):
		return False
	if _to_int(run.get("run_id"), 0) <= 0:
		return False
	return True


def select_targets(runs: list[dict[str, Any]], max_summaries: int) -> list[dict[str, Any]]:
	if max_summaries <= 0:
		return []
	candidates = [run for run in runs if _is_run_eligible(run)]
	# Newest-first by created_at; stable secondary on run_id desc to keep ties deterministic.
	candidates.sort(
		key=lambda r: (
			_parse_iso8601(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
			_to_int(r.get("run_id"), 0),
		),
		reverse=True,
	)
	return candidates[:max_summaries]


def _truncate_step(content: str, head_chars: int, tail_chars: int) -> str:
	"""Keep head + tail of a single step log, preserving error context at the end."""
	if len(content) <= head_chars + tail_chars:
		return content
	head = content[:head_chars]
	tail = content[-tail_chars:]
	return f"{head}\n... [truncated {len(content) - head_chars - tail_chars} chars] ...\n{tail}"


def build_summary_input(
	full_logs: list[dict[str, str]],
	*,
	char_cap: int,
	per_step_head: int = DEFAULT_PER_STEP_HEAD_CHARS,
	per_step_tail: int = DEFAULT_PER_STEP_TAIL_CHARS,
) -> str:
	"""Assemble step logs into a single bounded prompt input.

	Each step is head/tail-truncated, then steps are concatenated until the
	total approaches `char_cap`. If still over the cap, the assembled text is
	itself head/tail-truncated to fit.
	"""
	parts: list[str] = []
	total = 0
	for step in full_logs:
		name = str(step.get("step_name") or "unknown_step")
		content = str(step.get("content") or "")
		if not content:
			continue
		body = _truncate_step(content, per_step_head, per_step_tail)
		block = f"=== STEP: {name} ===\n{body}\n"
		parts.append(block)
		total += len(block)
		if total >= char_cap * 2:
			# Hard cut so we don't blow up memory if a run has thousands of steps.
			break
	text = "".join(parts)
	if len(text) <= char_cap:
		return text
	head_size = max(char_cap // 3, 1)
	tail_size = max(char_cap - head_size, 1)
	return text[:head_size] + "\n... [run-level truncation] ...\n" + text[-tail_size:]


def _format_user_message(run: dict[str, Any], logs_text: str) -> str:
	failure_point = run.get("failure_point") or {}
	failure_block = ""
	if failure_point.get("step_name") or failure_point.get("job_name"):
		failure_block = (
			f"Failing job/step (collector-detected): "
			f"{failure_point.get('job_name') or 'unknown'} / "
			f"{failure_point.get('step_name') or 'unknown'}\n"
		)
	return (
		f"Run: {run.get('repository')} #{run.get('run_id')}\n"
		f"Workflow: {run.get('workflow_name') or 'unknown'} "
		f"(family={run.get('workflow_family') or 'unknown'})\n"
		f"Outcome: {run.get('conclusion') or 'unknown'}, "
		f"duration={_to_int(run.get('duration_seconds'), 0)}s, "
		f"attempt={_to_int(run.get('run_attempt'), 1)}, "
		f"retries={_to_int(run.get('retries'), 0)}\n"
		f"{failure_block}"
		f"\nStep logs (truncated):\n{logs_text}"
	)


class OpenRouterSummarizer:
	"""Thin wrapper for OpenRouter chat-completions calls."""

	def __init__(
		self,
		api_key: str,
		*,
		model: str,
		base_url: str,
		timeout_seconds: int,
		max_output_tokens: int,
	) -> None:
		self.api_key = api_key
		self.model = model
		self.base_url = base_url.rstrip("/")
		self.timeout_seconds = timeout_seconds
		self.max_output_tokens = max_output_tokens

	def summarize(
		self, run: dict[str, Any], logs_text: str
	) -> tuple[str, int]:
		body = json.dumps(
			{
				"model": self.model,
				"messages": [
					{"role": "system", "content": SYSTEM_PROMPT},
					{"role": "user", "content": _format_user_message(run, logs_text)},
				],
				"max_tokens": self.max_output_tokens,
				"temperature": 0.0,
			}
		).encode("utf-8")
		request = urllib.request.Request(
			f"{self.base_url}/chat/completions",
			data=body,
			headers={
				"Authorization": f"Bearer {self.api_key}",
				"Content-Type": "application/json",
				"X-Title": "workflow-log-summarize-unselected-runs",
			},
			method="POST",
		)
		try:
			with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
				raw = resp.read().decode("utf-8")
		except urllib.error.HTTPError as exc:
			detail = ""
			try:
				detail = exc.read().decode("utf-8", errors="replace")[:300]
			except Exception:  # noqa: BLE001
				pass
			raise RuntimeError(f"chat/completions HTTP {exc.code}: {detail}") from exc
		except urllib.error.URLError as exc:
			raise RuntimeError(f"chat/completions connection error: {exc}") from exc
		try:
			payload = json.loads(raw)
		except json.JSONDecodeError as exc:
			raise RuntimeError(f"chat/completions invalid JSON: {exc}") from exc
		choices = payload.get("choices") or []
		if not choices:
			raise RuntimeError("chat/completions empty choices")
		content = ((choices[0] or {}).get("message") or {}).get("content") or ""
		content = content.strip()
		if not content:
			raise RuntimeError("chat/completions empty content")
		usage = payload.get("usage") or {}
		tokens_used = _to_int(usage.get("total_tokens"), 0)
		if tokens_used <= 0:
			tokens_used = _to_int(usage.get("prompt_tokens"), 0) + _to_int(
				usage.get("completion_tokens"), 0
			)
		return content, max(tokens_used, 0)


def _write_json_atomic(path: Path, payload: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path = path.with_suffix(path.suffix + ".tmp")
	tmp_path.write_text(
		json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
	)
	tmp_path.replace(path)


def _emit_telemetry(stats: dict[str, Any]) -> None:
	stats_with_op = {"op": SUMMARIZER_TELEMETRY_OP, **stats}
	print(f"AI_MEMORY_TELEMETRY: {json.dumps(stats_with_op, sort_keys=True)}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description=(
			"Summarize unselected workflow runs via gpt-5.4-mini and write log_summary "
			"fields back into workflow_log_report.json."
		)
	)
	parser.add_argument("--report", required=True, help="Path to workflow_log_report.json")
	parser.add_argument(
		"--max-summaries",
		type=int,
		default=DEFAULT_MAX_SUMMARIES,
		help="Cap on additional runs to summarize (default 100).",
	)
	parser.add_argument(
		"--token-budget",
		type=int,
		default=DEFAULT_TOKEN_BUDGET,
		help="Hard cap on total tokens spent across mini calls (default 1.5M).",
	)
	parser.add_argument(
		"--per-run-char-cap",
		type=int,
		default=DEFAULT_PER_RUN_INPUT_CHARS,
		help="Max prompt characters per run after step head/tail truncation.",
	)
	parser.add_argument("--model", default=None, help="OpenRouter model id (default gpt-5.4-mini).")
	parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenRouter base URL.")
	parser.add_argument(
		"--timeout-seconds",
		type=int,
		default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
		help="HTTP timeout per mini call.",
	)
	parser.add_argument(
		"--max-output-tokens",
		type=int,
		default=DEFAULT_OUTPUT_TOKENS,
		help="max_tokens for each mini summary (default 500).",
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	report_path = Path(args.report)
	if not report_path.exists():
		_warn(f"report {report_path} not found; skipping summarization")
		return 0

	api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
	gh_token = (os.getenv("GH_TOKEN") or "").strip()
	model = (os.getenv("WORKFLOW_LOG_SUMMARY_MODEL") or args.model or DEFAULT_MODEL).strip()

	stats: dict[str, Any] = {
		"targeted": 0,
		"summarized": 0,
		"skipped_fetch_error": 0,
		"skipped_summary_error": 0,
		"skipped_budget_exhausted": 0,
		"skipped_disabled": 0,
		"tokens_used": 0,
		"model": model,
		"started_at": datetime.now(timezone.utc).isoformat(),
	}

	if not api_key:
		_warn("OPENROUTER_API_KEY not set; skipping summarization (fail-open)")
		stats["skipped_disabled"] = 1
		_emit_telemetry(stats)
		return 0
	if not gh_token:
		_warn("GH_TOKEN not set; skipping summarization (fail-open)")
		stats["skipped_disabled"] = 1
		_emit_telemetry(stats)
		return 0
	if args.max_summaries <= 0 or args.token_budget <= 0:
		_warn("max_summaries or token_budget <= 0; skipping summarization")
		stats["skipped_disabled"] = 1
		_emit_telemetry(stats)
		return 0

	try:
		report = json.loads(report_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		_warn(f"failed to read report {report_path}: {exc}")
		_emit_telemetry(stats)
		return 0
	if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
		_warn("report missing 'runs' array; skipping summarization")
		_emit_telemetry(stats)
		return 0

	try:
		collector = _load_collector_module()
	except Exception as exc:  # noqa: BLE001 — fail-open contract
		_warn(f"unable to load collector helpers ({exc}); skipping summarization")
		_emit_telemetry(stats)
		return 0

	targets = select_targets(report["runs"], args.max_summaries)
	stats["targeted"] = len(targets)
	if not targets:
		_emit_telemetry(stats)
		return 0

	summarizer = OpenRouterSummarizer(
		api_key,
		model=model,
		base_url=args.base_url,
		timeout_seconds=args.timeout_seconds,
		max_output_tokens=args.max_output_tokens,
	)
	archive_cache: dict[tuple[str, int], Any] = {}

	for run in targets:
		if stats["tokens_used"] >= args.token_budget:
			stats["skipped_budget_exhausted"] += 1
			continue
		repository = str(run.get("repository") or "")
		run_id = _to_int(run.get("run_id"), 0)
		try:
			archive_bytes = collector._fetch_run_log_archive(
				repository, run_id, token=gh_token, cache=archive_cache
			)
			full_logs = collector.extract_full_logs(archive_bytes)
		except Exception as exc:  # noqa: BLE001 — fail-open per run
			_warn(f"log archive fetch failed for {repository}#{run_id}: {exc}")
			stats["skipped_fetch_error"] += 1
			continue
		logs_text = build_summary_input(full_logs, char_cap=args.per_run_char_cap)
		if not logs_text.strip():
			stats["skipped_fetch_error"] += 1
			continue
		try:
			summary, tokens_used = summarizer.summarize(run, logs_text)
		except Exception as exc:  # noqa: BLE001 — fail-open per run
			_warn(f"mini summary failed for {repository}#{run_id}: {exc}")
			stats["skipped_summary_error"] += 1
			# Tiny pause so a flaky upstream doesn't burn the budget instantly.
			time.sleep(0.25)
			continue
		run["log_summary"] = summary
		run["log_summary_meta"] = {
			"model": model,
			"tokens_used": tokens_used,
			"input_chars": len(logs_text),
			"created_at": datetime.now(timezone.utc).isoformat(),
		}
		stats["summarized"] += 1
		stats["tokens_used"] += tokens_used

	stats["finished_at"] = datetime.now(timezone.utc).isoformat()

	try:
		_write_json_atomic(report_path, report)
	except OSError as exc:
		_warn(f"failed to write report {report_path}: {exc}")
		_emit_telemetry(stats)
		return 0

	_emit_telemetry(stats)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
