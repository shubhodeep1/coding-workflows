#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "workspace_safety_check.sh"
REVIEW_STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEST_AND_MARK_STABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
REVIEW_CONFLICT_RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _workflow_doc(path: Path) -> dict[str, object]:
	doc = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(doc, dict):
		raise AssertionError(f"Workflow did not parse into a mapping: {path}")
	return doc


def _step(path: Path, step_name: str) -> dict[str, object]:
	jobs = _workflow_doc(path).get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"Workflow jobs mapping missing in {path}")
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if not isinstance(steps, list):
			continue
		for step in steps:
			if isinstance(step, dict) and str(step.get("name", "")).strip() == step_name:
				return step
	raise AssertionError(f"Step not found in {path}: {step_name}")


def _step_run_text(path: Path, step_name: str) -> str:
	run = _step(path, step_name).get("run")
	if not isinstance(run, str):
		raise AssertionError(f"Step does not define a run block: {step_name}")
	return run


def _run_helper(tmp_path: Path, cwd: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
	workspace_shell_env = tmp_path / "workspace-shell.env"
	workspace_shell_env.write_text(
		'if [ -n "${WORKSPACE_PATH:-}" ] && [ -d "${WORKSPACE_PATH}" ]; then\n'
		'  cd "${WORKSPACE_PATH}"\n'
		'fi\n',
		encoding="utf-8",
	)
	env = os.environ.copy()
	env.update(
		{
			"BASH_ENV": str(workspace_shell_env),
			"RUNNER_TEMP": str(tmp_path / "runner-temp"),
			"WORKSPACE_REUSE_ENABLED": "true",
			"WORKSPACE_KEY": "issue-42",
			"WORKSPACE_PATH": str(tmp_path / "runner-temp" / "workspaces" / "issue-42"),
		}
	)
	env.update(env_overrides)
	return subprocess.run(
		["bash", str(HELPER)],
		cwd=cwd,
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_helper_script_exists_and_is_executable() -> None:
	assert HELPER.exists()
	assert HELPER.stat().st_mode & 0o111


def test_helper_is_noop_when_workspace_reuse_is_disabled() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		result = _run_helper(
			tmp_path,
			REPO_ROOT,
			WORKSPACE_REUSE_ENABLED="false",
			WORKSPACE_KEY="bad/key",
			WORKSPACE_PATH=str(tmp_path / "escape"),
		)
		assert result.returncode == 0, result.stderr


def test_helper_accepts_valid_reused_workspace() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		workspace_path = tmp_path / "runner-temp" / "workspaces" / "issue-42"
		workspace_path.mkdir(parents=True)
		result = _run_helper(tmp_path, workspace_path)
		assert result.returncode == 0, result.stderr


def test_helper_accepts_launch_from_shared_workspaces_root() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		workspace_root = tmp_path / "runner-temp" / "workspaces"
		workspace_path = workspace_root / "issue-42"
		workspace_path.mkdir(parents=True)
		result = _run_helper(tmp_path, workspace_root)
		assert result.returncode == 0, result.stderr


def test_helper_rejects_workspace_root_escape() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		escape_path = tmp_path / "escape"
		escape_path.mkdir()
		result = _run_helper(tmp_path, escape_path, WORKSPACE_PATH=str(escape_path))
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_helper_rejects_workspace_root_itself() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		root_path = tmp_path / "runner-temp" / "workspaces"
		root_path.mkdir(parents=True)
		result = _run_helper(tmp_path, root_path, WORKSPACE_PATH=str(root_path))
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_helper_rejects_invalid_workspace_key() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		workspace_path = tmp_path / "runner-temp" / "workspaces" / "issue-42"
		workspace_path.mkdir(parents=True)
		result = _run_helper(tmp_path, workspace_path, WORKSPACE_KEY="bad/key")
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_helper_rejects_pwd_mismatch() -> None:
	with tempfile.TemporaryDirectory(prefix="workspace-safety-check-") as td:
		tmp_path = Path(td)
		workspace_path = tmp_path / "runner-temp" / "workspaces" / "issue-42"
		other_path = tmp_path / "runner-temp" / "workspaces" / "issue-99"
		workspace_path.mkdir(parents=True)
		other_path.mkdir(parents=True)
		result = _run_helper(tmp_path, other_path, WORKSPACE_PATH=str(workspace_path))
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_implement_workflow_stages_and_guards_all_codex_launches() -> None:
	stage_block = _step_run_text(IMPLEMENT_WORKFLOW, "Stage workflow support files")
	implement_block = _step_run_text(IMPLEMENT_WORKFLOW, "Run Codex implementation")
	repair_block = _step_run_text(IMPLEMENT_WORKFLOW, "Attempt post-Codex syntax repair")
	summary_block = _step_run_text(IMPLEMENT_WORKFLOW, "Generate AI issue summary for PR comment")

	assert "workspace_safety_check.sh" in stage_block
	assert "bash scripts/workspace_safety_check.sh" in implement_block
	assert "bash scripts/workspace_safety_check.sh" in repair_block
	assert "bash scripts/workspace_safety_check.sh" in summary_block


def test_ci_and_release_gate_run_workspace_safety_check_tests() -> None:
	ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
	release_text = TEST_AND_MARK_STABLE_WORKFLOW.read_text(encoding="utf-8")
	assert "tests/test_workspace_safety_check.py" in ci_text
	assert "tests/test_workspace_safety_check.py" in release_text


def test_review_workflow_bootstraps_and_restages_workspace_safety_helper() -> None:
	stage_block = REVIEW_STAGE_HELPER.read_text(encoding="utf-8")
	merge_detect_block = _step_run_text(REVIEW_WORKFLOW, "Detect merge conflicts")

	assert "workspace_safety_check.sh" in stage_block
	assert "workspace_safety_check.sh" in merge_detect_block


def test_validate_process_guards_codex_attempts_and_short_circuits_exit_78() -> None:
	text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	assert 'WORKSPACE_SAFETY_CHECK_HELPER=""' in text
	assert '".codex-workflow-src/scripts/workspace_safety_check.sh"' in text
	assert '".codex-workflow-src-main/scripts/workspace_safety_check.sh"' in text
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}" || return $?' in text
	assert 'local exit_code="${5:-1}"' in text
	assert 'exit "${exit_code}"' in text
	assert '[ "${DISCOVER_EXIT}" -eq 78 ]' in text
	assert '[ "${DIAGNOSE_EXIT}" -eq 78 ]' in text
	assert re.search(
		r'Workspace safety preflight failed before validation hint discovery could launch Codex\." \\\n\s+78',
		text,
	)
	assert re.search(
		r'Workspace safety preflight failed before validation diagnosis could launch Codex\." \\\n\s+78',
		text,
	)
	assert 'workspace_safety_violation' in text


def test_review_apply_fixes_guards_editor_and_aborts_on_exit_78() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert 'WORKSPACE_SAFETY_CHECK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_safety_check.sh"' in text
	assert 'if [ -x "${WORKSPACE_SAFETY_CHECK_HELPER}" ]; then' in text
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}"' in text
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}" || return $?' in text
	assert '[ "${cmd_rc}" -eq 78 ]' in text
	assert 'workspace_safety_violation; aborting without retry.' in text


def test_review_conflict_resolve_guards_resolver_and_aborts_on_exit_78() -> None:
	text = REVIEW_CONFLICT_RESOLVE.read_text(encoding="utf-8")
	assert 'WORKSPACE_SAFETY_CHECK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_safety_check.sh"' in text
	assert 'if [ -x "${WORKSPACE_SAFETY_CHECK_HELPER}" ]; then' in text
	assert 'if ! bash "${WORKSPACE_SAFETY_CHECK_HELPER}"; then' in text
	assert '[ "${_codex_exit}" -eq 78 ]' in text
	assert 'workspace_safety_violation.' in text


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	passed = 0
	for func in test_functions:
		func()
		passed += 1
	print(f"OK: {passed} workspace safety checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
