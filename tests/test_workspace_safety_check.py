#!/usr/bin/env python3
"""Contract tests for the W3 workspace safety guard and its launch wiring."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_SAFETY_CHECK = REPO_ROOT / "scripts" / "workspace_safety_check.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
REVIEW_CONFLICT_RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _run_workspace_safety_check(*, cwd: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env.update(env_overrides)
	return subprocess.run(
		["bash", str(WORKSPACE_SAFETY_CHECK)],
		cwd=cwd,
		env=env,
		capture_output=True,
		text=True,
	)


def test_workspace_safety_check_rejects_workspace_escape() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp_path = Path(tmpdir)
		workspace_dir = tmp_path / "workspaces" / "issue-123"
		workspace_dir.mkdir(parents=True)
		result = _run_workspace_safety_check(
			cwd=workspace_dir,
			env_overrides={
				"WORKSPACE_REUSE_ENABLED": "true",
				"WORKSPACE_PATH": "/tmp/escape",
				"WORKSPACE_KEY": "issue-123",
				"RUNNER_TEMP": tmpdir,
			},
		)
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_workspace_safety_check_rejects_invalid_workspace_key() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp_path = Path(tmpdir)
		workspace_dir = tmp_path / "workspaces" / "issue-123"
		workspace_dir.mkdir(parents=True)
		result = _run_workspace_safety_check(
			cwd=workspace_dir,
			env_overrides={
				"WORKSPACE_REUSE_ENABLED": "true",
				"WORKSPACE_PATH": str(workspace_dir),
				"WORKSPACE_KEY": "issue/123",
				"RUNNER_TEMP": tmpdir,
			},
		)
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_workspace_safety_check_rejects_pwd_mismatch() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp_path = Path(tmpdir)
		workspace_dir = tmp_path / "workspaces" / "issue-123"
		workspace_dir.mkdir(parents=True)
		result = _run_workspace_safety_check(
			cwd=tmp_path,
			env_overrides={
				"WORKSPACE_REUSE_ENABLED": "true",
				"WORKSPACE_PATH": str(workspace_dir),
				"WORKSPACE_KEY": "issue-123",
				"RUNNER_TEMP": tmpdir,
			},
		)
		assert result.returncode == 78
		assert "workspace_safety_violation" in result.stderr


def test_workspace_safety_check_is_noop_when_reuse_disabled() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		result = _run_workspace_safety_check(
			cwd=Path(tmpdir),
			env_overrides={
				"WORKSPACE_REUSE_ENABLED": "false",
				"WORKSPACE_PATH": "/tmp/escape",
				"WORKSPACE_KEY": "issue/123",
			},
		)
		assert result.returncode == 0
		assert result.stdout == ""
		assert result.stderr == ""


def test_workspace_safety_wiring_references_helper_in_all_scoped_launchers() -> None:
	implement_workflow = _read(IMPLEMENT_WORKFLOW)
	review_workflow = _read(REVIEW_WORKFLOW)
	validate_process = _read(VALIDATE_PROCESS)
	review_apply_fixes = _read(REVIEW_APPLY_FIXES)
	review_conflict_resolve = _read(REVIEW_CONFLICT_RESOLVE)

	assert "workspace_safety_check.sh" in implement_workflow
	assert implement_workflow.count("bash scripts/workspace_safety_check.sh") == 3
	assert "workspace_safety_check.sh" in review_workflow
	assert 'check_required_file "${SUPPORT_SCRIPTS_DIR}/workspace_safety_check.sh"' in review_workflow
	assert 'WORKSPACE_SAFETY_CHECK_HELPER="${_validate_script_dir}/workspace_safety_check.sh"' in validate_process
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}"' in validate_process
	assert 'WORKSPACE_SAFETY_CHECK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_safety_check.sh"' in review_apply_fixes
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}"' in review_apply_fixes
	assert 'WORKSPACE_SAFETY_CHECK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_safety_check.sh"' in review_conflict_resolve
	assert 'bash "${WORKSPACE_SAFETY_CHECK_HELPER}"' in review_conflict_resolve


def main() -> int:
	test_workspace_safety_check_rejects_workspace_escape()
	test_workspace_safety_check_rejects_invalid_workspace_key()
	test_workspace_safety_check_rejects_pwd_mismatch()
	test_workspace_safety_check_is_noop_when_reuse_disabled()
	test_workspace_safety_wiring_references_helper_in_all_scoped_launchers()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
