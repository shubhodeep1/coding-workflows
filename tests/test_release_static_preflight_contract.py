"""Contract tests for the release gate's workflow script-reference preflight.

Source: analysis/workflow-optimization-2026-09-23-2.md, Speed #2. Release run
35802596362 ran its live E2E jobs for ~98 minutes after validate-scripts had
already failed on a missing scripts/ reference. The resolve-version job of
test-and-mark-stable.yml now runs scripts/check_workflow_script_refs.py, and
every live job needs resolve-version, so a missing reference stops them first.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
CHECKER = REPO_ROOT / "scripts" / "check_workflow_script_refs.py"
LIVE_JOBS = (
	"e2e-smoke-test",
	"workflow-log-analysis-test",
	"orphan-workflows-test",
	"orchestrate-decompose-test",
	"validate-standalone-test",
	"clarify-rejects-unsolvable-test",
	"e2e-alt-model-test",
)

pytestmark = pytest.mark.skipif(
	shutil.which("bash") is None or shutil.which("python3") is None,
	reason="bash and python3 are required",
)


def _load_workflow() -> dict:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
	return _load_workflow()["jobs"]


def _resolve_version_steps() -> list[dict]:
	return _jobs()["resolve-version"]["steps"]


def _preflight_step() -> dict:
	for step in _resolve_version_steps():
		if step.get("id") == "script_ref_preflight":
			return step
	raise AssertionError("resolve-version has no script_ref_preflight step")


def _run_body(cwd: pathlib.Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": os.environ.get("HOME", "/tmp"),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	if env_overrides:
		env.update(env_overrides)
	return subprocess.run(
		["bash", "-c", _preflight_step()["run"]],
		cwd=str(cwd),
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)


def _fake_layout(tmp_path: pathlib.Path, *, create_target: bool) -> pathlib.Path:
	root = tmp_path / "repo"
	(root / "scripts").mkdir(parents=True)
	(root / ".github" / "workflows").mkdir(parents=True)
	shutil.copy2(CHECKER, root / "scripts" / "check_workflow_script_refs.py")
	(root / "scripts" / "stage_workflow_support.sh").write_text("", encoding="utf-8")
	(root / ".github" / "workflows" / "fake.yml").write_text(
		"name: fake\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
		"    steps:\n      - run: bash scripts/does_not_exist.sh\n",
		encoding="utf-8",
	)
	if create_target:
		(root / "scripts" / "does_not_exist.sh").write_text("#!/bin/sh\n", encoding="utf-8")
	return root


def test_step_present_before_resolve_version_tag() -> None:
	steps = _resolve_version_steps()
	names = [s.get("name") for s in steps]
	ids = [s.get("id") for s in steps]
	checkout = names.index("Checkout repository")
	preflight = ids.index("script_ref_preflight")
	resolve = ids.index("resolve")
	assert checkout < preflight < resolve
	step = steps[preflight]
	assert step["name"] == "Preflight workflow script references"
	assert step["env"]["RELEASE_STATIC_PREFLIGHT_ENABLED"] == (
		"${{ vars.RELEASE_STATIC_PREFLIGHT_ENABLED || 'true' }}"
	)


def test_run_body_has_no_expressions() -> None:
	assert "${{" not in _preflight_step()["run"]


def test_live_jobs_depend_on_resolve_version() -> None:
	jobs = _jobs()
	present = [name for name in LIVE_JOBS if name in jobs]
	assert present, "no live jobs found"
	for name in present:
		needs = jobs[name].get("needs")
		if isinstance(needs, str):
			needs = [needs]
		assert "resolve-version" in (needs or []), name


def test_validate_scripts_keeps_redundant_check() -> None:
	steps = _jobs()["validate-scripts"]["steps"]
	assert any("check_workflow_script_refs.py" in str(s.get("run", "")) for s in steps)


def test_passes_on_repo_checkout() -> None:
	result = _run_body(REPO_ROOT)
	assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["false", "FALSE"])
def test_switch_false_skips(tmp_path: pathlib.Path, value: str) -> None:
	root = _fake_layout(tmp_path, create_target=False)
	result = _run_body(root, {"RELEASE_STATIC_PREFLIGHT_ENABLED": value})
	assert result.returncode == 0, result.stderr
	assert "::notice::" in result.stdout


def test_invalid_switch_skips_without_echoing_value(tmp_path: pathlib.Path) -> None:
	root = _fake_layout(tmp_path, create_target=False)
	result = _run_body(root, {"RELEASE_STATIC_PREFLIGHT_ENABLED": "bogus-xyz"})
	assert result.returncode == 0, result.stderr
	assert "::warning::" in result.stdout
	assert "bogus-xyz" not in result.stdout
	assert "bogus-xyz" not in result.stderr


@pytest.mark.parametrize("value", [None, "", "true", "TRUE"])
def test_missing_reference_fails(tmp_path: pathlib.Path, value: str | None) -> None:
	root = _fake_layout(tmp_path, create_target=False)
	overrides = {} if value is None else {"RELEASE_STATIC_PREFLIGHT_ENABLED": value}
	result = _run_body(root, overrides)
	assert result.returncode != 0
	assert "does_not_exist.sh" in result.stderr


def test_resolving_reference_passes_in_temp_layout(tmp_path: pathlib.Path) -> None:
	root = _fake_layout(tmp_path, create_target=True)
	result = _run_body(root)
	assert result.returncode == 0, result.stderr


def test_missing_or_symlinked_checker_skips(tmp_path: pathlib.Path) -> None:
	missing = _fake_layout(tmp_path / "missing", create_target=False)
	(missing / "scripts" / "check_workflow_script_refs.py").unlink()
	result = _run_body(missing)
	assert result.returncode == 0, result.stderr
	assert "::warning::" in result.stdout

	linked = _fake_layout(tmp_path / "linked", create_target=False)
	checker = linked / "scripts" / "check_workflow_script_refs.py"
	checker.unlink()
	checker.symlink_to(CHECKER)
	result = _run_body(linked)
	assert result.returncode == 0, result.stderr
	assert "::warning::" in result.stdout
