#!/usr/bin/env python3
"""Tests for scripts/release_manifest.py (release manifest builder, issue #6903).

Every fixture lives under tmp_path; the real-repo test only reads the live
checkout and writes its output to tmp_path. No git command is run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_manifest.py"
SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "release_manifest.v1.json"
UPDATER = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
GOOD_SHA = "0123456789abcdef0123456789abcdef01234567"
GOOD_TAG = "v1.2.3"


def _write(root: Path, rel: str, data: str, mode: int = 0o644) -> Path:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(data, encoding="utf-8")
	os.chmod(path, mode)
	return path


def _fixture(tmp_path: Path) -> Path:
	root = tmp_path / "repo"
	_write(root, "CLAUDE.md", "# rules\n")
	_write(root, "workflow-templates/ai-update-workflows.yml", "name: update\n")
	_write(root, "workflow-templates/ai-review.yml", "name: review\n")
	_write(root, "workflow-templates/profiles/core.txt", "ai-review.yml\n")
	_write(root, "workflow-templates/retired_files.txt", "# none\n")
	_write(root, "workflow-templates/.claude/settings.json", "{}\n")
	_write(root, "workflow-templates/.claude/hooks/hook.sh", "#!/bin/sh\n", 0o755)
	_write(root, "workflow-templates/audit-gate/contract.json", "{}\n")
	_write(root, "workflow-templates/validation-harness/ignored.txt", "not attested\n")
	(root / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	for name in ("workflow_wrapper_refs.py", "apply_audit_gate_assets.py", "assemble_changelog.py", "verify_release_manifest.py", "release_manifest.py"):
		_write(root, f"scripts/{name}", f"# {name}\n", 0o755)
	_write(root, "scripts/tg_helpers.sh", "# tg\n")
	_write(root, "scripts/unrelated.py", "# not attested\n")
	return root


def _build(root: Path, output: Path, sha: str = GOOD_SHA, tag: str = GOOD_TAG) -> subprocess.CompletedProcess:
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	return subprocess.run(
		[sys.executable, str(SCRIPT), "build", "--repo-root", str(root), "--release-sha", sha, "--tag", tag, "--output", str(output)],
		capture_output=True,
		text=True,
		env=env,
	)


def _validator() -> jsonschema.Draft202012Validator:
	schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
	jsonschema.Draft202012Validator.check_schema(schema)
	return jsonschema.Draft202012Validator(schema)


def _entries(output: Path) -> dict[str, dict]:
	return {entry["path"]: entry for entry in json.loads(output.read_text(encoding="utf-8"))["files"]}


def test_build_is_byte_identical_and_valid(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	first, second = tmp_path / "a.json", tmp_path / "b.json"
	result_a = _build(root, first)
	result_b = _build(root, second)
	assert result_a.returncode == 0, result_a.stderr
	assert result_b.returncode == 0, result_b.stderr
	assert first.read_bytes() == second.read_bytes()
	assert first.read_bytes().endswith(b"\n")
	assert "RELEASE_MANIFEST outcome=written files=" in result_a.stdout
	manifest = json.loads(first.read_text(encoding="utf-8"))
	_validator().validate(manifest)
	assert manifest["schema_version"] == "release_manifest.v1"
	assert manifest["repository"] == "shubhodeep1/coding-workflows"
	assert manifest["release_sha"] == GOOD_SHA
	assert manifest["tag"] == GOOD_TAG
	assert "generated_at" not in manifest


def test_files_sorted_unique_and_scoped(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	output = tmp_path / "m.json"
	assert _build(root, output).returncode == 0
	paths = [entry["path"] for entry in json.loads(output.read_text(encoding="utf-8"))["files"]]
	encoded = [path.encode("utf-8") for path in paths]
	assert encoded == sorted(encoded)
	assert len(paths) == len(set(paths))
	assert "workflow-templates/validation-harness/ignored.txt" not in paths
	assert "scripts/unrelated.py" not in paths
	assert "scripts/tg_helpers.sh" not in paths
	assert "workflow-templates/audit-gate/contract.json" in paths
	assert paths.count("workflow-templates/ai-update-workflows.yml") == 1


def test_modes_and_symlink_entry(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	output = tmp_path / "m.json"
	assert _build(root, output).returncode == 0
	entries = _entries(output)
	assert entries["workflow-templates/.claude/hooks/hook.sh"]["mode"] == "100755"
	assert entries["workflow-templates/.claude/settings.json"]["mode"] == "100644"
	assert entries["workflow-templates/audit-gate/contract.json"]["mode"] == "100644"
	claude = entries["CLAUDE.md"]
	assert claude["sha256"] == hashlib.sha256(b"# rules\n").hexdigest()
	assert claude["size"] == len(b"# rules\n")
	link = entries["workflow-templates/CLAUDE.md"]
	assert link["mode"] == "120000"
	assert link["link_target"] == "../CLAUDE.md"
	assert link["sha256"] == hashlib.sha256(b"../CLAUDE.md").hexdigest()
	assert link["size"] == len("../CLAUDE.md")


def test_schema_rejects_known_symlink_as_regular_file(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	output = tmp_path / "m.json"
	assert _build(root, output).returncode == 0
	manifest = json.loads(output.read_text(encoding="utf-8"))
	for entry in manifest["files"]:
		if entry["path"] == "workflow-templates/CLAUDE.md":
			del entry["link_target"]
			entry["mode"] = "100644"
	assert not _validator().is_valid(manifest)


def test_uppercase_sha_is_lowercased(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	output = tmp_path / "m.json"
	assert _build(root, output, sha=GOOD_SHA.upper()).returncode == 0
	assert json.loads(output.read_text(encoding="utf-8"))["release_sha"] == GOOD_SHA


def _assert_refused(root: Path, output: Path, reason: str, **kwargs) -> None:
	result = _build(root, output, **kwargs)
	assert result.returncode != 0
	assert f"reason={reason}" in result.stderr
	assert str(root) not in result.stderr


@pytest.mark.parametrize(
	("mutate", "reason"),
	[
		(lambda root: (root / "workflow-templates/.claude/evil").symlink_to("/etc/passwd"), "unexpected_symlink"),
		(lambda root: (root / "workflow-templates/ai-link.yml").symlink_to("ai-review.yml"), "unexpected_symlink"),
		(lambda root: _write(root, "workflow-templates/.claude/bad name.md", "x\n"), "unsafe_path_name"),
		(lambda root: os.mkfifo(root / "workflow-templates/.claude/fifo"), "non_regular_file"),
		(lambda root: (root / "workflow-templates/retired_files.txt").unlink(), "required_path_missing"),
		(lambda root: (root / "workflow-templates/audit-gate/contract.json").unlink(), "required_path_missing"),
	],
)
def test_unsafe_tree_is_refused_without_writing(tmp_path: Path, mutate, reason: str) -> None:
	root = _fixture(tmp_path)
	mutate(root)
	output = tmp_path / "m.json"
	_assert_refused(root, output, reason)
	assert not output.exists()


def test_symlinked_claude_directory_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	elsewhere = tmp_path / "elsewhere"
	(root / "workflow-templates/.claude").rename(elsewhere)
	(root / "workflow-templates/.claude").symlink_to(elsewhere)
	output = tmp_path / "m.json"
	_assert_refused(root, output, "unexpected_symlink")
	assert not output.exists()


def test_symlinked_subdirectory_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	(tmp_path / "secrets").mkdir()
	(root / "workflow-templates/.claude/linked").symlink_to(tmp_path / "secrets")
	output = tmp_path / "m.json"
	_assert_refused(root, output, "unexpected_symlink")


def test_retargeted_claude_link_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	link = root / "workflow-templates/CLAUDE.md"
	link.unlink()
	(tmp_path / "secret").write_text("s\n", encoding="utf-8")
	link.symlink_to("../../secret")
	output = tmp_path / "m.json"
	_assert_refused(root, output, "unexpected_symlink_target")
	assert not output.exists()


def test_symlink_escaping_root_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	(root / "CLAUDE.md").unlink()
	outside = tmp_path / "outside.md"
	outside.write_text("x\n", encoding="utf-8")
	(root / "CLAUDE.md").symlink_to(outside)
	output = tmp_path / "m.json"
	result = _build(root, output)
	assert result.returncode != 0
	assert "reason=path_escape" in result.stderr or "reason=unexpected_symlink" in result.stderr
	assert not output.exists()


def test_dangling_claude_link_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	(root / "CLAUDE.md").unlink()
	output = tmp_path / "m.json"
	result = _build(root, output)
	assert result.returncode != 0
	assert "reason=dangling_symlink" in result.stderr or "reason=required_path_missing" in result.stderr
	assert not output.exists()


@pytest.mark.parametrize(
	("kwargs", "reason"),
	[
		({"sha": GOOD_SHA[:39]}, "invalid_release_sha"),
		({"sha": "g" * 40}, "invalid_release_sha"),
		({"tag": "1.2.3"}, "invalid_tag"),
		({"tag": "v1.2.3\n"}, "invalid_tag"),
	],
)
def test_invalid_arguments_leave_existing_output_unchanged(tmp_path: Path, kwargs: dict, reason: str) -> None:
	root = _fixture(tmp_path)
	output = tmp_path / "m.json"
	output.write_text("previous\n", encoding="utf-8")
	_assert_refused(root, output, reason, **kwargs)
	assert output.read_text(encoding="utf-8") == "previous\n"


def test_output_inside_surface_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	output = root / "workflow-templates" / "manifest.json"
	_assert_refused(root, output, "output_inside_surface")
	assert not output.exists()


def test_missing_audit_gate_tree_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	(root / "workflow-templates/audit-gate/contract.json").unlink()
	(root / "workflow-templates/audit-gate").rmdir()
	output = tmp_path / "m.json"
	_assert_refused(root, output, "required_path_missing")
	assert not output.exists()


def test_no_workflow_templates_is_refused(tmp_path: Path) -> None:
	root = _fixture(tmp_path)
	(root / "workflow-templates/ai-review.yml").unlink()
	(root / "workflow-templates/ai-update-workflows.yml").unlink()
	output = tmp_path / "m.json"
	_assert_refused(root, output, "no_workflow_templates")


def test_real_repo_tree(tmp_path: Path) -> None:
	output = tmp_path / "m.json"
	result = _build(REPO_ROOT, output)
	assert result.returncode == 0, result.stderr
	manifest = json.loads(output.read_text(encoding="utf-8"))
	_validator().validate(manifest)
	paths = {entry["path"] for entry in manifest["files"]}
	for template in (REPO_ROOT / "workflow-templates").glob("*.yml"):
		assert f"workflow-templates/{template.name}" in paths
	for required in (
		"CLAUDE.md",
		"workflow-templates/CLAUDE.md",
		"workflow-templates/retired_files.txt",
		"scripts/workflow_wrapper_refs.py",
		"scripts/apply_audit_gate_assets.py",
		"scripts/assemble_changelog.py",
		"scripts/verify_release_manifest.py",
		"scripts/release_manifest.py",
		"workflow-templates/audit-gate/contract.json",
	):
		assert required in paths, required
	assert "scripts/tg_helpers.sh" not in paths


def test_surface_rules_track_the_updater() -> None:
	"""Drift guard: every surface rule still maps to something the updater reads."""
	text = UPDATER.read_text(encoding="utf-8")
	for step in (
		"Prepare immutable workflow wrappers",
		"Apply canonical audit-gate assets",
		"Sync .claude/ assets from upstream",
		"Remove retired upstream files",
		"Sync top-level CLAUDE.md from upstream",
		"Sync changelog fragment assets from upstream",
		"Assemble changelog fragments",
		"Verify attested release manifest",
	):
		assert f"name: {step}" in text, step
	for needle in (
		'"${UPSTREAM_DIR}"/*.yml',
		'SELF_TEMPLATE="ai-update-workflows.yml"',
		'PROFILE_MANIFEST_DIR="${UPSTREAM_DIR}/profiles"',
		"retired_files.txt",
		'CLAUDE_UPSTREAM_DIR="${UPSTREAM_DIR}/.claude"',
		'CONTRACT_DIR="${UPSTREAM_DIR}/audit-gate"',
		'UPSTREAM_CLAUDE_MD="${UPSTREAM_DIR}/CLAUDE.md"',
		"scripts/workflow_wrapper_refs.py",
		"scripts/apply_audit_gate_assets.py",
		"assemble_changelog.py",
		"scripts/verify_release_manifest.py",
		"scripts/release_manifest.py",
	):
		assert needle in text, needle
