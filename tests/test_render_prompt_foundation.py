#!/usr/bin/env python3
"""Foundation tests for the prompt renderer shim and contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def test_render_prompt_sh_renders_implement_contract_defaults_and_env_values() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_sh_") as td:
		prompt_file = Path(td) / "mode-implement.txt"
		prompt_file.write_text(
			"Header\n{{SERENA_TOOL_HINTS}}\n{{WORKFLOW_EDIT_RESTRICTION}}\nFooter\n",
			encoding="utf-8",
		)

		env = _base_env()
		env["ALLOW_WORKFLOW_EDITS"] = "true"
		env["SERENA_TOOL_HINTS"] = "Serena hints:\n- use find_symbol"

		proc = subprocess.run(
			["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
			cwd=str(REPO_ROOT),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == (
		"Header\n"
		"Serena hints:\n"
		"- use find_symbol\n"
		"- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope.\n"
		"Footer\n"
	)


def test_render_prompt_py_reports_unknown_placeholder_contract_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_py_") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text("Before\n{{UNKNOWN}}\nAfter\n", encoding="utf-8")

		proc = subprocess.run(
			[
				sys.executable,
				str(RENDER_PROMPT_PY),
				str(prompt_file),
				"--legacy-mode-name",
				"mode-implement",
			],
			cwd=str(REPO_ROOT),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "unknown_in_template" in proc.stderr
	assert "UNKNOWN" in proc.stderr


def main() -> int:
	test_render_prompt_sh_renders_implement_contract_defaults_and_env_values()
	test_render_prompt_py_reports_unknown_placeholder_contract_violation()
	print("OK: render prompt foundation assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
