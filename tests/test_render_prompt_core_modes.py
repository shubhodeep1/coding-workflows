#!/usr/bin/env python3
"""Strict-contract tests for core prompt modes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"

ZERO_PLACEHOLDER_PROMPTS = (
	("mode-clarify", REPO_ROOT / "prompts" / "mode-clarify.txt"),
	("mode-clarify-respond", REPO_ROOT / "prompts" / "mode-clarify-respond.txt"),
	("mode-orchestrate", REPO_ROOT / "prompts" / "mode-orchestrate.txt"),
)

SERENA_PROMPTS = (
	("mode-implement-repair", REPO_ROOT / "prompts" / "mode-implement-repair.txt"),
	("mode-implement-repair-syntax", REPO_ROOT / "prompts" / "mode-implement-repair-syntax.txt"),
	("mode-implement-diagnose", REPO_ROOT / "prompts" / "mode-implement-diagnose.txt"),
)


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _normalize_prompt_text(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def _run_render_prompt(
	prompt_file: Path,
	*,
	legacy_mode_name: str | None = None,
	variables: dict[str, str] | None = None,
	cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
	args = [sys.executable, str(RENDER_PROMPT_PY), str(prompt_file)]
	if legacy_mode_name is not None:
		args.extend(["--legacy-mode-name", legacy_mode_name])
	for name, value in (variables or {}).items():
		args.extend(["--var", f"{name}={value}"])
	return subprocess.run(
		args,
		cwd=str(cwd),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)


def _run_render_prompt_sh(
	prompt_file: Path,
	*,
	env_overrides: dict[str, str] | None = None,
	cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	env.update(env_overrides or {})
	return subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
		cwd=str(cwd),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def test_zero_placeholder_core_modes_render_under_strict_contracts() -> None:
	for mode_name, prompt_file in ZERO_PLACEHOLDER_PROMPTS:
		proc = _run_render_prompt(prompt_file)

		assert proc.returncode == 0, f"{mode_name}: {proc.stderr}"
		assert proc.stderr == ""
		assert proc.stdout == _normalize_prompt_text(prompt_file.read_text(encoding="utf-8"))


def test_serena_core_modes_render_with_default_and_explicit_optional_hints() -> None:
	hint_value = "Serena hints:\n- inspect symbol"
	test_cases = (
		("default", {}, "\n"),
		("explicit", {"SERENA_TOOL_HINTS": hint_value}, "Serena hints:\n- inspect symbol\n"),
	)

	for mode_name, prompt_file in SERENA_PROMPTS:
		prompt_text = _normalize_prompt_text(prompt_file.read_text(encoding="utf-8"))
		assert prompt_text.count("{{SERENA_TOOL_HINTS}}") == 1

		for case_name, variables, replacement in test_cases:
			proc = _run_render_prompt(prompt_file, variables=variables)

			assert proc.returncode == 0, f"{mode_name}/{case_name}: {proc.stderr}"
			assert proc.stderr == ""
			assert "{{SERENA_TOOL_HINTS}}" not in proc.stdout
			assert proc.stdout == prompt_text.replace("{{SERENA_TOOL_HINTS}}\n", replacement)


def test_zero_placeholder_core_mode_contract_rejects_forbidden_placeholder() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_core_modes_") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text("Before\n{{SERENA_TOOL_HINTS}}\nAfter\n", encoding="utf-8")

		proc = _run_render_prompt(prompt_file, legacy_mode_name="mode-clarify")

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "forbidden_present" in proc.stderr
	assert "SERENA_TOOL_HINTS" in proc.stderr
	assert "mode-clarify.yml" in proc.stderr


def test_render_prompt_sh_enforces_core_mode_contracts_in_production_path() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_core_modes_sh_") as td:
		prompt_file = Path(td) / "prompts" / "mode-clarify.txt"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text("Before\n{{SERENA_TOOL_HINTS}}\nAfter\n", encoding="utf-8")

		proc = _run_render_prompt_sh(prompt_file)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "forbidden_present" in proc.stderr
	assert "SERENA_TOOL_HINTS" in proc.stderr
	assert "mode-clarify.yml" in proc.stderr


def main() -> int:
	test_zero_placeholder_core_modes_render_under_strict_contracts()
	test_serena_core_modes_render_with_default_and_explicit_optional_hints()
	test_zero_placeholder_core_mode_contract_rejects_forbidden_placeholder()
	test_render_prompt_sh_enforces_core_mode_contracts_in_production_path()
	print("OK: core mode prompt contracts render as expected")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
