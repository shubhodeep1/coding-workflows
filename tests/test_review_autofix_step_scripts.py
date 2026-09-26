"""Contracts for extracted review/autofix workflow steps."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/review_autofix.yml"
STAGER = ROOT / "scripts/stage_workflow_support.sh"
SCRIPTS = {
	"Pre-review deterministic merge-topology gate": "review_pre_review_merge_topology.sh",
	"Detect merge conflicts": "review_detect_merge_conflicts.sh",
	"Append review pipeline iteration summary": "review_append_iteration_summary.sh",
}


def _step(name: str) -> dict[str, object]:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	return next(step for step in workflow["jobs"]["codex-agent"]["steps"] if step.get("name") == name)


def test_extracted_steps_are_staged_and_keep_workflow_contracts() -> None:
	workflow_text = WORKFLOW.read_text(encoding="utf-8")
	stager_text = STAGER.read_text(encoding="utf-8")
	assert WORKFLOW.stat().st_size < 480000
	assert "REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS" in workflow_text
	for name, filename in SCRIPTS.items():
		script = ROOT / "scripts" / filename
		assert script.stat().st_mode & stat.S_IXUSR
		assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n# Extracted from review_autofix.yml:")
		assert "${{" not in script.read_text(encoding="utf-8")
		assert filename in stager_text.split("REQUIRED_BOOTSTRAP_SCRIPTS=", 1)[1].split("\n", 1)[0]
		assert filename in workflow_text.split("REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS:", 1)[1].split("REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS:", 1)[0]
		step = _step(name)
		assert filename in step["run"]

	pre_review = _step("Pre-review deterministic merge-topology gate")
	assert pre_review["env"]["LOCAL_HEAD_SHA"] == "${{ env.INITIAL_HEAD_SHA }}"
	assert pre_review["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert "AUTOFIX_RESUME_TERMINAL != 'true'" in pre_review["if"]
	assert "success()" in _step("Detect merge conflicts")["if"]
	summary = _step("Append review pipeline iteration summary")
	assert summary["if"] == "always() && env.RUNTIME_DIR != '' && env.CLAUDE_BRANCH_REVIEW_MODE != 'true'"
	assert summary["continue-on-error"] is True
	assert summary["env"]["EDITOR_COMMIT_PRODUCED"] == "${{ steps.commit_changes.outputs.did_commit }}"
	assert summary["env"]["AUTOFIX_ITERATION_METRIC"] == "${{ steps.retrigger_guard.outputs.autofix_iteration }}"


def test_summary_runs_from_checkout_when_staging_was_incomplete(tmp_path: Path) -> None:
	step = _step("Append review pipeline iteration summary")
	source_dir = tmp_path / ".codex-workflow-src-main/scripts"
	source_dir.mkdir(parents=True)
	shutil.copy2(ROOT / "scripts/review_append_iteration_summary.sh", source_dir)
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	summary = tmp_path / "summary.md"
	env = os.environ.copy()
	env.update({
		"SUPPORT_SCRIPTS_DIR": str(tmp_path / "missing-support"),
		"GITHUB_WORKSPACE": str(tmp_path),
		"RUNTIME_DIR": str(runtime),
		"GITHUB_STEP_SUMMARY": str(summary),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.pop("BASH_ENV", None)
	env.pop("ENV", None)
	result = subprocess.run(
		["bash", "-euo", "pipefail", "-c", step["run"]],
		cwd=tmp_path,
		env=env,
		check=True,
		capture_output=True,
		text=True,
	)
	assert "REVIEW_AUTOFIX_RUN_SUMMARY_V1 " in result.stdout
	assert "### Review Pipeline" in summary.read_text(encoding="utf-8")
	shutil.rmtree(tmp_path / ".codex-workflow-src-main")
	result = subprocess.run(
		["bash", "-euo", "pipefail", "-c", step["run"]],
		cwd=tmp_path,
		env=env,
		check=True,
		capture_output=True,
		text=True,
	)
	assert "::warning::review_append_iteration_summary.sh unavailable" in result.stdout
