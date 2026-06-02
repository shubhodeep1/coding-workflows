#!/usr/bin/env python3
"""Strict-render tests for review/judge/conflict prompt contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
PROMPTS_DIR = REPO_ROOT / "prompts"
CONTRACTS_DIR = PROMPTS_DIR / "contracts"

CONTRACT_NAMES = (
	"mode-judge.yml",
	"mode-judge-interim.yml",
	"mode-judge-review-blocked.yml",
	"mode-judge-stall-recovery.yml",
	"mode-orchestrate-poll-judge.yml",
	"review-consolidator.yml",
	"review-reviewer-checklist.yml",
	"conflict-resolver.yml",
	"integration-sync-conflict-resolver.yml",
)


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _normalized_text(path: Path) -> str:
	text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
	if not text.endswith("\n"):
		text += "\n"
	return text


def _run_render(
	prompt_path: Path,
	*,
	variables: dict[str, str] | None = None,
	legacy_mode_name: str | None = None,
	workdir: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
	command = [sys.executable, str(RENDER_PROMPT_PY), str(prompt_path)]
	if legacy_mode_name is not None:
		command.extend(["--legacy-mode-name", legacy_mode_name])
	for name, value in (variables or {}).items():
		command.extend(["--var", f"{name}={value}"])
	return subprocess.run(
		command,
		cwd=str(workdir),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)


def _run_render_sh(
	prompt_path: Path,
	*,
	env_overrides: dict[str, str | None] | None = None,
	workdir: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	for name, value in (env_overrides or {}).items():
		if value is None:
			env.pop(name, None)
		else:
			env[name] = value
	return subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_path)],
		cwd=str(workdir),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _assert_success(proc: subprocess.CompletedProcess[str]) -> None:
	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""


def test_contract_files_exist() -> None:
	for contract_name in CONTRACT_NAMES:
		assert (CONTRACTS_DIR / contract_name).is_file(), contract_name


def test_judge_prompts_render_under_current_contracts() -> None:
	mode_judge = _run_render(PROMPTS_DIR / "mode-judge.txt")
	_assert_success(mode_judge)
	assert "{{SEMBLE_PREFETCH}}" not in mode_judge.stdout
	assert "\n\nYou have full access to the repository checkout and all tools" in mode_judge.stdout

	review_blocked_prefetch = "=== SEMBLE: Review-Blocked Judge Context ===\nchunk"
	mode_judge_review_blocked = _run_render(
		PROMPTS_DIR / "mode-judge-review-blocked.txt",
		variables={"SEMBLE_PREFETCH": review_blocked_prefetch},
	)
	_assert_success(mode_judge_review_blocked)
	assert review_blocked_prefetch + "\n" in mode_judge_review_blocked.stdout
	assert "{{SEMBLE_PREFETCH}}" not in mode_judge_review_blocked.stdout

	stall_prefetch = "=== SEMBLE: Stall Judge Context ===\nchunk"
	mode_judge_stall = _run_render(
		PROMPTS_DIR / "mode-judge-stall-recovery.txt",
		variables={"SEMBLE_PREFETCH": stall_prefetch},
	)
	_assert_success(mode_judge_stall)
	assert stall_prefetch + "\n" in mode_judge_stall.stdout
	assert "{{SEMBLE_PREFETCH}}" not in mode_judge_stall.stdout


def test_shell_wrapper_renders_review_blocked_judge_under_current_contracts() -> None:
	review_blocked_prefetch = "=== SEMBLE: Review-Blocked Judge Context ===\nchunk"
	proc = _run_render_sh(
		PROMPTS_DIR / "mode-judge-review-blocked.txt",
		env_overrides={"SEMBLE_PREFETCH": review_blocked_prefetch},
	)
	_assert_success(proc)
	assert review_blocked_prefetch + "\n" in proc.stdout
	assert "{{SEMBLE_PREFETCH}}" not in proc.stdout


def test_no_placeholder_review_and_judge_prompts_round_trip() -> None:
	for prompt_name in (
		"mode-judge-interim.txt",
		"mode-orchestrate-poll-judge.txt",
		"review-consolidator.txt",
		"review-reviewer-checklist.txt",
	):
		prompt_path = PROMPTS_DIR / prompt_name
		proc = _run_render(prompt_path)
		_assert_success(proc)
		assert proc.stdout == _normalized_text(prompt_path), prompt_name


def test_conflict_resolver_renders_required_values_and_optional_hints(
) -> None:
	for resolver_hints in (
		None,
		"Resolver Serena hints:\n- use find_symbol",
	):
		variables = {
			"CONFLICTED_FILES_COUNT": "2",
			"CONFLICTED_FILES_LIST": "- prompts/conflict-resolver.txt\n- tests/test_render_prompt_review_judge_modes.py",
		}
		if resolver_hints is not None:
			variables["SERENA_TOOL_HINTS_RESOLVER"] = resolver_hints

		proc = _run_render(PROMPTS_DIR / "conflict-resolver.txt", variables=variables)
		_assert_success(proc)
		assert "Conflicted files reported by `git diff --name-only --diff-filter=U` (2 total):" in proc.stdout
		assert variables["CONFLICTED_FILES_LIST"] in proc.stdout
		assert "{{CONFLICTED_FILES_COUNT}}" not in proc.stdout
		assert "{{CONFLICTED_FILES_LIST}}" not in proc.stdout
		assert "{{SERENA_TOOL_HINTS_RESOLVER}}" not in proc.stdout
		if resolver_hints is None:
			assert "Resolver Serena hints:" not in proc.stdout
		else:
			assert resolver_hints + "\n" in proc.stdout


def test_integration_sync_conflict_resolver_renders_all_expected_values() -> None:
	variables = {
		"INTEGRATION_BRANCH": "orchestrator/project-3042",
		"MERGED_SUB_ISSUE_COUNT": "2",
		"TRACKING_ISSUE_NUMBER": "3042",
		"CONFLICTED_FILES_COUNT": "2",
		"CONFLICTED_FILES_LIST": "- prompts/mode-judge.txt\n- prompts/conflict-resolver.txt",
		"TRACKING_ISSUE_TITLE": "Strict rendering rollout",
		"TRACKING_ISSUE_BODY": "First line of intent.\nSecond line of intent.",
		"MERGED_SUB_ISSUES_LIST": "- #3050 review/judge contracts\n- #3051 follow-up",
		"INTENT_FINGERPRINTS_JSON": (
			'{"3050":{"prompts/mode-judge.txt":{"must_contain":["SEMBLE_PREFETCH"],'
			'"must_not_contain":[],"must_not_exist":[]}}}'
		),
	}

	proc = _run_render(
		PROMPTS_DIR / "integration-sync-conflict-resolver.txt",
		variables=variables,
	)
	_assert_success(proc)
	for placeholder in (
		"{{INTEGRATION_BRANCH}}",
		"{{MERGED_SUB_ISSUE_COUNT}}",
		"{{TRACKING_ISSUE_NUMBER}}",
		"{{CONFLICTED_FILES_COUNT}}",
		"{{CONFLICTED_FILES_LIST}}",
		"{{TRACKING_ISSUE_TITLE}}",
		"{{TRACKING_ISSUE_BODY}}",
		"{{MERGED_SUB_ISSUES_LIST}}",
		"{{INTENT_FINGERPRINTS_JSON}}",
		"{{SERENA_TOOL_HINTS_RESOLVER}}",
	):
		assert placeholder not in proc.stdout
	assert "(`orchestrator/project-3042`)" in proc.stdout
	assert "project #3042" in proc.stdout
	assert "Strict rendering rollout" in proc.stdout
	assert "First line of intent.\nSecond line of intent." in proc.stdout
	assert variables["MERGED_SUB_ISSUES_LIST"] in proc.stdout
	assert variables["INTENT_FINGERPRINTS_JSON"] in proc.stdout


def test_conflict_resolver_reports_missing_required_contract_violation() -> None:
	proc = _run_render(
		PROMPTS_DIR / "conflict-resolver.txt",
		variables={"CONFLICTED_FILES_COUNT": "1"},
	)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "missing_required" in proc.stderr
	assert "CONFLICTED_FILES_LIST" in proc.stderr


def test_shell_wrapper_reports_missing_required_contract_violation() -> None:
	proc = _run_render_sh(PROMPTS_DIR / "conflict-resolver.txt")

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "missing_required" in proc.stderr
	assert "CONFLICTED_FILES_COUNT" in proc.stderr
	assert "CONFLICTED_FILES_LIST" in proc.stderr


def test_review_consolidator_contract_reports_forbidden_placeholder() -> None:
	with tempfile.TemporaryDirectory(prefix="review_consolidator_contract_") as td:
		prompt_path = Path(td) / "review-consolidator-fixture.txt"
		prompt_path.write_text("Header\n{{SEMBLE_PREFETCH}}\nFooter\n", encoding="utf-8")

		proc = _run_render(
			prompt_path,
			legacy_mode_name="review-consolidator",
		)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "forbidden_present" in proc.stderr
	assert "SEMBLE_PREFETCH" in proc.stderr


def main() -> int:
	"""Keep this file runnable via `python3 tests/<file>.py`.

	The repo's CI uses explicit direct-run allowlists rather than pytest
	discovery, so this module must not require pytest to import or execute.
	"""
	test_contract_files_exist()
	test_judge_prompts_render_under_current_contracts()
	test_shell_wrapper_renders_review_blocked_judge_under_current_contracts()
	test_no_placeholder_review_and_judge_prompts_round_trip()
	test_conflict_resolver_renders_required_values_and_optional_hints()
	test_integration_sync_conflict_resolver_renders_all_expected_values()
	test_conflict_resolver_reports_missing_required_contract_violation()
	test_shell_wrapper_reports_missing_required_contract_violation()
	test_review_consolidator_contract_reports_forbidden_placeholder()
	print("OK: review/judge/conflict prompt contracts render and reject invalid inputs")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
