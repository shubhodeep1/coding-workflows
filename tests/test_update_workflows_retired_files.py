#!/usr/bin/env python3
"""Tests for the "Remove retired upstream files" step of update_workflows.yml.

The step deletes a consumer file listed in workflow-templates/retired_files.txt
only when its sha256 matches a released version; modified copies stay.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
MANIFEST = REPO_ROOT / "workflow-templates" / "retired_files.txt"
STEP_NAME = "Remove retired upstream files"


def _steps() -> list[dict]:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["update-wrappers"]["steps"]


def _step() -> dict:
	matches = [step for step in _steps() if step.get("name") == STEP_NAME]
	assert len(matches) == 1
	return matches[0]


def _sha(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _run(tmp_path: Path, manifest_text: str | None) -> tuple[subprocess.CompletedProcess, dict[str, str], list[str]]:
	upstream = tmp_path / "upstream"
	upstream.mkdir()
	if manifest_text is not None:
		(upstream / "retired_files.txt").write_text(manifest_text, encoding="utf-8")
	script = _step()["run"].replace("${{ steps.fetch.outputs.upstream_dir }}", str(upstream))
	output_file = tmp_path / "github_output"
	output_file.write_text("", encoding="utf-8")
	removed_file = Path("/tmp/retired_removed_files.txt")
	env = {**os.environ, "GITHUB_OUTPUT": str(output_file)}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path / "consumer", env=env, capture_output=True, text=True)
	outputs: dict[str, str] = {}
	for line in output_file.read_text(encoding="utf-8").splitlines():
		key, _, value = line.partition("=")
		outputs[key] = value
	removed = [line for line in removed_file.read_text(encoding="utf-8").splitlines() if line] if removed_file.exists() else []
	return result, outputs, removed


def _consumer(tmp_path: Path, files: dict[str, bytes]) -> Path:
	root = tmp_path / "consumer"
	for rel, data in files.items():
		path = root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data)
	root.mkdir(exist_ok=True)
	return root


def test_identical_copy_is_removed_modified_copy_is_kept(tmp_path: Path) -> None:
	released = b"released version\n"
	root = _consumer(
		tmp_path,
		{
			".claude/scripts/old.py": released,
			".claude/scripts/edited.py": b"consumer edit\n",
			".claude/scripts/kept.py": b"not retired\n",
		},
	)
	manifest = (
		"# comment line\n"
		"\n"
		f".claude/scripts/old.py {_sha(b'older version')} {_sha(released)}\n"
		f".claude/scripts/edited.py {_sha(released)}\n"
		f".claude/scripts/absent.py {_sha(released)}\n"
	)
	result, outputs, removed = _run(tmp_path, manifest)
	assert result.returncode == 0, result.stderr
	assert not (root / ".claude/scripts/old.py").exists()
	assert (root / ".claude/scripts/edited.py").exists()
	assert (root / ".claude/scripts/kept.py").exists()
	assert removed == [".claude/scripts/old.py"]
	assert outputs["retired_removed"] == "1"
	assert outputs["retired_has_changes"] == "true"
	assert "RETIRED_FILE_REMOVED path=.claude/scripts/old.py" in result.stdout
	assert "RETIRED_FILE_KEPT_MODIFIED path=.claude/scripts/edited.py" in result.stdout


def test_missing_manifest_is_a_noop(tmp_path: Path) -> None:
	root = _consumer(tmp_path, {".claude/scripts/old.py": b"x\n"})
	result, outputs, removed = _run(tmp_path, None)
	assert result.returncode == 0, result.stderr
	assert (root / ".claude/scripts/old.py").exists()
	assert outputs["retired_removed"] == "0"
	assert outputs["retired_has_changes"] == "false"
	assert removed == []


def test_unsafe_paths_and_symlinks_are_never_removed(tmp_path: Path) -> None:
	data = b"data\n"
	root = _consumer(tmp_path, {"target.txt": data})
	(root / "link.txt").symlink_to(root / "target.txt")
	outside = tmp_path / "outside.txt"
	outside.write_bytes(data)
	manifest = (
		f"../outside.txt {_sha(data)}\n"
		f"{outside} {_sha(data)}\n"
		f"link.txt {_sha(data)}\n"
	)
	result, outputs, removed = _run(tmp_path, manifest)
	assert result.returncode == 0, result.stderr
	assert outside.exists()
	assert (root / "link.txt").is_symlink()
	assert removed == []
	assert "reason=unsafe_path" in result.stdout


def test_repo_manifest_lists_the_retired_claude_assets() -> None:
	entries = {}
	for line in MANIFEST.read_text(encoding="utf-8").splitlines():
		if not line or line.startswith("#"):
			continue
		path, *hashes = line.split()
		assert hashes, path
		assert all(len(value) == 64 and int(value, 16) >= 0 for value in hashes), path
		entries[path] = hashes
	for path in (
		".claude/commands/claude-issue-dispatch.md",
		".claude/commands/fix-claude-pr.md",
		".claude/scripts/check_in_status.py",
		".claude/scripts/claude_fix_claim.py",
		".claude/scripts/stale_routines.py",
		".claude/scripts/permission_prompts.py",
		".claude/scripts/dispatch_workflow.py",
		".claude/scripts/edit_comment.py",
		".claude/scripts/security_pass_skip.py",
		".claude/hooks/pr_check_in_reminder.py",
		".claude/hooks/permission_prompt_logger.py",
	):
		assert path in entries, path
		# Nothing listed is still shipped.
		assert not (REPO_ROOT / "workflow-templates" / path).exists(), path


def test_commit_step_stages_removed_files() -> None:
	commit = [step for step in _steps() if step.get("name") == "Commit and push updates"][0]
	assert "steps.retired_files.outputs.retired_has_changes == 'true'" in commit["if"]
	assert 'git add -A -- "$changed_path"' in commit["run"]
	assert "/tmp/retired_removed_files.txt" in commit["run"]
