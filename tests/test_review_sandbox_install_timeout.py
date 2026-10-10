#!/usr/bin/env python3
"""A slow dependency install must not cost the editor run.

`scripts/review_untrusted_sandbox.sh prepare` installs the PR's dependencies
in a network-isolated container under a 900-second `timeout`. On 2026-10-09
pip backtracked through dozens of `ruff` and `structlog` releases for a
consumer's unpinned `[dev]` extras until that budget killed the container;
the kill counted as `Review dependency isolation failed`, the step failed,
the editor was skipped (31 minutes per review run, twice per run with the
OpenCode fallback) and the PR looped on "produced no output, will retry".

Now every install command runs under `DEPENDENCY_INSTALL_TIMEOUT_SECONDS`
(default 600) inside the container, and a container that still outruns its
budget is a failed install (warning), not a failed isolation boundary. Any
other non-zero container exit stays fatal. These tests run the shipped
script against a fake `docker`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "scripts" / "review_untrusted_sandbox.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"


@pytest.fixture
def tmp_path():
	# The registry proxy listens on a unix socket under RUNNER_TEMP; pytest's
	# default tmp path is too long for AF_UNIX (108 bytes), so use a short one.
	path = Path(tempfile.mkdtemp(prefix="rsit-", dir="/tmp"))
	try:
		yield path
	finally:
		shutil.rmtree(path, ignore_errors=True)


def _prepare(tmp_path: Path, run_exit: int, **extra_env: str) -> tuple[subprocess.CompletedProcess, str]:
	workspace = tmp_path / "checkout"
	workspace.mkdir()
	subprocess.run(["git", "init", "-q", str(workspace)], check=True)
	(workspace / "app.py").write_text("value = 1\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker-calls"
	docker = bin_dir / "docker"
	docker.write_text(
		f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n'
		'case "$1" in\n'
		'  build) printf "sha256:%064d\\n" 0 ;;\n'
		f'  run) exit {run_exit} ;;\n'
		'esac\n'
	)
	docker.chmod(0o755)
	github_env = tmp_path / "github_env"
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(workspace), SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"), GITHUB_ENV=str(github_env))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
			"DEPENDENCY_INSTALL_TIMEOUT_SECONDS"):
		env.pop(inherited, None)
	env.update(extra_env)
	proc = subprocess.run(["bash", str(SANDBOX), "prepare", "codex"], cwd=workspace, env=env,
		capture_output=True, text=True)
	return proc, (log.read_text() if log.exists() else "")


def _cleanup(tmp_path: Path) -> None:
	github_env = tmp_path / "github_env"
	if not github_env.exists():
		return
	root = github_env.read_text().split("REVIEW_SANDBOX_ROOT=", 1)[1].strip()
	subprocess.run(["bash", str(SANDBOX), "cleanup"], env=dict(os.environ, REVIEW_SANDBOX_ROOT=root,
		SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"), RUNNER_TEMP=str(tmp_path)), capture_output=True)


def test_timed_out_dependency_container_is_a_failed_install_not_an_isolation_failure(tmp_path):
	proc, log = _prepare(tmp_path, 124)
	try:
		assert proc.returncode == 0, proc.stderr
		assert "dependency container timed out after 900s" in proc.stderr
		assert "Review dependency isolation failed" not in proc.stderr
		assert "REVIEW_SANDBOX_ROOT=" in (tmp_path / "github_env").read_text()
		assert "run " in log
	finally:
		_cleanup(tmp_path)


@pytest.mark.parametrize("run_exit", (1, 137))
def test_any_other_container_failure_is_still_fatal(tmp_path, run_exit):
	# 137 before the 900s budget elapsed is an OOM kill, not a timeout.
	proc, _ = _prepare(tmp_path, run_exit)
	assert proc.returncode == 1
	assert "Review dependency isolation failed" in proc.stderr
	assert not (tmp_path / "github_env").exists()


def test_successful_container_prepares_as_before(tmp_path):
	proc, log = _prepare(tmp_path, 0)
	try:
		assert proc.returncode == 0, proc.stderr
		assert "timed out" not in proc.stderr
		assert "DEPENDENCY_INSTALL_TIMEOUT_SECONDS=600" in log
	finally:
		_cleanup(tmp_path)


@pytest.mark.parametrize("value,expected", (("45", "45"), ("abc", "600"), ("0", "600"), ("-5", "600"), ("", "600")))
def test_install_budget_is_passed_into_the_container_and_validated(tmp_path, value, expected):
	proc, log = _prepare(tmp_path, 0, DEPENDENCY_INSTALL_TIMEOUT_SECONDS=value)
	try:
		assert proc.returncode == 0, proc.stderr
		assert f"--env DEPENDENCY_INSTALL_TIMEOUT_SECONDS={expected} " in log
		assert ("Invalid DEPENDENCY_INSTALL_TIMEOUT_SECONDS" in proc.stderr) is (value not in ("45", ""))
	finally:
		_cleanup(tmp_path)


def test_every_install_command_inside_the_container_is_bounded():
	text = SANDBOX.read_text(encoding="utf-8")
	inner = text.split("/bin/bash -c '", 1)[1].split("\n\t\t' || dep_rc=$?", 1)[0]
	assert "bounded_install() {" in inner
	assert 'timeout --signal=TERM --kill-after=10s "${DEPENDENCY_INSTALL_TIMEOUT_SECONDS:-600}" "$@" 2>&1' in inner
	assert '[ $((SECONDS - started_at)) -ge "${DEPENDENCY_INSTALL_TIMEOUT_SECONDS:-600}" ]' in inner
	for command in ("npm ci --ignore-scripts", "yarn install --frozen-lockfile --ignore-scripts",
			"npx pnpm install --frozen-lockfile --ignore-scripts", "npm install --ignore-scripts",
			"pip install -r requirements.txt", 'pip install -e ".[dev]"', "pip install -e .",
			"python3 -m pip install pytest", "python3 -m pip install --user --break-system-packages pytest"):
		assert f"bounded_install {command}" in inner, command
		assert not re.search(r"(?<![\w-])" + re.escape(command) + r" 2>&1", inner), f"{command} runs unbounded"
	assert 'echo "::warning::Review dependency install timed out after' in inner
	# The venv itself is not a package manager call and must stay fatal.
	assert "python3 -m venv --system-site-packages /source/.review-venv || exit 1" in inner


def test_workflow_passes_the_repo_variable_with_the_default():
	text = WORKFLOW.read_text(encoding="utf-8")
	step = text.split("- name: Install project dependencies (best-effort)", 1)[1].split("- name: ", 1)[0]
	assert "DEPENDENCY_INSTALL_TIMEOUT_SECONDS: ${{ vars.DEPENDENCY_INSTALL_TIMEOUT_SECONDS || '600' }}" in step
