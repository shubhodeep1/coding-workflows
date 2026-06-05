#!/usr/bin/env python3
"""End-to-end tests for WORKFLOW.md overlay loading and prompt wiring."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LOAD_WORKFLOW_OVERLAY_PY = REPO_ROOT / "scripts" / "load_workflow_overlay.py"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "workflow_overlay.v1.json"
WORKFLOW_FILES = (
	REPO_ROOT / ".github" / "workflows" / "clarify.yml",
	REPO_ROOT / ".github" / "workflows" / "plan.yml",
	REPO_ROOT / ".github" / "workflows" / "implement.yml",
	REPO_ROOT / ".github" / "workflows" / "review_autofix.yml",
	REPO_ROOT / ".github" / "workflows" / "validate.yml",
)


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _parse_github_env(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		if not line:
			continue
		name, _, value = line.partition("=")
		values[name] = value
	return values


def _run_loader(repo_root: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
	github_env = repo_root / "overlay.env"
	proc = subprocess.run(
		[
			sys.executable,
			str(LOAD_WORKFLOW_OVERLAY_PY),
			"--repo-root",
			str(repo_root),
			"--schema-path",
			str(SCHEMA_PATH),
			"--github-env",
			str(github_env),
		],
		cwd=str(repo_root),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)
	return proc, github_env


def _run_render_prompt_py(prompt_file: Path, *, repo_root: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	env.update(extra_env or {})
	return subprocess.run(
		[sys.executable, str(RENDER_PROMPT_PY), str(prompt_file)],
		cwd=str(repo_root),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _run_render_prompt_sh(prompt_file: Path, *, repo_root: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	env.update(extra_env or {})
	return subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
		cwd=str(repo_root),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def test_absent_workflow_overlay_is_a_noop() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow_overlay_absent_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-inline.txt"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text("Base prompt\n", encoding="utf-8")

		proc, github_env = _run_loader(repo_root)

		assert proc.returncode == 0, proc.stderr
		assert proc.stdout == ""
		assert proc.stderr == ""
		values = _parse_github_env(github_env)
		assert values == {
			"WORKFLOW_OVERLAY_ENABLED": "false",
			"WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON": "",
			"WORKFLOW_OVERLAY_REPO_ROOT": "",
		}

		proc = _run_render_prompt_py(prompt_file, repo_root=repo_root, extra_env=values)
		assert proc.returncode == 0, proc.stderr
		assert proc.stderr == ""
		assert proc.stdout == "Base prompt\n"


def test_loader_rejects_unknown_top_level_keys() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow_overlay_unknown_key_") as td:
		repo_root = Path(td)
		overlay_file = repo_root / ".github" / "ai" / "WORKFLOW.md"
		overlay_file.parent.mkdir(parents=True, exist_ok=True)
		overlay_file.write_text(
			"---\n"
			"schema_version: workflow_overlay.v1\n"
			"prompt_overrides: []\n"
			"unexpected_flag: true\n"
			"---\n",
			encoding="utf-8",
		)

		proc, _github_env = _run_loader(repo_root)

		assert proc.returncode == 1
		assert proc.stdout == ""
		assert "unexpected_flag" in proc.stderr


def test_loader_exports_prompt_overrides_and_shell_shim_applies_them() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow_overlay_apply_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-inline.txt"
		fragment_file = repo_root / ".github" / "ai" / "fragments" / "append.txt"
		overlay_file = repo_root / ".github" / "ai" / "WORKFLOW.md"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		fragment_file.parent.mkdir(parents=True, exist_ok=True)
		overlay_file.parent.mkdir(parents=True, exist_ok=True)

		prompt_file.write_text("Base prompt\n", encoding="utf-8")
		fragment_file.write_text("Overlay appendix\n", encoding="utf-8")
		overlay_file.write_text(
			"---\n"
			"schema_version: workflow_overlay.v1\n"
			"prompt_overrides:\n"
			"  - mode: mode-inline\n"
			"    append_path: .github/ai/fragments/append.txt\n"
			"---\n"
			"Human-readable workflow notes.\n",
			encoding="utf-8",
		)

		loader_proc, github_env = _run_loader(repo_root)

		assert loader_proc.returncode == 0, loader_proc.stderr
		assert loader_proc.stdout == ""
		assert loader_proc.stderr == ""
		values = _parse_github_env(github_env)
		assert values["WORKFLOW_OVERLAY_ENABLED"] == "true"
		assert values["WORKFLOW_OVERLAY_REPO_ROOT"] == str(repo_root)
		assert json.loads(values["WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON"]) == [
			{
				"mode": "mode-inline",
				"append_path": ".github/ai/fragments/append.txt",
			}
		]

		proc = _run_render_prompt_sh(prompt_file, repo_root=repo_root, extra_env=values)
		assert proc.returncode == 0, proc.stderr
		assert proc.stderr == ""
		assert proc.stdout == "Base prompt\nOverlay appendix\n"


def test_render_prompt_rejects_nonexistent_overlay_repo_root() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow_overlay_bad_root_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-inline.txt"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text("Base prompt\n", encoding="utf-8")

		proc = _run_render_prompt_py(
			prompt_file,
			repo_root=repo_root,
			extra_env={
				"WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON": '[{"mode":"mode-inline","append_path":"extra.txt"}]',
				"WORKFLOW_OVERLAY_REPO_ROOT": str(repo_root / "missing-root"),
			},
		)

		assert proc.returncode == 1
		assert proc.stdout == ""
		assert "WORKFLOW_OVERLAY_REPO_ROOT must point to an existing directory" in proc.stderr


def test_render_prompt_rejects_invalid_overlay_mode_names() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow_overlay_bad_mode_") as td:
		repo_root = Path(td)
		prompt_file = repo_root / "prompts" / "mode-inline.txt"
		prompt_file.parent.mkdir(parents=True, exist_ok=True)
		prompt_file.write_text("Base prompt\n", encoding="utf-8")

		proc = _run_render_prompt_py(
			prompt_file,
			repo_root=repo_root,
			extra_env={
				"WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON": '[{"mode":"---","append_path":"extra.txt"}]',
				"WORKFLOW_OVERLAY_REPO_ROOT": str(repo_root),
			},
		)

		assert proc.returncode == 1
		assert proc.stdout == ""
		assert "contains invalid mode name '---'" in proc.stderr


def test_target_workflows_stage_schema_and_invoke_loader() -> None:
	for workflow_path in WORKFLOW_FILES:
		workflow_text = workflow_path.read_text(encoding="utf-8")
		assert "load_workflow_overlay.py" in workflow_text, workflow_path
		assert "workflow_overlay.v1.json" in workflow_text, workflow_path
		assert "WORKFLOW.md overlay is opt-in by file presence" in workflow_text, workflow_path
		assert "--github-env \"${GITHUB_ENV}\"" in workflow_text, workflow_path

	assert 'python3 scripts/load_workflow_overlay.py \\\n            --repo-root "${GITHUB_WORKSPACE}"' in (REPO_ROOT / ".github" / "workflows" / "clarify.yml").read_text(encoding="utf-8")
	assert 'python3 scripts/load_workflow_overlay.py \\\n            --repo-root "${GITHUB_WORKSPACE}"' in (REPO_ROOT / ".github" / "workflows" / "plan.yml").read_text(encoding="utf-8")
	assert 'python3 scripts/load_workflow_overlay.py \\\n            --repo-root "${GITHUB_WORKSPACE}"' in (REPO_ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8")
	assert 'python3 "${SUPPORT_SCRIPTS_DIR}/load_workflow_overlay.py" \\\n            --repo-root "${GITHUB_WORKSPACE}"' in (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	assert 'python3 scripts/load_workflow_overlay.py \\\n            --repo-root "${GITHUB_WORKSPACE}"' in (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")


if __name__ == "__main__":
	test_absent_workflow_overlay_is_a_noop()
	test_loader_rejects_unknown_top_level_keys()
	test_loader_exports_prompt_overrides_and_shell_shim_applies_them()
	test_render_prompt_rejects_nonexistent_overlay_repo_root()
	test_render_prompt_rejects_invalid_overlay_mode_names()
	test_target_workflows_stage_schema_and_invoke_loader()
