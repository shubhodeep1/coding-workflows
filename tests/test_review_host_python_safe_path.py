#!/usr/bin/env python3
"""Keep PR-controlled modules off the credential-bearing review job's import path."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github/workflows/review_autofix.yml"
WORKSPACE_HELPER = REPO_ROOT / "scripts/workspace_init.sh"
STAGING_HELPER = REPO_ROOT / "scripts/stage_workflow_support.sh"
SCRIPTS = REPO_ROOT / "scripts"


def _bootstrap_sibling_importers() -> list[Path]:
	registry = re.search(r'^REQUIRED_BOOTSTRAP_SCRIPTS="([^"]+)"', STAGING_HELPER.read_text(encoding="utf-8"), re.MULTILINE)
	assert registry is not None
	importers = []
	for script_name in registry.group(1).split():
		if not script_name.endswith(".py"):
			continue
		path = SCRIPTS / script_name
		module = ast.parse(path.read_text(encoding="utf-8"))
		imports = [node.module for node in module.body if isinstance(node, ast.ImportFrom) and node.module]
		imports.extend(alias.name for node in module.body if isinstance(node, ast.Import) for alias in node.names)
		if any((SCRIPTS / (name.split(".", 1)[0] + ".py")).is_file() for name in imports):
			importers.append(path)
	assert SCRIPTS / "ai_memory.py" in importers
	return importers


def _poison(path: Path, sentinel: Path) -> None:
	path.write_text(f"open({str(sentinel)!r}, 'w').write('executed')\n", encoding="utf-8")


def test_review_job_protects_host_python_imports() -> None:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert workflow["jobs"]["codex-agent"]["env"]["PYTHONSAFEPATH"] == "1"


def test_workspace_helper_isolates_every_stdin_python_invocation() -> None:
	python_lines = [line for line in WORKSPACE_HELPER.read_text(encoding="utf-8").splitlines() if re.search(r'\bpython3\s+-', line)]
	assert len(python_lines) == 2
	assert all(re.search(r'\bPYTHONSAFEPATH=1\b', line) for line in python_lines), "stdin Python calls must set inline PYTHONSAFEPATH=1"
	assert all(re.search(r'\bpython3\s+-I\s+-B\s+-\s', line) for line in python_lines)


def test_workspace_helper_never_imports_poisoned_source_modules(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	sentinels = [tmp_path / f"{module}.executed" for module in ("pathlib", "shutil", "sitecustomize")]
	for module, sentinel in zip(("pathlib", "shutil", "sitecustomize"), sentinels):
		_poison(source / f"{module}.py", sentinel)
	(source / "tracked.txt").write_text("safe\n", encoding="utf-8")
	workspace = tmp_path / "runner-temp/workspaces/issue-6308"
	env = os.environ.copy()
	env.update({
		"RUNNER_TEMP": str(tmp_path / "runner-temp"),
		"GITHUB_OUTPUT": str(tmp_path / "output"),
		"GITHUB_ENV": str(tmp_path / "env"),
		"WORKSPACE_SOURCE_PATH": str(source),
		"WORKSPACE_PATH": str(workspace),
		"WORKSPACE_ISSUE_IDENTIFIER": "issue-6308",
		"PYTHONPATH": str(source),
		"PYTHONSAFEPATH": "",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	for command in ("metadata", "finalize"):
		result = subprocess.run(["bash", str(WORKSPACE_HELPER), command], cwd=source, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
	assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "safe\n"
	assert not any(sentinel.exists() for sentinel in sentinels)
	assert not list(source.rglob("__pycache__"))
	assert not list(workspace.rglob("__pycache__"))


@pytest.mark.skipif(sys.version_info < (3, 11), reason="PYTHONSAFEPATH requires Python 3.11+")
def test_review_job_python_c_excludes_pr_cwd(tmp_path: Path) -> None:
	sentinel = tmp_path / "executed"
	_poison(tmp_path / "pathlib.py", sentinel)
	env = os.environ.copy()
	env["PYTHONSAFEPATH"] = "1"
	env.pop("PYTHONPATH", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run([sys.executable, "-c", "import pathlib"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert not sentinel.exists()


@pytest.mark.parametrize("script", _bootstrap_sibling_importers(), ids=lambda path: path.name)
@pytest.mark.skipif(sys.version_info < (3, 11), reason="PYTHONSAFEPATH requires Python 3.11+")
def test_bootstrap_sibling_imports_survive_safe_path(script: Path, tmp_path: Path) -> None:
	sentinel = tmp_path / "executed"
	_poison(tmp_path / "ai_memory_lib.py", sentinel)
	env = os.environ.copy()
	env["PYTHONSAFEPATH"] = "1"
	env.pop("PYTHONPATH", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert not sentinel.exists()
