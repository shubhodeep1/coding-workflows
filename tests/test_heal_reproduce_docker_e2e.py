#!/usr/bin/env python3
"""Opt-in end-to-end test of scripts/heal_reproduce.sh: real Docker, real gate.

Skipped unless HEAL_REPRODUCE_DOCKER_E2E=1 and a Docker daemon answers,
because it builds the sandbox image (network) and prepares dependencies,
which takes minutes. Same opt-in shape as
tests/test_codex_isolated_exec_docker_e2e.py.

The temp repository holds a stdlib `unittest` file with a `__main__` runner,
so the gate module runs it as `python3 <file>` without pytest. The test
passes at the base commit and fails at the next one, so the helper must
report `reproduced` / `regression` and leave no worktree or temp root behind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "heal_reproduce.sh"

TEST_FILE = """import unittest

from lib import value


class Regress(unittest.TestCase):
	def test_value(self):
		self.assertEqual(value.X, 1)


if __name__ == "__main__":
	unittest.main()
"""


def docker_ready() -> bool:
	if os.environ.get("HEAL_REPRODUCE_DOCKER_E2E") != "1" or not shutil.which("docker"):
		return False
	return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ready(), reason="set HEAL_REPRODUCE_DOCKER_E2E=1 with a Docker daemon")


def _git(cwd: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def test_regression_is_reproduced_in_real_container(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	(repo / "lib").mkdir(parents=True)
	(repo / "tests").mkdir()
	(repo / "lib" / "__init__.py").write_text("", encoding="utf-8")
	(repo / "lib" / "value.py").write_text("X = 1\n", encoding="utf-8")
	(repo / "tests" / "test_regress.py").write_text(TEST_FILE, encoding="utf-8")
	_git(repo, "init", "-q")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "green")
	base = _git(repo, "rev-parse", "HEAD")
	(repo / "lib" / "value.py").write_text("X = 2\n", encoding="utf-8")
	_git(repo, "commit", "-q", "-am", "regress")
	failing = _git(repo, "rev-parse", "HEAD")
	tests = tmp_path / "tests.txt"
	tests.write_text("tests/test_regress.py\n", encoding="utf-8")
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	out = tmp_path / "repro.json"
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GIT_", "WORKFLOW_HEAL_"))}
	env.update({"RUNNER_TEMP": str(runner_temp), "TMPDIR": str(runner_temp), "GH_TOKEN": "ghp_e2enotpassed000000000000000000000000"})
	proc = subprocess.run(["bash", str(SCRIPT), "--tests-file", str(tests), "--sha", failing, "--baseline", base, "--out", str(out)],
		cwd=repo, env=env, capture_output=True, text=True, timeout=1500)
	assert proc.returncode == 0, proc.stderr
	data = json.loads(out.read_text(encoding="utf-8"))
	assert (data["status"], data["reason"]) == ("reproduced", "regression"), (data, proc.stderr[-4000:])
	assert (data["failing_at"], data["passing_at"]) == (failing, base)
	assert data["tests"] == [{"test": "tests/test_regress.py", "status": "reproduced", "reason": "regression"}]
	assert "HEAL_REPRODUCE outcome=reproduced reason=regression tests=1" in proc.stderr
	assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
	assert not list(runner_temp.glob("heal-repro*"))
