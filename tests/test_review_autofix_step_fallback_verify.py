#!/usr/bin/env python3
"""The review_autofix.yml ``.codex-workflow-src`` fallback must be verified (#10208).

The ``codex-agent`` step wrappers resolve their script from the staged
support bundle (``SUPPORT_SCRIPTS_DIR``, under ``RUNNER_TEMP``) and fall back
to ``${GITHUB_WORKSPACE}/.codex-workflow-src/scripts``. When the job fails
before the trusted support checkout ("Reject project OpenCode configuration
before agent setup", for example), that directory still holds whatever the
PR head committed there, and the ``always()`` / ``failure()`` steps would
source it with GH_PAT in reach.

These tests run each wrapper's real ``run:`` text against a planted
``.codex-workflow-src/scripts/<script>`` and check it is sourced only when
the "Ensure workflow support source checkout" sentinel and ``git rev-parse
HEAD`` both equal ``SCRIPT_REF``. The step text is read without PyYAML so
the checks run wherever bash and git are available.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_STEP_SCRIPTS,
	REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_WORKFLOW_PATH,
)


SENTINEL = "REVIEW_SUPPORT_SOURCE_VERIFIED_SHA"
# #10221 gate: the wrapper only selects the .codex-workflow-src fallback once
# "Ensure workflow support source checkout" set this flag; the #10208 verify
# line then re-checks the SHA sentinel and HEAD before the script is sourced.
GATE_FLAG = "WORKFLOW_SUPPORT_SOURCE_VERIFIED"
_WORKFLOW_TEXT = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
_CODEX_AGENT_TEXT = _WORKFLOW_TEXT.split("\n  codex-agent:\n", 1)[1]

DEFAULT_WRAPPER_STEPS = sorted(
	(step_name, script_name, severity)
	for step_name, (script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items()
	if script_name not in REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS
	and script_name not in REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS
	and script_name not in REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS
)


def _step_block(step_name: str) -> str:
	match = re.search(
		r"^      - name: [\"']?" + re.escape(step_name) + r"[\"']?\n(?P<body>(?:(?!      - ).*\n)*)",
		_CODEX_AGENT_TEXT,
		re.MULTILINE,
	)
	assert match, f"codex-agent step not found: {step_name}"
	return match.group("body")


def _step_run(step_name: str) -> str:
	lines = _step_block(step_name).split("\n")
	start = lines.index("        run: |") + 1
	body: list[str] = []
	for line in lines[start:]:
		if line and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	return "\n".join(body).rstrip("\n") + "\n"


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
		env.pop(key, None)
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit_support_checkout(workspace: Path) -> str:
	support = workspace / ".codex-workflow-src"
	support.mkdir(parents=True, exist_ok=True)
	_git(support, "init", "-q")
	(support / "README").write_text("support\n", encoding="utf-8")
	_git(support, "add", "README")
	_git(support, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "support")
	return _git(support, "rev-parse", "HEAD")


def _activated_git_env(tmp_path: Path) -> dict[str, str]:
	"""GIT_DIR/GIT_WORK_TREE as "Activate workspace shell context" exports them.

	They point at a different repository (the PR checkout), so a verify line
	that does not clear them reads the wrong HEAD and refuses the fallback.
	"""
	pr_checkout = tmp_path / "pr-checkout"
	pr_checkout.mkdir(exist_ok=True)
	_git(pr_checkout, "init", "-q")
	(pr_checkout / "README").write_text("pr\n", encoding="utf-8")
	_git(pr_checkout, "add", "README")
	_git(pr_checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "pr")
	return {"GIT_DIR": str(pr_checkout / ".git"), "GIT_WORK_TREE": str(pr_checkout)}


def _plant(workspace: Path, script_name: str, marker: Path) -> None:
	scripts = workspace / ".codex-workflow-src" / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	if script_name.endswith(".py"):
		body = f"from pathlib import Path\nPath({str(marker)!r}).write_text('sourced')\n"
	else:
		body = f"#!/usr/bin/env bash\nprintf sourced > {str(marker)!r}\n"
	(scripts / script_name).write_text(body, encoding="utf-8")


def _run_step(run: str, tmp_path: Path, workspace: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
	script = tmp_path / "step.sh"
	script.write_text(run, encoding="utf-8")
	runner_temp = tmp_path / "runner-temp"
	runner_temp.mkdir(exist_ok=True)
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in ("SUPPORT_SCRIPTS_DIR", "SCRIPT_REF", SENTINEL, GATE_FLAG, "GIT_DIR", "GIT_WORK_TREE", "BASH_ENV", "ENV")
	}
	env.update(
		{
			"GITHUB_WORKSPACE": str(workspace),
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(tmp_path / "github-output"),
			"GITHUB_ENV": str(tmp_path / "github-env"),
			"GIT_CONFIG_GLOBAL": os.devnull,
			"GIT_CONFIG_NOSYSTEM": "1",
			# Keep git from walking out of tmp_path into an enclosing repo.
			"GIT_CEILING_DIRECTORIES": str(tmp_path.parent),
		}
	)
	env.update(extra_env)
	# GitHub's default run: shell for ubuntu runners.
	return subprocess.run(
		["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


@pytest.mark.parametrize(("step_name", "script_name", "severity"), DEFAULT_WRAPPER_STEPS, ids=[s[0] for s in DEFAULT_WRAPPER_STEPS])
def test_wrapper_refuses_unverified_workspace_fallback(step_name: str, script_name: str, severity: str, tmp_path: Path) -> None:
	run = _step_run(step_name)
	expected_rc = 0 if severity == "warning" else 1
	workspace = tmp_path / "ws"
	workspace.mkdir()
	marker = tmp_path / "marker"
	_plant(workspace, script_name, marker)

	# Early failure: nothing set SCRIPT_REF or the sentinel. The #10221 gate
	# never selects the fallback, so the step reports the script as missing.
	result = _run_step(run, tmp_path, workspace, {})
	assert not marker.exists(), f"{step_name}: sourced a PR-tree script with no verified support checkout"
	assert result.returncode == expected_rc, (step_name, result.returncode, result.stdout, result.stderr)
	assert f"{script_name} not found in the staged support bundle" in result.stdout, (step_name, result.stdout)

	# A valid-looking SCRIPT_REF without the sentinel is still refused.
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": "a" * 40})
	assert not marker.exists(), f"{step_name}: sourced the fallback without the verify sentinel"
	assert result.returncode == expected_rc, (step_name, result.stdout, result.stderr)

	# Sentinel set, but .codex-workflow-src is not at SCRIPT_REF.
	head = _commit_support_checkout(workspace)
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": "b" * 40, SENTINEL: "b" * 40, GATE_FLAG: "true"})
	assert not marker.exists(), f"{step_name}: sourced a fallback whose HEAD differs from SCRIPT_REF"
	assert result.returncode == expected_rc, (step_name, result.stdout, result.stderr)

	# The #10221 gate passes (flag set, HEAD == SCRIPT_REF) but the #10208 SHA
	# sentinel is missing: the verify line refuses the fallback.
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": head, GATE_FLAG: "true"})
	assert not marker.exists(), f"{step_name}: sourced the fallback without the verify sentinel"
	assert result.returncode == expected_rc, (step_name, result.stdout, result.stderr)
	assert "Refusing unverified .codex-workflow-src fallback" in result.stdout, step_name


@pytest.mark.parametrize(("step_name", "script_name", "severity"), DEFAULT_WRAPPER_STEPS, ids=[s[0] for s in DEFAULT_WRAPPER_STEPS])
def test_wrapper_keeps_verified_fallback_and_staged_bundle(step_name: str, script_name: str, severity: str, tmp_path: Path) -> None:
	del severity
	run = _step_run(step_name)
	workspace = tmp_path / "ws"
	workspace.mkdir()
	marker = tmp_path / "marker"
	head = _commit_support_checkout(workspace)
	_plant(workspace, script_name, marker)
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": head, SENTINEL: head, GATE_FLAG: "true"})
	assert result.returncode == 0, (step_name, result.stdout, result.stderr)
	assert marker.read_text() == "sourced", f"{step_name}: verified .codex-workflow-src fallback must still be sourced"

	# Same, with the job-wide GIT_DIR/GIT_WORK_TREE of the split PR workspace.
	marker.unlink()
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": head, SENTINEL: head, GATE_FLAG: "true", **_activated_git_env(tmp_path)})
	assert result.returncode == 0, (step_name, result.stdout, result.stderr)
	assert marker.read_text() == "sourced", f"{step_name}: verified fallback must be sourced after GIT_DIR is exported"

	# The staged bundle outside the workspace needs no sentinel.
	marker.unlink()
	(workspace / ".codex-workflow-src" / "scripts" / script_name).unlink()
	staged = tmp_path / "runner-temp" / "coding-workflows-runtime-x" / "scripts"
	staged.mkdir(parents=True)
	(staged / script_name).write_text(f"printf sourced > {str(marker)!r}\n", encoding="utf-8")
	result = _run_step(run, tmp_path, workspace, {"SUPPORT_SCRIPTS_DIR": str(staged)})
	assert result.returncode == 0, (step_name, result.stdout, result.stderr)
	assert marker.read_text() == "sourced", f"{step_name}: staged support bundle must be sourced"
	assert "Refusing unverified" not in result.stdout


def _infra_round_env() -> dict[str, str]:
	return {"PR_NUMBER": "1", "GITHUB_RUN_ID": "1", "AUTOFIX_FAILURE_HEAD_SHA": "c" * 40}


def _findings_record_env(tmp_path: Path) -> dict[str, str]:
	reviews = tmp_path / "previous-reviews"
	reviews.mkdir(exist_ok=True)
	return {"PREVIOUS_REVIEWS_DIR": str(reviews)}


@pytest.mark.parametrize(
	("step_name", "script_name"),
	(
		("Classify infra-failed review round", "review_sweep_round_cap.sh"),
		("Write review findings record", "review_findings_record.py"),
	),
)
def test_non_wrapper_fallbacks_refuse_unverified_workspace_copy(step_name: str, script_name: str, tmp_path: Path) -> None:
	run = _step_run(step_name)
	assert f'.codex-workflow-src/scripts/{script_name}"' in run
	env = _infra_round_env() if script_name.endswith(".sh") else _findings_record_env(tmp_path)
	workspace = tmp_path / "ws"
	workspace.mkdir()
	marker = tmp_path / "marker"
	_plant(workspace, script_name, marker)

	result = _run_step(run, tmp_path, workspace, env)
	assert not marker.exists(), f"{step_name}: ran a PR-tree helper with no verified support checkout"
	assert result.returncode == 0, (step_name, result.stdout, result.stderr)
	assert "Refusing unverified .codex-workflow-src fallback" in result.stdout

	head = _commit_support_checkout(workspace)
	result = _run_step(run, tmp_path, workspace, {**env, "SCRIPT_REF": head, SENTINEL: head, **_activated_git_env(tmp_path)})
	assert result.returncode == 0, (step_name, result.stdout, result.stderr)
	assert marker.read_text() == "sourced", f"{step_name}: verified fallback must still be used"


def test_every_codex_agent_workspace_fallback_is_verified() -> None:
	# The optional middle group admits the #10221-gated wrapper shape
	# ("[ -f ... ] || <flag test> || <HEAD test> || VAR=...").
	fallback_re = re.compile(
		r'^ *\[ -f "\$\{(?P<var>[A-Za-z_]+)\}" \] \|\| (?:.* \|\| )?(?P=var)="\$\{GITHUB_WORKSPACE\}/\.codex-workflow-src/scripts/[^"]+"\n(?P<next>.*)$',
		re.MULTILINE,
	)
	sites = list(fallback_re.finditer(_CODEX_AGENT_TEXT))
	assert len(sites) >= len(DEFAULT_WRAPPER_STEPS) + 2
	for site in sites:
		var = site.group("var")
		nxt = site.group("next").strip()
		assert nxt.startswith(f'case "${{{var}}}" in "${{GITHUB_WORKSPACE}}/.codex-workflow-src/"*) '), f"{var}: fallback not followed by the verify line"
		assert f'[ "${{{SENTINEL}:-}}" = "${{SCRIPT_REF}}" ]' in nxt
		assert '[ "$(env -u GIT_DIR -u GIT_WORK_TREE git -C "${GITHUB_WORKSPACE}/.codex-workflow-src" rev-parse HEAD 2>/dev/null)" = "${SCRIPT_REF}" ]' in nxt
		assert f'{var}=""; }} ;; esac' in nxt


def test_support_verify_step_sets_sentinel_only_after_sha_check(tmp_path: Path) -> None:
	run = _step_run("Ensure workflow support source checkout")
	workspace = tmp_path / "ws"
	workspace.mkdir()
	head = _commit_support_checkout(workspace)
	github_env = tmp_path / "github-env"
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": "d" * 40})
	assert result.returncode == 1
	assert not github_env.exists() or SENTINEL not in github_env.read_text()
	result = _run_step(run, tmp_path, workspace, {"SCRIPT_REF": head})
	assert result.returncode == 0, (result.stdout, result.stderr)
	assert f"{SENTINEL}={head}\n" in github_env.read_text()
	writers = re.findall(rf"^      - name: (.+)\n(?:(?!      - ).*\n)*?.*{SENTINEL}=", _CODEX_AGENT_TEXT, re.MULTILINE)
	assert writers == ["Ensure workflow support source checkout"], writers


def test_pr_head_checkout_does_not_persist_credentials() -> None:
	block = _step_block("Checkout repo")
	assert "        uses: actions/checkout@" in block
	assert "          token: ${{ secrets.GH_PAT }}\n" in block
	assert re.search(r"^          persist-credentials: false$", block, re.MULTILINE), (
		"PR-head checkout must not leave GH_PAT in .git/config (#10208)"
	)
