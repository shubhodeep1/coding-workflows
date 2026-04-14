#!/usr/bin/env python3
"""Tests for scripts/analyze_workflow_logs.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import urllib.error
from datetime import date
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_workflow_logs.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


spec = importlib.util.spec_from_file_location("analyze_workflow_logs", ANALYZER_PATH)
assert spec is not None and spec.loader is not None
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_dated_output_path_with_suffix_collision():
	with tempfile.TemporaryDirectory(prefix="analyze-output-") as td:
		output_dir = Path(td)
		(output_dir / "workflow-optimization-2026-04-11.md").write_text("x", encoding="utf-8")
		(output_dir / "workflow-optimization-2026-04-11-2.md").write_text("x", encoding="utf-8")
		resolved = analyzer.resolve_dated_output_path(
			None,
			str(output_dir),
			today=date(2026, 4, 11),
		)
		assert resolved == output_dir / "workflow-optimization-2026-04-11-3.md"


def test_truncate_to_budget_drops_deep_dive_before_recent_runs():
	context = {
		"summary": {"total_runs": 10},
		"deep_dive_logs": [{"name": "a.log", "excerpt": "x" * 4000} for _ in range(2)],
		"recent_runs": [{"run_id": i, "detail": "y" * 2000} for i in range(2)],
		"slow_runs": [{"run_id": 1}],
		"failing_runs": [{"run_id": 2}],
		"errors": [{"message": "z"}],
		"per_repo": {"owner/repo": {"total_runs": 10}},
		"per_workflow_family": {"plan": {"total_runs": 5}},
	}
	budget = analyzer._estimate_tokens({
		"summary": context["summary"],
		"recent_runs": context["recent_runs"],
		"slow_runs": context["slow_runs"],
		"failing_runs": context["failing_runs"],
		"errors": context["errors"],
		"per_repo": context["per_repo"],
		"per_workflow_family": context["per_workflow_family"],
	}) + 64
	trimmed = analyzer.truncate_to_budget(context, budget)
	assert trimmed["deep_dive_logs"] == []
	assert trimmed["recent_runs"] == context["recent_runs"]
	assert trimmed["truncation"]["removed"]["deep_dive_logs"] == 2
	assert trimmed["truncation"]["removed"]["recent_runs"] == 0


def test_build_parser_thinking_level_defaults_to_medium():
	args = analyzer.build_parser().parse_args([])
	assert args.thinking_level == "medium"


def test_build_parser_accepts_explicit_thinking_level():
	args = analyzer.build_parser().parse_args(["--thinking-level", "high"])
	assert args.thinking_level == "high"


def test_call_openrouter_builds_payload_and_parses_response():
	captured = {"url": "", "payload": {}, "headers": []}
	orig_urlopen = analyzer.urllib.request.urlopen
	old_cache_disabled = os.environ.get("OPENROUTER_PROMPT_CACHE_DISABLED")
	os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = "false"

	class FakeResponse:
		def __init__(self, body: str):
			self._body = body

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			return False

		def read(self) -> bytes:
			return self._body.encode("utf-8")

	def fake_urlopen(request, timeout=0):
		captured["url"] = request.full_url
		captured["payload"] = json.loads(request.data.decode("utf-8"))
		captured["headers"] = request.header_items()
		assert timeout == 17
		return FakeResponse(
			json.dumps(
				{
					"choices": [{"message": {"content": "# report"}}],
					"usage": {
						"prompt_tokens": 11,
						"completion_tokens": 22,
						"total_tokens": 33,
						"prompt_tokens_details": {"cache_write_tokens": 7, "cached_tokens": 5},
					},
				}
			)
		)

	analyzer.urllib.request.urlopen = fake_urlopen
	try:
		content, usage = analyzer.call_openrouter(
			api_key="test-key",
			model="openai/gpt-5.3-codex",
			messages=[{"role": "user", "content": "hello"}],
			temperature=0.0,
			max_tokens=123,
			base_url="https://openrouter.ai/api/v1",
			request_timeout_seconds=17,
			thinking_level="high",
		)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen
		if old_cache_disabled is None:
			os.environ.pop("OPENROUTER_PROMPT_CACHE_DISABLED", None)
		else:
			os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = old_cache_disabled

	assert content == "# report\n"
	assert usage == {
		"prompt_tokens": 11,
		"completion_tokens": 22,
		"total_tokens": 33,
		"cache_creation_input_tokens": 7,
		"cache_read_input_tokens": 5,
		"cache_breakpoint_enabled": True,
		"cache_breakpoint_fallback_retry": False,
	}
	assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
	assert captured["payload"]["model"] == "openai/gpt-5.3-codex"
	assert captured["payload"]["max_tokens"] == 123
	assert captured["payload"]["reasoning"] == "high"
	assert captured["payload"]["messages"][0]["cache_control"] == {"type": "ephemeral"}
	assert any(k.lower() == "authorization" and v == "Bearer test-key" for k, v in captured["headers"])

def test_call_openrouter_omits_reasoning_when_thinking_level_empty_or_none():
	orig_urlopen = analyzer.urllib.request.urlopen
	old_cache_disabled = os.environ.get("OPENROUTER_PROMPT_CACHE_DISABLED")
	os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = "false"

	class FakeResponse:
		def __init__(self, body: str):
			self._body = body

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			return False

		def read(self) -> bytes:
			return self._body.encode("utf-8")

	def run_case(thinking_level):
		captured_payload: dict[str, object] = {}

		def fake_urlopen(request, timeout=0):
			del timeout
			captured_payload.clear()
			captured_payload.update(json.loads(request.data.decode("utf-8")))
			return FakeResponse(json.dumps({"choices": [{"message": {"content": "# ok"}}]}))

		analyzer.urllib.request.urlopen = fake_urlopen
		content, usage = analyzer.call_openrouter(
			api_key="test-key",
			model="openai/gpt-5.3-codex",
			messages=[{"role": "user", "content": "hello"}],
			temperature=0.0,
			max_tokens=123,
			base_url="https://openrouter.ai/api/v1",
			request_timeout_seconds=17,
			thinking_level=thinking_level,
		)
		assert content == "# ok\n"
		assert usage is None
		assert "reasoning" not in captured_payload
		assert captured_payload["messages"][0]["cache_control"] == {"type": "ephemeral"}

	try:
		run_case("")
		run_case("   ")
		run_case(None)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen
		if old_cache_disabled is None:
			os.environ.pop("OPENROUTER_PROMPT_CACHE_DISABLED", None)
		else:
			os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = old_cache_disabled

def test_call_openrouter_http_error_is_reported():
	orig_urlopen = analyzer.urllib.request.urlopen

	def fake_urlopen(request, timeout=0):
		raise urllib.error.HTTPError(
			url=request.full_url,
			code=429,
			msg="Too Many Requests",
			hdrs=None,
			fp=io.BytesIO(b'{"error":"rate_limited"}'),
		)

	analyzer.urllib.request.urlopen = fake_urlopen
	try:
		try:
			analyzer.call_openrouter(
				api_key="test-key",
				model="model",
				messages=[{"role": "user", "content": "hello"}],
				temperature=0.0,
				max_tokens=64,
				base_url="https://openrouter.ai/api/v1",
				request_timeout_seconds=10,
			)
			raise AssertionError("expected RuntimeError")
		except RuntimeError as exc:
			assert "HTTP 429" in str(exc)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen


def test_call_openrouter_skips_breakpoint_for_gemini_models():
	captured_payload: dict[str, object] = {}
	orig_urlopen = analyzer.urllib.request.urlopen
	old_cache_disabled = os.environ.get("OPENROUTER_PROMPT_CACHE_DISABLED")
	os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = "false"

	class FakeResponse:
		def __init__(self, body: str):
			self._body = body

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			return False

		def read(self) -> bytes:
			return self._body.encode("utf-8")

	def fake_urlopen(request, timeout=0):
		del timeout
		captured_payload.clear()
		captured_payload.update(json.loads(request.data.decode("utf-8")))
		return FakeResponse(json.dumps({"choices": [{"message": {"content": "# ok"}}]}))

	analyzer.urllib.request.urlopen = fake_urlopen
	try:
		content, usage = analyzer.call_openrouter(
			api_key="test-key",
			model="google/gemini-2.5-pro",
			messages=[{"role": "user", "content": "hello"}],
			temperature=0.0,
			max_tokens=123,
			base_url="https://openrouter.ai/api/v1",
			request_timeout_seconds=17,
		)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen
		if old_cache_disabled is None:
			os.environ.pop("OPENROUTER_PROMPT_CACHE_DISABLED", None)
		else:
			os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = old_cache_disabled

	assert content == "# ok\n"
	assert usage is None
	assert "cache_control" not in captured_payload["messages"][0]


def test_call_openrouter_disables_breakpoint_when_cache_switch_enabled():
	captured_payload: dict[str, object] = {}
	orig_urlopen = analyzer.urllib.request.urlopen
	old_cache_disabled = os.environ.get("OPENROUTER_PROMPT_CACHE_DISABLED")
	os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = "true"

	class FakeResponse:
		def __init__(self, body: str):
			self._body = body

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			return False

		def read(self) -> bytes:
			return self._body.encode("utf-8")

	def fake_urlopen(request, timeout=0):
		del timeout
		captured_payload.clear()
		captured_payload.update(json.loads(request.data.decode("utf-8")))
		return FakeResponse(json.dumps({"choices": [{"message": {"content": "# ok"}}], "usage": {"prompt_tokens": 3}}))

	analyzer.urllib.request.urlopen = fake_urlopen
	try:
		content, usage = analyzer.call_openrouter(
			api_key="test-key",
			model="openai/gpt-5.3-codex",
			messages=[{"role": "user", "content": "hello"}],
			temperature=0.0,
			max_tokens=123,
			base_url="https://openrouter.ai/api/v1",
			request_timeout_seconds=17,
		)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen
		if old_cache_disabled is None:
			os.environ.pop("OPENROUTER_PROMPT_CACHE_DISABLED", None)
		else:
			os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = old_cache_disabled

	assert content == "# ok\n"
	assert usage is not None
	assert usage["cache_breakpoint_enabled"] is False
	assert "cache_control" not in captured_payload["messages"][0]


def test_call_openrouter_retries_without_breakpoint_on_cache_control_error():
	payloads: list[dict[str, object]] = []
	orig_urlopen = analyzer.urllib.request.urlopen
	old_cache_disabled = os.environ.get("OPENROUTER_PROMPT_CACHE_DISABLED")
	os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = "false"

	class FakeResponse:
		def __init__(self, body: str):
			self._body = body

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			return False

		def read(self) -> bytes:
			return self._body.encode("utf-8")

	def fake_urlopen(request, timeout=0):
		del timeout
		payload = json.loads(request.data.decode("utf-8"))
		payloads.append(payload)
		if len(payloads) == 1:
			raise urllib.error.HTTPError(
				url=request.full_url,
				code=422,
				msg="Unprocessable Entity",
				hdrs=None,
				fp=io.BytesIO(b'{"error":"cache_control is not supported"}'),
			)
		return FakeResponse(
			json.dumps(
				{
					"choices": [{"message": {"content": "# ok"}}],
					"usage": {
						"prompt_tokens": 8,
						"completion_tokens": 4,
						"total_tokens": 12,
						"cache_creation_input_tokens": 2,
						"cache_read_input_tokens": 1,
					},
				}
			)
		)

	analyzer.urllib.request.urlopen = fake_urlopen
	try:
		content, usage = analyzer.call_openrouter(
			api_key="test-key",
			model="openai/gpt-5.3-codex",
			messages=[{"role": "user", "content": "hello"}],
			temperature=0.0,
			max_tokens=64,
			base_url="https://openrouter.ai/api/v1",
			request_timeout_seconds=10,
		)
	finally:
		analyzer.urllib.request.urlopen = orig_urlopen
		if old_cache_disabled is None:
			os.environ.pop("OPENROUTER_PROMPT_CACHE_DISABLED", None)
		else:
			os.environ["OPENROUTER_PROMPT_CACHE_DISABLED"] = old_cache_disabled

	assert content == "# ok\n"
	assert len(payloads) == 2
	assert payloads[0]["messages"][0]["cache_control"] == {"type": "ephemeral"}
	assert "cache_control" not in payloads[1]["messages"][0]
	assert usage is not None
	assert usage["cache_creation_input_tokens"] == 2
	assert usage["cache_read_input_tokens"] == 1
	assert usage["cache_breakpoint_enabled"] is True
	assert usage["cache_breakpoint_fallback_retry"] is True

def test_load_input_data_rejects_malformed_json_input():
	with tempfile.TemporaryDirectory(prefix="analyze-input-") as td:
		input_file = Path(td) / "bad.json"
		input_file.write_text("{bad", encoding="utf-8")
		args = analyzer.build_parser().parse_args(["--input", str(input_file)])
		try:
			analyzer.load_input_data(args)
			raise AssertionError("expected ValueError")
		except ValueError as exc:
			assert "invalid JSON" in str(exc)


def test_load_input_data_collects_deep_dive_logs_from_collector_runs():
	with tempfile.TemporaryDirectory(prefix="analyze-input-runs-") as td:
		input_file = Path(td) / "collector.json"
		_write_json(
			input_file,
			{
				"schema_version": "workflow_log_collector.v1",
				"runs": [
					{
						"repository": "owner/repo",
						"run_id": 55,
						"log_excerpts": [
							{"step_name": "build", "excerpt": "line1"},
							{"step_name": "test", "excerpt": "line2"},
						],
					}
				],
			},
		)
		args = analyzer.build_parser().parse_args(["--input", str(input_file)])
		loaded = analyzer.load_input_data(args)
		assert loaded["deep_dive_logs"] == [
			{"name": "owner/repo/55/build", "excerpt": "line1"},
			{"name": "owner/repo/55/test", "excerpt": "line2"},
		]


def test_build_parser_max_output_tokens_default_100000():
	args = analyzer.build_parser().parse_args([])
	assert args.max_output_tokens == 100000


def test_load_prompt_template_returns_content_without_placeholders():
	with tempfile.TemporaryDirectory(prefix="analyze-prompt-raw-") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text("You are a test analyzer.", encoding="utf-8")
		rendered = analyzer.load_prompt_template(prompt_file)
		assert rendered == "You are a test analyzer."


def test_load_prompt_template_renders_serena_placeholder():
	with tempfile.TemporaryDirectory(prefix="analyze-prompt-render-") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text("Before\n{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}\nAfter\n", encoding="utf-8")
		rendered = analyzer.load_prompt_template(prompt_file)
		assert "SERENA MCP EFFICIENCY (MANDATORY):" in rendered
		assert "{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}" not in rendered


def test_load_prompt_template_errors_on_unresolved_placeholder():
	with tempfile.TemporaryDirectory(prefix="analyze-prompt-error-") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text("{{SERENA_EFFICIENCY_BLOCK_UNKNOWN}}\n", encoding="utf-8")
		try:
			analyzer.load_prompt_template(prompt_file)
			raise AssertionError("expected RuntimeError")
		except RuntimeError as exc:
			assert "unable to render prompt file" in str(exc)


def test_main_missing_openrouter_api_key_returns_2():
	original = os.environ.get("OPENROUTER_API_KEY")
	try:
		if "OPENROUTER_API_KEY" in os.environ:
			del os.environ["OPENROUTER_API_KEY"]
		exit_code = analyzer.main([])
	finally:
		if original is None:
			os.environ.pop("OPENROUTER_API_KEY", None)
		else:
			os.environ["OPENROUTER_API_KEY"] = original
	assert exit_code == 2


def test_main_generates_dated_report_using_stubs():
	with tempfile.TemporaryDirectory(prefix="analyze-main-") as td:
		tmp = Path(td)
		data_dir = tmp / "data"
		output_dir = tmp / "analysis"
		prompt_file = tmp / "prompt.txt"
		_write_json(
			data_dir / "workflow_log_report.json",
			{
				"schema_version": "workflow_log_collector.v1",
				"scope": {"repositories": ["owner/repo"], "workflow_families": ["plan"]},
				"runs": [
					{
						"repository": "owner/repo",
						"run_id": 101,
						"workflow_name": "AI Plan",
						"workflow_family": "plan",
						"conclusion": "success",
						"duration_seconds": 42,
						"created_at": "2026-04-11T10:00:00Z",
					}
				],
				"summary": {"total_runs": 1},
				"errors": [],
			},
		)
		prompt_file.write_text("You are a test analyzer.", encoding="utf-8")

		orig_call_openrouter = analyzer.call_openrouter
		orig_resolve_dated_output_path = analyzer.resolve_dated_output_path

		def fake_call_openrouter(**kwargs):
			assert kwargs["model"] == "unit-test-model"
			assert kwargs["max_tokens"] == 200
			return "## Executive Summary\n- ok\n", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

		def fake_resolve_dated_output_path(output_path, out_dir, *, today=None):
			assert output_path is None
			assert out_dir == str(output_dir)
			return output_dir / "workflow-optimization-2026-04-11.md"

		analyzer.call_openrouter = fake_call_openrouter
		analyzer.resolve_dated_output_path = fake_resolve_dated_output_path

		original_key = os.environ.get("OPENROUTER_API_KEY")
		try:
			os.environ["OPENROUTER_API_KEY"] = "k"
			exit_code = analyzer.main(
				[
					"--data-dir",
					str(data_dir),
					"--output-dir",
					str(output_dir),
					"--prompt-file",
					str(prompt_file),
					"--model",
					"unit-test-model",
					"--max-output-tokens",
					"200",
				]
			)
		finally:
			if original_key is None:
				os.environ.pop("OPENROUTER_API_KEY", None)
			else:
				os.environ["OPENROUTER_API_KEY"] = original_key
			analyzer.call_openrouter = orig_call_openrouter
			analyzer.resolve_dated_output_path = orig_resolve_dated_output_path

		assert exit_code == 0
		report_path = output_dir / "workflow-optimization-2026-04-11.md"
		assert report_path.exists()
		assert "## Executive Summary" in report_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
