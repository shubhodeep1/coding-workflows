#!/usr/bin/env python3
"""Foundation tests for the prompt renderer shim and contracts."""

from __future__ import annotations

import os
import shutil
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


def test_render_prompt_py_renders_inline_placeholders_and_yaml_scalar_defaults() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_inline_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-inline.txt"
		contract_file = repo_root / "prompts" / "contracts" / "mode-inline.yml"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		contract_file.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text(
			"attempt {{MAX_ATTEMPTS}} enabled={{ENABLED}}\n{{BODY}}\n",
			encoding="utf-8",
		)
		contract_file.write_text(
			"required_vars: []\n"
			"optional_vars:\n"
			"  ENABLED: true\n"
			"  MAX_ATTEMPTS: 3\n"
			"  BODY: \"Body line\"\n"
			"forbidden_vars: []\n",
			encoding="utf-8",
		)

		proc = subprocess.run(
			[sys.executable, str(RENDER_PROMPT_PY), str(prompt_file)],
			cwd=str(repo_root),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "attempt 3 enabled=true\nBody line\n"


def test_render_prompt_sh_uses_trusted_backend_locations_only() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_trusted_backend_") as td:
		runtime_root = Path(td)
		prompt_file = runtime_root / "prompts" / "mode-implement.txt"
		support_render_prompt_sh = runtime_root / "support" / "scripts" / "render_prompt.sh"
		trusted_backend = runtime_root / ".codex-workflow-src" / "scripts" / "render_prompt.py"
		trusted_contract = runtime_root / ".codex-workflow-src" / "prompts" / "contracts" / "mode-implement.yml"
		malicious_backend = runtime_root / "scripts" / "render_prompt.py"

		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		support_render_prompt_sh.parent.mkdir(parents=True, exist_ok=True)
		trusted_backend.parent.mkdir(parents=True, exist_ok=True)
		trusted_contract.parent.mkdir(parents=True, exist_ok=True)
		malicious_backend.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("{{WORKFLOW_EDIT_RESTRICTION}}\n", encoding="utf-8")
		malicious_backend.write_text(
			"import sys\nsys.stdout.write('MALICIOUS\\n')\n",
			encoding="utf-8",
		)
		shutil.copy2(RENDER_PROMPT_SH, support_render_prompt_sh)
		shutil.copy2(RENDER_PROMPT_PY, trusted_backend)
		shutil.copy2(REPO_ROOT / "prompts" / "contracts" / "mode-implement.yml", trusted_contract)
		env = _base_env()
		env["ALLOW_WORKFLOW_EDITS"] = "false"

		proc = subprocess.run(
			["bash", str(support_render_prompt_sh), str(prompt_file)],
			cwd=str(runtime_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "- Do not change CI workflows.\n"


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
	test_render_prompt_py_renders_inline_placeholders_and_yaml_scalar_defaults()
	test_render_prompt_sh_uses_trusted_backend_locations_only()
	test_render_prompt_py_reports_unknown_placeholder_contract_violation()
	print("OK: render prompt foundation assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
