#!/usr/bin/env python3
"""Contract tests for publishing the attested release manifest (issue #6943).

Both release workflows (mark-stable.yml, test-and-mark-stable.yml) must build
the manifest after tagging, attest it, upload it as a release asset, and only
then notify consumers; dry runs skip all three steps. The second half runs
scripts/release_manifest_publish.sh against throwaway git repositories and a
fake `gh` under tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
	REPO_ROOT / ".github" / "workflows" / "mark-stable.yml",
	REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml",
)
SCRIPT = REPO_ROOT / "scripts" / "release_manifest_publish.sh"
ASSET_NAME = "release-manifest.v1.json"
DRY_RUN_GUARD = "${{ !inputs.dry_run }}"
NEW_STEPS = ("Build release manifest", "Attest release manifest", "Upload release manifest asset")
STEP_ORDER = (
	"Tag version and update stable pointer",
	"Build release manifest",
	"Attest release manifest",
	"Create GitHub Release",
	"Upload release manifest asset",
	"Notify consumer repos via repository_dispatch",
)
TAG = "v1.2.3"


def _load(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8"))


def _release_steps(workflow: dict) -> dict[str, dict]:
	return {step["name"]: step for step in workflow["jobs"]["release"]["steps"]}


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_steps_run_after_tagging_and_before_dispatch(workflow_path: Path) -> None:
	names = [step["name"] for step in _load(workflow_path)["jobs"]["release"]["steps"]]
	positions = [names.index(name) for name in STEP_ORDER]
	assert positions == sorted(positions), names
	for name in NEW_STEPS:
		assert names.count(name) == 1


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_new_steps_skip_dry_runs_and_fail_closed(workflow_path: Path) -> None:
	steps = _release_steps(_load(workflow_path))
	for name in NEW_STEPS:
		step = steps[name]
		assert step.get("if") == DRY_RUN_GUARD, name
		assert "continue-on-error" not in step, name
	# Dispatch keeps the default success() gate, so a manifest failure stops it.
	assert steps["Notify consumer repos via repository_dispatch"].get("if") == DRY_RUN_GUARD


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_attestation_subject_is_the_built_manifest(workflow_path: Path) -> None:
	steps = _release_steps(_load(workflow_path))
	build = steps["Build release manifest"]
	assert build["id"] == "release_manifest"
	assert "scripts/release_manifest_publish.sh build" in build["run"]
	attest = steps["Attest release manifest"]
	assert attest["uses"].startswith("actions/attest-build-provenance@v")
	assert attest["with"] == {"subject-path": "${{ steps.release_manifest.outputs.manifest_path }}"}
	upload = steps["Upload release manifest asset"]
	assert "scripts/release_manifest_publish.sh upload" in upload["run"]
	assert upload["env"]["RELEASE_MANIFEST_PATH"] == "${{ steps.release_manifest.outputs.manifest_path }}"


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_permissions_add_exactly_attestation_scopes(workflow_path: Path) -> None:
	workflow = _load(workflow_path)
	assert workflow["permissions"] == {"contents": "write"}
	# The release job keeps its existing CI-gate scopes (checks/actions read,
	# #6797) and adds exactly the two attestation scopes (#6943).
	assert workflow["jobs"]["release"]["permissions"] == {
		"contents": "write",
		"id-token": "write",
		"attestations": "write",
		"checks": "read",
		"actions": "read",
	}
	for job_id, job in workflow["jobs"].items():
		if job_id == "release":
			continue
		perms = job.get("permissions") or {}
		assert "id-token" not in perms, job_id
		assert "attestations" not in perms, job_id


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_expressions_reach_run_only_through_env(workflow_path: Path) -> None:
	steps = _release_steps(_load(workflow_path))
	for name in ("Build release manifest", "Upload release manifest asset"):
		assert "${{" not in steps[name]["run"], name
	assert set(steps["Build release manifest"]["env"]) == {"RELEASE_VERSION", "RELEASE_MANIFEST_DIR"}
	assert set(steps["Upload release manifest asset"]["env"]) == {
		"GH_TOKEN",
		"RELEASE_VERSION",
		"RELEASE_MANIFEST_PATH",
		"RELEASE_REPOSITORY",
	}


def test_new_steps_are_identical_in_both_workflows() -> None:
	first, second = (_release_steps(_load(path)) for path in WORKFLOWS)
	for name in NEW_STEPS:
		assert first[name] == second[name], name


def test_workflows_stay_under_size_guard() -> None:
	for path in WORKFLOWS:
		assert path.stat().st_size < 480_000, path.name


def test_script_header_and_mode() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert text.startswith("#!/usr/bin/env bash\n")
	assert "set -euo pipefail" in text
	assert SCRIPT.stat().st_mode & 0o777 == 0o755


# ── Behavioural tests for scripts/release_manifest_publish.sh ─────────────


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		[
			"git",
			"-c", "user.name=test",
			"-c", "user.email=test@example.invalid",
			"-c", "commit.gpgsign=false",
			"-c", "tag.gpgsign=false",
			*args,
		],
		cwd=cwd,
		check=True,
		capture_output=True,
		text=True,
	).stdout.strip()


def _write(root: Path, rel: str, data: str, mode: int = 0o644) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(data, encoding="utf-8")
	os.chmod(path, mode)


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
	root = tmp_path / "repo"
	root.mkdir()
	_write(root, "CLAUDE.md", "# rules\n")
	_write(root, "workflow-templates/ai-update-workflows.yml", "name: update\n")
	_write(root, "workflow-templates/ai-review.yml", "name: review\n")
	_write(root, "workflow-templates/retired_files.txt", "# none\n")
	_write(root, "workflow-templates/.claude/settings.json", "{}\n")
	_write(root, "workflow-templates/audit-gate/contract.json", "{}\n")
	(root / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	for name in ("release_manifest.py", "workflow_wrapper_refs.py"):
		(root / "scripts").mkdir(exist_ok=True)
		shutil.copy2(REPO_ROOT / "scripts" / name, root / "scripts" / name)
	for name in ("apply_audit_gate_assets.py", "assemble_changelog.py", "verify_release_manifest.py"):
		_write(root, f"scripts/{name}", f"# {name}\n", 0o755)
	_git(root, "init", "-q")
	_git(root, "add", "-A")
	_git(root, "commit", "-q", "-m", "release")
	_git(root, "tag", "-a", TAG, "-m", f"Release {TAG}")
	return root, _git(root, "rev-parse", "HEAD")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GIT_", "RUNNER_", "RELEASE_MANIFEST"))}
	env.update({"TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", "GH_RETRY_MAX_ATTEMPTS": "1"})
	env.update(extra)
	return env


def _run(args: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=cwd)


def _worktrees(root: Path) -> list[str]:
	return [line for line in _git(root, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def test_build_writes_verified_manifest_and_outputs(tmp_path: Path) -> None:
	root, sha = _make_repo(tmp_path)
	out_dir = tmp_path / "out"
	gh_output = tmp_path / "gh_output"
	gh_output.write_text("", encoding="utf-8")
	result = _run(
		["build", "--tag", TAG, "--release-sha", sha.upper(), "--output-dir", str(out_dir), "--repo-root", str(root)],
		_env(tmp_path, GITHUB_OUTPUT=str(gh_output), RUNNER_TEMP=str(tmp_path)),
	)
	assert result.returncode == 0, result.stderr
	manifest_path = out_dir / ASSET_NAME
	manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	assert manifest["release_sha"] == sha
	assert manifest["tag"] == TAG
	assert manifest["files"]
	assert f"RELEASE_MANIFEST outcome=built tag={TAG} sha={sha}" in result.stdout
	outputs = gh_output.read_text(encoding="utf-8")
	assert f"manifest_path={manifest_path}\n" in outputs
	assert "manifest_sha256=" in outputs
	assert len(_worktrees(root)) == 1


@pytest.mark.parametrize(
	("mutate", "reason"),
	[
		(lambda sha: {"sha": "0" * 40}, "tag_sha_mismatch"),
		(lambda sha: {"sha": sha[:39]}, "invalid_release_sha"),
		(lambda sha: {"tag": "1.2.3"}, "invalid_tag"),
		(lambda sha: {"tag": "v9.9.9"}, "tag_unresolved"),
	],
)
def test_build_refuses_bad_inputs(tmp_path: Path, mutate, reason: str) -> None:
	root, sha = _make_repo(tmp_path)
	values = {"sha": sha, "tag": TAG}
	values.update(mutate(sha))
	out_dir = tmp_path / "out"
	result = _run(
		["build", "--tag", values["tag"], "--release-sha", values["sha"], "--output-dir", str(out_dir), "--repo-root", str(root)],
		_env(tmp_path),
	)
	assert result.returncode != 0
	assert f"::error::RELEASE_MANIFEST outcome=failed reason={reason}" in result.stderr
	assert not (out_dir / ASSET_NAME).exists()
	assert len(_worktrees(root)) == 1


def test_build_builder_refusal_removes_worktree(tmp_path: Path) -> None:
	root, _ = _make_repo(tmp_path)
	(root / "workflow-templates" / "retired_files.txt").unlink()
	_git(root, "add", "-A")
	_git(root, "commit", "-q", "-m", "break")
	_git(root, "tag", "-a", "v1.2.4", "-m", "Release v1.2.4")
	sha = _git(root, "rev-parse", "HEAD")
	out_dir = tmp_path / "out"
	result = _run(
		["build", "--tag", "v1.2.4", "--release-sha", sha, "--output-dir", str(out_dir), "--repo-root", str(root)],
		_env(tmp_path),
	)
	assert result.returncode != 0
	assert "reason=builder_failed" in result.stderr
	assert not (out_dir / ASSET_NAME).exists()
	assert len(_worktrees(root)) == 1


def _fake_gh(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env bash\n"
		"printf '%s\\n' \"$*\" >> \"${FAKE_GH_LOG}\"\n"
		"if [ \"$1 $2\" = \"release upload\" ]; then exit \"${FAKE_GH_UPLOAD_RC:-0}\"; fi\n"
		"if [ \"$1 $2\" = \"release view\" ]; then printf '%s' \"${FAKE_GH_VIEW}\"; exit 0; fi\n"
		"exit 1\n",
		encoding="utf-8",
	)
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	return bin_dir


def _upload(tmp_path: Path, view: dict | None, upload_rc: int = 0) -> tuple[subprocess.CompletedProcess, Path, Path]:
	manifest = tmp_path / "out" / ASSET_NAME
	manifest.parent.mkdir(parents=True, exist_ok=True)
	manifest.write_text('{"x": 1}\n', encoding="utf-8")
	log = tmp_path / "gh.log"
	bin_dir = _fake_gh(tmp_path)
	env = _env(
		tmp_path,
		PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		FAKE_GH_LOG=str(log),
		FAKE_GH_VIEW=json.dumps(view or {}),
		FAKE_GH_UPLOAD_RC=str(upload_rc),
	)
	result = _run(["upload", "--tag", TAG, "--manifest", str(manifest), "--repo", "o/r"], env)
	return result, manifest, log


def test_upload_success(tmp_path: Path) -> None:
	size = len('{"x": 1}\n')
	result, manifest, log = _upload(tmp_path, {"assets": [{"name": ASSET_NAME, "size": size}]})
	assert result.returncode == 0, result.stderr
	assert f"release upload {TAG} {manifest} --clobber --repo o/r" in log.read_text(encoding="utf-8")
	assert f"RELEASE_MANIFEST outcome=uploaded tag={TAG} asset={ASSET_NAME} size={size}" in result.stdout


@pytest.mark.parametrize(
	("view", "upload_rc", "reason"),
	[
		({"assets": [{"name": "other.json", "size": 9}]}, 0, "asset_missing"),
		({"assets": [{"name": ASSET_NAME, "size": 1}]}, 0, "asset_size_mismatch"),
		({"assets": []}, 1, "upload_failed"),
	],
)
def test_upload_failures(tmp_path: Path, view: dict, upload_rc: int, reason: str) -> None:
	result, _, _ = _upload(tmp_path, view, upload_rc)
	assert result.returncode != 0
	assert f"::error::RELEASE_MANIFEST outcome=failed reason={reason} tag={TAG}" in result.stderr


def test_upload_requires_repository(tmp_path: Path) -> None:
	manifest = tmp_path / ASSET_NAME
	manifest.write_text("{}\n", encoding="utf-8")
	result = _run(["upload", "--tag", TAG, "--manifest", str(manifest)], _env(tmp_path))
	assert result.returncode != 0
	assert "reason=missing_repository" in result.stderr


def test_upload_refuses_wrongly_named_manifest(tmp_path: Path) -> None:
	manifest = tmp_path / "other.json"
	manifest.write_text("{}\n", encoding="utf-8")
	result = _run(["upload", "--tag", TAG, "--manifest", str(manifest), "--repo", "o/r"], _env(tmp_path))
	assert result.returncode != 0
	assert f"::error::RELEASE_MANIFEST outcome=failed reason=manifest_name_mismatch tag={TAG}" in result.stderr


def test_unknown_command_is_usage_error(tmp_path: Path) -> None:
	result = _run(["publish"], _env(tmp_path))
	assert result.returncode != 0
	assert "reason=usage" in result.stderr
