#!/usr/bin/env python3
"""scripts/validate_changed_files_syntax.sh: the heal editor's in-container validator.

`scripts/heal_isolated_implement.sh` mounts the script at `/validator.sh` and
runs `/bin/bash /validator.sh` with the edited work tree as the working
directory and no arguments, inside the clarify sandbox image (Debian
bookworm's Python 3.11). These tests drive it the same way on temp work trees,
so they run on CI's pinned Python and inside that image (`tests-sandbox-python`
in `.github/workflows/ci.yml`, #9233).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"

pytestmark = pytest.mark.skipif(
	shutil.which("git") is None,
	reason="the validator lists changed files with git; git is not installed",
)


def _clean_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	for key in ("CAPTURE_FILE", "RUNTIME_DIR", "ALLOW_WORKFLOW_EDITS"):
		env.pop(key, None)
	env["HOME"] = str(home)
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env.update(extra or {})
	return env


def _work_tree(tmp_path: Path, files: dict[str, str]) -> Path:
	tree = tmp_path / "source"
	tree.mkdir()
	home = tmp_path / "home"
	home.mkdir(exist_ok=True)
	subprocess.run(["git", "init", "-q", str(tree)], check=True, env=_clean_env(home))
	for name, content in files.items():
		target = tree / name
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(content, encoding="utf-8")
	return tree


def _run(tmp_path: Path, tree: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", str(SCRIPT)],
		cwd=tree,
		env=_clean_env(tmp_path / "home", extra_env),
		capture_output=True,
		text=True,
		check=False,
		timeout=120,
	)


def _yaml_importable() -> bool:
	return subprocess.run(["python3", "-c", "import yaml"], capture_output=True, check=False).returncode == 0


def test_valid_python_and_json_pass(tmp_path: Path) -> None:
	tree = _work_tree(tmp_path, {"pkg/ok.py": "x = 1\n", "data/ok.json": '{"a": [1, 2]}\n'})
	result = _run(tmp_path, tree)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "All changed files passed syntax validation." in result.stdout


def test_invalid_python_fails(tmp_path: Path) -> None:
	tree = _work_tree(tmp_path, {"bad.py": "def broken(:\n\tpass\n"})
	result = _run(tmp_path, tree)
	assert result.returncode == 1
	assert "Syntax error in bad.py" in result.stdout


def test_invalid_json_fails(tmp_path: Path) -> None:
	tree = _work_tree(tmp_path, {"bad.json": '{"a": 1,}\n'})
	result = _run(tmp_path, tree)
	assert result.returncode == 1
	assert "JSON syntax error in bad.json" in result.stdout


def test_invalid_python_is_captured_for_repair(tmp_path: Path) -> None:
	tree = _work_tree(tmp_path, {"bad.py": "def broken(:\n\tpass\n"})
	capture = tmp_path / "capture.txt"
	result = _run(tmp_path, tree, {"CAPTURE_FILE": str(capture)})
	assert result.returncode == 1
	assert "===== bad.py (python3 -m py_compile) =====" in capture.read_text(encoding="utf-8")


@pytest.mark.skipif(not _yaml_importable(), reason="python3 cannot import yaml here; the missing-checker case is covered below")
def test_valid_yaml_passes_and_invalid_yaml_fails(tmp_path: Path) -> None:
	(tmp_path / "good").mkdir()
	good = _work_tree(tmp_path / "good", {"ok.yml": "a:\n  - 1\n"})
	assert _run(tmp_path / "good", good).returncode == 0
	(tmp_path / "bad").mkdir()
	bad = _work_tree(tmp_path / "bad", {"bad.yaml": "a: [1, 2\n"})
	result = _run(tmp_path / "bad", bad)
	assert result.returncode == 1
	assert "YAML syntax error in bad.yaml" in result.stdout


def test_missing_yaml_checker_is_not_a_syntax_error(tmp_path: Path) -> None:
	"""#7030: an image without PyYAML reports the file unchecked (exit 3), never as a syntax error."""
	shim = tmp_path / "no-yaml"
	shim.mkdir()
	(shim / "yaml.py").write_text("raise ImportError('PyYAML hidden by the test')\n", encoding="utf-8")
	tree = _work_tree(tmp_path, {"workflow.yml": "a: [1, 2\n", "ok.py": "x = 1\n"})
	result = _run(tmp_path, tree, {"PYTHONPATH": str(shim)})
	assert result.returncode == 3, result.stdout + result.stderr
	assert "VALIDATE_CHANGED_FILES_SYNTAX outcome=validator_unavailable checker=yaml unchecked=1" in result.stdout
	assert "YAML syntax error" not in result.stdout


def test_real_syntax_error_wins_over_missing_yaml_checker(tmp_path: Path) -> None:
	shim = tmp_path / "no-yaml"
	shim.mkdir()
	(shim / "yaml.py").write_text("raise ImportError('PyYAML hidden by the test')\n", encoding="utf-8")
	tree = _work_tree(tmp_path, {"workflow.yml": "a: 1\n", "bad.json": "{\n"})
	result = _run(tmp_path, tree, {"PYTHONPATH": str(shim)})
	assert result.returncode == 1
