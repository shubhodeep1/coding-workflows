#!/usr/bin/env python3
"""Render-contract tests for validate and workflow-analysis prompt modes."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
FORBIDDEN_PLACEHOLDER = "WORKFLOW_EDIT_RESTRICTION"
UNKNOWN_PLACEHOLDER = "UNKNOWN_VALIDATE_ANALYTICS_VAR"
OUTPUT_CONTRACT_SENTINEL = "Terminal output contract:"
VERIFICATION_LOOP_SENTINEL = "Verification loop:"
VALIDATE_OUTPUT_CONTRACT_SENTINEL = "JSON schema:"


@dataclass(frozen=True)
class PassCase:
	mode_name: str
	sentinel: str
	placeholder_name: str | None = None
	expected_placeholder_occurrences: int | None = None


PASS_CASES = (
	PassCase("mode-validate-discover", "Role: validate-discover.", "SERENA_TOOL_HINTS", 0),
	PassCase(
		"mode-validate-generate",
		"You are executing the VALIDATE-GENERATE phase of the AI development pipeline.",
	),
	PassCase("mode-validate-fix-harness", "Role: validate-fix-harness.", "SERENA_TOOL_HINTS", 0),
	PassCase("mode-validate-diagnose", "Role: validate-diagnose.", "SERENA_TOOL_HINTS", 0),
	PassCase(
		"mode-validate-self-heal",
		"Role: validate-self-heal patch proposer.",
		"SERENA_TOOL_HINTS",
		1,
	),
	PassCase(
		"mode-workflow-analysis",
		"You are a workflow optimization analyst for an AI-powered GitHub Actions pipeline.",
		"SEMBLE_PREFETCH",
		0,
	),
	PassCase(
		"mode-workflow-audit",
		"You are a workflow and script auditor for an AI-powered GitHub Actions pipeline.",
		"SEMBLE_PREFETCH",
		0,
	),
	PassCase(
		"mode-workflow-api-redundancy",
		"You are a conservative GitHub API call consolidation auditor for an AI-powered GitHub Actions pipeline.",
		"SEMBLE_PREFETCH",
		0,
	),
	PassCase(
		"mode-security-audit",
		"**Chief Security Officer.** Your job is to perform a default-branch security audit of this repository using OWASP Top 10 + STRIDE framing.",
	),
)
SERENA_VALIDATION_MODES = (
	"mode-validate-discover",
	"mode-validate-fix-harness",
	"mode-validate-diagnose",
)
SEMBLE_WORKFLOW_MODES = (
	"mode-workflow-analysis",
	"mode-workflow-audit",
	"mode-workflow-api-redundancy",
)
ALL_MODE_NAMES = tuple(case.mode_name for case in PASS_CASES)


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("ALLOW_WORKFLOW_EDITS", None)
	env.pop("SEMBLE_PREFETCH", None)
	env.pop("SERENA_TOOL_HINTS", None)
	return env


def _prompt_path(mode_name: str) -> Path:
	return PROMPTS_DIR / f"{mode_name}.txt"


def _normalized_text(path: Path) -> str:
	text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
	if not text.endswith("\n"):
		text += "\n"
	return text


def _run_render(
	prompt_file: Path,
	*,
	variables: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	for name, value in sorted((variables or {}).items()):
		env[name] = value
	return subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
		cwd=str(REPO_ROOT),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _assert_success(proc: subprocess.CompletedProcess[str]) -> None:
	assert proc.returncode == 0, (
		f"render_prompt.sh failed with {proc.returncode}\n"
		f"stdout:\n{proc.stdout}\n\n"
		f"stderr:\n{proc.stderr}"
	)
	assert proc.stderr == ""


def _assert_contract_failure(proc: subprocess.CompletedProcess[str], *, category: str, name: str) -> None:
	assert proc.returncode == 1, proc.stderr
	assert proc.stdout == ""
	assert category in proc.stderr
	assert name in proc.stderr


def _assert_output_contract_rendered(rendered_text: str) -> None:
	assert OUTPUT_CONTRACT_SENTINEL in rendered_text
	assert "{{REFERENCE_OUTPUT_CONTRACT}}" not in rendered_text


def test_real_prompts_render_with_contract_defaults() -> None:
	for case in PASS_CASES:
		prompt_file = _prompt_path(case.mode_name)
		proc = _run_render(prompt_file)

		_assert_success(proc)
		assert case.sentinel in proc.stdout
		_assert_output_contract_rendered(proc.stdout)
		if case.mode_name in {"mode-validate-generate", "mode-validate-fix-harness"}:
			assert VERIFICATION_LOOP_SENTINEL in proc.stdout
			assert "{{REFERENCE_VERIFICATION_LOOP}}" not in proc.stdout
		if case.mode_name == "mode-validate-generate":
			assert VALIDATE_OUTPUT_CONTRACT_SENTINEL in proc.stdout
		if case.placeholder_name is not None:
			token = f"{{{{{case.placeholder_name}}}}}"
			assert proc.stdout.count(token) == case.expected_placeholder_occurrences


def test_serena_optional_placeholder_substitution_for_validation_modes() -> None:
	hints = "Serena hints:\n- use find_symbol\n- keep apply_patch primary"
	for mode_name in SERENA_VALIDATION_MODES:
		proc = _run_render(_prompt_path(mode_name), variables={"SERENA_TOOL_HINTS": hints})

		_assert_success(proc)
		assert hints in proc.stdout
		assert "{{SERENA_TOOL_HINTS}}" not in proc.stdout


def test_semble_optional_placeholder_substitution_for_workflow_analysis_modes() -> None:
	prefetch = "=== SEMBLE: Analytics Context ===\nchunk one\n=== END SEMBLE ==="
	for mode_name in SEMBLE_WORKFLOW_MODES:
		proc = _run_render(_prompt_path(mode_name), variables={"SEMBLE_PREFETCH": prefetch})

		_assert_success(proc)
		assert prefetch in proc.stdout
		assert "{{SEMBLE_PREFETCH}}" not in proc.stdout


def test_validate_self_heal_handles_standalone_and_literal_serena_markers() -> None:
	prompt_file = _prompt_path("mode-validate-self-heal")
	raw_prompt = _normalized_text(prompt_file)
	hints = "Serena hints:\n- inspect prompt-only defects"

	assert raw_prompt.count("{{SERENA_TOOL_HINTS}}") == 2

	proc_without_var = _run_render(prompt_file)
	_assert_success(proc_without_var)
	_assert_output_contract_rendered(proc_without_var.stdout)
	assert proc_without_var.stdout.count("{{SERENA_TOOL_HINTS}}") == 1
	assert "`{{SERENA_TOOL_HINTS}}`" in proc_without_var.stdout

	proc_with_var = _run_render(prompt_file, variables={"SERENA_TOOL_HINTS": hints})
	_assert_success(proc_with_var)
	_assert_output_contract_rendered(proc_with_var.stdout)
	assert hints in proc_with_var.stdout
	assert proc_with_var.stdout.count("{{SERENA_TOOL_HINTS}}") == 1
	assert "`{{SERENA_TOOL_HINTS}}`" in proc_with_var.stdout


def test_contracts_reject_forbidden_placeholder_for_all_validate_and_workflow_modes() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_validate_analytics_modes_") as td:
		prompt_file = Path(td) / "prompt.txt"

		for mode_name in ALL_MODE_NAMES:
			prompt_file = Path(td) / f"{mode_name}.txt"
			prompt_file.write_text(
				f"Header\n{{{{{FORBIDDEN_PLACEHOLDER}}}}}\nFooter\n",
				encoding="utf-8",
			)
			proc = _run_render(prompt_file)
			_assert_contract_failure(proc, category="forbidden_present", name=FORBIDDEN_PLACEHOLDER)


def test_contracts_reject_unknown_placeholder_for_all_validate_and_workflow_modes() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_validate_analytics_modes_") as td:
		prompt_file = Path(td) / "prompt.txt"

		for mode_name in ALL_MODE_NAMES:
			prompt_file = Path(td) / f"{mode_name}.txt"
			prompt_file.write_text(
				f"Header\n{{{{{UNKNOWN_PLACEHOLDER}}}}}\nFooter\n",
				encoding="utf-8",
			)
			proc = _run_render(prompt_file)
			_assert_contract_failure(proc, category="unknown_in_template", name=UNKNOWN_PLACEHOLDER)


def main() -> int:
	test_real_prompts_render_with_contract_defaults()
	test_serena_optional_placeholder_substitution_for_validation_modes()
	test_semble_optional_placeholder_substitution_for_workflow_analysis_modes()
	test_validate_self_heal_handles_standalone_and_literal_serena_markers()
	test_contracts_reject_forbidden_placeholder_for_all_validate_and_workflow_modes()
	test_contracts_reject_unknown_placeholder_for_all_validate_and_workflow_modes()
	print("OK: validate/workflow-analysis prompt contracts render correctly")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
