#!/usr/bin/env python3
"""Focused contract tests for review-side Codex thread reuse wiring."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "codex_thread_reuse.sh"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
REVIEW_CONFLICT_RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
REVIEW_APPLY_CONTINUATION = REPO_ROOT / "prompts" / "mode-review-apply-fixes-continuation.txt"
REVIEW_APPLY_CONTRACT = REPO_ROOT / "prompts" / "contracts" / "mode-review-apply-fixes-continuation.yml"
REVIEW_CONFLICT_CONTINUATION = REPO_ROOT / "prompts" / "mode-review-conflict-resolver-continuation.txt"
REVIEW_CONFLICT_CONTRACT = REPO_ROOT / "prompts" / "contracts" / "mode-review-conflict-resolver-continuation.yml"
CONFLICT_LIVE_CONTEXT_MARKER = "=== THREAD REUSE LIVE CONTEXT ==="


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _load_render_prompt_module():
	spec = importlib.util.spec_from_file_location("render_prompt_module", RENDER_PROMPT_PY)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _run_helper(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", str(HELPER), *args],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _render_prompt(prompt_path: Path, *, values: dict[str, str]) -> str:
	env = _base_env()
	env.update(values)
	proc = subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_path)],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def test_render_prompt_autodiscovers_review_continuation_contracts() -> None:
	render_prompt = _load_render_prompt_module()
	for prompt_path, expected_contract, env_updates in (
		(
			REVIEW_APPLY_CONTINUATION,
			REVIEW_APPLY_CONTRACT,
			{"SERENA_TOOL_HINTS": "- Use Serena when helpful."},
		),
		(
			REVIEW_CONFLICT_CONTINUATION,
			REVIEW_CONFLICT_CONTRACT,
			{
				"PREVIOUS_ATTEMPT_NUMBER": "1",
				"MAX_ATTEMPTS": "3",
				"PREVIOUS_ATTEMPT_FAILURE_KIND": "validation",
				"MARKER_VIOLATION_COUNT": "1",
				"MARKER_VIOLATION_FILES": "          - scripts/example.py",
				"FINGERPRINT_VIOLATION_COUNT": "2",
				"FINGERPRINT_VIOLATION_DETAILS": "          - scripts/example.py: missing must_contain pattern",
				"SERENA_TOOL_HINTS_RESOLVER": "- Resolver Serena hint.",
			},
		),
	):
		contract_path = render_prompt.discover_contract_path(prompt_path, prompt_path.stem)
		assert contract_path == expected_contract.resolve()
		rendered = _render_prompt(prompt_path, values=env_updates)
		assert "{{" not in rendered
		if prompt_path == REVIEW_CONFLICT_CONTINUATION:
			assert "Previous attempt: 1 of 3" in rendered
			assert "Failure kind: validation" in rendered
			assert "Remaining conflict-marker files (1):" in rendered
			assert "Resolver Serena hint." in rendered


def test_transform_prompt_replace_prefix_preserves_review_editor_response_schema_and_shrinks_prompt() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_review_editor_") as td:
		tmp_path = Path(td)
		source = tmp_path / "source.txt"
		continuation = tmp_path / "continuation.txt"
		output = tmp_path / "output.txt"
		source_text = (
			"full editor prompt header\n"
			+ ("reviewer evidence\n" * 200)
			+ "FINAL RESPONSE FORMAT\n"
			+ "Changes made:\n"
			+ "Change status:\n"
		)
		source.write_text(source_text, encoding="utf-8")
		continuation_text = _render_prompt(
			REVIEW_APPLY_CONTINUATION,
			values={"SERENA_TOOL_HINTS": ""},
		)
		continuation.write_text(continuation_text, encoding="utf-8")
		proc = _run_helper(
			[
				"transform-prompt",
				"replace-prefix",
				str(source),
				str(continuation),
				str(output),
				"FINAL RESPONSE FORMAT",
			],
			env=_base_env(),
		)
		assert proc.returncode == 0, proc.stderr
		transformed = output.read_text(encoding="utf-8")
		assert transformed == continuation_text + "FINAL RESPONSE FORMAT\nChanges made:\nChange status:\n"
		assert len(transformed) < len(source_text)


def test_transform_prompt_replace_prefix_preserves_resolver_live_context_tail_and_shrinks_prompt() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_review_resolver_") as td:
		tmp_path = Path(td)
		source = tmp_path / "source.txt"
		continuation = tmp_path / "continuation.txt"
		output = tmp_path / "output.txt"
		source_text = (
			"full resolver prompt header\n"
			+ ("merged sub-issue detail\n" * 200)
			+ f"{CONFLICT_LIVE_CONTEXT_MARKER}\n"
			+ "conflicted-file-tail\n"
			+ "semble-tail\n"
		)
		source.write_text(source_text, encoding="utf-8")
		continuation_text = _render_prompt(
			REVIEW_CONFLICT_CONTINUATION,
			values={
				"PREVIOUS_ATTEMPT_NUMBER": "2",
				"MAX_ATTEMPTS": "3",
				"PREVIOUS_ATTEMPT_FAILURE_KIND": "timeout",
				"MARKER_VIOLATION_COUNT": "1",
				"MARKER_VIOLATION_FILES": "          - tests/e2e_smoke_canary.txt",
				"FINGERPRINT_VIOLATION_COUNT": "0",
				"FINGERPRINT_VIOLATION_DETAILS": "(none)",
				"SERENA_TOOL_HINTS_RESOLVER": "",
			},
		)
		continuation.write_text(continuation_text, encoding="utf-8")
		proc = _run_helper(
			[
				"transform-prompt",
				"replace-prefix",
				str(source),
				str(continuation),
				str(output),
				CONFLICT_LIVE_CONTEXT_MARKER,
			],
			env=_base_env(),
		)
		assert proc.returncode == 0, proc.stderr
		transformed = output.read_text(encoding="utf-8")
		assert transformed == continuation_text + f"{CONFLICT_LIVE_CONTEXT_MARKER}\nconflicted-file-tail\nsemble-tail\n"
		assert len(transformed) < len(source_text)


def test_review_apply_fixes_contains_thread_reuse_wiring() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert 'CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}"' in text
	assert 'CODEX_THREAD_REUSE_HELPER="$(resolve_support_script codex_thread_reuse.sh || true)"' in text
	assert "resolve_review_thread_reuse_asset()" in text
	assert "review_thread_reuse_enabled()" in text
	assert "CODEX_THREAD_REUSE_ENABLED requested; OpenCode editor uses the fresh full-prompt path." in text
	assert "codex_thread_reuse_install_wrapper" not in text
	assert 'PATH="${EDITOR_CODEX_PATH}" run_editor_codex_attempt' in text


def test_review_conflict_resolve_contains_thread_reuse_wiring() -> None:
	text = REVIEW_CONFLICT_RESOLVE.read_text(encoding="utf-8")
	assert 'CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}"' in text
	assert 'CODEX_THREAD_REUSE_HELPER=""' in text
	assert '"scripts/codex_thread_reuse.sh"' in text
	assert "resolve_conflict_thread_reuse_asset()" in text
	assert "conflict_thread_reuse_enabled()" in text
	assert "render_conflict_thread_reuse_continuation()" in text
	assert CONFLICT_LIVE_CONTEXT_MARKER in text
	assert "CODEX_THREAD_REUSE_ENABLED requested; OpenCode conflict resolver uses the fresh full-prompt path." in text
	assert "codex_thread_reuse_install_wrapper" not in text
	assert '"${resolver_opencode_cmd[@]}"' in text
	assert 'PREVIOUS_ATTEMPT_FAILURE_KIND="${failure_kind}"' in text


def main() -> int:
	test_render_prompt_autodiscovers_review_continuation_contracts()
	test_transform_prompt_replace_prefix_preserves_review_editor_response_schema_and_shrinks_prompt()
	test_transform_prompt_replace_prefix_preserves_resolver_live_context_tail_and_shrinks_prompt()
	test_review_apply_fixes_contains_thread_reuse_wiring()
	test_review_conflict_resolve_contains_thread_reuse_wiring()
	print("OK: review-side thread reuse compatibility assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
