"""Q64: consumer wrappers pin each reusable workflow to a commit
(`<file>@<sha> # stable`). The "Resolve workflow support ref" steps of the
workflows below stage their support scripts from that same commit instead of
a newer `stable`; validate.yml does the same in its own step (#7123). Every
such step is run here for a pin, a tag, look-alike identities and this
repository.

tele-funtoken-msg-scoring#4566: a pinned run must never fall back to `main`
or `stable` support code. The fallback and main-snapshot checkouts run only
for an unpinned `stable` ref, the checkout HEAD is verified, the
integration-ref resolver follows the pin and fails closed, and shared actions
are referenced by commit."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
	"plan.yml",
	"clarify.yml",
	"implement.yml",
	"orchestrate_clarify_respond.yml",
	"orchestrate.yml",
	"orchestrate_poll.yml",
	"check_failure_triage.yml",
	"cancel_on_pr_close.yml",
	"sync_ai_labels.yml",
	"workflow_failure_heal.yml",
)
RESOLVER_WORKFLOWS = ("plan.yml", "clarify.yml", "implement.yml", "orchestrate_clarify_respond.yml", "validate.yml")
SUPPORT_REPO = "shubhodeep1/coding-workflows"
PIN = "de7fef2fce27d8e45ff8451eeba7fb980f13ee66"


def _resolve_steps(workflow: str) -> list[dict]:
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	return [
		step
		for job in data["jobs"].values()
		for step in job.get("steps", [])
		if str(step.get("name", "")).startswith("Resolve workflow support ref")
	]


def _run(step: dict, repository: str, workflow_ref: str, tmp_path: Path) -> dict[str, str]:
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	script = (
		step["run"]
		.replace("${{ github.repository }}", repository)
		.replace("${{ github.sha }}", "1" * 40)
	)
	assert "${{" not in script
	env = {
		**os.environ,
		"GITHUB_ENV": str(env_file),
		"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref}),
	}
	subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
	return dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if "=" in line)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_support_ref_follows_the_consumer_pin(workflow: str, tmp_path: Path) -> None:
	steps = _resolve_steps(workflow)
	assert steps, workflow
	reusable = f"shubhodeep1/coding-workflows/.github/workflows/{workflow}"
	other = "validate.yml" if workflow != "validate.yml" else "plan.yml"
	for step in steps:
		assert step["env"]["SUPPORT_REF_WORKFLOW_JOB_JSON"] == "${{ toJSON(job) }}"
		cases = (
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN}", PIN),
			("shubhodeep1/binance-blessings", f"{reusable}@refs/tags/stable", "stable"),
			("shubhodeep1/binance-blessings", "", "stable"),
			("shubhodeep1/binance-blessings", f"evil/coding-workflows/.github/workflows/{workflow}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"shubhodeep1/coding-workflows/.github/workflows/{other}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN[:-1]}", "stable"),
			("shubhodeep1/coding-workflows", f"{reusable}@{PIN}", "1" * 40),
		)
		for repository, workflow_ref, expected in cases:
			values = _run(step, repository, workflow_ref, tmp_path)
			assert len(values) == 1, (step["name"], values)
			assert list(values.values()) == [expected], (workflow, step["name"], repository, workflow_ref, values)


def _all_steps(workflow: str) -> list[dict]:
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	return [step for job in data["jobs"].values() for step in job.get("steps", [])]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_pinned_runs_never_check_out_support_from_main(workflow: str) -> None:
	main_checkouts = 0
	for step in _all_steps(workflow):
		with_ = step.get("with") or {}
		if not str(step.get("uses", "")).startswith("actions/checkout@"):
			continue
		if with_.get("repository") != SUPPORT_REPO or with_.get("ref") != "main":
			continue
		main_checkouts += 1
		condition = str(step.get("if", ""))
		assert re.search(r"env\.(DUPLICATE_NOTICE_)?SCRIPT_REF == 'stable'", condition), (workflow, step["name"], condition)
	assert main_checkouts, f"{workflow} has no main support checkout left to guard; drop it from WORKFLOWS"


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_pinned_support_checkout_head_is_verified(workflow: str, tmp_path: Path) -> None:
	steps = [
		step
		for step in _all_steps(workflow)
		if step.get("name") in ("Ensure workflow support source checkout", "Verify pinned workflow support checkout")
	]
	assert steps, workflow
	support = tmp_path / ".codex-workflow-src"
	support.mkdir()
	_git(support, "init", "-q")
	(support / "f").write_text("x", encoding="utf-8")
	_git(support, "add", "f")
	_git(support, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "c")
	head = _git(support, "rev-parse", "HEAD")
	for step in steps:
		script = step["run"]
		assert "${{" not in script, (workflow, step["name"])
		for script_ref, ok in ((head, True), ("2" * 40, False), ("stable", True)):
			result = subprocess.run(
				["bash", "-c", script],
				cwd=tmp_path,
				env={**os.environ, "SCRIPT_REF": script_ref, "GIT_CONFIG_GLOBAL": os.devnull},
				capture_output=True,
				text=True,
			)
			assert (result.returncode == 0) == ok, (workflow, step["name"], script_ref, result.stdout, result.stderr)
			if not ok:
				assert "does not match the pinned commit" in result.stdout + result.stderr


def _resolver_step(workflow: str) -> dict:
	steps = [step for step in _all_steps(workflow) if "resolver_ref_pinned=false" in str(step.get("run", ""))]
	assert len(steps) == 1, workflow
	return steps[0]


def _run_resolver(step: dict, workflow_ref: str, routing: str, tmp_path: Path) -> subprocess.CompletedProcess:
	# A git that always fails: staging the resolver can never succeed.
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir(exist_ok=True)
	fake_git = fake_bin / "git"
	fake_git.write_text("#!/bin/sh\necho 'fatal: simulated clone failure' >&2\nexit 128\n", encoding="utf-8")
	fake_git.chmod(0o755)
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	script = step["run"].replace("${{ github.repository }}", "shubhodeep1/binance-blessings").replace("${{ github.sha }}", "1" * 40)
	assert "${{" not in script
	env = {
		**os.environ,
		"PATH": f"{fake_bin}:{os.environ['PATH']}",
		"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref}),
		"CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED": routing,
		"GH_TOKEN": "test-token",
		"ISSUE_NUMBER": "5",
		"TRACKING_ISSUE": "5",
		"GITHUB_OUTPUT": str(output),
		"RUNNER_TEMP": str(tmp_path),
		"GITHUB_RUN_ID": "1",
		"GITHUB_RUN_ATTEMPT": "1",
	}
	return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("workflow", RESOLVER_WORKFLOWS)
def test_integration_resolver_follows_the_pin_and_fails_closed(workflow: str, tmp_path: Path) -> None:
	step = _resolver_step(workflow)
	assert step["env"]["SUPPORT_REF_WORKFLOW_JOB_JSON"] == "${{ toJSON(job) }}"
	reusable = f"{SUPPORT_REPO}/.github/workflows/{workflow}"
	(tmp_path / "pinned").mkdir()
	(tmp_path / "unpinned").mkdir()
	pinned = _run_resolver(step, f"{reusable}@{PIN}", "false", tmp_path / "pinned")
	assert pinned.returncode != 0, (pinned.stdout, pinned.stderr)
	assert f"pinned_ref={PIN}" in pinned.stdout + pinned.stderr
	unpinned = _run_resolver(step, f"{reusable}@refs/tags/stable", "false", tmp_path / "unpinned")
	assert unpinned.returncode == 0, (unpinned.stdout, unpinned.stderr)
	assert "pinned_ref=" not in unpinned.stdout + unpinned.stderr
	assert "ref=" in (tmp_path / "unpinned" / "github_output").read_text(encoding="utf-8")


def test_shared_actions_are_referenced_by_commit() -> None:
	# validate.yml's setup-runtime moves to the commit carrying the pinned
	# install-codex reference in a follow-up; nothing else may use a moving ref.
	allowed = {("validate.yml", "setup-runtime@stable")}
	paths = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
	offenders = []
	for path in paths:
		for match in re.finditer(r"uses:\s*shubhodeep1/coding-workflows/\.github/actions/([\w.-]+@[\w./-]+)", path.read_text(encoding="utf-8")):
			ref = match.group(1)
			if re.fullmatch(r"[\w.-]+@[0-9a-f]{40}", ref):
				continue
			if (path.name, ref) in allowed:
				continue
			offenders.append(f"{path.relative_to(ROOT)}: {ref}")
	assert not offenders, offenders
