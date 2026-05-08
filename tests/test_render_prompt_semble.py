#!/usr/bin/env python3
"""Contract tests for scripts/render_prompt.sh Semble placeholder handling."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_prompt.sh"


def _run_render(template_body: str, *, semble_prefetch: str | None = None, allow_workflow_edits: str | None = None) -> subprocess.CompletedProcess[str]:
	with tempfile.TemporaryDirectory(prefix="render-prompt-semble-") as td:
		tmp = Path(td)
		template = tmp / "prompt.txt"
		template.write_text(template_body, encoding="utf-8")
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		if allow_workflow_edits is not None:
			env["ALLOW_WORKFLOW_EDITS"] = allow_workflow_edits
		if semble_prefetch is not None:
			prefetch = tmp / "semble.txt"
			prefetch.write_text(semble_prefetch, encoding="utf-8")
			env["SEMBLE_PREFETCH_FILE"] = str(prefetch)
		return subprocess.run(
			["bash", str(RENDER_SCRIPT), str(template)],
			text=True,
			capture_output=True,
			env=env,
			check=False,
		)


def test_render_prompt_injects_semble_prefetch_from_file() -> None:
	result = _run_render("before\n{{SEMBLE_PREFETCH}}\nafter\n", semble_prefetch="=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===\n")
	assert result.returncode == 0, result.stderr
	assert result.stdout == "before\n=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===\nafter\n"
	assert result.stderr == "", result.stderr


def test_render_prompt_omits_semble_placeholder_when_file_unset() -> None:
	result = _run_render("alpha\n{{SEMBLE_PREFETCH}}\nomega\n")
	assert result.returncode == 0, result.stderr
	assert result.stdout == "alpha\nomega\n"


def test_render_prompt_keeps_workflow_edit_restriction_behavior() -> None:
	result = _run_render("{{WORKFLOW_EDIT_RESTRICTION}}\n{{SEMBLE_PREFETCH}}\n", allow_workflow_edits="true")
	assert result.returncode == 0, result.stderr
	assert "CI workflow edits under .github/workflows/ are permitted" in result.stdout
	assert "{{SEMBLE_PREFETCH}}" not in result.stdout


def main() -> int:
	test_render_prompt_injects_semble_prefetch_from_file()
	test_render_prompt_omits_semble_placeholder_when_file_unset()
	test_render_prompt_keeps_workflow_edit_restriction_behavior()
	print("OK: render_prompt Semble placeholder contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
