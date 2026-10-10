#!/usr/bin/env python3
"""A transient Docker Hub failure must not cost the review editor.

`scripts/review_untrusted_sandbox.sh prepare` builds the review sandbox image
from a Docker Hub base image. On PR #6645 (runs 37989220029 and 37994304266)
Docker Hub answered 500/504 once, the build failed, the sandbox prepare step
failed, the editor was skipped and the run was reported as
`editor_empty_noop`. The build now retries registry and network failures,
and `review_autofix.yml` names a failed prepare `sandbox_prepare_failed`.
These tests run the shipped script against a fake `docker` and pin the
workflow wiring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "scripts" / "review_untrusted_sandbox.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"

TRANSIENT = "ERROR: failed to solve: node:22.16.0-bookworm-slim: failed to resolve source metadata for docker.io/library/node:22.16.0-bookworm-slim: unexpected status from HEAD request to https://registry-1.docker.io/v2/library/node/manifests/22.16.0-bookworm-slim: 500 Internal Server Error"
PERMANENT = "ERROR: failed to solve: process \"/bin/sh -c npm install -g opencode-ai@1.2.3\" did not complete successfully: exit code: 1"


@pytest.fixture
def tmp_path():
	# The registry proxy listens on a unix socket under RUNNER_TEMP; pytest's
	# default tmp path is too long for AF_UNIX (108 bytes), so use a short one.
	path = Path(tempfile.mkdtemp(prefix="rsbr-", dir="/tmp"))
	try:
		yield path
	finally:
		shutil.rmtree(path, ignore_errors=True)


def _prepare(tmp_path: Path, build_errors: list[str], **extra_env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
	"""Run `prepare codex`; the fake docker fails one build per entry of build_errors, then succeeds."""
	workspace = tmp_path / "checkout"
	workspace.mkdir()
	subprocess.run(["git", "init", "-q", str(workspace)], check=True)
	(workspace / "app.py").write_text("value = 1\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker-calls"
	counter = tmp_path / "build-count"
	errors_dir = tmp_path / "build-errors"
	errors_dir.mkdir()
	for index, message in enumerate(build_errors, start=1):
		(errors_dir / str(index)).write_text(message + "\n")
	docker = bin_dir / "docker"
	docker.write_text(
		f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n'
		'case "$1" in\n'
		'  build)\n'
		f'    n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{counter}"\n'
		f'    if [ -f "{errors_dir}/$n" ]; then cat "{errors_dir}/$n" >&2; exit 1; fi\n'
		'    printf "sha256:%064d\\n" 0 ;;\n'
		'  run) exit 0 ;;\n'
		'esac\n'
	)
	docker.chmod(0o755)
	github_env = tmp_path / "github_env"
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(workspace), SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"), GITHUB_ENV=str(github_env),
		REVIEW_SANDBOX_BUILD_RETRY_SLEEP_1="0", REVIEW_SANDBOX_BUILD_RETRY_SLEEP_2="0")
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
			"DEPENDENCY_INSTALL_TIMEOUT_SECONDS", "REVIEW_SANDBOX_BUILD_ATTEMPTS"):
		env.pop(inherited, None)
	env.update(extra_env)
	proc = subprocess.run(["bash", str(SANDBOX), "prepare", "codex"], cwd=workspace, env=env,
		capture_output=True, text=True)
	calls = log.read_text().splitlines() if log.exists() else []
	return proc, [call for call in calls if call.startswith("build ")]


def _cleanup(tmp_path: Path) -> None:
	github_env = tmp_path / "github_env"
	if not github_env.exists():
		return
	root = github_env.read_text().split("REVIEW_SANDBOX_ROOT=", 1)[1].strip()
	subprocess.run(["bash", str(SANDBOX), "cleanup"], env=dict(os.environ, REVIEW_SANDBOX_ROOT=root,
		SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"), RUNNER_TEMP=str(tmp_path)), capture_output=True)


def test_first_build_succeeds_without_a_retry(tmp_path):
	proc, builds = _prepare(tmp_path, [])
	try:
		assert proc.returncode == 0, proc.stderr
		assert len(builds) == 1
		assert "REVIEW_SANDBOX_BUILD attempt=1 outcome=ok" in proc.stderr
		assert "REVIEW_SANDBOX_ROOT=" in (tmp_path / "github_env").read_text()
	finally:
		_cleanup(tmp_path)


@pytest.mark.parametrize("message", (
	TRANSIENT,
	"ERROR: failed to solve: failed to fetch oauth token: unexpected status from GET request to https://auth.docker.io/token: 504 Gateway Timeout",
	"ERROR: toomanyrequests: You have reached your pull rate limit. too many requests",
	"ERROR: failed to do request: Head \"https://example.invalid/v2/\": net/http: TLS handshake timeout",
	"npm error code ECONNRESET\nnpm error network aborted: socket hang up",
))
def test_registry_failure_is_retried_then_the_prepare_succeeds(tmp_path, message):
	proc, builds = _prepare(tmp_path, [message])
	try:
		assert proc.returncode == 0, proc.stderr
		assert len(builds) == 2
		assert "REVIEW_SANDBOX_BUILD attempt=1 outcome=retry rc=1" in proc.stderr
		assert "REVIEW_SANDBOX_BUILD attempt=2 outcome=ok" in proc.stderr
		# The failed attempt's own error output stays visible in the log.
		assert message in proc.stderr
	finally:
		_cleanup(tmp_path)


def test_registry_failure_that_persists_fails_after_the_attempt_budget(tmp_path):
	proc, builds = _prepare(tmp_path, [TRANSIENT] * 5)
	assert proc.returncode != 0
	assert len(builds) == 3
	assert "REVIEW_SANDBOX_BUILD attempt=2 outcome=retry rc=1" in proc.stderr
	assert "REVIEW_SANDBOX_BUILD attempt=3 outcome=fail rc=1" in proc.stderr
	assert not (tmp_path / "github_env").exists()


def test_other_build_failures_are_not_retried(tmp_path):
	proc, builds = _prepare(tmp_path, [PERMANENT])
	assert proc.returncode != 0
	assert len(builds) == 1
	assert "REVIEW_SANDBOX_BUILD attempt=1 outcome=fail rc=1" in proc.stderr
	assert "outcome=retry" not in proc.stderr


@pytest.mark.parametrize("value,expected_builds", (("1", 1), ("2", 2), ("abc", 3), ("0", 3)))
def test_attempt_budget_is_configurable_and_validated(tmp_path, value, expected_builds):
	proc, builds = _prepare(tmp_path, [TRANSIENT] * 5, REVIEW_SANDBOX_BUILD_ATTEMPTS=value)
	assert proc.returncode != 0
	assert len(builds) == expected_builds


def test_invalid_retry_sleep_falls_back_instead_of_aborting(tmp_path):
	# A non-numeric sleep would make `sleep` fail under `set -e`; it falls
	# back to the default instead, so cap the wait by letting one retry run.
	proc, builds = _prepare(tmp_path, [TRANSIENT], REVIEW_SANDBOX_BUILD_RETRY_SLEEP_1="soon",
		REVIEW_SANDBOX_BUILD_ATTEMPTS="2")
	try:
		assert proc.returncode == 0, proc.stderr
		assert len(builds) == 2
	finally:
		_cleanup(tmp_path)


def _step(name: str) -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	return text.split(f"- name: {name}\n", 1)[1].split("\n      - name: ", 1)[0]


def test_workflow_keeps_the_prepare_stderr_and_names_a_failed_prepare():
	install = _step("Install project dependencies (best-effort)")
	assert "id: deps_prepare" in install
	for knob, default in (("REVIEW_SANDBOX_BUILD_ATTEMPTS", "3"), ("REVIEW_SANDBOX_BUILD_RETRY_SLEEP_1", "10"), ("REVIEW_SANDBOX_BUILD_RETRY_SLEEP_2", "30")):
		assert f"{knob}: ${{{{ vars.{knob} || '{default}' }}}}" in install
	tee = '2> >(tee -a "${sandbox_prepare_log}" >&2)'
	assert install.count(tee) == 3
	assert install.count('review_untrusted_sandbox.sh" prepare') == 3
	assert 'sandbox_prepare_log="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/sandbox_prepare_stderr.txt"' in install
	summary = _step("Post editor summary comment")
	assert "SANDBOX_PREPARE_STEP_OUTCOME: ${{ steps.deps_prepare.outcome }}" in summary
	assert 'elif [ "${SANDBOX_PREPARE_STEP_OUTCOME:-}" = "failure" ]; then' in summary
	assert 'autofix_empty_failure_reason="sandbox_prepare_failed"' in summary
	assert 'echo "AUTOFIX_SANDBOX_PREPARE_FAILED=true" >> "$GITHUB_ENV"' in summary
	# The reviewer branch keeps precedence: it is checked first.
	assert summary.index('"${REVIEWERS_STEP_OUTCOME:-}" = "failure"') < summary.index('"${SANDBOX_PREPARE_STEP_OUTCOME:-}" = "failure"')
	# The retry-friendly no-output flow is unchanged.
	assert 'echo "AUTOFIX_EDITOR_EMPTY_NOOP=true" >> "$GITHUB_ENV"' in summary
	evidence = '--evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt"'
	for name in ("Post editor summary comment", "Telegram editor-changes-lost warning",
			"Telegram editor-noop-suspicious warning", "Assemble failure evidence"):
		assert evidence in _step(name), name
	assert _step("Assemble failure evidence").count(evidence) == 2


def _summary_branch() -> str:
	summary = _step("Post editor summary comment")
	start = summary.index('            elif [ "${SANDBOX_PREPARE_STEP_OUTCOME:-}" = "failure" ]; then')
	end = summary.index("            # review-autofix-failure:v1 marker", start)
	lines = summary[start:end].splitlines()[1:]
	return "\n".join(line[14:] if line.startswith(" " * 14) else line.strip() for line in lines)


@pytest.mark.parametrize("stderr_text,expected_error", (
	(TRANSIENT + "\nREVIEW_SANDBOX_BUILD attempt=3 outcome=fail rc=1\n", TRANSIENT[len("ERROR: "):]),
	("#7 ERROR: process failed\n", "process failed"),
	("::error::Review dependency isolation failed\n", "Review dependency isolation failed"),
	("", ""),
))
def test_failed_prepare_branch_writes_evidence_and_a_headline(tmp_path, stderr_text, expected_error):
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	if stderr_text:
		(runtime / "sandbox_prepare_stderr.txt").write_text(stderr_text)
	github_env = tmp_path / "github_env"
	script = 'set -euo pipefail\nautofix_empty_failure_reason=editor_empty_noop\nnoop_explanation=x\nif true; then\n' + _summary_branch() + '\n' \
		+ 'printf "reason=%s\\nexplanation=%s\\n" "${autofix_empty_failure_reason}" "${noop_explanation}"\n'
	env = dict(os.environ, RUNTIME_DIR=str(runtime), GITHUB_ENV=str(github_env), SANDBOX_PREPARE_STEP_OUTCOME="failure",
		AUTOFIX_FAILURE_HEAL_PY=str(ROOT / "scripts" / "workflow_failure_heal.py"))
	proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	assert "reason=sandbox_prepare_failed" in proc.stdout
	assert "AUTOFIX_SANDBOX_PREPARE_FAILED=true" in github_env.read_text()
	evidence = (runtime / "sandbox_prepare_failure_evidence.txt").read_text()
	assert evidence.startswith("sandbox_prepare_failed=true\n")
	assert ("REVIEW_SANDBOX_BUILD attempt=3 outcome=fail rc=1" in evidence) is ("outcome=fail" in stderr_text)
	if expected_error:
		assert f"::error::Review sandbox prepare failed: {expected_error}" in evidence
		assert "Review sandbox prepare failed: " in proc.stdout
		assert f"error_present=1" in proc.stdout
	else:
		assert evidence.endswith("::error::Review sandbox prepare failed\n")
		assert "error_present=1" in proc.stdout
	assert 'step "Install project dependencies"' in proc.stdout


def test_run_summary_names_a_failed_prepare_after_the_reviewers():
	text = (ROOT / "scripts" / "review_autofix_step_iteration_summary.sh").read_text(encoding="utf-8")
	body = text.split("def determine_finalize_reason() -> str:", 1)[1]
	reviewers = body.index('if bool_env("AUTOFIX_REVIEWERS_FAILED"):')
	sandbox = body.index('if bool_env("AUTOFIX_SANDBOX_PREPARE_FAILED"):\n        return "sandbox_prepare_failed"')
	empty = body.index('if bool_env("AUTOFIX_EDITOR_EMPTY_NOOP"):')
	assert reviewers < sandbox < empty
