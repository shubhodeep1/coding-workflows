#!/usr/bin/env python3
"""Contract checks for memory-maintenance extraction and Codex no-op semantics."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import memory_maintenance_extract_learnings as extractor


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "memory_maintenance.yml"


def _workflow_text() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def test_memory_maintenance_declares_codex_contract_noop() -> None:
	wf = _workflow_text()
	assert "batch_noop" in wf
	assert "codex_contract_noop" in wf
	assert "no_codex_execution_path" in wf


def test_extraction_workflow_is_automatic_short_and_fail_open() -> None:
	wf = _workflow_text()
	yaml.safe_load(wf)
	step_start = wf.index("      - name: Extract repository learnings (fail-open)")
	step_end = wf.index("      - name: Run memory compaction", step_start)
	extraction_step = wf[step_start:step_end]

	assert "continue-on-error: true" in extraction_step
	assert "python3 scripts/memory_maintenance_extract_learnings.py" in extraction_step
	assert "python3 - <<" not in extraction_step
	disabled_notice_index = extraction_step.index("Repository learnings extraction disabled")
	helper_index = extraction_step.index("python3 scripts/memory_maintenance_extract_learnings.py")
	assert disabled_notice_index < helper_index
	assert "exit 0" in extraction_step[disabled_notice_index:helper_index]
	assert f"{extractor.MODEL_EXTRACTION_FAILURE_EXIT})" in extraction_step
	assert "Repository learnings model extraction failed; continuing without extraction" in extraction_step


def test_empty_source_writes_empty_artifacts_without_render_or_request(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	source_output = tmp_path / "source.json"
	learning_output = tmp_path / "learnings.json"
	monkeypatch.setattr(
		extractor,
		"discover_merged_learnings",
		lambda _repository_root: {"source_refs": [], "items": []},
	)

	def unexpected_call(*_args: Any, **_kwargs: Any) -> None:
		raise AssertionError("empty source must not render a prompt or call OpenRouter")

	monkeypatch.setattr(extractor, "render_extraction_prompt", unexpected_call)
	monkeypatch.setattr(extractor, "request_extracted_learnings", unexpected_call)

	extractor.run_extraction(tmp_path, source_output, learning_output)

	assert json.loads(source_output.read_text(encoding="utf-8")) == {
		"source_refs": [],
		"items": [],
	}
	assert json.loads(learning_output.read_text(encoding="utf-8")) == []


class _FakeResponse:
	def __init__(self, payload: dict[str, Any]) -> None:
		self.payload = payload

	def __enter__(self) -> _FakeResponse:
		return self

	def __exit__(self, *_args: Any) -> None:
		return None

	def read(self) -> bytes:
		return json.dumps(self.payload).encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


def _build_memory_checkout(tmp_path: Path) -> Path:
	branch_dir = tmp_path / "memory-checkout"
	memory_root = branch_dir / "ai-memory"
	for issue_number in range(1, 14):
		_write_json(
			memory_root / "global" / "canonical" / f"active-{issue_number}.json",
			{
				"category": "task_summaries",
				"status": "active",
				"lineage": {"issue_number": issue_number},
				"summary": f"Active summary {issue_number}",
				"details": f"details-{issue_number}-" + ("x" * 1600),
				"confidence": 0.8,
				"record_id": f"active-{issue_number}",
				"timestamps": {"created_at": f"2026-08-{issue_number:02d}T00:00:00Z"},
			},
		)
		_write_json(
			memory_root / "tasks" / f"issue-{issue_number}" / "lineage" / "task_lineage.v1.json",
			{
				"state": "merged",
				"issue_number": issue_number,
				"issue_url": f"https://example.test/issues/{issue_number}",
				"prs": [
					{
						"pr_number": 100 + issue_number,
						"url": f"https://example.test/pulls/{100 + issue_number}",
					}
				],
				"runs": [{"run_id": f"run-{issue_number}-{index}"} for index in range(1, 5)],
				"updated_at": f"2026-08-{issue_number:02d}T12:00:00Z",
			},
		)

	_write_json(
		memory_root / "tasks" / "issue-13" / "candidates" / "newer.json",
		{
			"category": "task_summaries",
			"status": "candidate",
			"scope": {"issue_number": 13},
			"summary": "Candidate must not replace active",
			"details": "candidate details",
			"confidence": 0.99,
			"record_id": "candidate-13",
			"timestamps": {"created_at": "2099-01-01T00:00:00Z"},
		},
	)
	return branch_dir


def test_success_preserves_discovery_request_normalization_and_telemetry(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	branch_dir = _build_memory_checkout(tmp_path)
	monkeypatch.setattr(
		extractor.ai_memory_lib,
		"read_memory_root_from_branch",
		lambda *_args, **_kwargs: branch_dir,
	)
	monkeypatch.setattr(
		extractor.ai_memory_lib,
		"resolve_memory_root_dir",
		lambda checkout_path, root_relative: checkout_path / root_relative,
	)

	render_calls: list[dict[str, Any]] = []

	def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
		render_calls.append({"command": command, **kwargs})
		return subprocess.CompletedProcess(command, 0, stdout="rendered prompt", stderr="")

	monkeypatch.setattr(extractor.subprocess, "run", fake_run)
	model_items = [
		{
			"summary": "  alpha\n beta  " if index == 0 else f"summary-{index}" + ("s" * 600),
			"details": f"details-{index}" + ("d" * 12050),
			"confidence": [0.2, 0.999, 0.876, 0.7, 0.8, 0.9][index],
		}
		for index in range(6)
	]
	response_payload = {
		"model": "provider/response-model",
		"usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
		"choices": [{"message": {"content": json.dumps(model_items)}}],
	}
	request_calls: list[dict[str, Any]] = []

	def fake_urlopen(request: urllib.request.Request, *, timeout: int) -> _FakeResponse:
		request_calls.append({"request": request, "timeout": timeout})
		return _FakeResponse(response_payload)

	monkeypatch.setattr(extractor.urllib.request, "urlopen", fake_urlopen)
	usage_calls: list[dict[str, Any]] = []

	def fake_usage_formatter(usage: dict[str, Any], **kwargs: Any) -> str:
		usage_calls.append({"usage": usage, **kwargs})
		return (
			"INFO: openrouter usage phase=memory-maintenance "
			"call=extract-repository-learnings model=provider/response-model"
		)

	monkeypatch.setattr(
		extractor.openrouter_prompt_cache,
		"format_openrouter_usage_line",
		fake_usage_formatter,
		raising=False,
	)
	monkeypatch.setattr(extractor.openrouter_prompt_cache, "is_cache_disabled", lambda: False)
	monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-api-key")

	source_output = tmp_path / "source.json"
	learning_output = tmp_path / "learnings.json"
	extractor.run_extraction(tmp_path, source_output, learning_output)

	source_payload = json.loads(source_output.read_text(encoding="utf-8"))
	assert [item["issue_number"] for item in source_payload["items"]] == list(range(13, 1, -1))
	assert source_payload["items"][0]["task_summary"] == "Active summary 13"
	assert len(source_payload["items"][0]["task_details"]) == 1500
	assert source_payload["items"][0]["source_run_ids"] == ["run-13-2", "run-13-3", "run-13-4"]
	assert source_payload["source_refs"] == [
		ref
		for issue_number in range(13, 8, -1)
		for ref in (
			f"https://example.test/issues/{issue_number}",
			f"https://example.test/pulls/{100 + issue_number}",
		)
	]
	assert not branch_dir.exists()

	assert len(render_calls) == 1
	assert render_calls[0]["command"] == [
		"bash",
		"scripts/render_prompt.sh",
		"prompts/mode-extract-learnings.txt",
	]
	assert json.loads(render_calls[0]["env"]["LEARNINGS_SOURCE_JSON"]) == source_payload["items"]
	assert len(request_calls) == 1
	request_call = request_calls[0]
	request_body = json.loads(request_call["request"].data.decode("utf-8"))
	assert request_call["timeout"] == 120
	assert request_call["request"].full_url == extractor.OPENROUTER_URL
	assert request_call["request"].method == "POST"
	assert request_call["request"].get_header("Authorization") == "Bearer super-secret-api-key"
	assert request_call["request"].get_header("Content-type") == "application/json"
	assert request_body == {
		"model": "openai/gpt-5.6-luna",
		"messages": [{"role": "user", "content": "rendered prompt"}],
		"temperature": 0.0,
		"max_tokens": 1200,
	}

	learnings = json.loads(learning_output.read_text(encoding="utf-8"))
	assert len(learnings) == 5
	assert learnings[0] == {
		"summary": "alpha beta",
		"details": ("details-0" + ("d" * 12050))[:12000],
		"confidence": 0.6,
	}
	assert len(learnings[1]["summary"]) == 500
	assert learnings[1]["confidence"] == 0.95
	assert learnings[2]["confidence"] == 0.88
	assert usage_calls == [
		{
			"usage": response_payload["usage"],
			"model": "provider/response-model",
			"phase": "memory-maintenance",
			"call_label": "extract-repository-learnings",
			"cache_enabled": True,
			"cache_breakpoint_enabled": None,
			"cache_breakpoint_fallback_retry": None,
		}
	]
	stderr = capsys.readouterr().err
	assert stderr.count("INFO: openrouter usage") == 1
	assert "super-secret-api-key" not in stderr
	assert "rendered prompt" not in stderr


def _configure_nonempty_extraction(
	monkeypatch: pytest.MonkeyPatch,
	response_payload: dict[str, Any] | None,
) -> None:
	monkeypatch.setattr(
		extractor,
		"discover_merged_learnings",
		lambda _repository_root: {"source_refs": ["https://example.test/issues/1"], "items": [{"id": 1}]},
	)
	monkeypatch.setattr(extractor, "render_extraction_prompt", lambda *_args: "sensitive prompt")
	monkeypatch.setattr(
		extractor.openrouter_prompt_cache,
		"format_openrouter_usage_line",
		lambda *_args, **_kwargs: "INFO: openrouter usage safe-line",
		raising=False,
	)
	monkeypatch.setattr(extractor.openrouter_prompt_cache, "is_cache_disabled", lambda: False)
	monkeypatch.setenv("OPENROUTER_API_KEY", "sensitive-key")
	if response_payload is not None:
		monkeypatch.setattr(
			extractor.urllib.request,
			"urlopen",
			lambda *_args, **_kwargs: _FakeResponse(response_payload),
		)


def test_malformed_model_output_is_classified_without_leaking_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	_configure_nonempty_extraction(
		monkeypatch,
		{
			"usage": {"total_tokens": 3},
			"choices": [{"message": {"content": "malformed-sensitive-model-output"}}],
		},
	)
	learning_output = tmp_path / "learnings.json"
	monkeypatch.setattr(extractor, "resolve_repo_root", lambda: tmp_path)
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"memory_maintenance_extract_learnings.py",
			"--source-output",
			str(tmp_path / "source.json"),
			"--learning-output",
			str(learning_output),
		],
	)

	assert extractor.main() == extractor.MODEL_EXTRACTION_FAILURE_EXIT

	assert json.loads(learning_output.read_text(encoding="utf-8")) == []
	stderr = capsys.readouterr().err
	assert stderr.count("INFO: openrouter usage") == 1
	assert "model_extraction_failed" in stderr
	assert "malformed-sensitive-model-output" not in stderr
	assert "sensitive-key" not in stderr


def test_request_failure_is_classified_without_promotion_ready_output(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	_configure_nonempty_extraction(monkeypatch, None)

	def fail_request(*_args: Any, **_kwargs: Any) -> None:
		raise urllib.error.URLError("sensitive-request-failure")

	monkeypatch.setattr(extractor.urllib.request, "urlopen", fail_request)
	learning_output = tmp_path / "learnings.json"
	monkeypatch.setattr(extractor, "resolve_repo_root", lambda: tmp_path)
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"memory_maintenance_extract_learnings.py",
			"--source-output",
			str(tmp_path / "source.json"),
			"--learning-output",
			str(learning_output),
		],
	)

	assert extractor.main() == extractor.MODEL_EXTRACTION_FAILURE_EXIT

	assert json.loads(learning_output.read_text(encoding="utf-8")) == []
	stderr = capsys.readouterr().err
	assert "model_extraction_failed" in stderr
	assert "sensitive-request-failure" not in stderr
	assert "sensitive-key" not in stderr


if __name__ == "__main__":
	test_memory_maintenance_declares_codex_contract_noop()
