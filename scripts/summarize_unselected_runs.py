#!/usr/bin/env python3
"""Summarize unselected workflow runs via gpt-5.6-luna to widen analysis coverage.

Runs as an optional pre-step of the Codex analysis pass in
`.github/workflows/workflow-log-analysis.yml`. The collector writes per-step
`log_excerpts` only for the top-15 "notable" runs (failures, retries, slow)
plus a small successful-run sample (~7%); every other run in the window has
metadata but no log content. This script picks up to `--max-summaries` of
those unselected runs (newest-first), fetches each run's log archive from
GitHub, and asks gpt-5.6-luna for a terse per-run summary that preserves the
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

try:
	from cost_audit import build_run_cost_telemetry
except ModuleNotFoundError:
	from scripts.cost_audit import build_run_cost_telemetry

try:
	from openrouter_prompt_cache import format_openrouter_usage_line, is_cache_disabled
except ModuleNotFoundError:
	from scripts.openrouter_prompt_cache import format_openrouter_usage_line, is_cache_disabled


DEFAULT_MODEL = "openai/gpt-5.6-luna"
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
	"- `BREAK_GLASS:` / `CONTEXT_BUDGET_WARN:` lines (preserve verbatim, max 3 total)\n"
	"- AI_MEMORY_TELEMETRY: lines (preserve verbatim, max 3)\n"
	"- `SEMBLE_*` / `SERENA_*` lines (preserve verbatim, max 3 per prefix family; include `target=`, `bytes=` / `response_bytes=`, `reason=`, `result=`, and `tool=` values when present)\n"
	"- GH API call hot-spots (high call counts, HTTP 429, secondary rate limit)\n"
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


def _load_normalize_usage() -> Any:
	"""Import openrouter_prompt_cache.normalize_usage if available.

	Reusing the existing helper keeps token-budget accounting consistent with
	the rest of the OpenRouter call-sites (it handles `prompt_tokens_details`,
	`input_token_details`, and other variant shapes that some providers
	return). If the helper can't be loaded we fall back to a local parser.
	"""
	try:
		spec = importlib.util.spec_from_file_location(
			"_openrouter_prompt_cache", SCRIPTS_DIR / "openrouter_prompt_cache.py"
		)
		if spec is None or spec.loader is None:
			return None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return getattr(module, "normalize_usage", None)
	except Exception:  # noqa: BLE001 — fail-open: fall back to local parser
		return None


_NORMALIZE_USAGE = _load_normalize_usage()


def _extract_tokens_used(
	usage: Any, *, input_chars: int, max_output_tokens: int
) -> int:
	"""Best-effort token accounting that always returns a positive number.

	Order of preference:
	1. `normalize_usage()` from openrouter_prompt_cache (handles nested shapes).
	2. Flat `total_tokens` / sum of `prompt_tokens` + `completion_tokens`.
	3. Conservative estimate `input_chars/4 + max_output_tokens` so the token
	budget keeps advancing even if a provider omits `usage` entirely.
	"""
	usage_dict = usage if isinstance(usage, dict) else {}
	if _NORMALIZE_USAGE is not None:
		try:
			normalized = _NORMALIZE_USAGE(usage_dict) or {}
		except Exception:  # noqa: BLE001
			normalized = {}
		total = normalized.get("total_tokens")
		if isinstance(total, int) and total > 0:
			return total
		prompt = normalized.get("prompt_tokens") or 0
		completion = normalized.get("completion_tokens") or 0
		if (prompt or completion) and isinstance(prompt, int) and isinstance(completion, int):
			return max(prompt + completion, 0)
	flat_total = _to_int(usage_dict.get("total_tokens"), 0)
	if flat_total > 0:
		return flat_total
	flat_sum = _to_int(usage_dict.get("prompt_tokens"), 0) + _to_int(
		usage_dict.get("completion_tokens"), 0
	)
	if flat_sum > 0:
		return flat_sum
	# ~4 chars/token is the OpenAI rule-of-thumb; conservative for English logs.
	return max(input_chars // 4 + max(max_output_tokens, 0), 1)


def _to_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _parse_iso8601(value: Any) -> datetime | None:
	# Defensive type guard: callers pass run.get("created_at") through a
	# sort key, so a malformed (non-string) value would otherwise raise
	# AttributeError on .strip() and abort summarization for the whole
	# window. Treat anything non-stringy as "no timestamp".
	if not isinstance(value, str):
		return None
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
	# Treat whitespace-only `log_summary` as missing so a prior empty write
	# doesn't permanently block re-summarization (analyzer also drops blanks
	# in `_normalized_run_view`).
	log_summary = run.get("log_summary")
	if isinstance(log_summary, str) and log_summary.strip():
		return False
	excerpts = run.get("log_excerpts")
	if isinstance(excerpts, list) and excerpts:
		return False
	# Repository must be a non-empty, non-whitespace string. The collector
	# normally produces "owner/repo", but we guard against malformed rows
	# so they don't reach _fetch_run_log_archive with an empty target.
	repository = run.get("repository")
	if not isinstance(repository, str) or not repository.strip():
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
	"""Keep head + tail of a single step log, preserving error context at the end.

	Both bounds may be 0; we slice explicitly because Python's
	`content[-0:]` evaluates to `content[0:]` (the full string), which would
	silently defeat truncation when `tail_chars == 0`.
	"""
	if len(content) <= head_chars + tail_chars:
		return content
	head = content[:head_chars] if head_chars > 0 else ""
	tail = content[-tail_chars:] if tail_chars > 0 else ""
	marker = f"... [truncated {len(content) - head_chars - tail_chars} chars] ..."
	if head and tail:
		return f"{head}\n{marker}\n{tail}"
	if head:
		return f"{head}\n{marker}"
	if tail:
		return f"{marker}\n{tail}"
	return marker


def _full_logs_to_text(full_logs: list[dict[str, str]]) -> str:
	parts: list[str] = []
	for step in full_logs:
		content = str(step.get("content") or "")
		if not content:
			continue
		parts.append(content)
	return "\n".join(parts)


def _run_wall_clock_ms(run: dict[str, Any]) -> int | None:
	duration_seconds = _to_int(run.get("duration_seconds"), 0)
	if duration_seconds <= 0:
		return None
	return duration_seconds * 1000


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
	# Defensive clamps so a misconfigured CLI flag or env override can't
	# produce unbounded output: char_cap >= 1, per-step bounds >= 0.
	char_cap = max(int(char_cap), 1)
	per_step_head = max(int(per_step_head), 0)
	per_step_tail = max(int(per_step_tail), 0)
	parts: list[str] = []
	for step in full_logs:
		name = str(step.get("step_name") or "unknown_step")
		content = str(step.get("content") or "")
		if not content:
			continue
		body = _truncate_step(content, per_step_head, per_step_tail)
		block = f"=== STEP: {name} ===\n{body}\n"
		parts.append(block)
	# Don't `break` early on the assembled length: log archives are sorted
	# earliest-first, so cutting off mid-stream would usually drop the late
	# steps (often the failing step + final warnings) that carry the most
	# signal. Each step is already head/tail-truncated, so memory stays
	# bounded (~per_step_head + per_step_tail per step), and the run-level
	# truncation below preserves both the head and tail of the assembled
	# text — meaning late steps survive into the summary input.
	text = "".join(parts)
	if len(text) <= char_cap:
		return text
	# Run-level truncation must keep len(result) <= char_cap *including* the
	# marker, so we subtract the marker length from the head/tail budget. If
	# `char_cap` is smaller than the marker itself, return a marker prefix
	# so we still don't exceed the cap.
	marker = "\n... [run-level truncation] ...\n"
	budget = char_cap - len(marker)
	if budget <= 0:
		return marker[:char_cap]
	if budget == 1:
		# Only one slot left after the marker — give it to the head.
		return text[:1] + marker
	# Strict invariant: head_size + tail_size == budget so the result is
	# exactly char_cap chars long (head + marker + tail). The previous
	# `max(..., 1)` clamp on tail_size could push the total over by one
	# when head_size already consumed the entire budget.
	head_size = max(budget // 3, 1)
	tail_size = budget - head_size
	return text[:head_size] + marker + text[-tail_size:]


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
		response_model = payload.get("model")
		if not isinstance(response_model, str) or not response_model.strip():
			response_model = self.model
		print(
			format_openrouter_usage_line(
				payload.get("usage"),
				model=response_model,
				phase="workflow-log-analysis",
				call_label="summarize-unselected-run",
				cache_enabled=not is_cache_disabled(),
				cache_breakpoint_enabled=None,
				cache_breakpoint_fallback_retry=None,
			),
			file=sys.stderr,
		)
		content = ((choices[0] or {}).get("message") or {}).get("content") or ""
		content = content.strip()
		if not content:
			raise RuntimeError("chat/completions empty content")
		tokens_used = _extract_tokens_used(
			payload.get("usage"),
			input_chars=len(logs_text),
			max_output_tokens=self.max_output_tokens,
		)
		return content, tokens_used


def _write_json_atomic(path: Path, payload: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path = path.with_suffix(path.suffix + ".tmp")
	tmp_path.write_text(
		json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
	)
	tmp_path.replace(path)


def _emit_telemetry(stats: dict[str, Any]) -> None:
	stats_with_op = {"op": SUMMARIZER_TELEMETRY_OP, **stats}
	print(
		f"AI_MEMORY_TELEMETRY: {json.dumps(stats_with_op, ensure_ascii=True, sort_keys=True)}",
		file=sys.stderr,
	)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description=(
			"Summarize unselected workflow runs via gpt-5.6-luna and write log_summary "
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
		help=(
			"Best-effort cap on total tokens spent across mini calls. The script "
			"performs a pre-flight estimate (input_chars/4 + max_output_tokens) "
			"and stops scheduling new requests once the budget would be "
			"exceeded; actual token usage from a final call may still overshoot "
			"the target slightly (default 1.5M)."
		),
	)
	parser.add_argument(
		"--per-run-char-cap",
		type=int,
		default=DEFAULT_PER_RUN_INPUT_CHARS,
		help="Max prompt characters per run after step head/tail truncation.",
	)
	parser.add_argument("--model", default=None, help="OpenRouter model id (default gpt-5.6-luna).")
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


def _first_non_blank(*candidates: Any) -> str | None:
	"""Return the first string candidate that is not None/empty/whitespace.

	`os.getenv("X")` returning `"   "` is truthy in Python, so a naive
	`os.getenv("X") or default` skips the fallback and yields whitespace
	that later strips to `""`. This helper picks the first candidate that
	carries actual content after stripping.
	"""
	for value in candidates:
		if isinstance(value, str) and value.strip():
			return value.strip()
	return None


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	report_path = Path(args.report)
	if not report_path.exists():
		_warn(f"report {report_path} not found; skipping summarization")
		return 0

	api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
	gh_token = (os.getenv("GH_TOKEN") or "").strip()
	model = _first_non_blank(
		os.getenv("WORKFLOW_LOG_SUMMARY_MODEL"), args.model, DEFAULT_MODEL
	) or DEFAULT_MODEL

	stats: dict[str, Any] = {
		"targeted": 0,
		"summarized": 0,
		"skipped_fetch_error": 0,
		"skipped_empty_logs": 0,
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
	# No archive cache: this script visits each (repo, run_id) at most once,
	# so the collector's payload-bytes cache (collect_workflow_logs.py:777-779)
	# would only retain hundreds of MB of log archives for no benefit. A
	# per-run fetch with `cache=None` keeps memory bounded by a single
	# archive at a time.

	for index, run in enumerate(targets):
		if stats["tokens_used"] >= args.token_budget:
			# Account for the current run plus everything after it in one shot,
			# then bail — no point fetching log archives we won't summarize.
			stats["skipped_budget_exhausted"] += len(targets) - index
			break
		repository = str(run.get("repository") or "")
		run_id = _to_int(run.get("run_id"), 0)
		try:
			archive_bytes = collector._fetch_run_log_archive(
				repository, run_id, token=gh_token, cache=None
			)
			full_logs = collector.extract_full_logs(archive_bytes)
			run["cost_telemetry"] = build_run_cost_telemetry(
				_full_logs_to_text(full_logs),
				fallback_wall_clock_ms=_run_wall_clock_ms(run),
			)
		except Exception as exc:  # noqa: BLE001 — fail-open per run
			_warn(f"log archive fetch failed for {repository}#{run_id}: {exc}")
			stats["skipped_fetch_error"] += 1
			continue
		logs_text = build_summary_input(full_logs, char_cap=args.per_run_char_cap)
		if not logs_text.strip():
			# The fetch succeeded — the archive was just empty/unsupported.
			# Count it separately so telemetry can distinguish transient
			# fetch errors from "nothing to summarize".
			stats["skipped_empty_logs"] += 1
			continue
		# Pre-flight token estimate using the same formula as the
		# `_extract_tokens_used` fallback (~4 chars/token + max output).
		# Skipping over-budget runs here keeps overshoot bounded — the
		# alternative is per-call after-the-fact accounting that can blow
		# past the cap on a single large summary.
		estimated_tokens = (len(logs_text) // 4) + max(args.max_output_tokens, 0)
		if stats["tokens_used"] + estimated_tokens > args.token_budget:
			stats["skipped_budget_exhausted"] += len(targets) - index
			break
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
