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


def _normalize_text(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _load_reference_text(file_name: str) -> str:
	return _normalize_text((REPO_ROOT / "prompts" / "references" / file_name).read_text(encoding="utf-8"))


def test_output_contract_reference_includes_status_update_cadence() -> None:
	reference_text = _load_reference_text("output-contract.txt")
	assert "Emit one short preamble sentence (≤20 words) before each tool-call batch" in reference_text
	assert "After every 3–5 tool calls" in reference_text
	assert "`Checkpoint: <bullet list of files touched, what changed>`" in reference_text
	assert "Finish with the requested deliverable shape for this prompt." in reference_text


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


def test_render_prompt_sh_renders_header_with_empty_repo_learnings() -> None:
	proc = subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(REPO_ROOT / "prompts" / "header.txt")],
		cwd=str(REPO_ROOT),
		env={**_base_env(), "REPO_LEARNINGS": ""},
		text=True,
		capture_output=True,
		timeout=60,
	)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "Role: AI pipeline phase agent. Goal: produce the artefact described below.\n\n"


def test_render_prompt_sh_renders_header_with_populated_repo_learnings() -> None:
	proc = subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(REPO_ROOT / "prompts" / "header.txt")],
		cwd=str(REPO_ROOT),
		env={
			**_base_env(),
			"REPO_LEARNINGS": "Repository learnings from prior merged work:\n- Prefer bounded prompt injections\n- Keep memory extraction fail-open",
		},
		text=True,
		capture_output=True,
		timeout=60,
	)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == (
		"Role: AI pipeline phase agent. Goal: produce the artefact described below.\n"
		"Repository learnings from prior merged work:\n"
		"- Prefer bounded prompt injections\n"
		"- Keep memory extraction fail-open\n"
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
		trusted_reference_dir = runtime_root / ".codex-workflow-src" / "prompts" / "references"
		malicious_backend = runtime_root / "scripts" / "render_prompt.py"

		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		support_render_prompt_sh.parent.mkdir(parents=True, exist_ok=True)
		trusted_backend.parent.mkdir(parents=True, exist_ok=True)
		trusted_contract.parent.mkdir(parents=True, exist_ok=True)
		trusted_reference_dir.mkdir(parents=True, exist_ok=True)
		malicious_backend.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("{{REFERENCE_OUTPUT_CONTRACT}}\n{{WORKFLOW_EDIT_RESTRICTION}}\n", encoding="utf-8")
		malicious_backend.write_text(
			"import sys\nsys.stdout.write('MALICIOUS\\n')\n",
			encoding="utf-8",
		)
		shutil.copy2(RENDER_PROMPT_SH, support_render_prompt_sh)
		shutil.copy2(RENDER_PROMPT_PY, support_render_prompt_sh.with_name("render_prompt.py"))
		shutil.copy2(RENDER_PROMPT_PY, trusted_backend)
		shutil.copy2(REPO_ROOT / "prompts" / "contracts" / "mode-implement.yml", trusted_contract)
		shutil.copy2(REPO_ROOT / "prompts" / "references" / "output-contract.txt", trusted_reference_dir)
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
	assert proc.stdout == _load_reference_text("output-contract.txt") + "- Do not change CI workflows.\n"


def test_render_prompt_py_renders_reference_placeholders_and_mode_specific_append() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_references_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-validate-generate.txt"
		contract_file = repo_root / "prompts" / "contracts" / "mode-validate-generate.yml"
		reference_dir = repo_root / "prompts" / "references"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		contract_file.parent.mkdir(parents=True, exist_ok=True)
		reference_dir.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("Header\n{{REFERENCE_OUTPUT_CONTRACT}}\nFooter\n", encoding="utf-8")
		contract_file.write_text(
			"required_vars: []\n"
			"optional_vars:\n"
			"  REFERENCE_OUTPUT_CONTRACT: \"\"\n"
			"forbidden_vars: []\n",
			encoding="utf-8",
		)
		(reference_dir / "output-contract.txt").write_text("Shared output block.\n", encoding="utf-8")
		(reference_dir / "validate-output-contract.txt").write_text("Validate-only output block.\n", encoding="utf-8")

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
	assert proc.stdout == "Header\nShared output block.\nValidate-only output block.\nFooter\n"


def test_render_prompt_py_reports_missing_mode_specific_append_reference() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_missing_append_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-validate-generate.txt"
		contract_file = repo_root / "prompts" / "contracts" / "mode-validate-generate.yml"
		reference_dir = repo_root / "prompts" / "references"
		render_script = repo_root / "scripts" / "render_prompt.py"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		contract_file.parent.mkdir(parents=True, exist_ok=True)
		reference_dir.mkdir(parents=True, exist_ok=True)
		render_script.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("Header\n{{REFERENCE_OUTPUT_CONTRACT}}\nFooter\n", encoding="utf-8")
		contract_file.write_text(
			"required_vars: []\n"
			"optional_vars:\n"
			"  REFERENCE_OUTPUT_CONTRACT: \"\"\n"
			"forbidden_vars: []\n",
			encoding="utf-8",
		)
		(reference_dir / "output-contract.txt").write_text("Shared output block.\n", encoding="utf-8")
		shutil.copy2(RENDER_PROMPT_PY, render_script)

		proc = subprocess.run(
			[sys.executable, str(render_script), str(prompt_file)],
			cwd=str(repo_root),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert (
		"Append reference file 'validate-output-contract.txt' for placeholder 'REFERENCE_OUTPUT_CONTRACT' not found"
		in proc.stderr
	)
	assert "prompts/references/validate-output-contract.txt" in proc.stderr


def test_render_prompt_py_reports_missing_reference_file() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_foundation_missing_reference_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-clarify.txt"
		contract_file = repo_root / "prompts" / "contracts" / "mode-clarify.yml"
		render_script = repo_root / "scripts" / "render_prompt.py"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		contract_file.parent.mkdir(parents=True, exist_ok=True)
		render_script.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("Header\n{{REFERENCE_OUTPUT_CONTRACT}}\nFooter\n", encoding="utf-8")
		contract_file.write_text(
			"required_vars: []\n"
			"optional_vars:\n"
			"  REFERENCE_OUTPUT_CONTRACT: \"\"\n"
			"forbidden_vars: []\n",
			encoding="utf-8",
		)
		shutil.copy2(RENDER_PROMPT_PY, render_script)

		proc = subprocess.run(
			[sys.executable, str(render_script), str(prompt_file)],
			cwd=str(repo_root),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "Reference file for placeholder 'REFERENCE_OUTPUT_CONTRACT' not found" in proc.stderr
	assert "prompts/references/output-contract.txt" in proc.stderr


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
	test_output_contract_reference_includes_status_update_cadence()
	test_render_prompt_sh_renders_implement_contract_defaults_and_env_values()
	test_render_prompt_sh_renders_header_with_empty_repo_learnings()
	test_render_prompt_sh_renders_header_with_populated_repo_learnings()
	test_render_prompt_py_renders_inline_placeholders_and_yaml_scalar_defaults()
	test_render_prompt_sh_uses_trusted_backend_locations_only()
	test_render_prompt_py_renders_reference_placeholders_and_mode_specific_append()
	test_render_prompt_py_reports_missing_mode_specific_append_reference()
	test_render_prompt_py_reports_missing_reference_file()
	test_render_prompt_py_reports_unknown_placeholder_contract_violation()
	print("OK: render prompt foundation assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
