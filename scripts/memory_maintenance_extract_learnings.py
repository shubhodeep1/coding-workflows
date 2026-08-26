#!/usr/bin/env python3
"""Extract repository learnings for the memory-maintenance workflow."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

try:
	import ai_memory_lib
	import openrouter_prompt_cache
	from repo_root import repo_root as resolve_repo_root
except ModuleNotFoundError:
	from scripts import ai_memory_lib, openrouter_prompt_cache
	from scripts.repo_root import repo_root as resolve_repo_root


REQUESTED_MODEL = "openai/gpt-5.6-luna"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DISCOVERY_FAILURE_EXIT = 10
PROMPT_RENDER_FAILURE_EXIT = 11
MODEL_EXTRACTION_FAILURE_EXIT = 12


class DiscoveryFailure(RuntimeError):
	"""Merged-summary discovery or source-output writing failed."""


class PromptRenderFailure(RuntimeError):
	"""The existing prompt renderer could not prepare the request."""


class ModelExtractionFailure(RuntimeError):
	"""The OpenRouter request, response, or normalized output failed."""


def _load_json(path: Path) -> dict[str, Any] | None:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError, json.JSONDecodeError):
		return None
	return payload if isinstance(payload, dict) else None


def _issue_number_for_record(record: dict[str, Any]) -> int | None:
	lineage = record.get("lineage") or {}
	scope = record.get("scope") or {}
	raw_issue_number = lineage.get("issue_number")
	if raw_issue_number is None:
		raw_issue_number = scope.get("issue_number")
	try:
		return int(raw_issue_number)
	except (TypeError, ValueError):
		return None


def discover_merged_learnings(repository_root: Path) -> dict[str, Any]:
	"""Return the bounded merged-task source payload used by the prompt."""
	branch_dir: Path | None = None
	try:
		branch_dir = ai_memory_lib.read_memory_root_from_branch(
			repository_root,
			memory_branch=os.getenv("AI_MEMORY_BRANCH", "ai-memory"),
			memory_root_relative=os.getenv("AI_MEMORY_ROOT", "ai-memory"),
		)
		memory_root = ai_memory_lib.resolve_memory_root_dir(
			branch_dir,
			os.getenv("AI_MEMORY_ROOT", "ai-memory"),
		)

		task_summary_records_by_issue: dict[int, dict[str, Any]] = {}

		def consider_record(record: dict[str, Any]) -> None:
			if str(record.get("category") or "").strip().lower() != "task_summaries":
				return
			status = str(record.get("status") or "").strip().lower()
			if status not in {"active", "candidate", "promoted"}:
				return
			issue_number = _issue_number_for_record(record)
			if issue_number is None:
				return
			summary = str(record.get("summary") or "").strip()
			details = str(record.get("details") or "").strip()
			if not summary or not details:
				return
			timestamps = record.get("timestamps") or {}
			sort_key = (
				1 if status == "active" else 0,
				str(timestamps.get("promoted_at") or timestamps.get("created_at") or ""),
				float(record.get("confidence") or 0.0),
				str(record.get("record_id") or ""),
			)
			candidate = {
				"record_id": str(record.get("record_id") or ""),
				"summary": summary,
				"details": details[:1500],
				"confidence": float(record.get("confidence") or 0.0),
				"sort_key": sort_key,
			}
			existing = task_summary_records_by_issue.get(issue_number)
			if existing is None or candidate["sort_key"] > existing["sort_key"]:
				task_summary_records_by_issue[issue_number] = candidate

		canonical_dir = memory_root / "global" / "canonical"
		for record_path in sorted(canonical_dir.rglob("*.json")):
			payload = _load_json(record_path)
			if payload is not None:
				consider_record(payload)

		for record_path in sorted((memory_root / "tasks").glob("issue-*/candidates/*.json")):
			payload = _load_json(record_path)
			if payload is not None:
				consider_record(payload)

		merged_items: list[dict[str, Any]] = []
		# The reusable workflow still runs on the existing monthly scheduler.
		# Until cadence changes, sample a bounded set of recent merged tasks
		# instead of assuming a 24h-only extraction window.
		for lineage_path in sorted((memory_root / "tasks").glob("issue-*/lineage/task_lineage.v1.json")):
			lineage = _load_json(lineage_path)
			if not isinstance(lineage, dict) or lineage.get("state") != "merged":
				continue
			try:
				issue_number = int(lineage.get("issue_number"))
			except (TypeError, ValueError):
				continue
			summary_record = task_summary_records_by_issue.get(issue_number)
			if summary_record is None:
				continue

			prs = lineage.get("prs") or []
			pr_entry = prs[-1] if prs else {}
			run_ids: list[str] = []
			for run_entry in lineage.get("runs") or []:
				run_id = str((run_entry or {}).get("run_id") or "").strip()
				if run_id:
					run_ids.append(run_id)

			merged_items.append(
				{
					"issue_number": issue_number,
					"issue_url": str(lineage.get("issue_url") or ""),
					"pr_number": pr_entry.get("pr_number"),
					"pr_url": str(pr_entry.get("url") or ""),
					"merged_at": str(lineage.get("updated_at") or ""),
					"task_summary": summary_record["summary"],
					"task_details": summary_record["details"],
					"task_confidence": summary_record["confidence"],
					"source_record_id": summary_record["record_id"],
					"source_run_ids": run_ids[-3:],
				}
			)

		merged_items.sort(
			key=lambda item: (str(item.get("merged_at") or ""), int(item.get("issue_number") or 0)),
			reverse=True,
		)
		merged_items = merged_items[:12]

		source_refs: list[str] = []
		for item in merged_items:
			for ref in (item.get("issue_url"), item.get("pr_url")):
				if isinstance(ref, str) and ref and ref not in source_refs:
					source_refs.append(ref)

		return {
			"source_refs": source_refs[:10],
			"items": merged_items,
		}
	finally:
		if branch_dir is not None:
			shutil.rmtree(branch_dir, ignore_errors=True)


def render_extraction_prompt(repository_root: Path, source_items: list[dict[str, Any]]) -> str:
	"""Render the existing extraction prompt with the discovered source items."""
	render_environment = os.environ.copy()
	render_environment["LEARNINGS_SOURCE_JSON"] = json.dumps(
		source_items,
		ensure_ascii=False,
		separators=(",", ":"),
	)
	result = subprocess.run(
		["bash", "scripts/render_prompt.sh", "prompts/mode-extract-learnings.txt"],
		cwd=repository_root,
		env=render_environment,
		text=True,
		capture_output=True,
		check=False,
	)
	if result.returncode != 0:
		raise PromptRenderFailure("prompt render failed")
	return result.stdout


def _strip_code_fences(text: str) -> str:
	stripped = text.strip()
	if not stripped.startswith("```"):
		return stripped
	lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
	return "\n".join(lines).strip()


def normalize_model_output(content: str) -> list[dict[str, object]]:
	"""Normalize the model response to the existing bounded learning shape."""
	parsed = json.loads(_strip_code_fences(content) or "[]")
	if not isinstance(parsed, list):
		raise ValueError("repository learnings extractor response must be a JSON array")

	normalized: list[dict[str, object]] = []
	for item in parsed:
		if not isinstance(item, dict):
			continue
		summary = " ".join(str(item.get("summary") or "").split()).strip()
		details = str(item.get("details") or "").strip()
		try:
			confidence = float(item.get("confidence"))
		except (TypeError, ValueError):
			continue
		if not summary or not details:
			continue
		normalized.append(
			{
				"summary": summary[:500],
				"details": details[:12000],
				"confidence": round(max(0.6, min(confidence, 0.95)), 2),
			}
		)
		if len(normalized) >= 5:
			break
	return normalized


def request_extracted_learnings(prompt: str) -> list[dict[str, object]]:
	"""Call OpenRouter, emit safe usage telemetry, and normalize its content."""
	openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
	if not openrouter_api_key:
		raise ModelExtractionFailure("OPENROUTER_API_KEY is required")
	request_payload = json.dumps(
		{
			"model": REQUESTED_MODEL,
			"messages": [{"role": "user", "content": prompt}],
			"temperature": 0.0,
			"max_tokens": 1200,
		}
	).encode("utf-8")
	request = urllib.request.Request(
		OPENROUTER_URL,
		data=request_payload,
		headers={
			"Authorization": f"Bearer {openrouter_api_key}",
			"Content-Type": "application/json",
		},
		method="POST",
	)

	with urllib.request.urlopen(request, timeout=120) as response:
		raw_response = json.loads(response.read().decode("utf-8"))
	if not isinstance(raw_response, dict):
		raise ValueError("repository learnings extractor response must be a JSON object")

	response_model = raw_response.get("model")
	if not isinstance(response_model, str) or not response_model.strip():
		response_model = REQUESTED_MODEL
	usage_formatter = getattr(openrouter_prompt_cache, "format_openrouter_usage_line", None)
	if not callable(usage_formatter):
		raise RuntimeError("format_openrouter_usage_line is unavailable")
	print(
		usage_formatter(
			raw_response.get("usage"),
			model=response_model,
			phase="memory-maintenance",
			call_label="extract-repository-learnings",
			cache_enabled=not openrouter_prompt_cache.is_cache_disabled(),
			cache_breakpoint_enabled=None,
			cache_breakpoint_fallback_retry=None,
		),
		file=sys.stderr,
	)

	content = str((raw_response.get("choices") or [{}])[0].get("message", {}).get("content") or "")
	return normalize_model_output(content)


def _write_json(path: Path, payload: Any, *, sort_keys: bool = False) -> None:
	path.write_text(
		json.dumps(payload, ensure_ascii=True, sort_keys=sort_keys),
		encoding="utf-8",
	)


def run_extraction(repository_root: Path, source_output: Path, learning_output: Path) -> None:
	"""Run all extraction stages, raising a stage-specific failure on error."""
	try:
		_write_json(source_output, {"source_refs": [], "items": []}, sort_keys=True)
		_write_json(learning_output, [])
		source_payload = discover_merged_learnings(repository_root)
		_write_json(source_output, source_payload, sort_keys=True)
	except Exception as exc:
		raise DiscoveryFailure("source discovery failed") from exc

	source_items = source_payload.get("items") or []
	if not source_items:
		return

	try:
		prompt = render_extraction_prompt(repository_root, source_items)
	except PromptRenderFailure:
		raise
	except Exception as exc:
		raise PromptRenderFailure("prompt render failed") from exc

	try:
		normalized_learnings = request_extracted_learnings(prompt)
		_write_json(learning_output, normalized_learnings)
	except Exception as exc:
		raise ModelExtractionFailure("model extraction failed") from exc


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source-output", required=True, type=Path)
	parser.add_argument("--learning-output", required=True, type=Path)
	args = parser.parse_args()

	try:
		run_extraction(resolve_repo_root(), args.source_output, args.learning_output)
	except DiscoveryFailure:
		print("memory_maintenance_extract_learnings: source_discovery_failed", file=sys.stderr)
		return DISCOVERY_FAILURE_EXIT
	except PromptRenderFailure:
		print("memory_maintenance_extract_learnings: prompt_render_failed", file=sys.stderr)
		return PROMPT_RENDER_FAILURE_EXIT
	except ModelExtractionFailure:
		print("memory_maintenance_extract_learnings: model_extraction_failed", file=sys.stderr)
		return MODEL_EXTRACTION_FAILURE_EXIT
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
